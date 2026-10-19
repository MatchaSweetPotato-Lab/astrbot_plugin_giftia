import asyncio
import json
import re

from astrbot.api import logger

from ..database.utils import parse_aliases
from ..llm.json_parse import parse_markdown_json
from ..llm.preset_prompts import DEFAULT_PASSIVE_ATTITUDE_SUMMARY_PROMPT
from ..llm.prompt import normalize_profile_value, truncate_message_content
from ..utils.token_utils import extract_tokens_robust
from .maintenance import MemoryMaintenanceMixin
from .passive_context import PassiveContextMixin

RELATION_DELTA_LIMIT = 5
MEMORY_SUMMARY_BATCH_SIZE = 100
ATTITUDE_REVIEW_CHANGE_COUNT = 5
ATTITUDE_REVIEW_MAJOR_DELTA = 4


class PassiveSummaryTaskMixin(MemoryMaintenanceMixin, PassiveContextMixin):
    async def _call_summary_llm(
        self,
        task_name: str,
        system_prompt: str,
        user_prompt: str,
        bot_name: str = "",
        group_or_user_id: str = "",
    ) -> str | None:
        logger.debug(
            f"[Giftia Passive Memory] Final prompts: task={task_name}, "
            f"bot={bot_name}, session={group_or_user_id}\n"
            f"=== System prompt ===\n{system_prompt}\n"
            f"=== User prompt ===\n{user_prompt}"
        )
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
                        completion_text = llm_resp.completion_text
                        try:
                            log_text = json.dumps(
                                json.loads(completion_text), ensure_ascii=False
                            )
                        except (json.JSONDecodeError, TypeError):
                            log_text = completion_text
                        logger.debug(
                            "[Giftia Memory] %s response: %s",
                            task_name,
                            log_text,
                        )
                        return completion_text
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
        user_prompt = self._build_profile_user_prompt(group_or_user_id, context)
        completion_text = await self._call_summary_llm(
            "好感度维护",
            sys_prompt,
            user_prompt,
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
        )
        if not completion_text:
            return False

        logger.info(f"[Giftia Passive Memory] 好感度维护返回内容:\n{completion_text}")
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
        updated_relation_users = set()
        source_messages = context.get("alias_observation_messages") or []
        source_ids = [msg.db_id for msg in source_messages if msg.db_id]
        occurred_at = next(
            (msg.time for msg in reversed(source_messages) if msg.time), ""
        )

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
                    f"[Giftia Passive Memory] 好感度维护解析到非法/非当前活跃用户 ID: {resolved_user_id} ({target_user})，跳过入库"
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
                    reason = normalize_profile_value(update_match.group(2))
                    if delta != 0 and reason and not relation_updates:
                        relation_updates.append((delta, " ".join(reason.split())[:120]))
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
                if resolved_user_id in updated_relation_users:
                    continue
                updated_relation_users.add(resolved_user_id)
                applied = await self.plugin.data_cache.update_relation(
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                    user_id=resolved_user_id,
                    relation=delta,
                    reason=reason,
                    source_ids=source_ids,
                    occurred_at=occurred_at,
                )
                if applied:
                    logger.info(
                        "[Giftia Memory] Affinity changed for %s by %+d: %s",
                        resolved_user_id,
                        delta,
                        reason,
                    )

        return True

    async def _run_attitude_summary_task(
        self,
        bot_name: str,
        group_or_user_id: str,
        nickname: str,
        self_id: str,
        context: dict | None = None,
    ) -> None:
        """Review mutual interaction tone from durable, unevaluated affinity history.

        Args:
            bot_name: Owning bot configuration.
            group_or_user_id: Owning conversation.
            nickname: Observing bot's name.
            self_id: Observing bot account.
            context: Optional current source batch supplementing the history.
        """
        pending = await self.plugin.db.get_pending_attitude_evaluations(
            bot_name,
            group_or_user_id,
            min_changes=ATTITUDE_REVIEW_CHANGE_COUNT,
            major_delta=ATTITUDE_REVIEW_MAJOR_DELTA,
        )
        if not pending:
            return
        messages = (context or {}).get("alias_observation_messages") or []
        system_prompt = self._format_prompt_template(
            DEFAULT_PASSIVE_ATTITUDE_SUMMARY_PROMPT, nickname, self_id
        )
        for candidate in pending:
            user_id = candidate["user_id"]
            evaluated_id = candidate["evaluated_id"]
            remaining_count = candidate["change_count"]
            # Freeze the boundary so changes arriving during inference stay pending.
            while evaluated_id < candidate["through_id"]:
                try:
                    window_size = 50
                    # Keep an eligible final batch so a failed tail can retry on its own.
                    if 50 < remaining_count < 50 + ATTITUDE_REVIEW_CHANGE_COUNT:
                        window_size = remaining_count - ATTITUDE_REVIEW_CHANGE_COUNT
                    new_changes = await self.plugin.db.get_relation_changes(
                        bot_name,
                        group_or_user_id,
                        user_id,
                        limit=window_size,
                        after_id=evaluated_id,
                        through_id=candidate["through_id"],
                        oldest_first=True,
                    )
                    if not new_changes:
                        break
                    through_id = new_changes[-1]["id"]
                    history = await self.plugin.db.get_relation_changes(
                        bot_name, group_or_user_id, user_id, through_id=through_id
                    )
                    profile = await self.plugin.db.get_user_profile_record(
                        bot_name, group_or_user_id, user_id
                    )
                    related_messages = [
                        msg
                        for msg in messages
                        if str(msg.user_id) in {str(user_id), str(self_id)}
                        and not msg.is_recalled
                        and msg.role != "operation_log"
                    ][-50:]
                    user_prompt = json.dumps(
                        {
                            "user_id": user_id,
                            "current_attitude": (profile or {}).get("attitude") or "",
                            "current_relation": history[-1]["relation_after"],
                            "evaluated_through_id": evaluated_id,
                            "relation_changes": [
                                {
                                    "id": change["id"],
                                    "time": change["occurred_at"],
                                    "delta": change["delta"],
                                    "relation_before": change["relation_before"],
                                    "relation_after": change["relation_after"],
                                    "reason": change["reason"],
                                    "is_new": change["id"] > evaluated_id,
                                }
                                for change in history
                            ],
                            "related_chat_history": [
                                {
                                    "user_id": msg.user_id,
                                    "time": msg.time,
                                    "content": truncate_message_content(
                                        msg.content, 500
                                    ),
                                }
                                for msg in related_messages
                            ],
                        },
                        ensure_ascii=False,
                    )
                    completion = await self._call_summary_llm(
                        "互动态度维护",
                        system_prompt,
                        user_prompt,
                        bot_name=bot_name,
                        group_or_user_id=group_or_user_id,
                    )
                    response = parse_markdown_json(completion or "")
                    if not isinstance(response, dict) or set(response) != {"attitude"}:
                        raise ValueError("Expected exactly one attitude field")
                    attitude = response["attitude"]
                    if attitude is not None:
                        if not isinstance(attitude, str):
                            raise ValueError("Interaction tone must be text or null")
                        attitude = " ".join(normalize_profile_value(attitude).split())
                        if not attitude or len(attitude) > 20:
                            raise ValueError(
                                "Interaction tone must contain 1 to 20 characters"
                            )
                    if not await self.plugin.db.save_attitude_evaluation(
                        bot_name,
                        group_or_user_id,
                        user_id,
                        expected_cursor=evaluated_id,
                        through_id=through_id,
                        attitude=attitude,
                    ):
                        break
                    fmt_key = f"{bot_name}:{group_or_user_id}:{user_id}"
                    self.plugin.data_cache.user_profile_records.pop(fmt_key, None)
                    self.plugin.data_cache.user_profiles.pop(fmt_key, None)
                    evaluated_id = through_id
                    remaining_count -= len(new_changes)
                    logger.info(
                        "[Giftia Memory] Reviewed interaction tone for %s through change %s",
                        user_id,
                        through_id,
                    )
                except Exception:
                    logger.exception(
                        "[Giftia Memory] Interaction tone review failed for %s; history retained",
                        user_id,
                    )
                    break

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
            messages = (
                await self.plugin.db.chat_history_repo.get_pending_memory_messages(
                    bot_name,
                    group_or_user_id,
                    start_id,
                    end_id,
                    limit=MEMORY_SUMMARY_BATCH_SIZE,
                )
            )
            if not messages:
                return True
            selected, text_size = [], 0
            for msg in messages:
                if selected and text_size + len(msg.content or "") > 16000:
                    break
                selected.append(msg)
                text_size += len(msg.content or "")
            has_source = any(
                not msg.is_recalled
                and msg.role != "operation_log"
                and (msg.content or msg.media_id_list)
                for msg in selected
            )
            context = None
            nickname = self.plugin.bot_map.get(bot_name, {}).get("nickname", bot_name)
            if has_source:
                context = await self._build_summary_context(
                    bot_name, group_or_user_id, self_id, selected
                )
                if not await self._run_memory_summary_task(
                    bot_name, group_or_user_id, self_id, nickname, context
                ):
                    return False
            else:
                await self.plugin.db.memories_repo.insert_memories(
                    bot_name,
                    group_or_user_id,
                    [],
                    source_end=selected[-1].db_id,
                    source_ids=[msg.db_id for msg in selected],
                )
            await self.plugin.db.upsert_kv_data(f"passive_memory:silent_count:{key}", 0)
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
                try:
                    await self._run_attitude_summary_task(
                        bot_name, group_or_user_id, nickname, self_id, context
                    )
                except Exception:
                    logger.exception(
                        "[Giftia Memory] Interaction tone maintenance deferred for %s",
                        key,
                    )
            return True
        except Exception:
            logger.exception(
                "[Giftia Memory] Summary failed for %s; source cursor retained", key
            )
            return False
