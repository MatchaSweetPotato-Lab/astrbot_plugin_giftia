"""Independent extraction, new-memory-triggered consolidation, and indexing."""

import asyncio
import hashlib
import json
from datetime import datetime
from xml.etree.ElementTree import Element, SubElement, indent, tostring

from astrbot.api import logger

from ..conversation.memory_recall import (
    conf_float,
    conf_int,
    resolve_memory_search_limit,
)
from ..llm.json_parse import parse_markdown_json
from ..llm.preset_prompts import DEFAULT_MEMORY_CONSOLIDATION_PROMPT
from .atomic import DirectSpeech, MemoryChanges, memory_input, validate_memory_changes


class MemoryMaintenanceMixin:
    def _memory_user_prompt(
        self,
        bot_name,
        session_id,
        self_id,
        messages,
        media,
        existing,
        consolidating=False,
        new_memories=None,
        related_memory_ids=None,
    ):
        """Render bounded model inputs with explicit provenance boundaries.

        Args:
            bot_name: Bot configuration owning the records.
            session_id: Conversation owning the records.
            self_id: Observing bot account.
            messages: New message batch with database IDs.
            media: Previously completed captions.
            existing: Scoped memory snapshot with revisions.
            consolidating: Whether this is a queued consolidation call.
            new_memories: Newly extracted records with a historical match.
            related_memory_ids: Historical matches keyed by new memory ID.

        Returns:
            Escaped XML separating source messages from prior memories.
        """
        root = Element(
            "memory_input",
            {
                "bot_name": bot_name,
                "session_id": session_id,
                "self_id": self_id,
                "task": "consolidation" if consolidating else "extraction",
                "now": datetime.now().astimezone().isoformat(),
            },
        )
        for tag, records in (
            ("new_memories", new_memories or []),
            ("existing_memories", existing),
        ):
            memories = SubElement(
                root, tag, {"new_batch": str(tag == "new_memories").lower()}
            )
            for row in records:
                item = memory_input(row)
                item["evidence"] = [
                    {**ref, "quote": str(ref.get("quote") or "")[:160]}
                    for ref in item.get("evidence", [])[-6:]
                ]
                item["source_memory_ids"] = item.get("source_memory_ids", [])[-10:]
                attrs = {}
                if tag == "new_memories":
                    attrs["related_memory_ids"] = json.dumps(
                        (related_memory_ids or {}).get(row["memory_id"], [])
                    )
                SubElement(memories, "memory", attrs).text = json.dumps(
                    item, ensure_ascii=False
                )
        captions = SubElement(root, "available_media")
        for media_id, caption in media.items():
            SubElement(captions, "media", {"id": media_id}).text = json.dumps(
                caption, ensure_ascii=False
            )
        history = SubElement(root, "new_messages")
        for msg in messages:
            speech = DirectSpeech(msg.content or "")
            direct = "".join(speech.parts)
            allowed = (
                not msg.is_recalled
                and msg.role != "operation_log"
                and not direct.lstrip().startswith("/")
            )
            SubElement(
                history,
                "message",
                {
                    "source_id": str(msg.db_id),
                    "message_id": msg.message_id,
                    "user_id": str(msg.user_id),
                    "nickname": msg.nickname or "",
                    "time": msg.time or "",
                    "evidence_allowed": str(allowed).lower(),
                    "sender_is_bot": str(str(msg.user_id) == self_id).lower(),
                    "mentioned_user_ids": json.dumps(speech.mentioned_user_ids),
                    "reply_user_ids": json.dumps(speech.reply_user_ids),
                    "media_ids": json.dumps(msg.media_id_list or []),
                },
            ).text = direct
        indent(root, space="  ")
        return tostring(root, encoding="unicode")

    async def _recall_maintenance_memories(
        self,
        bot_name: str,
        session_id: str,
        queries: list[str],
        exclude_ids: set[str] | None = None,
        history_end: int | None = None,
    ) -> tuple[list[dict], list[list[str]]]:
        """Recall each source independently and share the input budget fairly.

        Args:
            bot_name: Owning bot configuration.
            session_id: Owning conversation.
            queries: Text of each newly extracted record.
            exclude_ids: All records created in the extraction batch.
            history_end: Last historical row before the extraction batch.

        Returns:
            Deduplicated authoritative rows and retained IDs for every query.
        """
        embedding = self.plugin.embedding_conf
        search_limit = max(1, min(20, resolve_memory_search_limit(embedding)))
        if history_end == 0:
            return [], [[] for _ in queries]
        excluded = set(exclude_ids or ())
        if embedding.get("enabled", False) and history_end is not None:
            async with self.plugin.db.conn.execute(
                "SELECT memory_id FROM memories WHERE bot_name=? AND group_or_user_id=? AND id>?",
                (bot_name, session_id, history_end),
            ) as cursor:
                excluded.update(row[0] for row in await cursor.fetchall())
        groups = []
        for query in queries:
            if not query.strip():
                groups.append([])
                continue
            candidates = []
            if embedding.get("enabled", False):
                candidates = await self.plugin.ltm.search_memory(
                    bot_name=bot_name,
                    group_or_user_id=session_id,
                    query=query,
                    limit=search_limit,
                    threshold=conf_float(embedding, "threshold", 0.7),
                    exclude_ids=sorted(excluded),
                    raise_errors=True,
                )
                candidates = [row for row in candidates if row["id"] not in excluded]
                rerank = getattr(self.plugin, "rerank_conf", {})
                if candidates and rerank.get("enabled", False):
                    candidates = await self.plugin.ltm.rerank_memories(
                        query=query,
                        memories=candidates,
                        top_k=max(1, conf_int(rerank, "top_k", search_limit)),
                        threshold=conf_float(rerank, "threshold", 0.45),
                        raise_errors=True,
                    )
            matches = await self.plugin.db.memory_maintenance_repo.related_snapshot(
                bot_name,
                session_id,
                query,
                [row["id"] for row in candidates],
                limit=search_limit,
                exclude_ids=excluded,
                history_end=history_end,
                text_fallback=not embedding.get("enabled", False),
            )
            if embedding.get("enabled", False) and not matches:
                async with self.plugin.db.conn.execute(
                    "SELECT 1 FROM memories WHERE bot_name=? AND group_or_user_id=? "
                    "AND status='active' AND indexed_revision<revision "
                    "AND (expires_at IS NULL OR julianday(expires_at)>julianday('now')) "
                    "AND (? IS NULL OR id<=?) LIMIT 1",
                    (bot_name, session_id, history_end, history_end),
                ) as cursor:
                    if await cursor.fetchone():
                        raise RuntimeError("Historical memory index is incomplete")
            groups.append(matches)
        # Every query gets its first hit before any query gets a second slot.
        # At most 200 new records are selected for one consolidation attempt.
        selected = {}
        for rank in range(search_limit):
            for group in groups:
                if rank < len(group) and len(selected) < 200:
                    row = group[rank]
                    selected.setdefault(row["memory_id"], row)
        return list(selected.values()), [
            [row["memory_id"] for row in group if row["memory_id"] in selected]
            for group in groups
        ]

    async def _run_memory_summary_task(
        self, bot_name, group_or_user_id, self_id, nickname, context
    ) -> bool:
        """Persist only new, directly evidenced records without historical recall.

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
        context["new_memory_ids"] = []
        identity = json.dumps(
            [(msg.db_id, msg.message_id, msg.time) for msg in messages]
        )
        batch_key = f"atomic_memory:batch:{bot_name}:{group_or_user_id}:{hashlib.sha256(identity.encode()).hexdigest()}"
        if await self.plugin.db.get_kv_data(batch_key, 0):
            return True
        repo = self.plugin.db.memory_maintenance_repo
        media = context.get("memory_media", {})
        prompt = self._format_prompt_template(
            self._get_memory_summary_prompt(), nickname, self_id
        )
        prompt += "\n\nJSON Schema:\n" + json.dumps(
            MemoryChanges.model_json_schema(), ensure_ascii=False
        )
        completion = await self._call_summary_llm(
            "原子记忆归纳",
            prompt,
            self._memory_user_prompt(
                bot_name,
                group_or_user_id,
                self_id,
                messages,
                media,
                [],
            ),
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
        )
        if not completion:
            return False
        try:
            parsed = MemoryChanges.model_validate(parse_markdown_json(completion))
            changes = validate_memory_changes(parsed, [], messages, media, self_id)
            affected = await repo.apply_changes(
                bot_name,
                group_or_user_id,
                changes,
                {},
                batch_key=batch_key,
                source_end=messages[-1].db_id,
                observer_id=str(self_id),
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
        context["new_memory_ids"] = affected
        logger.info(
            "[Giftia Memory] Saved %s atomic memory changes for %s:%s",
            len(changes),
            bot_name,
            group_or_user_id,
        )
        return True

    async def consolidate_memories(
        self, bot_name: str, session_id: str, self_id: str = ""
    ) -> bool:
        """Consume one queued extraction batch, reviewing only historical matches.

        Args:
            bot_name: Owning bot configuration.
            session_id: Owning conversation.
            self_id: Observing account, overridden by the durable job identity.

        Returns:
            Whether job progress was committed, including an unmatched batch.
        """
        repo = self.plugin.db.memory_maintenance_repo
        job = await repo.pending_consolidation(bot_name, session_id)
        if job is None:
            return False
        batch = job["payload"]
        self_id = str(batch.get("self_id") or self_id)
        if batch["history_end"]:
            await self._sync_memory_vectors(bot_name, session_id)
        config = self.plugin.conf.get("memory_config", {})
        limit = max(1, min(200, int(config.get("memory_consolidation_batch_size", 60))))
        pending = set(batch["pending_ids"][:limit])
        placeholders = ",".join("?" for _ in pending)
        async with self.plugin.db.conn.execute(
            "SELECT * FROM memories WHERE bot_name=? AND group_or_user_id=? "
            f"AND memory_id IN ({placeholders}) ORDER BY id",
            (bot_name, session_id, *sorted(pending)),
        ) as cursor:
            selected = [dict(row) for row in await cursor.fetchall()]
        active = [row for row in selected if row["status"] == "active"]
        try:
            related, related_ids = await self._recall_maintenance_memories(
                bot_name,
                session_id,
                [row["text"] for row in active],
                exclude_ids=set(batch["memory_ids"]),
                history_end=batch["history_end"],
            )
        except Exception as error:
            logger.warning(
                "[Giftia Memory] Recall failed for %s:%s; job retained: %s",
                bot_name,
                session_id,
                error,
            )
            return False
        matched = [row for row, ids in zip(active, related_ids) if ids]
        existing = [*matched, *related]
        changes = []
        if matched:
            nickname = self.plugin.bot_map.get(bot_name, {}).get("nickname", bot_name)
            prompt = (
                self._format_prompt_template(
                    DEFAULT_MEMORY_CONSOLIDATION_PROMPT, nickname, self_id
                )
                + "\n\nJSON Schema:\n"
                + json.dumps(MemoryChanges.model_json_schema(), ensure_ascii=False)
            )
            completion = await self._call_summary_llm(
                "关联记忆整理",
                prompt,
                self._memory_user_prompt(
                    bot_name,
                    session_id,
                    self_id,
                    [],
                    {},
                    related,
                    True,
                    new_memories=matched,
                    related_memory_ids={
                        row["memory_id"]: ids
                        for row, ids in zip(active, related_ids)
                        if ids
                    },
                ),
                bot_name=bot_name,
                group_or_user_id=session_id,
            )
            if not completion:
                return False
            try:
                parsed = MemoryChanges.model_validate(parse_markdown_json(completion))
                changes = validate_memory_changes(
                    parsed,
                    existing,
                    [],
                    {},
                    self_id,
                    consolidating=True,
                    new_memory_ids={row["memory_id"] for row in matched},
                )
            except (ValueError, TypeError) as error:
                logger.warning(
                    "[Giftia Memory] Rejected consolidation for %s:%s: %s",
                    bot_name,
                    session_id,
                    error,
                )
                return False
        try:
            affected = await repo.apply_changes(
                bot_name,
                session_id,
                changes,
                {row["memory_id"]: row["revision"] for row in [*selected, *related]},
                reviewed=True,
                reviewed_ids=pending,
                consolidation_job=job,
                processed_ids=pending,
            )
        except (ValueError, TypeError) as error:
            logger.warning(
                "[Giftia Memory] Consolidation commit failed for %s:%s: %s",
                bot_name,
                session_id,
                error,
            )
            return False
        self.plugin.data_cache.memories.pop(f"{bot_name}:{session_id}", None)
        for memory_id in affected:
            self.plugin.data_cache.remove_session_recalled_memory(memory_id)
        await self._sync_memory_vectors(bot_name, session_id)
        logger.info(
            "[Giftia Memory] Checked %s new memories for %s:%s; %s had historical matches",
            len(pending),
            bot_name,
            session_id,
            len(matched),
        )
        return True

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
            async with self.plugin.db.conn.execute(
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
                    vector = None
                    if row["status"] == "active":
                        result = await self.plugin.ltm.add_memory(
                            bot_name=bot_name,
                            group_or_user_id=session_id,
                            text=row["text"],
                            time=row["created_at"],
                            metadata=row["metadata"] or "{}",
                            memory_id=row["memory_id"],
                        )
                        if not result:
                            break
                        _, vector = result
                    await self.plugin.db.conn.execute(
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
                    await self.plugin.db.conn.commit()
                except Exception as error:
                    logger.warning(
                        "[Giftia Memory] Deferred vector indexing for %s: %s",
                        row["memory_id"],
                        error,
                    )
                    break

    async def maintain_memories(self) -> None:
        """Flush quiet sessions, retry queued additions, and maintain indexes.

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
                async with self.plugin.db.conn.execute(
                    "SELECT value FROM kv_store WHERE key LIKE 'atomic_memory:session:%' "
                    "OR key LIKE 'atomic_memory:consolidation:%'"
                ) as cursor:
                    routes = [json.loads(row[0]) for row in await cursor.fetchall()]
                scopes = {
                    (route["bot_name"], route["session_id"]): route.get("self_id", "")
                    for route in routes
                }
                async with self.plugin.db.conn.execute(
                    "SELECT DISTINCT bot_name, group_or_user_id FROM memories "
                    "WHERE revision>indexed_revision"
                ) as cursor:
                    for row in await cursor.fetchall():
                        scopes.setdefault((row[0], row[1]), "")
                for (bot_name, session_id), self_id in scopes.items():
                    if bot_name not in self.plugin.bot_map:
                        continue
                    key = f"{bot_name}:{session_id}"
                    if (
                        summarize_enabled
                        and await self.plugin.db.memory_maintenance_repo.pending_consolidation(
                            bot_name, session_id
                        )
                    ):
                        self._schedule_memory_consolidation(
                            bot_name, session_id, self_id
                        )
                    lock = self.passive_memory_locks.setdefault(key, asyncio.Lock())
                    if lock.locked() or key in self.summary_tasks:
                        continue
                    try:
                        async with lock:
                            if (
                                summarize_enabled
                                and self_id
                                and await self.plugin.db.get_kv_data(
                                    f"passive_memory:silence_armed:{key}", 0
                                )
                            ):
                                recent = await self.plugin.db.get_messages(
                                    session_id, bot_name, limit=1
                                )
                                if (
                                    recent
                                    and datetime.now().timestamp()
                                    - datetime.fromisoformat(
                                        recent[-1].time.replace("Z", "+00:00")
                                    ).timestamp()
                                    >= 300
                                ):
                                    await self._run_background_summarize(
                                        bot_name,
                                        session_id,
                                        self_id,
                                        int(
                                            await self.plugin.db.get_kv_data(
                                                f"passive_memory:last_summarized_id:{key}",
                                                0,
                                            )
                                        )
                                        + 1,
                                        await self.plugin.db.get_max_message_id(
                                            bot_name, session_id
                                        ),
                                    )
                            expired = await self.plugin.db.memory_maintenance_repo.expire_agreements(
                                bot_name, session_id
                            )
                            if expired:
                                self.plugin.data_cache.memories.pop(key, None)
                                self.plugin.data_cache.session_recalled_memories.pop(
                                    key, None
                                )
                            await self._sync_memory_vectors(bot_name, session_id)
                    except Exception:
                        logger.exception(
                            "[Giftia Memory] Maintenance failed for %s", key
                        )
        finally:
            self.maintenance_tasks.discard(task)
