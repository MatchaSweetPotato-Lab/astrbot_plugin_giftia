import re
from datetime import datetime

from astrbot.api import logger

from ..llm.preset_prompts import (
    DEFAULT_PASSIVE_MEMORY_SUMMARY_PROMPT,
    DEFAULT_PASSIVE_PROFILE_SUMMARY_PROMPT,
)
from ..llm.prompt import (
    USER_PROFILE_FIELDS,
    normalize_profile_value,
    parse_caption_to_str,
    process_media_captions_for_prompt,
    truncate_message_content,
)

SESSION_PROFILE_FIELDS = {"aliases", "attitude"}


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

    def _format_user_profile_record_for_summary(
        self,
        record: dict | None,
        allowed_fields: set[str] | None = None,
        include_relation_score: bool = True,
    ) -> str:
        record = record or {}

        parts = []
        allowed_fields = (
            set(allowed_fields)
            if allowed_fields is not None
            else {field for field, _ in USER_PROFILE_FIELDS}
        )
        for field, label in USER_PROFILE_FIELDS:
            if field not in allowed_fields:
                continue
            value = normalize_profile_value(record.get(field))
            if value:
                parts.append(f"{label}：{value}")

        if include_relation_score:
            parts.append(f"好感度: {record.get('relation') or 0}")

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

        active_user_briefs = []
        user_profile_blocks = []
        for uid in sorted(active_users_in_range):
            nickname = user_id_to_nickname.get(uid, "")
            profile_record = await self.plugin.data_cache.get_user_profile_record(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                user_id=uid,
            )

            # Apply preferred names only when rendering, preserving source snapshots.
            call_name = profile_record.get("call_name") if profile_record else None
            if call_name:
                active_user_briefs.append({"user_id": str(uid), "call_name": call_name})

            block_lines = [f"用户 {uid} ({nickname})" if nickname else f"用户 {uid}"]
            block_lines.append(
                "现有画像:\n"
                + self._format_user_profile_record_for_summary(
                    profile_record,
                    allowed_fields=SESSION_PROFILE_FIELDS,
                    include_relation_score=True,
                )
            )
            user_profile_blocks.append("\n".join(block_lines))

        media_captions_block = ""
        if remaining_captions:
            media_captions_block = "\n".join(
                parse_caption_to_str(c) for c in remaining_captions
            )

        return {
            "nickname_to_user_id": nickname_to_user_id,
            "user_id_to_nickname": user_id_to_nickname,
            "active_users_in_range": active_users_in_range,
            "active_user_briefs": active_user_briefs,
            "user_profiles_text": "\n---\n".join(user_profile_blocks) or "无",
            "media_captions_block": media_captions_block,
            "chat_history_text": chat_history_text,
            "alias_observation_messages": db_messages,
            "memory_media_captions": [
                caption for caption in media_captions if caption.is_captioned
            ],
        }

    def _build_profile_user_prompt(self, group_or_user_id: str, context: dict) -> str:
        user_prompt_parts = [
            f"<session_id>{group_or_user_id}</session_id>",
            f"<current_user_profiles>\n{context['user_profiles_text']}\n</current_user_profiles>",
        ]
        if context["media_captions_block"]:
            user_prompt_parts.append(
                f"<media_content>\n{context['media_captions_block']}\n</media_content>"
            )
        user_prompt_parts.append(
            f"<chat_history>\n{context['chat_history_text']}\n</chat_history>"
        )
        return "\n\n".join(user_prompt_parts)
