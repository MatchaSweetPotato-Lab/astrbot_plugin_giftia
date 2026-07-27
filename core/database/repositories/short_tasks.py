from datetime import datetime
import aiosqlite
from .base import BaseRepository
from ...utils.schemas import ShortTask

class ShortTasksRepository(BaseRepository):
    @staticmethod
    def _short_task_from_row(row) -> ShortTask:
        return ShortTask(
            task_id=row["task_id"],
            bot_name=row["bot_name"],
            group_or_user_id=row["group_or_user_id"],
            creator_user_id=row["creator_user_id"] or "",
            creator_nickname=row["creator_nickname"] or "",
            content=row["content"] or "",
            status=row["status"] or "active",
            closed_by_user_id=row["closed_by_user_id"] or "",
            close_reason=row["close_reason"] or "",
            expires_at=row["expires_at"] or "",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )

    async def insert_short_task(self, task: ShortTask) -> None:
        await self.conn.execute(
            """
            INSERT INTO short_tasks (
                task_id, bot_name, group_or_user_id, creator_user_id,
                creator_nickname, content, status, closed_by_user_id,
                close_reason, expires_at, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task.task_id,
                task.bot_name,
                task.group_or_user_id,
                task.creator_user_id,
                task.creator_nickname,
                task.content,
                task.status,
                task.closed_by_user_id,
                task.close_reason,
                task.expires_at,
                task.created_at,
                task.updated_at,
            ),
        )
        await self.conn.commit()

    async def expire_short_tasks(
        self, bot_name: str | None = None, group_or_user_id: str | None = None
    ) -> int:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conditions = [
            "status = 'active'",
            "expires_at IS NOT NULL",
            "expires_at != ''",
            "expires_at <= ?",
        ]
        condition_params = [now]
        if bot_name:
            conditions.append("bot_name = ?")
            condition_params.append(bot_name)
        if group_or_user_id:
            conditions.append("group_or_user_id = ?")
            condition_params.append(group_or_user_id)

        cursor = await self.conn.execute(
            f"""
            UPDATE short_tasks
            SET status = 'expired',
                closed_by_user_id = 'system',
                close_reason = '任务已过期',
                updated_at = ?
            WHERE {" AND ".join(conditions)}
            """,
            [now] + condition_params,
        )
        await self.conn.commit()
        return cursor.rowcount or 0

    async def get_short_tasks(
        self,
        bot_name: str,
        group_or_user_id: str,
        statuses: list[str] | None = None,
        limit: int | None = None,
    ) -> list[ShortTask]:
        conditions = ["bot_name = ?", "group_or_user_id = ?"]
        params: list = [bot_name, group_or_user_id]
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            conditions.append(f"status IN ({placeholders})")
            params.extend(statuses)

        limit_sql = ""
        if limit is not None:
            limit_sql = "LIMIT ?"
            params.append(limit)

        async with self.conn.execute(
            f"""
            SELECT task_id, bot_name, group_or_user_id, creator_user_id,
                   creator_nickname, content, status, closed_by_user_id,
                   close_reason, expires_at, created_at, updated_at
            FROM short_tasks
            WHERE {" AND ".join(conditions)}
            ORDER BY
                CASE status
                    WHEN 'active' THEN 0
                    WHEN 'completed' THEN 1
                    WHEN 'canceled' THEN 2
                    WHEN 'expired' THEN 3
                    ELSE 4
                END,
                created_at ASC
            {limit_sql}
            """,
            params,
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._short_task_from_row(row) for row in rows]

    async def get_short_task(
        self, task_id: str, bot_name: str, group_or_user_id: str
    ) -> ShortTask | None:
        async with self.conn.execute(
            """
            SELECT task_id, bot_name, group_or_user_id, creator_user_id,
                   creator_nickname, content, status, closed_by_user_id,
                   close_reason, expires_at, created_at, updated_at
            FROM short_tasks
            WHERE task_id = ? AND bot_name = ? AND group_or_user_id = ?
            LIMIT 1
            """,
            (task_id, bot_name, group_or_user_id),
        ) as cursor:
            row = await cursor.fetchone()
        return self._short_task_from_row(row) if row else None

    async def count_active_short_tasks(
        self, bot_name: str, group_or_user_id: str
    ) -> int:
        async with self.conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM short_tasks
            WHERE bot_name = ? AND group_or_user_id = ? AND status = 'active'
            """,
            (bot_name, group_or_user_id),
        ) as cursor:
            row = await cursor.fetchone()
        return row["total"] if row else 0

    async def update_short_task_status(
        self,
        task_id: str,
        bot_name: str,
        group_or_user_id: str,
        status: str,
        closed_by_user_id: str = "",
        close_reason: str = "",
    ) -> bool:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self.conn.execute(
            """
            UPDATE short_tasks
            SET status = ?,
                closed_by_user_id = ?,
                close_reason = ?,
                updated_at = ?
            WHERE task_id = ? AND bot_name = ? AND group_or_user_id = ?
              AND status = 'active'
            """,
            (
                status,
                closed_by_user_id,
                close_reason,
                now,
                task_id,
                bot_name,
                group_or_user_id,
            ),
        )
        await self.conn.commit()
        return (cursor.rowcount or 0) > 0

    async def update_short_task(
        self,
        task_id: str,
        bot_name: str,
        group_or_user_id: str,
        content: str,
        status: str,
        expires_at: str,
        closed_by_user_id: str = "",
        close_reason: str = "",
    ) -> bool:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self.conn.execute(
            """
            UPDATE short_tasks
            SET content = ?,
                status = ?,
                expires_at = ?,
                closed_by_user_id = ?,
                close_reason = ?,
                updated_at = ?
            WHERE task_id = ? AND bot_name = ? AND group_or_user_id = ?
            """,
            (
                content,
                status,
                expires_at,
                closed_by_user_id,
                close_reason,
                now,
                task_id,
                bot_name,
                group_or_user_id,
            ),
        )
        await self.conn.commit()
        return (cursor.rowcount or 0) > 0

    async def delete_short_task(
        self, task_id: str, bot_name: str, group_or_user_id: str
    ) -> bool:
        cursor = await self.conn.execute(
            """
            DELETE FROM short_tasks
            WHERE task_id = ? AND bot_name = ? AND group_or_user_id = ?
            """,
            (task_id, bot_name, group_or_user_id),
        )
        await self.conn.commit()
        return (cursor.rowcount or 0) > 0

    async def clear_short_tasks(
        self,
        bot_name: str,
        group_or_user_id: str,
        statuses: list[str] | None = None,
    ) -> int:
        conditions = ["bot_name = ?", "group_or_user_id = ?"]
        params: list = [bot_name, group_or_user_id]
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            conditions.append(f"status IN ({placeholders})")
            params.extend(statuses)

        cursor = await self.conn.execute(
            f"""
            DELETE FROM short_tasks
            WHERE {" AND ".join(conditions)}
            """,
            params,
        )
        await self.conn.commit()
        return cursor.rowcount or 0

    async def get_short_task_stats(
        self, bot_name: str, group_or_user_id: str
    ) -> dict:
        await self.expire_short_tasks(bot_name, group_or_user_id)
        stats = {
            "active": 0,
            "completed": 0,
            "canceled": 0,
            "expired": 0,
            "total": 0,
            "latest_updated_at": "",
        }
        async with self.conn.execute(
            """
            SELECT status, COUNT(*) AS total, MAX(updated_at) AS latest_updated_at
            FROM short_tasks
            WHERE bot_name = ? AND group_or_user_id = ?
            GROUP BY status
            """,
            (bot_name, group_or_user_id),
        ) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            status = row["status"] or ""
            total = row["total"] or 0
            if status in stats:
                stats[status] = total
            stats["total"] += total
            latest = row["latest_updated_at"] or ""
            if latest and latest > stats["latest_updated_at"]:
                stats["latest_updated_at"] = latest
        return stats
