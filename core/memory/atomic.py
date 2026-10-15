"""Validated atomic memories and source evidence for maintenance tasks."""

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


class MemoryChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    memory_id: str = Field(default="", max_length=128)
    kind: Literal["event", "agreement", "relationship", "preference", "habit"]
    associated_user_ids: list[str] = Field(min_length=1, max_length=20)
    topic: str = Field(min_length=1, max_length=80)
    text: str = Field(
        min_length=1,
        max_length=300,
        description="第一人称回忆正文。",
    )
    status: Literal["active", "completed", "cancelled"] = "active"
    occurred_at: str = ""
    expires_at: str = ""
    importance: StrictInt = Field(default=5, ge=1, le=10)
    evidence: list[MemoryEvidence] = Field(default_factory=list, max_length=20)
    source_memory_ids: list[str] = Field(default_factory=list, max_length=100)
    supersedes: list[str] = Field(default_factory=list, max_length=100)


class MemoryChanges(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    memories: list[MemoryChange] = Field(max_length=50)


def memory_input(row: dict) -> dict:
    """Expose authoritative state with its stored provenance.

    Args:
        row: A memory row from the scoped SQLite repository.

    Returns:
        Compact fields suitable for model input and validation.
    """
    try:
        metadata = json.loads(row.get("metadata") or "{}")
    except (TypeError, ValueError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        **metadata,
        **{
            key: row[key]
            for key in (
                "memory_id",
                "kind",
                "status",
                "text",
                "importance",
                "expires_at",
                "created_at",
                "updated_at",
                "revision",
            )
            if key in row
        },
    }


def recall_memory_metadata(memory: dict) -> dict:
    """Normalize recall metadata and expose the effective lifecycle state.

    Args:
        memory: Recalled record with optional authoritative lifecycle columns.

    Returns:
        Copied metadata with current type, status and deadline. Elapsed deadlines
        mark active records expired without discarding their historical facts.
    """
    metadata = memory.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            metadata = {}
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    for key in ("kind", "status", "expires_at"):
        if key in memory:
            metadata[key] = memory[key]
    status = metadata.get("status") or "active"
    expires_at = metadata.get("expires_at")
    if status == "active" and isinstance(expires_at, str) and expires_at:
        try:
            if datetime.fromisoformat(
                expires_at.replace("Z", "+00:00")
            ).timestamp() <= (datetime.now(timezone.utc).timestamp()):
                status = "expired"
        except (ValueError, OverflowError, OSError):
            pass
    metadata["status"] = status
    return metadata


def validate_memory_changes(
    result: MemoryChanges,
    existing: list[dict],
    messages: list,
    media: dict,
    self_id: str,
    consolidating: bool = False,
    new_memory_ids: set[str] | None = None,
) -> list[dict]:
    """Validate an entire batch before any memory or cursor is written.

    Args:
        result: Parsed structured model response.
        existing: Exact scoped memory snapshot supplied to the model.
        messages: New source messages; empty during consolidation.
        media: Already available captions keyed by media ID.
        self_id: Account of the observing bot.
        consolidating: Whether only existing provenance may be used.
        new_memory_ids: Newly extracted records providing fresh evidence for closures.

    Returns:
        Normalized changes including immutable, verified evidence.

    Raises:
        ValueError: A reference, transition, or evidence claim is invalid.
    """
    known = {row["memory_id"]: memory_input(row) for row in existing}
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
    for old in known.values():
        if old.get("user_id"):
            members.add(str(old["user_id"]))
        members.update(str(uid) for uid in old.get("associated_user_ids", []) if uid)
    changes, touched = [], set()
    for change in result.memories:
        if not consolidating and (
            change.memory_id or change.source_memory_ids or change.supersedes
        ):
            raise ValueError("Extraction may only create new memories")
        item = change.model_dump()
        target = known.get(change.memory_id)
        if target:
            for key in ("expires_at", "importance"):
                if key not in change.model_fields_set and target.get(key) is not None:
                    item[key] = target[key]
        if change.memory_id and (target is None or change.memory_id in touched):
            raise ValueError("Unknown or repeated memory target")
        if any(uid not in members for uid in change.associated_user_ids):
            raise ValueError("Unknown associated participant")
        item["associated_user_ids"] = sorted(
            {uid for uid in change.associated_user_ids if uid and uid != self_id}
        )
        if not item["associated_user_ids"]:
            raise ValueError("A memory must involve a group member")
        if (
            target
            and target.get("schema_version") == 2
            and target.get("kind") != item["kind"]
        ):
            raise ValueError("An existing atom cannot change its type")
        if change.status != "active":
            fresh_evidence = [
                ref
                for memory_id in change.source_memory_ids
                if memory_id in (new_memory_ids or ())
                for ref in known.get(memory_id, {}).get("evidence", [])
                if str(ref.get("user_id") or "")
                in (target.get("associated_user_ids", []) if target else [])
                and str(ref.get("user_id") or "") != self_id
            ]
            if (
                not target
                or change.kind != "agreement"
                or not consolidating
                or not fresh_evidence
            ):
                raise ValueError("Only newly evidenced agreements may be closed")
        if target and target["status"] != "active":
            raise ValueError("A closed memory cannot be reopened by maintenance")
        if consolidating and (change.evidence or not change.source_memory_ids):
            raise ValueError("Consolidation must reference existing memories")
        if not consolidating and not change.evidence:
            raise ValueError("Incremental memories require direct source evidence")

        evidence = list(target.get("evidence", [])) if target else []
        for ref in change.source_memory_ids:
            if ref not in known:
                raise ValueError("Unknown source memory")
            evidence.extend(known[ref].get("evidence", []))
        member_evidence = []
        for ref in change.evidence:
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
            if str(msg.user_id) != self_id:
                member_evidence.append(verified)
        if not consolidating and not member_evidence:
            raise ValueError("Self output alone cannot establish a memory")
        evidence = list({ref["source_id"]: ref for ref in evidence}.values())
        if change.kind == "habit":
            observations = [
                ref
                for ref in evidence
                if ref.get("user_id") and str(ref["user_id"]) != self_id
            ]
            supporting = [known[ref] for ref in change.source_memory_ids]
            if consolidating:
                if any(
                    source.get("kind") not in {"event", "habit"}
                    for source in supporting
                ):
                    raise ValueError("Habits require event or habit sources")
                has_habit = (target and target.get("kind") == "habit") or any(
                    source.get("kind") == "habit" for source in supporting
                )
                if (
                    not has_habit
                    and len({ref["source_id"] for ref in observations}) < 3
                ):
                    raise ValueError(
                        "New habits inferred from events require three independent original observations"
                    )
            times = [ref["time"] for ref in observations if ref.get("time")]
            if times:
                observed = [
                    datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
                        timezone.utc
                    )
                    for value in times
                ]
                item["observed_from"] = min(observed).isoformat()
                item["observed_until"] = max(observed).isoformat()

        for key in ("occurred_at", "expires_at"):
            if item[key]:
                try:
                    timestamp = datetime.fromisoformat(item[key].replace("Z", "+00:00"))
                    item[key] = timestamp.astimezone(timezone.utc).isoformat()
                except (ValueError, TypeError, OverflowError) as error:
                    raise ValueError(f"Invalid {key}") from error
        if item["expires_at"] and change.kind != "agreement":
            raise ValueError("Only agreements have automatic expiration")
        if not item["occurred_at"]:
            item["occurred_at"] = target.get("occurred_at", "") if target else ""

        for ref in change.supersedes:
            old = known.get(ref)
            if old is None or ref == change.memory_id or ref in touched:
                reason = (
                    "ID is not present in the input snapshot"
                    if old is None
                    else "ID equals the retained memory_id"
                    if ref == change.memory_id
                    else "ID was already updated or superseded by an earlier change"
                )
                raise ValueError(
                    f"Invalid superseded memory: memory_id={change.memory_id!r}, "
                    f"supersedes={ref!r}; {reason}"
                )
            if old["status"] != "active" or (
                old.get("schema_version") == 2 and old["kind"] != change.kind
            ):
                raise ValueError(
                    "Only active atoms of the same type may be superseded: "
                    f"memory_id={change.memory_id!r}, supersedes={ref!r}, "
                    f"existing status={old['status']!r}, kind={old['kind']!r}, "
                    f"output kind={change.kind!r}"
                )
            if ref not in change.source_memory_ids:
                raise ValueError(
                    f"Retired memory {ref!r} must be included in source_memory_ids"
                )
            if change.kind == "habit" and old["kind"] == "event":
                raise ValueError("Habit consolidation must preserve source events")
        item["evidence"] = evidence
        item["new_source_ids"] = sorted({ref.source_id for ref in change.evidence})
        item["source_memory_ids"] = sorted(
            set(
                change.source_memory_ids
                + (target.get("source_memory_ids", []) if target else [])
            )
            - {change.memory_id}
        )
        fingerprint = json.dumps(
            [
                change.kind,
                item["associated_user_ids"],
                change.topic,
                " ".join(change.text.split()),
                sorted({ref["source_id"] for ref in evidence}),
                sorted(change.source_memory_ids) if not evidence else [],
            ],
            ensure_ascii=False,
        )
        item["fingerprint"] = hashlib.sha256(fingerprint.encode()).hexdigest()
        touched.update([change.memory_id, *change.supersedes])
        changes.append(item)
    return changes
