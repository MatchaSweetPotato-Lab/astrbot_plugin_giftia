"""Incremental fact extraction and optional vector indexing."""

import asyncio
import hashlib
import json
from datetime import datetime
from xml.etree.ElementTree import Element, SubElement, indent, tostring

from astrbot.api import logger

from ..llm.json_parse import parse_markdown_json
from .atomic import (
    DirectSpeech,
    MemoryRecords,
    memory_metadata,
    validate_memory_records,
)


class MemoryMaintenanceMixin:
    def _memory_user_prompt(
        self,
        bot_name,
        session_id,
        self_id,
        messages,
        media,
        active_users_text: str = "",
    ):
        """Render bounded model inputs with explicit provenance boundaries.

        Args:
            bot_name: Bot configuration owning the records.
            session_id: Conversation owning the records.
            self_id: Observing bot account.
            messages: New message batch with database IDs.
            media: Previously completed captions.
            active_users_text: Scoped account, nickname, and preferred-name mapping.

        Returns:
            Escaped participant names, source messages, and available media captions.
        """
        root = Element(
            "memory_input",
            {
                "bot_name": bot_name,
                "session_id": session_id,
                "self_id": self_id,
                "task": "extraction",
                "now": datetime.now().astimezone().isoformat(),
            },
        )
        SubElement(root, "active_users").text = active_users_text or "无"
        captions = SubElement(root, "available_media")
        rendered_media = set()
        history = SubElement(root, "new_messages")
        for msg in messages:
            speech = DirectSpeech(msg.content or "")
            direct = "".join(speech.parts)
            media_ids = [
                media_id
                for media_id in dict.fromkeys(msg.media_id_list or [])
                if media_id in speech.media_ids
            ]
            for media_id in media_ids:
                if media_id in media and media_id not in rendered_media:
                    SubElement(captions, "media", {"id": media_id}).text = json.dumps(
                        media[media_id], ensure_ascii=False
                    )
                    rendered_media.add(media_id)
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
                    "media_ids": json.dumps(media_ids),
                },
            ).text = direct
        indent(root, space="  ")
        return tostring(root, encoding="unicode")

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
        identity = json.dumps(
            [(msg.db_id, msg.message_id, msg.time) for msg in messages]
        )
        batch_key = f"atomic_memory:batch:{bot_name}:{group_or_user_id}:{hashlib.sha256(identity.encode()).hexdigest()}"
        if await self.plugin.db.get_kv_data(batch_key, 0):
            return True
        repo = self.plugin.db.memories_repo
        media = context.get("memory_media", {})
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
                bot_name,
                group_or_user_id,
                self_id,
                messages,
                media,
                active_users_text=context.get("active_users_text", ""),
            ),
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
        )
        if not completion:
            return False
        try:
            parsed = MemoryRecords.model_validate(parse_markdown_json(completion))
            records = validate_memory_records(parsed, messages, media, self_id)
            affected = await repo.insert_memories(
                bot_name,
                group_or_user_id,
                records,
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
            "[Giftia Memory] Saved %s factual memories for %s:%s",
            len(affected),
            bot_name,
            group_or_user_id,
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
        """Flush quiet sessions and retry optional vector indexing.

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
                            await self._sync_memory_vectors(bot_name, session_id)
                    except Exception:
                        logger.exception(
                            "[Giftia Memory] Maintenance failed for %s", key
                        )
        finally:
            self.maintenance_tasks.discard(task)
