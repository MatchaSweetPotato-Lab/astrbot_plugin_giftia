"""Typed memory records and verified source evidence."""

import hashlib
import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt


class DirectSpeech(HTMLParser):
    """Extract direct speech while excluding serialized quotation blocks."""

    def __init__(self, content: str):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.parts: list[str] = []
        self.reply_user_ids: list[str] = []
        self.feed(content)
        self.close()
        self.mentioned_user_ids = list(
            dict.fromkeys(
                named or bare
                for named, bare in re.findall(
                    r"@(?:[^@\n<>]*?\(([^()\s]+)\)|([A-Za-z0-9_-]+))",
                    "".join(self.parts),
                )
            )
        )
        self.media_ids = list(
            dict.fromkeys(
                re.findall(r"\[(?:图片|语音|视频):([^\]\s]+)\]", "".join(self.parts))
            )
        )

    def handle_starttag(self, tag, attrs):
        """Track quoted content.

        Args:
            tag: Opening HTML tag.
            attrs: Parsed attributes.
        """
        if tag == "quote":
            if not self.depth:
                sender_id = dict(attrs).get("sender_id")
                if sender_id:
                    self.reply_user_ids.append(sender_id)
            self.depth += 1

    def handle_endtag(self, tag):
        """Leave a quoted block.

        Args:
            tag: Closing HTML tag.
        """
        if tag == "quote":
            self.depth = max(0, self.depth - 1)

    def handle_data(self, data):
        """Retain unquoted speech.

        Args:
            data: Text between markup.
        """
        if not self.depth:
            self.parts.append(data)


class MemoryEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    source_id: StrictInt = Field(gt=0, description="new_messages 中消息的 source_id。")


class MemoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    memory_type: Literal["fact", "event"] = Field(
        description="fact 为被提及的持续信息或既往经历，需去重整理；event 为本批新近发生的事件或互动，含本次讲述旧事的动作，直接保存。同段聊天可拆成两类记忆。"
    )
    associated_user_ids: list[str] = Field(min_length=1, max_length=20)
    topic: str = Field(min_length=1, max_length=80)
    text: str = Field(
        min_length=1,
        max_length=300,
        description="简明陈述一个事实，自身用我；每位群友首次出现时用称呼（完整 user_id），无称呼时用完整 user_id。",
    )
    occurred_at: str = Field(
        default="",
        description="仅 event 可填写明确的实际发生时间（带时区的 ISO 格式），不明确时留空；fact 留空，记录时间由程序保存。",
    )
    importance: StrictInt = Field(
        default=5,
        ge=1,
        le=10,
        description="事实对后续互动的重要程度，1 最低、10 最高；不表示可信度或新旧程度。",
    )
    evidence: list[MemoryEvidence] = Field(
        min_length=1,
        max_length=20,
        description="必填且非空，至少引用一条 new_messages 中的直接来源消息，每项只填写 source_id。",
    )


class ManualMemoryRecord(MemoryRecord):
    """An administrator assertion without generated chat evidence."""

    evidence: list[MemoryEvidence] = Field(default_factory=list, max_length=0)


class MemoryRecords(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    memories: list[MemoryRecord] = Field(max_length=50)


class MemoryNoveltyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    candidate_id: StrictInt = Field(ge=1)
    is_new: bool
    text: str | None = Field(
        default=None,
        min_length=1,
        max_length=300,
        description="仅保留本批新增信息的完整事实正文，保留涉及群友的称呼、完整账号锚定及必要的时间限定；无需修改或丢弃时为 null。",
    )
    topic: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        description="需要修改时输出候选的新主题；无需修改或丢弃时为 null。",
    )


class MemoryNoveltyReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    decisions: list[MemoryNoveltyDecision] = Field(min_length=1, max_length=50)


def memory_metadata(memory: dict) -> dict:
    """Read metadata without interpreting retired classification fields.

    Args:
        memory: A persisted or recalled record.

    Returns:
        Metadata containing factual context and source evidence.
    """
    metadata = memory.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            metadata = {}
    if not isinstance(metadata, dict):
        return {}
    return {
        key: value
        for key, value in metadata.items()
        if key not in {"kind", "status", "expires_at", "source_memory_ids"}
    }


def validate_memory_records(
    result: MemoryRecords,
    messages: list,
    media: dict,
    self_id: str,
) -> list[dict]:
    """Validate a new batch and snapshot its original evidence before writing.

    Args:
        result: Parsed structured model response.
        messages: New source messages.
        media: Already available captions keyed by media ID.
        self_id: Account of the observing bot.

    Returns:
        New records including immutable, verified evidence.

    Raises:
        ValueError: A participant, timestamp, or evidence reference is invalid.
    """
    sources = {
        msg.db_id: msg
        for msg in messages
        if msg.db_id and not msg.is_recalled and msg.role != "operation_log"
    }
    members = {str(msg.user_id) for msg in sources.values()} | {self_id}
    for msg in sources.values():
        direct = DirectSpeech(msg.content or "")
        members.update(direct.mentioned_user_ids)
        members.update(direct.reply_user_ids)
    records = []
    for record in result.memories:
        item = record.model_dump()
        if any(uid not in members for uid in record.associated_user_ids):
            raise ValueError("Unknown associated participant")
        item["associated_user_ids"] = sorted(
            {uid for uid in record.associated_user_ids if uid and uid != self_id}
        )
        if not item["associated_user_ids"]:
            raise ValueError("A memory must involve a group member")
        if not record.evidence:
            raise ValueError("Incremental memories require direct source evidence")
        evidence = []
        for ref in record.evidence:
            msg = sources.get(ref.source_id)
            if msg is None:
                raise ValueError("Evidence is outside the new message batch")
            speech = DirectSpeech(msg.content or "")
            direct = "".join(speech.parts)
            if direct.lstrip().startswith("/"):
                raise ValueError("Commands cannot establish memory evidence")
            media_ids = []
            quotes = [direct] if direct.strip() else []
            for media_id in dict.fromkeys(msg.media_id_list or []):
                if media_id not in speech.media_ids:
                    continue
                caption = media.get(media_id) or {}
                caption_text = "\n".join(
                    str(caption[key]).strip()
                    for key in ("text", "caption")
                    if str(caption.get(key) or "").strip()
                )
                if caption_text:
                    media_ids.append(media_id)
                    quotes.append("媒体转述：" + caption_text)
            plain_text = re.sub(
                r"\[(?:图片|语音|视频|合并转发)(?::[^\]]*)?\]", "", direct
            ).strip()
            if not plain_text and not media_ids:
                raise ValueError("Evidence has no direct source content")
            verified = {
                **ref.model_dump(),
                "media_ids": media_ids,
                "quote": "\n".join(quotes),
                "user_id": str(msg.user_id),
                "message_id": msg.message_id,
                "time": msg.time,
            }
            evidence.append(verified)
        item["evidence"] = list({ref["source_id"]: ref for ref in evidence}.values())
        item["new_source_ids"] = sorted({ref.source_id for ref in record.evidence})
        if item["memory_type"] == "fact":
            item["occurred_at"] = ""
        elif item["occurred_at"]:
            try:
                timestamp = datetime.fromisoformat(
                    item["occurred_at"].replace("Z", "+00:00")
                )
                item["occurred_at"] = timestamp.astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError, OverflowError) as error:
                raise ValueError("Invalid occurred_at") from error
        fingerprint = json.dumps(
            [
                item["memory_type"],
                item["associated_user_ids"],
                record.topic,
                " ".join(record.text.split()),
                item["new_source_ids"],
                item["occurred_at"] if item["memory_type"] == "event" else "",
            ],
            ensure_ascii=False,
        )
        item["fingerprint"] = hashlib.sha256(fingerprint.encode()).hexdigest()
        records.append(item)
    return records
