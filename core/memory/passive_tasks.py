import asyncio
import re

from astrbot.api import logger

from ..database.utils import parse_aliases
from ..utils.token_utils import extract_tokens_robust
from .maintenance import MemoryMaintenanceMixin
from .passive_context import PassiveContextMixin

RELATION_DELTA_LIMIT = 5
MEMORY_SUMMARY_BATCH_SIZE = 100


class PassiveSummaryTaskMixin(MemoryMaintenanceMixin, PassiveContextMixin):
    async def _call_summary_llm(
        self,
        task_name: str,
        system_prompt: str,
        user_prompt: str,
        bot_name: str = "",
        group_or_user_id: str = "",
    ) -> str | None:
        provider_ids = self.plugin.passive_memory_provider_ids
        if not provider_ids:
            logger.warning(
                "[Giftia Passive Memory] 未配置被动总结提供商(passive_memory_provider_ids)，跳过后台总结。"
            )
            return None

        for provider_id in provider_ids:
            for attempt in range(2):
                try:
                    logger.info(
                        f"[Giftia Passive Memory] 尝试使用提供商 {provider_id} (第 {attempt + 1} 次) 进行{task_name}"
                    )
                    logger.debug(
                        f"[Giftia Passive Memory] {task_name} system_prompt:\n{system_prompt}"
                    )
                    logger.debug(
                        f"[Giftia Passive Memory] {task_name} user_prompt:\n{user_prompt}"
                    )
                    llm_resp = await asyncio.wait_for(
                        self.plugin.context.llm_generate(
                            chat_provider_id=provider_id,
                            system_prompt=system_prompt,
                            prompt=user_prompt,
                        ),
                        timeout=120,
                    )
                    if llm_resp and self.plugin:
                        prompt_tokens, completion_tokens, total_tokens = (
                            extract_tokens_robust(llm_resp)
                        )
                        model_name = provider_id
                        await self.plugin.db.log_token_usage(
                            bot_name=bot_name,
                            group_or_user_id=group_or_user_id,
                            type="passive_summary",
                            provider_id=provider_id,
                            model_name=model_name,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            total_tokens=total_tokens,
                            extra_info={"task_name": task_name},
                        )
                    if llm_resp and llm_resp.completion_text:
                        logger.debug(
                            "[Giftia Memory] %s raw response: %s",
                            task_name,
                            llm_resp.completion_text,
                        )
                        return llm_resp.completion_text
                except Exception as e:
                    logger.error(
                        f"[Giftia Passive Memory] 提供商 {provider_id} 调用{task_name}报错: {e}"
                    )

        logger.error(
            f"[Giftia Passive Memory] 所有配置的总结提供商均调用失败，{task_name}终止。"
        )
        return None

    async def _run_profile_summary_task(
        self,
        bot_name: str,
        group_or_user_id: str,
        nickname: str,
        self_id: str,
        context: dict,
    ) -> bool:
        sys_prompt = self._format_prompt_template(
            self._get_profile_summary_prompt(), nickname=nickname, self_id=self_id
        )
        logger.debug(f"[Giftia Passive Memory] 关系画像维护系统提示词:\n{sys_prompt}")
        user_prompt = self._build_profile_user_prompt(group_or_user_id, context)
        completion_text = await self._call_summary_llm(
            "关系画像维护",
            sys_prompt,
            user_prompt,
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
        )
        if not completion_text:
            return False

        logger.info(f"[Giftia Passive Memory] 关系画像维护返回内容:\n{completion_text}")
        alias_messages = [
            (str(msg.user_id or ""), str(msg.content or ""))
            for msg in context.get("alias_observation_messages") or []
            if msg.user_id
            and not getattr(msg, "is_recalled", False)
            and getattr(msg, "role", "message") not in ("assistant", "operation_log")
            and not self._is_bot_reference(
                msg.user_id, msg.user_id, self_id, bot_name=bot_name
            )
        ]
        observed_aliases = set()

        user_profile_matches = re.finditer(
            r"<summary_user_profile\s+([^>]*)>(.*?)</summary_user_profile>",
            completion_text,
            re.DOTALL,
        )
        for match in user_profile_matches:
            attr_str = match.group(1)
            attrs = dict(re.findall(r'(\w+)=["\']([^"\']*)["\']', attr_str))
            target_user = attrs.get("user_id", "").strip()
            profile_content = match.group(2).strip()
            resolved_user_id = context["nickname_to_user_id"].get(
                target_user, target_user
            )
            active_users = context.get("active_users_in_range") or set()
            if resolved_user_id not in active_users:
                logger.warning(
                    f"[Giftia Passive Memory] 关系画像维护解析到非法/非当前活跃用户 ID: {resolved_user_id} ({target_user})，跳过入库"
                )
                continue

            relation_updates = []

            def strip_relation_update(update_match):
                update_attr_str = update_match.group(1)
                update_attrs = dict(
                    re.findall(r'(\w+)=["\']([^"\']*)["\']', update_attr_str)
                )
                delta_str = (update_attrs.get("delta") or "").strip()
                if re.fullmatch(r"[+-]\d+", delta_str):
                    delta = max(
                        -RELATION_DELTA_LIMIT,
                        min(RELATION_DELTA_LIMIT, int(delta_str)),
                    )
                    if delta != 0:
                        relation_updates.append((delta, update_match.group(2).strip()))
                return ""

            profile_content = re.sub(
                r"<update_relation\s+([^>]*)>(.*?)</update_relation>",
                strip_relation_update,
                profile_content,
                flags=re.DOTALL,
            ).strip()
            has_profile_content = bool(profile_content and profile_content != "无")
            if not resolved_user_id or (
                not has_profile_content and not relation_updates
            ):
                continue
            if self._is_bot_reference(
                target_user,
                resolved_user_id,
                self_id,
                nickname=nickname,
                bot_name=bot_name,
            ):
                logger.debug(
                    f"[Giftia Passive Memory] 跳过机器人自身画像更新: {target_user}"
                )
                continue

            profile_fields = {}
            if has_profile_content:
                profile_fields = self._parse_session_profile_fields(profile_content)
            if "aliases" in profile_fields:
                # The model resolves the addressee; old profile text alone is not evidence.
                aliases = []
                for alias in parse_aliases(profile_fields.pop("aliases")):
                    key = (resolved_user_id, alias.lower())
                    if key in observed_aliases:
                        continue
                    if any(
                        sender_id != resolved_user_id and alias in content
                        for sender_id, content in alias_messages
                    ):
                        aliases.append(alias)
                        observed_aliases.add(key)
                if aliases:
                    profile_fields["aliases"] = "，".join(aliases)

            if not profile_fields and not relation_updates:
                continue

            if profile_fields:
                await self.plugin.data_cache.set_user_profile(
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                    user_id=resolved_user_id,
                    profile_fields=profile_fields,
                )
                logger.info(
                    f"[Giftia Passive Memory] 已更新用户 {resolved_user_id} 画像"
                )

            for delta, reason in relation_updates:
                await self.plugin.data_cache.update_relation(
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                    user_id=resolved_user_id,
                    relation=delta,
                )
                logger.info(
                    f"[Giftia Passive Memory] 用户 {resolved_user_id} 好感度变动 {delta}，原因: {reason}"
                )

        return True

    async def _run_background_summarize(
        self,
        bot_name: str,
        group_or_user_id: str,
        self_id: str,
        start_id: int,
        end_id: int,
    ) -> bool:
        """Persist one bounded batch before advancing its source cursor.

        Args:
            bot_name: Owning bot configuration.
            group_or_user_id: Owning conversation.
            self_id: Observing bot account.
            start_id: First unprocessed database message ID.
            end_id: Upper bound of the observed source interval.

        Returns:
            Whether the selected messages were durably handled.
        """
        key = f"{bot_name}:{group_or_user_id}"
        try:
            messages = await self.plugin.db.get_messages_by_id_range(
                bot_name,
                group_or_user_id,
                start_id,
                end_id,
                limit=MEMORY_SUMMARY_BATCH_SIZE,
            )
            if not messages:
                return True
            # Persist pending work before inference, including forced batches,
            # so failures remain retryable and a later wake-up cannot skip them.
            await self.plugin.db.upsert_kv_data(
                f"passive_memory:silence_armed:{key}", 1
            )
            selected, text_size = [], 0
            for msg in messages:
                if selected and text_size + len(msg.content or "") > 16000:
                    break
                selected.append(msg)
                text_size += len(msg.content or "")
            has_human_source = any(
                str(msg.user_id) != str(self_id)
                and not msg.is_recalled
                and msg.role != "operation_log"
                and (msg.content or msg.media_id_list)
                for msg in selected
            )
            context = None
            nickname = self.plugin.bot_map.get(bot_name, {}).get("nickname", bot_name)
            if has_human_source:
                context = await self._build_summary_context(
                    bot_name, group_or_user_id, self_id, selected
                )
                if not await self._run_memory_summary_task(
                    bot_name, group_or_user_id, self_id, nickname, context
                ):
                    return False
                if context.get("new_memory_ids"):
                    self._schedule_memory_consolidation(
                        bot_name, group_or_user_id, self_id
                    )
            processed_end = selected[-1].db_id
            await self.plugin.db.upsert_kv_data(
                f"passive_memory:last_summarized_id:{key}", processed_end
            )
            await self.plugin.db.upsert_kv_data(f"passive_memory:silent_count:{key}", 0)
            activity_version = self.activity_versions.get(key, 0)
            latest_id = await self.plugin.db.get_max_message_id(
                bot_name, group_or_user_id
            )
            await self.plugin.db.upsert_kv_data(
                f"passive_memory:silence_armed:{key}", int(processed_end < latest_id)
            )
            if self.activity_versions.get(key, 0) != activity_version:
                await self.plugin.db.upsert_kv_data(
                    f"passive_memory:silence_armed:{key}", 1
                )
            if context:
                try:
                    await self._run_profile_summary_task(
                        bot_name, group_or_user_id, nickname, self_id, context
                    )
                except Exception:
                    logger.exception(
                        "[Giftia Memory] Lightweight relationship update failed for %s",
                        key,
                    )
            return True
        except Exception:
            logger.exception(
                "[Giftia Memory] Summary failed for %s; source cursor retained", key
            )
            return False
