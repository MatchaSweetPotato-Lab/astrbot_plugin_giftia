import asyncio
import re
from datetime import datetime

from astrbot.api import logger

from ..utils.schemas import normalize_memory_importance
from ..utils.token_utils import extract_tokens_robust
from .passive_context import PassiveContextMixin

RELATION_DELTA_LIMIT = 5


class PassiveSummaryTaskMixin(PassiveContextMixin):
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
                    llm_resp = await self.plugin.context.llm_generate(
                        chat_provider_id=provider_id,
                        system_prompt=system_prompt,
                        prompt=user_prompt,
                    )
                    if llm_resp and self.plugin:
                        prompt_tokens, completion_tokens, total_tokens = extract_tokens_robust(llm_resp)
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
                        return llm_resp.completion_text
                except Exception as e:
                    logger.error(
                        f"[Giftia Passive Memory] 提供商 {provider_id} 调用{task_name}报错: {e}"
                    )

        logger.error(
            f"[Giftia Passive Memory] 所有配置的总结提供商均调用失败，{task_name}终止。"
        )
        return None

    async def _run_memory_summary_task(
        self,
        bot_name: str,
        group_or_user_id: str,
        self_id: str,
        nickname: str,
        context: dict,
    ) -> bool | None:
        if not self.plugin.embedding_conf.get("enabled", False):
            logger.debug(
                "[Giftia Passive Memory] 嵌入模型未启用，跳过长期记忆提炼，仅维护画像。"
            )
            return None

        sys_prompt = self._format_prompt_template(
            self._get_memory_summary_prompt(), nickname=nickname, self_id=self_id
        )
        user_prompt = self._build_memory_user_prompt(group_or_user_id, context)
        completion_text = await self._call_summary_llm(
            "长期记忆提炼", sys_prompt, user_prompt,
            bot_name=bot_name, group_or_user_id=group_or_user_id,
        )
        if not completion_text:
            return False

        logger.info(
            f"[Giftia Passive Memory] 长期记忆提炼返回内容:\n{completion_text}"
        )

        memory_matches = re.finditer(
            r"<memory([^>]*)>(.*?)</memory>",
            completion_text,
            re.DOTALL,
        )
        for match in memory_matches:
            attrs = {
                key: value
                for key, value in re.findall(
                    r'([a-zA-Z_][\w-]*)=["\']([^"\']*)["\']',
                    match.group(1) or "",
                )
            }
            users_attr = attrs.get("users", "")
            importance = normalize_memory_importance(attrs.get("importance"), 5)
            text = match.group(2).strip()

            if not text or text == "无":
                continue

            associated_ids = []
            seen_associated_ids = set()
            self_id_str = str(self_id)
            if users_attr:
                for u in re.split(r"[,，]", users_attr):
                    u = u.strip()
                    resolved_uid = context["nickname_to_user_id"].get(u, u)
                    resolved_uid = str(resolved_uid).strip() if resolved_uid else ""
                    if not resolved_uid or resolved_uid == self_id_str:
                        continue
                    if resolved_uid not in seen_associated_ids:
                        associated_ids.append(resolved_uid)
                        seen_associated_ids.add(resolved_uid)

            if not associated_ids:
                logger.info(
                    f"[Giftia Passive Memory] 跳过长期记忆入库：未关联到除机器人自身以外的用户。记忆内容: {text}"
                )
                continue

            primary_user = associated_ids[0]

            await self.plugin.data_cache.add_memory(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                text=text,
                user_id=primary_user,
                associated_user_ids=associated_ids,
                importance=importance,
            )
            logger.info(
                f"[Giftia Passive Memory] 已成功记录长期记忆: {text} (关联用户: {associated_ids}, 重要度: {importance})"
            )
        return True

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
            "关系画像维护", sys_prompt, user_prompt,
            bot_name=bot_name, group_or_user_id=group_or_user_id,
        )
        if not completion_text:
            return False

        logger.info(f"[Giftia Passive Memory] 关系画像维护返回内容:\n{completion_text}")
        await self._refresh_known_alias_observations(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            self_id=self_id,
            db_messages=context.get("alias_observation_messages") or [],
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
                    f"[Giftia Passive Memory] 关系画像维护解析到非法/非当前活跃用户 ID: {resolved_user_id} ({target_user})，跳过入库"
                )
                continue

            title = attrs.get("title")
            if title is not None:
                title = title.strip()

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
                        relation_updates.append(
                            (delta, update_match.group(2).strip())
                        )
                return ""

            profile_content = re.sub(
                r"<update_relation\s+([^>]*)>(.*?)</update_relation>",
                strip_relation_update,
                profile_content,
                flags=re.DOTALL,
            ).strip()
            has_profile_content = bool(profile_content and profile_content != "无")
            if not resolved_user_id or (
                not has_profile_content and title is None and not relation_updates
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

            if (
                not profile_fields
                and title is None
                and not relation_updates
            ):
                continue

            if profile_fields or title is not None:
                await self.plugin.data_cache.set_user_profile(
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                    user_id=resolved_user_id,
                    title=title,
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

        group_profile_matches = re.finditer(
            r"<summary_group_profile>(.*?)</summary_group_profile>",
            completion_text,
            re.DOTALL,
        )
        for match in group_profile_matches:
            group_profile_content = match.group(1).strip()
            if group_profile_content and group_profile_content != "无":
                await self.plugin.data_cache.set_group_profile(
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                    profile=group_profile_content,
                )
                logger.info("[Giftia Passive Memory] 已更新群画像")

        return True

    async def _run_long_profile_summary_task(
        self,
        bot_name: str,
        group_or_user_id: str,
        nickname: str,
        self_id: str,
        user_id: str,
        user_nickname: str,
        profile_record: dict | None,
        sample_messages: list,
        fetched_message_count: int,
        sample_limit: int,
    ) -> bool:
        sys_prompt = self._format_prompt_template(
            self._get_long_profile_summary_prompt(), nickname=nickname, self_id=self_id
        )
        logger.debug(f"[Giftia Passive Memory] 用户画像维护({user_id})系统提示词:\n{sys_prompt}")
        user_prompt = await self._build_long_profile_user_prompt(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            user_id=user_id,
            nickname=user_nickname,
            profile_record=profile_record,
            sample_messages=sample_messages,
            fetched_message_count=fetched_message_count,
            sample_limit=sample_limit,
        )
        completion_text = await self._call_summary_llm(
            f"用户画像维护({user_id})", sys_prompt, user_prompt,
            bot_name=bot_name, group_or_user_id=group_or_user_id,
        )
        if not completion_text:
            return False

        logger.info(
            f"[Giftia Passive Memory] 用户画像维护返回内容({user_id}):\n{completion_text}"
        )
        profile_fields = self._parse_long_profile_fields(completion_text)
        if profile_fields:
            await self.plugin.data_cache.set_user_profile(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                user_id=user_id,
                profile_fields=profile_fields,
            )
            logger.info(
                f"[Giftia Passive Memory] 已更新用户 {user_id} 用户画像字段: {', '.join(sorted(profile_fields))}"
            )
        else:
            logger.debug(
                f"[Giftia Passive Memory] 用户 {user_id} 用户画像无可更新字段"
            )
        return True

    async def _maybe_run_long_profile_summary_for_user(
        self,
        bot_name: str,
        group_or_user_id: str,
        nickname: str,
        self_id: str,
        user_id: str,
        user_nickname: str,
    ) -> bool | None:
        if not user_id or self._is_bot_reference(
            user_id, user_id, self_id, nickname=nickname, bot_name=bot_name
        ):
            return None

        today = datetime.now().date().isoformat()
        base_key = f"passive_memory:long_profile:{bot_name}:{group_or_user_id}:{user_id}"
        last_run_date = await self.plugin.db.get_kv_data(f"{base_key}:last_run_date", "")
        if last_run_date == today:
            return None

        if not hasattr(self, "long_profile_locks"):
            self.long_profile_locks = {}
        if base_key not in self.long_profile_locks:
            self.long_profile_locks[base_key] = asyncio.Lock()

        async with self.long_profile_locks[base_key]:
            last_run_date = await self.plugin.db.get_kv_data(
                f"{base_key}:last_run_date", ""
            )
            if last_run_date == today:
                return None

            last_analyzed_id = await self.plugin.db.get_kv_data(
                f"{base_key}:last_analyzed_id", 0
            )
            sample_limit = self._get_long_profile_sample_limit()
            fetched_messages = await self.plugin.db.get_user_messages_after_id(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                user_id=user_id,
                after_id=last_analyzed_id,
                limit=sample_limit,
            )
            if not fetched_messages:
                return None

            sample_messages = [
                msg for msg in fetched_messages if self._is_long_profile_sample_message(msg)
            ]
            if not sample_messages:
                return None

            profile_record = await self.plugin.data_cache.get_user_profile_record(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                user_id=user_id,
            )
            has_existing = self._has_existing_long_profile(profile_record)
            message_threshold, text_threshold = self._get_long_profile_thresholds(
                has_existing
            )
            sample_text_length = sum(
                self._long_profile_sample_text_length(msg) for msg in sample_messages
            )
            if (
                len(sample_messages) < message_threshold
                and sample_text_length < text_threshold
            ):
                return None

            latest_fetched_id = max(getattr(msg, "db_id", 0) for msg in fetched_messages)
            if latest_fetched_id <= last_analyzed_id:
                return None

            ok = await self._run_long_profile_summary_task(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                nickname=nickname,
                self_id=self_id,
                user_id=user_id,
                user_nickname=user_nickname,
                profile_record=profile_record,
                sample_messages=sample_messages,
                fetched_message_count=len(fetched_messages),
                sample_limit=sample_limit,
            )
            if not ok:
                return False

            await self.plugin.db.upsert_kv_data(
                f"{base_key}:last_analyzed_id", latest_fetched_id
            )
            await self.plugin.db.upsert_kv_data(f"{base_key}:last_run_date", today)
            logger.info(
                f"[Giftia Passive Memory] 用户 {user_id} 用户画像游标推进到 {latest_fetched_id}"
            )
            return True

    async def _run_long_profile_summary_tasks(
        self,
        bot_name: str,
        group_or_user_id: str,
        nickname: str,
        self_id: str,
        db_messages: list,
    ) -> bool:
        candidate_users = {}
        for msg in db_messages or []:
            user_id = str(getattr(msg, "user_id", "") or "").strip()
            if not user_id or self._is_bot_reference(
                user_id, user_id, self_id, nickname=nickname, bot_name=bot_name
            ):
                continue
            candidate_users[user_id] = getattr(msg, "nickname", "") or ""

        any_success = False
        for user_id, user_nickname in candidate_users.items():
            result = await self._maybe_run_long_profile_summary_for_user(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                nickname=nickname,
                self_id=self_id,
                user_id=user_id,
                user_nickname=user_nickname,
            )
            if result is True:
                any_success = True
        return any_success

    async def _run_background_summarize(
        self,
        bot_name: str,
        group_or_user_id: str,
        self_id: str,
        start_id: int,
        end_id: int,
    ):
        """后台异步总结历史消息段，提炼记忆与状态"""
        try:
            db_messages = await self.plugin.db.get_messages_by_id_range(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                start_id=start_id,
                end_id=end_id,
            )
            if not db_messages:
                return

            bot_conf = self.plugin.bot_map.get(bot_name, {})
            nickname = bot_conf.get("nickname", bot_name)

            bot_participated = any(
                str(msg.user_id) == str(self_id) for msg in db_messages
            )
            memory_ok = None
            profile_ok = None
            if bot_participated:
                summary_context = await self._build_summary_context(
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                    self_id=self_id,
                    db_messages=db_messages,
                )

                memory_ok = await self._run_memory_summary_task(
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                    self_id=self_id,
                    nickname=nickname,
                    context=summary_context,
                )
                profile_ok = await self._run_profile_summary_task(
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                    nickname=nickname,
                    self_id=self_id,
                    context=summary_context,
                )
            else:
                logger.debug(
                    f"[Giftia Passive Memory] {bot_name}:{group_or_user_id} 消息范围 {start_id}-{end_id} 内机器人没有直接参与，跳过长期记忆和群片段画像总结。"
                )

            await self._run_long_profile_summary_tasks(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                nickname=nickname,
                self_id=self_id,
                db_messages=db_messages,
            )

            if profile_ok is False and memory_ok is not True:
                await self.plugin.db.upsert_kv_data(
                    f"passive_memory:last_summarized_id:{bot_name}:{group_or_user_id}",
                    start_id - 1,
                )

        except Exception as e:
            logger.error(
                f"[Giftia Passive Memory] 后台总结执行异常: {e}", exc_info=True
            )
            await self.plugin.db.upsert_kv_data(
                f"passive_memory:last_summarized_id:{bot_name}:{group_or_user_id}",
                start_id - 1,
            )
