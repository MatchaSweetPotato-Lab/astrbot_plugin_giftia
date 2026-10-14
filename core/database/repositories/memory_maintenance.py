"""Atomic memory writes, optimistic concurrency, and maintenance snapshots."""

import asyncio
import json
import uuid
from datetime import datetime, timezone

import aiosqlite

from .base import BaseRepository


class MemoryMaintenanceRepository(BaseRepository):
    def __init__(self, conn):
        super().__init__(conn)
        self.write_lock = asyncio.Lock()

    async def snapshot(
        self,
        bot_name: str,
        session_id: str,
        limit: int = 100,
        pending: bool = False,
        reviewed_only: bool = False,
    ) -> list[dict]:
        """Read a bounded, scoped snapshot for a model maintenance call.

        Args:
            bot_name: Owning bot configuration.
            session_id: Owning group or private session.
            limit: Maximum number of atoms.
            pending: Prefer oldest records not reviewed at their current revision.
            reviewed_only: Read previously reviewed context without consuming backlog.

        Returns:
            Authoritative SQLite rows with revisions and provenance.
        """
        condition = "AND revision > reviewed_revision" if pending else ""
        if reviewed_only:
            condition += " AND reviewed_revision >= revision"
        order = (
            "id ASC"
            if pending
            else "CASE kind WHEN 'agreement' THEN 0 WHEN 'preference' THEN 1 ELSE 2 END, id DESC"
        )
        async with self.conn.execute(
            f"SELECT * FROM memories WHERE bot_name=? AND group_or_user_id=? "
            f"AND status='active' {condition} ORDER BY {order} LIMIT ?",
            (bot_name, session_id, limit),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def apply_changes(
        self,
        bot_name: str,
        session_id: str,
        changes: list[dict],
        expected: dict[str, int],
        batch_key: str = "",
        reviewed: bool = False,
        source_end: int | None = None,
    ) -> list[str]:
        """Commit a validated batch and its checkpoint in one transaction.

        Args:
            bot_name: Authoritative bot partition.
            session_id: Authoritative conversation partition.
            changes: Validated atoms and their provenance.
            expected: Revisions of the exact model input snapshot.
            batch_key: Idempotency key for an incremental source interval.
            reviewed: Mark input revisions and derived outputs as consolidated.
            source_end: Last source message handled by an incremental batch.

        Returns:
            IDs of inserted, updated, or superseded atoms.

        Raises:
            ValueError: A memory changed or disappeared during model inference.
        """
        async with self.write_lock:
            async with self.conn.execute("PRAGMA database_list") as cursor:
                database_file = (await cursor.fetchone())[2]
            # Other repositories commit on the shared connection. A separate file
            # connection prevents those commits from splitting this transaction.
            writer = (
                await aiosqlite.connect(database_file) if database_file else self.conn
            )
            writer.row_factory = aiosqlite.Row
            try:
                await writer.execute("BEGIN IMMEDIATE")
                if batch_key:
                    async with writer.execute(
                        "SELECT 1 FROM kv_store WHERE key=?", (batch_key,)
                    ) as cursor:
                        if await cursor.fetchone():
                            await writer.rollback()
                            return []
                for memory_id, revision in expected.items():
                    async with writer.execute(
                        "SELECT revision FROM memories WHERE memory_id=? AND bot_name=? AND group_or_user_id=?",
                        (memory_id, bot_name, session_id),
                    ) as cursor:
                        row = await cursor.fetchone()
                    if row is None or row["revision"] != revision:
                        raise ValueError("Memory snapshot changed during maintenance")
                if source_end is not None:
                    async with writer.execute(
                        "SELECT 1 FROM chat_history WHERE id=? AND bot_name=? AND group_or_user_id=?",
                        (source_end, bot_name, session_id),
                    ) as cursor:
                        if not await cursor.fetchone():
                            raise ValueError(
                                "Source interval was removed during inference"
                            )
                now = datetime.now(timezone.utc).isoformat()
                affected = []
                for item in changes:
                    references = [*item["source_memory_ids"], *item["supersedes"]]
                    if item["memory_id"]:
                        references.append(item["memory_id"])
                    if any(ref not in expected for ref in references):
                        raise ValueError(
                            "Memory writes must reference the scoped snapshot"
                        )
                    for source_id in item.get("new_source_ids", []):
                        async with writer.execute(
                            "SELECT 1 FROM chat_history WHERE id=? AND bot_name=? AND group_or_user_id=? "
                            "AND COALESCE(is_recalled, 0)=0 AND COALESCE(role, 'message')!='operation_log'",
                            (source_id, bot_name, session_id),
                        ) as cursor:
                            if not await cursor.fetchone():
                                raise ValueError(
                                    "A source message disappeared or was recalled"
                                )
                    memory_id = item["memory_id"] or str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            json.dumps([bot_name, session_id, item["fingerprint"]]),
                        )
                    )
                    async with writer.execute(
                        "SELECT metadata, revision FROM memories WHERE memory_id=? AND bot_name=? AND group_or_user_id=?",
                        (memory_id, bot_name, session_id),
                    ) as cursor:
                        old = await cursor.fetchone()
                    metadata = {
                        key: value
                        for key, value in item.items()
                        if key
                        not in {
                            "memory_id",
                            "text",
                            "importance",
                            "fingerprint",
                            "supersedes",
                            "kind",
                            "status",
                            "expires_at",
                            "new_source_ids",
                        }
                    }
                    metadata.update(
                        schema_version=2, user_id=item["associated_user_ids"][0]
                    )
                    metadata.update(
                        kind=item["kind"],
                        status=item["status"],
                        expires_at=item["expires_at"],
                    )
                    if old:
                        try:
                            previous = json.loads(old["metadata"] or "{}")
                        except (ValueError, TypeError):
                            previous = {}
                        if isinstance(previous, dict):
                            metadata = {**previous, **metadata}
                    revision = old["revision"] + 1 if old else 1
                    await writer.execute(
                        "INSERT INTO memories (bot_name, group_or_user_id, memory_id, text, metadata, importance, "
                        "kind, status, expires_at, revision, reviewed_revision, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(memory_id) DO UPDATE SET text=excluded.text, metadata=excluded.metadata, "
                        "importance=excluded.importance, kind=excluded.kind, status=excluded.status, expires_at=excluded.expires_at, "
                        "revision=excluded.revision, reviewed_revision=excluded.reviewed_revision, vector=NULL, updated_at=excluded.updated_at",
                        (
                            bot_name,
                            session_id,
                            memory_id,
                            item["text"],
                            json.dumps(metadata, ensure_ascii=False),
                            item["importance"],
                            item["kind"],
                            item["status"],
                            item["expires_at"] or None,
                            revision,
                            revision if reviewed else 0,
                            now,
                            now,
                        ),
                    )
                    affected.append(memory_id)
                    for old_id in item["supersedes"]:
                        await writer.execute(
                            "UPDATE memories SET status='superseded', revision=revision+1, updated_at=? "
                            "WHERE memory_id=? AND bot_name=? AND group_or_user_id=? AND status='active'",
                            (now, old_id, bot_name, session_id),
                        )
                        affected.append(old_id)
                if reviewed:
                    for memory_id, revision in expected.items():
                        await writer.execute(
                            "UPDATE memories SET reviewed_revision=revision WHERE memory_id=? "
                            "AND bot_name=? AND group_or_user_id=? AND revision=?",
                            (memory_id, bot_name, session_id, revision),
                        )
                if batch_key:
                    await writer.execute(
                        "INSERT INTO kv_store (key, value, created_at, updated_at) VALUES (?, 1, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=1, updated_at=excluded.updated_at",
                        (batch_key, now, now),
                    )
                if source_end is not None:
                    await writer.execute(
                        "INSERT INTO kv_store (key, value, created_at, updated_at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=MAX(CAST(kv_store.value AS INTEGER), excluded.value), "
                        "updated_at=excluded.updated_at",
                        (
                            f"passive_memory:last_summarized_id:{bot_name}:{session_id}",
                            source_end,
                            now,
                            now,
                        ),
                    )
                await writer.commit()
                return affected
            except BaseException:
                await writer.rollback()
                raise
            finally:
                if database_file:
                    await writer.close()

    async def expire_agreements(self, bot_name: str, session_id: str) -> int:
        """Expire elapsed agreements without claiming they were fulfilled.

        Args:
            bot_name: Owning bot.
            session_id: Owning conversation.

        Returns:
            Number of agreements whose validity ended.
        """
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self.conn.execute(
            "UPDATE memories SET status='expired', revision=revision+1, updated_at=? "
            "WHERE bot_name=? AND group_or_user_id=? AND kind='agreement' AND status='active' "
            "AND expires_at IS NOT NULL AND julianday(expires_at)<=julianday(?)",
            (now, bot_name, session_id, now),
        )
        await self.conn.commit()
        return cursor.rowcount
