import asyncio
import json
import uuid
from datetime import datetime, timezone

import aiosqlite

from ...memory.atomic import memory_metadata
from ...utils.schemas import MemoryItem, normalize_memory_importance
from .base import BaseRepository


class MemoriesRepository(BaseRepository):
    def __init__(self, conn):
        super().__init__(conn)
        self.write_lock = asyncio.Lock()

    async def insert_memory(
        self, bot_name: str, group_or_user_id: str, memory: MemoryItem
    ):
        """写入记忆"""
        update_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await self.conn.execute(
            """
            INSERT INTO memories (
                bot_name, group_or_user_id, memory_id, text, vector, metadata,
                importance, hit_count, last_hit_at, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                bot_name,
                group_or_user_id,
                memory.memory_id,
                memory.text,
                memory.vector,
                memory.metadata,
                normalize_memory_importance(memory.importance),
                int(memory.hit_count or 0),
                memory.last_hit_at or None,
                update_time,
                update_time,
            ),
        )
        await self.conn.commit()

    async def get_memories(
        self, group_or_user_id: str, bot_name: str, limit: int = 10
    ) -> list[MemoryItem]:
        """Read recent factual memories in chronological order.

        Args:
            group_or_user_id: Owning conversation.
            bot_name: Owning bot configuration.
            limit: Maximum number of recent memory records.

        Returns:
            Memory records with their saved evidence.
        """
        async with self.conn.execute(
            """
            SELECT * FROM (
                SELECT memory_id, text, vector, metadata, importance, hit_count, last_hit_at,
                       created_at, updated_at
                FROM memories
                WHERE group_or_user_id = ? AND bot_name = ?
                ORDER BY created_at DESC
                LIMIT ?
            )
            ORDER BY created_at ASC
            """,
            (group_or_user_id, bot_name, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            MemoryItem(
                memory_id=row["memory_id"],
                text=row["text"],
                vector=row["vector"],
                metadata=json.dumps(memory_metadata(dict(row)), ensure_ascii=False),
                importance=normalize_memory_importance(row["importance"]),
                hit_count=int(row["hit_count"] or 0),
                last_hit_at=row["last_hit_at"] or "",
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    async def record_memory_hits(
        self, memory_ids: list[str], hit_at: str | None = None
    ):
        """记录长期记忆被有效召回的次数与最近命中时间。"""
        clean_ids = []
        seen = set()
        for memory_id in memory_ids or []:
            memory_id = str(memory_id or "").strip()
            if memory_id and memory_id not in seen:
                seen.add(memory_id)
                clean_ids.append(memory_id)

        if not clean_ids:
            return

        hit_at = hit_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        placeholders = ",".join("?" for _ in clean_ids)
        await self.conn.execute(
            f"""
            UPDATE memories
            SET hit_count = COALESCE(hit_count, 0) + 1,
                last_hit_at = ?
            WHERE memory_id IN ({placeholders})
            """,
            [hit_at, *clean_ids],
        )
        await self.conn.commit()

    async def delete_memory(self, memory_id: str):
        """删除记忆"""
        await self.conn.execute(
            """
            DELETE FROM memories WHERE memory_id = ?
            """,
            (memory_id,),
        )
        await self.conn.commit()

    async def delete_all_memories(self, bot_name: str, group_or_user_id: str):
        """删除全部记忆"""
        await self.conn.execute(
            """
            DELETE FROM memories WHERE group_or_user_id = ? AND bot_name = ?
            """,
            (group_or_user_id, bot_name),
        )
        await self.conn.commit()

    async def insert_memories(
        self,
        bot_name: str,
        session_id: str,
        records: list[dict],
        batch_key: str = "",
        source_end: int | None = None,
        source_ids: list[int] | None = None,
    ) -> list[str]:
        """Commit new facts, source progress, and replay protection together.

        Args:
            bot_name: Owning bot configuration.
            session_id: Owning conversation.
            records: Validated new records and their source snapshots.
            batch_key: Idempotency key for an incremental message batch.
            source_end: Last source message handled by that batch.
            source_ids: Exact observed messages completed atomically with the facts.

        Returns:
            IDs of newly inserted records.

        Raises:
            ValueError: Source messages disappeared or were recalled during inference.
        """
        async with self.write_lock:
            async with self.conn.execute("PRAGMA database_list") as cursor:
                database_file = (await cursor.fetchone())[2]
            # Isolate this transaction from other repositories' shared commits.
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
                if source_end is not None:
                    async with writer.execute(
                        "SELECT 1 FROM chat_history WHERE id=? AND bot_name=? AND group_or_user_id=?",
                        (source_end, bot_name, session_id),
                    ) as cursor:
                        if not await cursor.fetchone():
                            raise ValueError(
                                "Source interval was removed during inference"
                            )
                if source_ids:
                    placeholders = ",".join("?" for _ in source_ids)
                    async with writer.execute(
                        "SELECT COUNT(*) FROM chat_history WHERE bot_name=? "
                        "AND group_or_user_id=? AND memory_status='pending' "
                        f"AND id IN ({placeholders})",
                        (bot_name, session_id, *source_ids),
                    ) as cursor:
                        if (await cursor.fetchone())[0] != len(source_ids):
                            raise ValueError(
                                "Observed source messages changed during inference"
                            )
                now = datetime.now(timezone.utc).isoformat()
                inserted = []
                for item in records:
                    memory_id = str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            json.dumps([bot_name, session_id, item["fingerprint"]]),
                        )
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
                    metadata = {
                        key: value
                        for key, value in item.items()
                        if key
                        not in {"text", "importance", "fingerprint", "new_source_ids"}
                    }
                    metadata.update(
                        schema_version=2, user_id=item["associated_user_ids"][0]
                    )
                    cursor = await writer.execute(
                        "INSERT INTO memories (bot_name, group_or_user_id, memory_id, text, metadata, "
                        "importance, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(memory_id) DO NOTHING",
                        (
                            bot_name,
                            session_id,
                            memory_id,
                            item["text"],
                            json.dumps(metadata, ensure_ascii=False),
                            item["importance"],
                            now,
                            now,
                        ),
                    )
                    if cursor.rowcount:
                        inserted.append(memory_id)
                if batch_key:
                    await writer.execute(
                        "INSERT INTO kv_store (key, value, created_at, updated_at) VALUES (?, 1, ?, ?)",
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
                if source_ids:
                    await writer.execute(
                        "UPDATE chat_history SET memory_status='summarized' "
                        "WHERE bot_name=? AND group_or_user_id=? "
                        f"AND id IN ({placeholders})",
                        (bot_name, session_id, *source_ids),
                    )
                await writer.commit()
                return inserted
            except BaseException:
                await writer.rollback()
                raise
            finally:
                if database_file:
                    await writer.close()
