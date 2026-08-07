import re
from datetime import datetime

from ..utils.schemas import FORWARD_MEDIA_PATTERN

from astrbot.api import logger

from ..llm.preset_prompts import (
    DEFAULT_PASSIVE_LONG_PROFILE_SUMMARY_PROMPT,
    DEFAULT_PASSIVE_MEMORY_SUMMARY_PROMPT,
    DEFAULT_PASSIVE_PROFILE_SUMMARY_PROMPT,
)
from ..llm.prompt import (
    USER_PROFILE_FIELDS,
    normalize_profile_text,
    normalize_profile_value,
    parse_caption_to_str,
    parse_message_to_str,
    process_media_captions_for_prompt,
    truncate_message_content,
)

SESSION_PROFILE_FIELDS = {"aliases", "attitude", "agreements"}
LONG_PROFILE_FIELDS = {"personality", "interests", "extra"}
DEFAULT_LONG_PROFILE_SAMPLE_LIMIT = 200
DEFAULT_LONG_PROFILE_MESSAGE_THRESHOLD = 200
DEFAULT_LONG_PROFILE_TEXT_THRESHOLD = 5000
DEFAULT_LONG_PROFILE_INITIAL_MESSAGE_THRESHOLD = 200
DEFAULT_LONG_PROFILE_INITIAL_TEXT_THRESHOLD = 5000


def format_time_to_seconds(db_value: str) -> str:
    if not db_value:
        return ""
    try:
        dt = datetime.fromisoformat(db_value)
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return db_value[:19] if len(db_value) >= 19 else db_value


class PassiveContextMixin:
    def _format_prompt_template(
        self, prompt_template: str, nickname: str, self_id: str
    ) -> str:
        try:
            return prompt_template.format(nickname=nickname, self_id=self_id)
        except Exception as e:
            logger.warning(
                f"[Giftia Passive Memory] 提示词变量格式化失败，将使用原文: {e}"
            )
            return prompt_template

    def _get_memory_summary_prompt(self) -> str:
        return DEFAULT_PASSIVE_MEMORY_SUMMARY_PROMPT

    def _get_profile_summary_prompt(self) -> str:
        return DEFAULT_PASSIVE_PROFILE_SUMMARY_PROMPT

    def _get_long_profile_summary_prompt(self) -> str:
        return DEFAULT_PASSIVE_LONG_PROFILE_SUMMARY_PROMPT

    def _get_long_profile_sample_limit(self) -> int:
        return DEFAULT_LONG_PROFILE_SAMPLE_LIMIT

    def _get_long_profile_thresholds(self, has_existing_long_profile: bool) -> tuple[int, int]:
        if has_existing_long_profile:
            return (
                DEFAULT_LONG_PROFILE_MESSAGE_THRESHOLD,
                DEFAULT_LONG_PROFILE_TEXT_THRESHOLD,
            )
        return (
            DEFAULT_LONG_PROFILE_INITIAL_MESSAGE_THRESHOLD,
            DEFAULT_LONG_PROFILE_INITIAL_TEXT_THRESHOLD,
        )

    def _format_user_profile_record_for_summary(
        self,
        record: dict | None,
        allowed_fields: set[str] | None = None,
        include_relation_score: bool = True,
        include_relation_title: bool = True,
    ) -> str:
        if not record:
            return "无"

        parts = []
        structured_lines = []
        allowed_fields = (
            set(allowed_fields)
            if allowed_fields is not None
            else {field for field, _ in USER_PROFILE_FIELDS}
        )
        has_any_structured_field = any(
            normalize_profile_value(record.get(field))
            for field, _ in USER_PROFILE_FIELDS
        )
        for field, label in USER_PROFILE_FIELDS:
            if field not in allowed_fields:
                continue
            value = normalize_profile_value(record.get(field))
            if value:
                structured_lines.append(f"- {label}：{value}")
        if structured_lines:
            parts.append("结构化画像:\n" + "\n".join(structured_lines))

        legacy_profile = normalize_profile_text(record.get("profile"))
        if legacy_profile and not has_any_structured_field:
            parts.append("历史画像参考:\n" + legacy_profile)

        relation_parts = []
        title = record.get("title") if include_relation_title else None
        relation = record.get("relation") if include_relation_score else None
        if relation not in (None, "", 0):
            relation_parts.append(f"好感度: {relation}")
        if title:
            relation_parts.append(f"头衔: {title}")
        if relation_parts:
            parts.append("关系状态: " + "，".join(relation_parts))

        return "\n".join(parts) if parts else "无"

    def _parse_profile_fields(
        self, profile_content: str, allowed_fields: set[str] | None = None
    ) -> dict[str, str]:
        parsed = {}
        for field, _ in USER_PROFILE_FIELDS:
            if allowed_fields is not None and field not in allowed_fields:
                continue
            match = re.search(
                rf"<{field}>(.*?)</{field}>",
                profile_content,
                re.DOTALL,
            )
            if not match:
                continue
            value = normalize_profile_value(match.group(1))
            if value:
                parsed[field] = value
        return parsed

    def _parse_session_profile_fields(self, profile_content: str) -> dict[str, str]:
        return self._parse_profile_fields(profile_content, SESSION_PROFILE_FIELDS)

    def _has_any_profile_field_tag(self, profile_content: str) -> bool:
        return any(
            re.search(rf"<{field}>.*?</{field}>", profile_content, re.DOTALL)
            for field, _ in USER_PROFILE_FIELDS
        )

    def _has_existing_long_profile(self, record: dict | None) -> bool:
        if not record:
            return False
        return any(normalize_profile_value(record.get(field)) for field in LONG_PROFILE_FIELDS)

    def _is_long_profile_sample_message(self, msg) -> bool:
        if not msg or getattr(msg, "is_recalled", 0):
            return False
        if getattr(msg, "role", "message") == "operation_log":
            return False

        content = str(getattr(msg, "content", "") or "").strip()
        media_ids = getattr(msg, "media_id_list", None) or []
        if media_ids:
            return True
        if not content:
            return False

        normalized = FORWARD_MEDIA_PATTERN.sub("", content)
        normalized = re.sub(r"\[(?:图片|语音|视频|合并转发消息)\]", "", normalized)
        normalized = normalized.strip()
        if len(normalized) >= 4:
            return True
        return normalized not in {"哈", "哈哈", "hhh", "？", "?", "。", "嗯", "哦", "草"}

    def _long_profile_sample_text_length(self, msg) -> int:
        content = str(getattr(msg, "content", "") or "")
        content = FORWARD_MEDIA_PATTERN.sub("", content)
        return len(content.strip())

    async def _build_long_profile_user_prompt(
        self,
        bot_name: str,
        group_or_user_id: str,
        user_id: str,
        nickname: str,
        profile_record: dict | None,
        sample_messages: list,
        fetched_message_count: int,
        sample_limit: int,
    ) -> str:
        all_media_ids = []
        for msg in sample_messages:
            if msg.media_id_list:
                all_media_ids.extend(msg.media_id_list)
        unique_media_ids = list(dict.fromkeys(all_media_ids))

        media_captions = []
        for media_id in unique_media_ids:
            media_caption = await self.plugin.data_cache.get_caption_by_hash(media_id)
            if media_caption:
                media_captions.append(media_caption)

        processed_messages, remaining_captions = process_media_captions_for_prompt(
            messages=sample_messages,
            media_captions=media_captions,
            threshold=100,
        )

        user_message_lines = [
            parse_message_to_str(msg) for msg in processed_messages
        ]
        sample_start = sample_messages[0].time if sample_messages else ""
        sample_end = sample_messages[-1].time if sample_messages else ""

        user_prompt_parts = [
            f"<session_id>{group_or_user_id}</session_id>",
            f"<target_user>\nuser_id: {user_id}\nnickname: {nickname or '未知'}\n</target_user>",
            (
                "<sample_info>\n"
                f"selected_messages: {len(sample_messages)}\n"
                f"fetched_recent_messages: {fetched_message_count}\n"
                f"sample_limit: {sample_limit}\n"
                f"time_range: {format_time_to_seconds(sample_start)} 到 {format_time_to_seconds(sample_end)}\n"
                "note: 如果 fetched_recent_messages 等于 sample_limit，可能已省略更旧的未分析消息。\n"
                "</sample_info>"
            ),
            (
                "<current_user_profile>\n"
                + self._format_user_profile_record_for_summary(
                    profile_record,
                    allowed_fields=LONG_PROFILE_FIELDS,
                    include_relation_score=False,
                    include_relation_title=False,
                )
                + "\n</current_user_profile>"
            ),
        ]
        if remaining_captions:
            media_captions_block = "\n".join(
                parse_caption_to_str(caption) for caption in remaining_captions
            )
            user_prompt_parts.append(
                f"<media_content>\n{media_captions_block}\n</media_content>"
            )
        user_prompt_parts.append(
            "<user_messages>\n"
            + "\n".join(user_message_lines)
            + "\n</user_messages>"
        )
        return "\n\n".join(user_prompt_parts)

    def _parse_long_profile_fields(self, completion_text: str) -> dict[str, str]:
        match = re.search(
            r"<long_user_profile(?:\s+[^>]*)?>(.*?)</long_user_profile>",
            completion_text or "",
            re.DOTALL,
        )
        profile_content = match.group(1).strip() if match else completion_text or ""
        if not profile_content or profile_content.strip() == "无":
            return {}

        return self._parse_profile_fields(profile_content, LONG_PROFILE_FIELDS)

    def _is_bot_reference(
        self,
        target_user: str,
        resolved_user_id: str,
        self_id: str,
        nickname: str = "",
        bot_name: str = "",
    ) -> bool:
        target = str(target_user or "").strip()
        resolved = str(resolved_user_id or "").strip()
        bot_refs = {
            str(self_id or "").strip(),
            str(nickname or "").strip(),
            str(bot_name or "").strip(),
            "bot",
        }
        bot_refs.discard("")
        return target in bot_refs or resolved in bot_refs

    async def _refresh_known_alias_observations(
        self,
        bot_name: str,
        group_or_user_id: str,
        self_id: str,
        db_messages: list,
    ) -> None:
        aliases = await self.plugin.db.get_session_user_aliases(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
        )
        if not aliases:
            return

        scan_messages = []
        for msg in db_messages or []:
            sender_id = str(msg.user_id or "").strip()
            content = str(msg.content or "")
            if not sender_id or not content:
                continue
            if self._is_bot_reference(
                sender_id,
                sender_id,
                self_id,
                bot_name=bot_name,
            ):
                continue
            scan_messages.append((sender_id, content))

        if not scan_messages:
            return

        observations = []
        observed_keys = set()
        for item in aliases:
            target_user_id = str(item.get("user_id") or "").strip()
            alias = str(item.get("alias") or "").strip()
            if not target_user_id or not alias:
                continue
            if self._is_bot_reference(
                target_user_id,
                target_user_id,
                self_id,
                bot_name=bot_name,
            ):
                continue

            observed = any(
                sender_id != target_user_id and alias in content
                for sender_id, content in scan_messages
            )
            key = (target_user_id, alias)
            if observed and key not in observed_keys:
                observed_keys.add(key)
                observations.append((target_user_id, alias, 1))

        if not observations:
            return

        await self.plugin.db.increment_user_alias_counts(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            observations=observations,
        )
        for target_user_id, _, _ in observations:
            fmt_key = f"{bot_name}:{group_or_user_id}:{target_user_id}"
            self.plugin.data_cache.user_profile_records.pop(fmt_key, None)
        logger.debug(
            f"[Giftia Passive Memory] 已刷新旧外号观测次数: {len(observations)} 条"
        )

    async def _build_summary_context(
        self,
        bot_name: str,
        group_or_user_id: str,
        self_id: str,
        db_messages: list,
    ) -> dict:
        active_users_in_range = {
            msg.user_id
            for msg in db_messages
            if msg.user_id
            and not self._is_bot_reference(
                msg.user_id,
                msg.user_id,
                self_id,
                bot_name=bot_name,
            )
        }

        nickname_to_user_id = {}
        user_id_to_nickname = {}
        for msg in db_messages:
            if msg.user_id and msg.nickname:
                if not self._is_bot_reference(
                    msg.user_id, msg.user_id, self_id, bot_name=bot_name
                ):
                    nickname_to_user_id[msg.nickname] = msg.user_id
                    user_id_to_nickname[msg.user_id] = msg.nickname

        all_media_ids = []
        for msg in db_messages:
            if msg.media_id_list:
                all_media_ids.extend(msg.media_id_list)
        unique_media_ids = list(dict.fromkeys(all_media_ids))

        media_captions = []
        for media_id in unique_media_ids:
            media_caption = await self.plugin.data_cache.get_caption_by_hash(media_id)
            if media_caption:
                media_captions.append(media_caption)

        processed_messages, remaining_captions = process_media_captions_for_prompt(
            messages=db_messages,
            media_captions=media_captions,
            threshold=100,
        )

        chat_history_lines = []
        for msg in processed_messages:
            truncated_content = truncate_message_content(msg.content or "")
            chat_history_lines.append(
                f"[{format_time_to_seconds(msg.time)}] {msg.nickname}({msg.user_id}): {truncated_content}"
            )
        chat_history_text = "\n".join(chat_history_lines)

        active_user_lines = []
        user_profile_blocks = []
        for uid in sorted(active_users_in_range):
            nickname = user_id_to_nickname.get(uid, "")
            profile_record = await self.plugin.data_cache.get_user_profile_record(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                user_id=uid,
            )

            # 获取 Bot 对用户的自定义称呼并注入到 active_users_text 中
            call_name = profile_record.get("call_name") if profile_record else None
            parts = [f"群内昵称: {nickname}" if nickname else None]
            if call_name:
                parts.append(f"你对他的称呼: {call_name}")
            info_str = "，".join(p for p in parts if p)
            active_user_lines.append(f"- {uid} ({info_str})" if info_str else f"- {uid}")

            block_lines = [f"用户 {uid} ({nickname})" if nickname else f"用户 {uid}"]
            block_lines.append(
                "现有画像:\n" + self._format_user_profile_record_for_summary(
                    profile_record,
                    allowed_fields=SESSION_PROFILE_FIELDS,
                    include_relation_score=False,
                )
            )
            user_profile_blocks.append("\n".join(block_lines))

        group_profile = await self.plugin.data_cache.get_group_profile(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
        )

        media_captions_block = ""
        if remaining_captions:
            media_captions_block = "\n".join(
                parse_caption_to_str(c) for c in remaining_captions
            )

        return {
            "nickname_to_user_id": nickname_to_user_id,
            "user_id_to_nickname": user_id_to_nickname,
            "active_users_in_range": active_users_in_range,
            "active_users_text": "\n".join(active_user_lines) or "无",
            "user_profiles_text": "\n---\n".join(user_profile_blocks) or "无",
            "group_profile": group_profile or "无",
            "media_captions_block": media_captions_block,
            "chat_history_text": chat_history_text,
            "alias_observation_messages": db_messages,
        }

    def _build_memory_user_prompt(self, group_or_user_id: str, context: dict) -> str:
        user_prompt_parts = [
            f"<session_id>{group_or_user_id}</session_id>",
            f"<active_users>\n{context['active_users_text']}\n</active_users>",
        ]
        if context["media_captions_block"]:
            user_prompt_parts.append(
                f"<media_content>\n{context['media_captions_block']}\n</media_content>"
            )
        user_prompt_parts.append(
            f"<chat_history>\n{context['chat_history_text']}\n</chat_history>"
        )
        return "\n\n".join(user_prompt_parts)

    def _build_profile_user_prompt(self, group_or_user_id: str, context: dict) -> str:
        user_prompt_parts = [
            f"<session_id>{group_or_user_id}</session_id>",
            f"<current_user_profiles>\n{context['user_profiles_text']}\n</current_user_profiles>",
            f"<current_group_profile>\n{context['group_profile']}\n</current_group_profile>",
        ]
        if context["media_captions_block"]:
            user_prompt_parts.append(
                f"<media_content>\n{context['media_captions_block']}\n</media_content>"
            )
        user_prompt_parts.append(
            f"<chat_history>\n{context['chat_history_text']}\n</chat_history>"
        )
        return "\n\n".join(user_prompt_parts)
