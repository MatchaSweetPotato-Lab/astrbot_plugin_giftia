from datetime import datetime

from ...utils.schemas import MemoryItem, normalize_memory_importance
from .base import BaseRepository


class MemoriesRepository(BaseRepository):
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
        """获取近期记忆，并按时间正序排列"""
        async with self.conn.execute(
            """
            SELECT * FROM (
                SELECT memory_id, text, vector, metadata, importance, hit_count, last_hit_at, created_at, updated_at
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
                metadata=row["metadata"],
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
