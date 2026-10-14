"""Incremental extraction, periodic consolidation, and optional vector indexing."""

import asyncio
import hashlib
import json
from datetime import datetime
from xml.etree.ElementTree import Element, SubElement, indent, tostring

from astrbot.api import logger

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
    ):
        """Render bounded model inputs with explicit provenance boundaries.

        Args:
            bot_name: Bot configuration owning the records.
            session_id: Conversation owning the records.
            self_id: Observing bot account.
            messages: New message batch with database IDs.
            media: Previously completed captions.
            existing: Scoped memory snapshot with revisions.
            consolidating: Whether this is a periodic consolidation call.

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
        memories = SubElement(root, "existing_memories", {"new_evidence": "false"})
        for row in existing:
            item = memory_input(row)
            item["evidence"] = [
                {**ref, "quote": str(ref.get("quote") or "")[:160]}
                for ref in item.get("evidence", [])[-6:]
            ]
            item["source_memory_ids"] = item.get("source_memory_ids", [])[-10:]
            SubElement(memories, "memory").text = json.dumps(item, ensure_ascii=False)
        captions = SubElement(root, "available_media")
        for media_id, caption in media.items():
            SubElement(captions, "media", {"id": media_id}).text = json.dumps(
                caption, ensure_ascii=False
            )
        history = SubElement(root, "new_messages")
        for msg in messages:
            allowed = not msg.is_recalled and msg.role != "operation_log"
            speech = DirectSpeech(msg.content or "")
            direct = "".join(speech.parts)
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

    async def _run_memory_summary_task(
        self, bot_name, group_or_user_id, self_id, nickname, context
    ) -> bool:
        """Extract atoms independently of embedding availability.

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
        repo = self.plugin.db.memory_maintenance_repo
        await repo.expire_agreements(bot_name, group_or_user_id)
        existing = await repo.snapshot(bot_name, group_or_user_id, limit=60)
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
                bot_name, group_or_user_id, self_id, messages, media, existing
            ),
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
        )
        if not completion:
            return False
        try:
            parsed = MemoryChanges.model_validate(parse_markdown_json(completion))
            changes = validate_memory_changes(
                parsed, existing, messages, media, self_id
            )
            affected = await repo.apply_changes(
                bot_name,
                group_or_user_id,
                changes,
                {row["memory_id"]: row["revision"] for row in existing},
                batch_key=batch_key,
                source_end=messages[-1].db_id,
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
            "[Giftia Memory] Saved %s atomic memory changes for %s:%s",
            len(changes),
            bot_name,
            group_or_user_id,
        )
        return True

    async def consolidate_memories(
        self, bot_name: str, session_id: str, self_id: str = "", force: bool = False
    ) -> bool:
        """Review old atoms once per revision and derive supported habits.

        Args:
            bot_name: Owning bot configuration.
            session_id: Owning conversation.
            self_id: Observing bot account, if previously observed.
            force: Bypass the configured interval for a manual run.

        Returns:
            Whether the selected batch was successfully consolidated.
        """
        config = self.plugin.conf.get("memory_config", {})
        interval = (
            max(0, float(config.get("memory_consolidation_interval_hours", 24))) * 3600
        )
        last_key = f"atomic_memory:consolidated_at:{bot_name}:{session_id}"
        last_run = float(await self.plugin.db.get_kv_data(last_key, 0) or 0)
        if not force and (
            not interval or datetime.now().timestamp() - last_run < interval
        ):
            return False
        repo = self.plugin.db.memory_maintenance_repo
        await repo.expire_agreements(bot_name, session_id)
        limit = max(
            10, min(200, int(config.get("memory_consolidation_batch_size", 60)))
        )
        existing = await repo.snapshot(bot_name, session_id, limit=limit, pending=True)
        if not existing:
            return False
        # Stable facts help the model merge a new observation into an existing
        # habit instead of creating a fresh summary on every maintenance run.
        pending = {row["memory_id"] for row in existing}
        related = await repo.snapshot(
            bot_name, session_id, limit=20, reviewed_only=True
        )
        existing.extend(row for row in related if row["memory_id"] not in pending)
        prompt = (
            DEFAULT_MEMORY_CONSOLIDATION_PROMPT
            + "\n\nJSON Schema:\n"
            + json.dumps(MemoryChanges.model_json_schema(), ensure_ascii=False)
        )
        completion = await self._call_summary_llm(
            "记忆定期整理",
            prompt,
            self._memory_user_prompt(
                bot_name, session_id, self_id, [], {}, existing, True
            ),
            bot_name=bot_name,
            group_or_user_id=session_id,
        )
        if not completion:
            return False
        try:
            parsed = MemoryChanges.model_validate(parse_markdown_json(completion))
            changes = validate_memory_changes(
                parsed, existing, [], {}, self_id, consolidating=True
            )
            affected = await repo.apply_changes(
                bot_name,
                session_id,
                changes,
                {row["memory_id"]: row["revision"] for row in existing},
                reviewed=True,
            )
        except (ValueError, TypeError) as error:
            logger.warning(
                "[Giftia Memory] Rejected consolidation for %s:%s: %s",
                bot_name,
                session_id,
                error,
            )
            return False
        self.plugin.data_cache.memories.pop(f"{bot_name}:{session_id}", None)
        for memory_id in affected:
            self.plugin.data_cache.remove_session_recalled_memory(memory_id)
        if not await repo.snapshot(bot_name, session_id, limit=1, pending=True):
            await self.plugin.db.upsert_kv_data(last_key, datetime.now().timestamp())
        await self._sync_memory_vectors(bot_name, session_id)
        logger.info(
            "[Giftia Memory] Consolidated %s pending atoms for %s:%s",
            len(pending),
            bot_name,
            session_id,
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
        """Flush quiet sessions, consolidate due records, and retry indexing.

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
                    "SELECT value FROM kv_store WHERE key LIKE 'atomic_memory:session:%'"
                ) as cursor:
                    routes = [json.loads(row[0]) for row in await cursor.fetchall()]
                scopes = {
                    (route["bot_name"], route["session_id"]): route.get("self_id", "")
                    for route in routes
                }
                async with self.plugin.db.conn.execute(
                    "SELECT DISTINCT bot_name, group_or_user_id FROM memories "
                    "WHERE revision>reviewed_revision OR revision>indexed_revision"
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
                            if summarize_enabled:
                                await self.consolidate_memories(
                                    bot_name, session_id, self_id
                                )
                            await self._sync_memory_vectors(bot_name, session_id)
                    except Exception:
                        logger.exception(
                            "[Giftia Memory] Maintenance failed for %s", key
                        )
        finally:
            self.maintenance_tasks.discard(task)
