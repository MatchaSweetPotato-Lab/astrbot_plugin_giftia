"""Incremental fact extraction and optional vector indexing."""

import asyncio
import hashlib
import json
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime
from xml.etree.ElementTree import Element, SubElement, fromstring, indent, tostring

from astrbot.api import logger

from ..llm.json_parse import parse_markdown_json
from ..llm.preset_prompts import DEFAULT_MEMORY_NOVELTY_PROMPT
from ..llm.prompt import (
    _apply_call_names_to_messages,
    parse_caption_to_str,
    process_media_captions_for_prompt,
)
from ..utils.schemas import MediaCaption, MessageData
from .atomic import (
    DirectSpeech,
    MemoryNoveltyReview,
    MemoryRecords,
    memory_metadata,
    validate_memory_records,
)
from .recall_scope import resolve_memory_recall_scope


class MemoryMaintenanceMixin:
    def _memory_user_prompt(
        self,
        messages: list[MessageData],
        media_captions: list[MediaCaption],
        active_user_briefs: list[dict] | None = None,
    ):
        """Render bounded model inputs with explicit provenance boundaries.

        Args:
            messages: New message batch with database IDs.
            media_captions: Previously completed captions, including media metadata.
            active_user_briefs: Scoped preferred names in the reply prompt's format.

        Returns:
            Escaped participant names, source messages, and available media captions.
        """
        messages = _apply_call_names_to_messages(
            messages,
            user_id="",
            user_profile=None,
            active_user_briefs=active_user_briefs,
        )
        root = Element("memory_input")
        source_messages = []
        direct_media_ids = set()
        history = SubElement(root, "new_messages")
        for msg in messages:
            if msg.is_recalled or msg.role == "operation_log":
                continue
            speech = DirectSpeech(msg.content or "")
            direct = "".join(speech.parts)
            if direct.lstrip().startswith("/"):
                continue
            media_ids = [
                media_id
                for media_id in msg.media_id_list or []
                if media_id in speech.media_ids
            ]
            source_messages.append(
                replace(msg, content=direct, media_id_list=media_ids)
            )
            direct_media_ids.update(media_ids)
            attrs = {"source_id": str(msg.db_id), "user_id": str(msg.user_id)}
            nickname = msg.nickname
            if nickname and nickname != str(msg.user_id):
                attrs["nickname"] = nickname
            if msg.time:
                attrs["time"] = msg.time
            if speech.reply_user_ids:
                attrs["reply_to"] = ",".join(dict.fromkeys(speech.reply_user_ids))
            SubElement(history, "message", attrs)
        # Filter before processing so quoted or ineligible media cannot enter the prompt.
        available_captions = {
            caption.hash_val: caption
            for caption in media_captions
            if caption.hash_val in direct_media_ids
            and caption.is_captioned
            and ((caption.text or "").strip() or (caption.caption or "").strip())
        }
        processed_messages, remaining_captions = process_media_captions_for_prompt(
            source_messages, list(available_captions.values()), threshold=100
        )
        for node, msg in zip(history, processed_messages):
            node.text = msg.content
        if remaining_captions:
            captions = Element("media_content")
            captions.extend(
                fromstring(parse_caption_to_str(caption))
                for caption in remaining_captions
            )
            root.insert(0, captions)
        SubElement(root, "time").text = (
            datetime.now().astimezone().isoformat(timespec="seconds")
        )
        indent(root, space="  ")
        return tostring(root, encoding="unicode")

    async def _run_memory_summary_task(
        self, bot_name, group_or_user_id, self_id, nickname, context
    ) -> bool:
        """Extract typed records, review facts, and preserve independent events.

        Args:
            bot_name: Owning bot configuration.
            group_or_user_id: Owning conversation.
            self_id: Observing bot account.
            nickname: Observing bot nickname.
            context: Current summary batch and completed media captions.

        Returns:
            Whether the batch is durably handled, including empty results.
        """
        messages = context["alias_observation_messages"]
        identity = json.dumps(
            [(msg.db_id, msg.message_id, msg.time) for msg in messages]
        )
        batch_key = f"atomic_memory:batch:{bot_name}:{group_or_user_id}:{hashlib.sha256(identity.encode()).hexdigest()}"
        if await self.plugin.db.get_kv_data(batch_key, 0):
            return True
        repo = self.plugin.db.memories_repo
        media_captions = context.get("memory_media_captions", [])
        media = {
            caption.hash_val: {"text": caption.text, "caption": caption.caption}
            for caption in media_captions
            if caption.is_captioned
        }
        prompt = self._format_prompt_template(
            self._get_memory_summary_prompt(), nickname, self_id
        )
        prompt += "\n\nJSON Schema:\n" + json.dumps(
            MemoryRecords.model_json_schema(), ensure_ascii=False
        )
        completion = await self._call_summary_llm(
            "原子记忆归纳",
            prompt,
            self._memory_user_prompt(
                messages,
                media_captions,
                active_user_briefs=context.get("active_user_briefs"),
            ),
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
        )
        if not completion:
            return False
        try:
            parsed = MemoryRecords.model_validate(parse_markdown_json(completion))
            records = validate_memory_records(parsed, messages, media, self_id)
            facts = [item for item in records if item["memory_type"] == "fact"]
            # Only fact review needs serialization across this bot's sessions.
            lock = (
                self.novelty_locks.setdefault(bot_name, asyncio.Lock())
                if facts
                else nullcontext()
            )
            async with lock:
                if facts:
                    order = {item["fingerprint"]: i for i, item in enumerate(records)}
                    facts = await self._review_memory_novelty(
                        bot_name, group_or_user_id, self_id, nickname, facts
                    )
                    records = sorted(
                        [item for item in records if item["memory_type"] == "event"]
                        + facts,
                        key=lambda item: order[item["fingerprint"]],
                    )
                    # Rebuild source snapshots and fingerprints after fact edits.
                    reviewed = MemoryRecords.model_validate(
                        {
                            "memories": [
                                {
                                    "memory_type": item["memory_type"],
                                    "associated_user_ids": item["associated_user_ids"],
                                    "topic": item["topic"],
                                    "text": item["text"],
                                    "occurred_at": item["occurred_at"],
                                    "importance": item["importance"],
                                    "evidence": [
                                        {"source_id": ref["source_id"]}
                                        for ref in item["evidence"]
                                    ],
                                }
                                for item in records
                            ]
                        }
                    )
                    records = validate_memory_records(
                        reviewed, messages, media, self_id
                    )
                affected = await repo.insert_memories(
                    bot_name,
                    group_or_user_id,
                    records,
                    batch_key=batch_key,
                    source_end=messages[-1].db_id,
                    source_ids=[msg.db_id for msg in messages],
                )
        except (ValueError, TypeError) as error:
            logger.warning(
                "[Giftia Memory] Rejected extraction for %s:%s: %s",
                bot_name,
                group_or_user_id,
                error,
            )
            return False
        self.plugin.data_cache.memories.pop(f"{bot_name}:{group_or_user_id}", None)
        for memory_id in affected:
            self.plugin.data_cache.remove_session_recalled_memory(memory_id)
        await self._sync_memory_vectors(bot_name, group_or_user_id)
        logger.info(
            "[Giftia Memory] Saved %s memories for %s:%s",
            len(affected),
            bot_name,
            group_or_user_id,
        )
        return True

    async def _review_memory_novelty(
        self, bot_name, session_id, self_id, nickname, records: list[dict]
    ) -> list[dict]:
        """Recall related facts and retain only candidates' new information.

        Args:
            bot_name: Owning bot configuration.
            session_id: Current conversation.
            self_id: Observing bot account.
            nickname: Observing bot nickname.
            records: Validated candidates with immutable source evidence.

        Returns:
            Approved candidates with optional text and topic edits, preserving evidence.

        Raises:
            ValueError: Recall or review failed, or decisions are incomplete.
        """
        if not records:
            return []
        scope = resolve_memory_recall_scope(self.plugin)
        limit = max(1, min(10, 50 // len(records)))
        metadata = "CASE WHEN json_valid(metadata) THEN metadata ELSE '{}' END"
        associated = (
            f"CASE WHEN json_type({metadata}, '$.associated_user_ids')='array' "
            f"THEN json_extract({metadata}, '$.associated_user_ids') ELSE '[]' END"
        )
        candidates = []
        for index, record in enumerate(records, 1):
            users = record["associated_user_ids"]
            placeholders = ",".join("?" for _ in users)
            predicate = "bot_name=?"
            params = [bot_name]
            if scope == "session":
                predicate += " AND group_or_user_id=?"
                params.append(session_id)
            predicate += (
                f" AND EXISTS (SELECT 1 FROM json_each({associated}) "
                f"WHERE type IN ('text', 'integer') AND CAST(value AS TEXT) IN ({placeholders}))"
                f" AND COALESCE(json_extract({metadata}, '$.memory_type'), 'fact')='fact'"
            )
            params.extend(users)
            # SQLite also covers disabled embeddings and records awaiting indexing.
            async with self.plugin.db.query(
                "SELECT memory_id, group_or_user_id, text, metadata FROM memories "
                f"WHERE {predicate} ORDER BY (text=?) DESC, "
                f"(json_extract({metadata}, '$.topic')=?) DESC, id DESC LIMIT ?",
                (*params, record["text"], record["topic"], limit),
            ) as cursor:
                related = {
                    row["memory_id"]: dict(row) for row in await cursor.fetchall()
                }
            if self.plugin.embedding_conf.get("enabled", False):
                try:
                    recalled = await self.search_and_filter_memories(
                        bot_name,
                        session_id,
                        record["text"],
                        limit=min(5, limit),
                        threshold=self.plugin.embedding_conf.get("threshold", 0.7),
                        user_ids=users,
                        raise_errors=True,
                        memory_type="fact",
                    )
                except Exception as error:
                    raise ValueError("Memory novelty recall failed") from error
                related.update((item["memory_id"], item) for item in recalled)
            snapshots = []
            for item in [record, *related.values()]:
                saved = memory_metadata(item) if "memory_id" in item else item
                snapshot = {
                    "associated_user_ids": saved.get("associated_user_ids", []),
                    "text": item["text"],
                    "occurred_at": saved.get("occurred_at", ""),
                }
                if "memory_id" in item:
                    snapshot["memory_id"] = item["memory_id"]
                    snapshot["source_session_id"] = item["group_or_user_id"]
                else:
                    snapshot["topic"] = item["topic"]
                snapshots.append(snapshot)
            candidates.append(
                {
                    "candidate_id": index,
                    "new_memory": snapshots[0],
                    "related_memories": snapshots[1:],
                }
            )
        if len(records) == 1 and not candidates[0]["related_memories"]:
            return records
        prompt = self._format_prompt_template(
            DEFAULT_MEMORY_NOVELTY_PROMPT, nickname, self_id
        )
        prompt += "\n\nJSON Schema:\n" + json.dumps(
            MemoryNoveltyReview.model_json_schema(), ensure_ascii=False
        )
        completion = await self._call_summary_llm(
            "记忆新颖性整理",
            prompt,
            json.dumps({"candidates": candidates}, ensure_ascii=False),
            bot_name=bot_name,
            group_or_user_id=session_id,
        )
        if not completion:
            raise ValueError("Memory novelty review returned no decisions")
        review = MemoryNoveltyReview.model_validate(parse_markdown_json(completion))
        decisions = {item.candidate_id: item for item in review.decisions}
        if len(decisions) != len(review.decisions) or set(decisions) != set(
            range(1, len(records) + 1)
        ):
            raise ValueError("Memory novelty review must cover every candidate once")
        kept = []
        for candidate, record in zip(candidates, records):
            decision = decisions[candidate["candidate_id"]]
            if decision.is_new:
                updated = dict(record)
                if decision.text is not None:
                    updated["text"] = decision.text
                if decision.topic is not None:
                    updated["topic"] = decision.topic
                kept.append(updated)
            else:
                if decision.text is not None or decision.topic is not None:
                    raise ValueError("Discarded candidates cannot contain edits")
                if not candidate["related_memories"] and not any(
                    set(record["associated_user_ids"])
                    & set(item["associated_user_ids"])
                    for item in kept
                ):
                    raise ValueError(
                        "Duplicate decision has no related fact to compare"
                    )
        return kept

    async def _sync_memory_vectors(self, bot_name: str, session_id: str) -> None:
        """Retry optional indexing without changing durable memory state.

        Args:
            bot_name: Owning bot configuration.
            session_id: Owning conversation.
        """
        if not self.plugin.embedding_conf.get("enabled", False):
            return
        lock = self.vector_locks.setdefault(f"{bot_name}:{session_id}", asyncio.Lock())
        async with lock:
            async with self.plugin.db.query(
                "SELECT * FROM memories WHERE bot_name=? AND group_or_user_id=? "
                "AND indexed_revision<revision ORDER BY id LIMIT 20",
                (bot_name, session_id),
            ) as cursor:
                rows = [dict(row) for row in await cursor.fetchall()]
            for row in rows:
                try:
                    removed = await self.plugin.ltm.delete_memory(row["memory_id"])
                    if not removed:
                        break
                    result = await self.plugin.ltm.add_memory(
                        bot_name=bot_name,
                        group_or_user_id=session_id,
                        text=row["text"],
                        time=row["created_at"],
                        metadata=json.dumps(memory_metadata(row), ensure_ascii=False),
                        memory_id=row["memory_id"],
                    )
                    if not result:
                        break
                    _, vector = result
                    async with self.plugin.db.transaction() as conn:
                        await conn.execute(
                            "UPDATE memories SET vector=?, indexed_revision=revision "
                            "WHERE memory_id=? AND bot_name=? AND group_or_user_id=? AND revision=?",
                            (
                                vector,
                                row["memory_id"],
                                bot_name,
                                session_id,
                                row["revision"],
                            ),
                        )
                except Exception as error:
                    logger.warning(
                        "[Giftia Memory] Deferred vector indexing for %s: %s",
                        row["memory_id"],
                        error,
                    )
                    break

    async def maintain_memories(self) -> None:
        """Close quiet reply windows, flush memories, and retry vector indexing.

        Returns:
            None. Failures remain retryable on the next scheduled tick.
        """
        if self._closed:
            return
        summarize_enabled = bool(
            self.plugin.passive_memory_enabled
            and self.plugin.passive_memory_provider_ids
        )
        if self.maintenance_lock.locked():
            return
        task = asyncio.current_task()
        self.maintenance_tasks.add(task)
        try:
            async with self.maintenance_lock:
                async with self.plugin.db.query(
                    "SELECT value FROM kv_store WHERE key LIKE 'atomic_memory:session:%'"
                ) as cursor:
                    routes = [json.loads(row[0]) for row in await cursor.fetchall()]
                scopes = {
                    (route["bot_name"], route["session_id"]): route.get("self_id", "")
                    for route in routes
                }
                async with self.plugin.db.query(
                    "SELECT DISTINCT bot_name, group_or_user_id FROM memories "
                    "WHERE revision>indexed_revision"
                ) as cursor:
                    for row in await cursor.fetchall():
                        scopes.setdefault((row[0], row[1]), "")
                for (bot_name, session_id), self_id in scopes.items():
                    if bot_name not in self.plugin.bot_map:
                        continue
                    key = f"{bot_name}:{session_id}"
                    lock = self.passive_memory_locks.setdefault(key, asyncio.Lock())
                    if lock.locked() or key in self.summary_tasks:
                        continue
                    try:
                        async with lock:
                            summarized = False
                            if summarize_enabled and self_id:
                                (
                                    first_id,
                                    last_id,
                                    pending,
                                ) = await self.plugin.db.chat_history_repo.get_pending_memory_stats(
                                    bot_name, session_id
                                )
                                recent = (
                                    await self.plugin.db.get_messages(
                                        session_id, bot_name, limit=1
                                    )
                                    if pending
                                    or self.plugin.active_reply_counters.get(key, 0) > 0
                                    else []
                                )
                                if (
                                    recent
                                    and datetime.now().timestamp()
                                    - datetime.fromisoformat(
                                        recent[-1].time.replace("Z", "+00:00")
                                    ).timestamp()
                                    >= 300
                                ):
                                    # Close the reply window before the model call so failures
                                    # do not preserve it and later messages can reactivate.
                                    self.plugin.active_reply_counters[key] = 0
                                    if pending:
                                        summarized = True
                                        await self._run_background_summarize(
                                            bot_name,
                                            session_id,
                                            self_id,
                                            first_id,
                                            last_id,
                                        )
                                if not summarized:
                                    nickname = self.plugin.bot_map[bot_name].get(
                                        "nickname", bot_name
                                    )
                                    await self._run_attitude_summary_task(
                                        bot_name, session_id, nickname, self_id
                                    )
                            await self._sync_memory_vectors(bot_name, session_id)
                    except Exception:
                        logger.exception(
                            "[Giftia Memory] Maintenance failed for %s", key
                        )
        finally:
            self.maintenance_tasks.discard(task)
