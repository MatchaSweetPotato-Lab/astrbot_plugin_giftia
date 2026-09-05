import json
from datetime import datetime
import aiosqlite
from .base import BaseRepository
from ...utils.schemas import (
    MessageData,
    FORWARD_MEDIA_PATTERN,
    FORWARD_NESTED_PATTERN,
)

def _decode_json_list(raw) -> list:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _decode_json_dict(raw) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _forward_stats(forward: dict) -> tuple[int, int, int]:
    nodes = forward.get("nodes") if isinstance(forward.get("nodes"), list) else []
    media_ids = set()
    nested_ids = set()
    for node in nodes:
        if not isinstance(node, dict):
            continue
        content = str(node.get("content") or "")
        media_ids.update(FORWARD_MEDIA_PATTERN.findall(content))
        nested_ids.update(FORWARD_NESTED_PATTERN.findall(content))
        raw_media_ids = node.get("media_ids")
        if isinstance(raw_media_ids, list):
            media_ids.update(str(media_id) for media_id in raw_media_ids if media_id)
    return len(nodes), len(media_ids), len(nested_ids)


def _row_has(row: aiosqlite.Row, key: str) -> bool:
    return key in row.keys()


def _row_to_message(row: aiosqlite.Row) -> MessageData:
    return MessageData(
        db_id=row["id"] if _row_has(row, "id") and row["id"] is not None else 0,
        nickname=row["nickname"],
        user_id=row["user_id"],
        group_or_user_id=(
            row["group_or_user_id"] if _row_has(row, "group_or_user_id") else ""
        ),
        time=row["created_at"],
        message_id=row["message_id"],
        content=row["content"] or "",
        is_recalled=row["is_recalled"],
        media_id_list=_decode_json_list(row["media_ids"]),
        forward_messages=[],
        role=row["role"] if _row_has(row, "role") and row["role"] else "message",
    )


class ChatHistoryRepository(BaseRepository):
    async def _upsert_forward_messages(self, bot_name: str, message: MessageData):
        if not message.forward_messages:
            return
        updated_at = datetime.now().isoformat()
        created_at = message.time or updated_at
        for forward in message.forward_messages:
            if not isinstance(forward, dict):
                continue
            forward_id = str(forward.get("id") or "").strip()
            if not forward_id:
                continue
            node_count, media_count, nested_count = _forward_stats(forward)
            await self.conn.execute(
                """
                INSERT INTO forwarded_message (
                    forward_id, bot_name, group_or_user_id, owner_message_id,
                    source, source_id, node_count, media_count, nested_count,
                    content, summary, is_summarized, query_times, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, 0, ?, ?)
                ON CONFLICT(forward_id, bot_name, group_or_user_id) DO UPDATE SET
                    owner_message_id = excluded.owner_message_id,
                    source = excluded.source,
                    source_id = excluded.source_id,
                    node_count = excluded.node_count,
                    media_count = excluded.media_count,
                    nested_count = excluded.nested_count,
                    content = excluded.content,
                    updated_at = excluded.updated_at
                """,
                (
                    forward_id,
                    bot_name,
                    message.group_or_user_id,
                    message.message_id,
                    str(forward.get("source") or ""),
                    str(forward.get("source_id") or ""),
                    node_count,
                    media_count,
                    nested_count,
                    json.dumps(forward, ensure_ascii=False),
                    created_at,
                    updated_at,
                ),
            )

    async def _attach_forward_messages(
        self, bot_name: str, group_or_user_id: str, messages: list[MessageData]
    ) -> list[MessageData]:
        message_ids = [msg.message_id for msg in messages if msg.message_id]
        if not message_ids:
            return messages
        placeholders = ",".join("?" for _ in message_ids)
        async with self.conn.execute(
            f"""
            SELECT owner_message_id, content
            FROM forwarded_message
            WHERE bot_name = ?
              AND group_or_user_id = ?
              AND owner_message_id IN ({placeholders})
            ORDER BY id ASC
            """,
            (bot_name, group_or_user_id, *message_ids),
        ) as cursor:
            rows = await cursor.fetchall()

        forward_map: dict[str, list[dict]] = {}
        for row in rows:
            forward = _decode_json_dict(row["content"])
            if forward:
                forward_map.setdefault(row["owner_message_id"], []).append(forward)
        for msg in messages:
            msg.forward_messages = forward_map.get(msg.message_id, [])
        return messages

    async def insert_message(
        self,
        bot_name: str,
        message: MessageData,
    ):
        await self.conn.execute(
            """
            INSERT OR IGNORE INTO chat_history (
                group_or_user_id, nickname, user_id, message_id, content,
                reply_decision, use_rag, is_recalled, bot_name, media_ids,
                role, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.group_or_user_id,
                message.nickname,
                message.user_id,
                message.message_id,
                message.content,
                2,  # 2: 未决策，3: @消息直接回复，1: 决策通过，0: 决策拒绝
                2,
                0,
                bot_name,
                json.dumps(message.media_id_list),
                message.role,
                message.time,
                message.time,
            ),
        )
        await self._upsert_forward_messages(bot_name, message)
        await self.conn.commit()

    async def get_messages(
        self, group_or_user_id: str, bot_name: str, limit: int = 100
    ) -> list[MessageData]:
        async with self.conn.execute(
            """
            SELECT * FROM (
                SELECT nickname, user_id, message_id, content, media_ids, is_recalled, role, created_at
                FROM chat_history
                WHERE group_or_user_id = ? AND bot_name = ?
                ORDER BY created_at DESC
                LIMIT ?
            )
            ORDER BY created_at ASC
            """,
            (group_or_user_id, bot_name, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        messages = [_row_to_message(row) for row in rows]
        return await self._attach_forward_messages(bot_name, group_or_user_id, messages)

    async def get_max_message_id(self, bot_name: str, group_or_user_id: str) -> int:
        """获取指定会话在 chat_history 表中的最大 id"""
        async with self.conn.execute(
            """
            SELECT MAX(id) as max_id FROM chat_history
            WHERE group_or_user_id = ? AND bot_name = ?
            """,
            (group_or_user_id, bot_name),
        ) as cursor:
            row = await cursor.fetchone()
        return row["max_id"] if row and row["max_id"] is not None else 0

    async def get_messages_by_id_range(
        self, bot_name: str, group_or_user_id: str, start_id: int, end_id: int
    ) -> list[MessageData]:
        """获取指定 id 范围内的历史消息"""
        async with self.conn.execute(
            """
                SELECT nickname, user_id, message_id, content, media_ids, is_recalled, role, created_at
                FROM chat_history
                WHERE group_or_user_id = ? AND bot_name = ? AND id >= ? AND id <= ?
                ORDER BY id ASC
            """,
            (group_or_user_id, bot_name, start_id, end_id),
        ) as cursor:
            rows = await cursor.fetchall()
        messages = [_row_to_message(row) for row in rows]
        return await self._attach_forward_messages(bot_name, group_or_user_id, messages)

    async def get_user_messages_after_id(
        self,
        bot_name: str,
        group_or_user_id: str,
        user_id: str,
        after_id: int,
        limit: int = 200,
    ) -> list[MessageData]:
        """获取某个用户在指定数据库 id 之后的最近消息，按时间正序返回。"""
        if limit <= 0:
            return []
        async with self.conn.execute(
            """
            SELECT * FROM (
                SELECT id, nickname, user_id, message_id, content, media_ids, is_recalled, role, created_at
                FROM chat_history
                WHERE group_or_user_id = ?
                  AND bot_name = ?
                  AND user_id = ?
                  AND id > ?
                  AND COALESCE(role, 'message') != 'operation_log'
                  AND COALESCE(is_recalled, 0) = 0
                ORDER BY id DESC
                LIMIT ?
            )
            ORDER BY id ASC
            """,
            (group_or_user_id, bot_name, user_id, after_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        messages = [_row_to_message(row) for row in rows]
        return await self._attach_forward_messages(bot_name, group_or_user_id, messages)

    async def get_message_count_by_id_range(
        self, bot_name: str, group_or_user_id: str, start_id: int, end_id: int
    ) -> int:
        """获取指定 id 范围内的消息数量"""
        async with self.conn.execute(
            """
            SELECT COUNT(*) as count FROM chat_history
            WHERE group_or_user_id = ? AND bot_name = ? AND id > ? AND id <= ?
            """,
            (group_or_user_id, bot_name, start_id, end_id),
        ) as cursor:
            row = await cursor.fetchone()
        return row["count"] if row else 0

    async def get_boundary_message_id(
        self, bot_name: str, group_or_user_id: str, offset: int
    ) -> int:
        """获取从最新消息往回偏移 offset 处的条目的 id（即上下文窗口边界 of id）"""
        if offset <= 0:
            offset = 1
        async with self.conn.execute(
            """
            SELECT id FROM chat_history
            WHERE group_or_user_id = ? AND bot_name = ?
            ORDER BY id DESC
            LIMIT 1 OFFSET ?
            """,
            (group_or_user_id, bot_name, offset - 1),
        ) as cursor:
            row = await cursor.fetchone()
        return row["id"] if row else 0

    async def get_message_by_id(
        self, message_id: str, group_or_user_id: str, bot_name: str
    ) -> MessageData | None:
        """通过消息ID获取消息"""
        async with self.conn.execute(
            """
            SELECT nickname, user_id, message_id, content, media_ids, is_recalled, role, created_at
            FROM chat_history
            WHERE message_id = ? AND group_or_user_id = ? AND bot_name = ?
            LIMIT 1
            """,
            (message_id, group_or_user_id, bot_name),
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            messages = await self._attach_forward_messages(
                bot_name, group_or_user_id, [_row_to_message(row)]
            )
            return messages[0]
        return None

    async def get_database_id_by_message_id(
        self, message_id: str, group_or_user_id: str, bot_name: str
    ) -> int | None:
        """获取指定 message_id 在 chat_history 表中的自增 id"""
        async with self.conn.execute(
            """
            SELECT id FROM chat_history
            WHERE message_id = ? AND group_or_user_id = ? AND bot_name = ?
            LIMIT 1
            """,
            (message_id, group_or_user_id, bot_name),
        ) as cursor:
            row = await cursor.fetchone()
        return row["id"] if row else None

    async def search_messages(
        self,
        group_or_user_id: str,
        bot_name: str,
        user_id: str | None = None,
        keyword: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        sort_order: str = "desc",
        limit: int = 100,
    ) -> list[MessageData]:
        """搜索历史消息，支持关键字、指定用户以及时间范围搜索"""
        query = """
            SELECT nickname, user_id, message_id, content, media_ids, is_recalled, role, created_at
            FROM chat_history
            WHERE group_or_user_id = ? AND bot_name = ?
        """
        params = [group_or_user_id, bot_name]

        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)

        if keyword:
            query += " AND content LIKE ?"
            params.append(f"%{keyword}%")

        if start_time is not None:
            query += " AND created_at >= ?"
            params.append(start_time)

        if end_time is not None:
            query += " AND created_at <= ?"
            params.append(end_time)

        if sort_order.lower() == "asc":
            query += " ORDER BY created_at ASC"
        else:
            query += " ORDER BY created_at DESC"

        query += " LIMIT ?"
        params.append(str(limit))

        async with self.conn.execute(query, tuple(params)) as cursor:
            rows = await cursor.fetchall()

        if sort_order.lower() == "desc":
            rows = list(reversed(list(rows)))

        messages = [_row_to_message(row) for row in rows]
        return await self._attach_forward_messages(bot_name, group_or_user_id, messages)

    async def get_message_context(
        self, message_id: str, group_or_user_id: str, bot_name: str, limit: int = 30
    ) -> list[MessageData]:
        """获取特定消息前后的上下文消息"""
        target_msg = await self.get_message_by_id(
            message_id, group_or_user_id, bot_name
        )
        if not target_msg:
            return []

        target_time = target_msg.time

        query_before = """
            SELECT nickname, user_id, message_id, content, media_ids, is_recalled, role, created_at
            FROM chat_history
            WHERE group_or_user_id = ? AND bot_name = ? AND created_at < ?
            ORDER BY created_at DESC
            LIMIT ?
        """
        async with self.conn.execute(
            query_before, (group_or_user_id, bot_name, target_time, limit)
        ) as cursor:
            rows_before = await cursor.fetchall()

        query_after = """
            SELECT nickname, user_id, message_id, content, media_ids, is_recalled, role, created_at
            FROM chat_history
            WHERE group_or_user_id = ? AND bot_name = ? AND created_at > ?
            ORDER BY created_at ASC
            LIMIT ?
        """
        async with self.conn.execute(
            query_after, (group_or_user_id, bot_name, target_time, limit)
        ) as cursor:
            rows_after = await cursor.fetchall()

        rows_before_list = list(rows_before)
        all_rows = rows_before_list[::-1] + list(rows_after)

        messages = [_row_to_message(row) for row in all_rows]
        messages.insert(len(rows_before_list), target_msg)

        return await self._attach_forward_messages(bot_name, group_or_user_id, messages)

    async def delete_chat_history(self, bot_name: str, group_or_user_id: str):
        await self.conn.execute(
            """
            DELETE FROM forwarded_message WHERE group_or_user_id = ? AND bot_name = ?
            """,
            (group_or_user_id, bot_name),
        )
        await self.conn.execute(
            """
            DELETE FROM chat_history WHERE group_or_user_id = ? AND bot_name = ?
            """,
            (group_or_user_id, bot_name),
        )
        await self.conn.commit()

    async def update_message_decision(
        self,
        bot_name: str,
        group_or_user_id: str,
        message_id: str,
        reply_decision: int,
        use_rag: int,
    ):
        await self.conn.execute(
            """
            UPDATE chat_history
            SET reply_decision = ?, use_rag = ?
            WHERE message_id = ? AND group_or_user_id = ? AND bot_name = ?
            """,
            (reply_decision, use_rag, message_id, group_or_user_id, bot_name),
        )
        await self.conn.commit()

    async def update_message_reply_decision(
        self,
        bot_name: str,
        group_or_user_id: str,
        message_id: str,
        reply_decision: int,
    ):
        await self.conn.execute(
            """
            UPDATE chat_history
            SET reply_decision = ?
            WHERE message_id = ? AND group_or_user_id = ? AND bot_name = ?
            """,
            (reply_decision, message_id, group_or_user_id, bot_name),
        )
        await self.conn.commit()

    async def update_message_recall(
        self,
        bot_name: str,
        group_or_user_id: str,
        message_ids: list[str],
        is_recalled: int,
    ):
        await self.conn.execute(
            """
            UPDATE chat_history
            SET is_recalled = ?
            WHERE message_id IN ({}) AND group_or_user_id = ? AND bot_name = ?
            """.format(",".join(["?"] * len(message_ids))),
            (is_recalled, *message_ids, group_or_user_id, bot_name),
        )
        await self.conn.commit()

    async def delete_message(
        self,
        bot_name: str,
        group_or_user_id: str,
        message_id: str,
    ) -> bool:
        """根据平台 message_id 精确删除单条聊天记录并级联清理 forwarded_message。

        Returns:
            bool: 是否成功删除了消息记录
        """
        del_chat = await self.conn.execute(
            """
            DELETE FROM chat_history
            WHERE message_id = ? AND group_or_user_id = ? AND bot_name = ?
            """,
            (message_id, group_or_user_id, bot_name),
        )
        deleted = del_chat.rowcount > 0
        if deleted:
            await self.conn.execute(
                """
                DELETE FROM forwarded_message
                WHERE owner_message_id = ? AND group_or_user_id = ? AND bot_name = ?
                """,
                (message_id, group_or_user_id, bot_name),
            )
        await self.conn.commit()
        return deleted

    async def delete_message_by_db_id(self, message_db_id: int) -> dict | None:
        """根据数据库主键 id 删除单条聊天记录，并级联清理关联的 forwarded_message"""
        async with self.conn.execute(
            """
            SELECT id, bot_name, group_or_user_id, message_id, content
            FROM chat_history
            WHERE id = ?
            LIMIT 1
            """,
            (message_db_id,),
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            return None

        bot_name = row["bot_name"]
        group_or_user_id = row["group_or_user_id"]
        message_id = row["message_id"]

        if message_id:
            await self.conn.execute(
                """
                DELETE FROM forwarded_message
                WHERE owner_message_id = ? AND group_or_user_id = ? AND bot_name = ?
                """,
                (message_id, group_or_user_id, bot_name),
            )

        await self.conn.execute(
            "DELETE FROM chat_history WHERE id = ?",
            (message_db_id,),
        )
        await self.conn.commit()

        return {
            "id": message_db_id,
            "bot_name": bot_name,
            "group_or_user_id": group_or_user_id,
            "message_id": message_id,
        }

    async def auto_clean_chat_history(
        self,
        max_count_per_session: int = 1000,
        max_age_days: int = 30,
        min_keep_per_session: int = 20,
    ) -> int:
        """
        按会话（Option A 并集删除）自动清理聊天记录：
        - 某条消息如果不属于最新的 min_keep_per_session 条保底记录，
          且满足（超出 max_count_per_session 条数 OR 早于 max_age_days 天前）时触发清理。
        """
        if max_count_per_session <= 0 and max_age_days <= 0:
            return 0

        clean_keep = max(1, int(min_keep_per_session or 20))
        clean_count = max(0, int(max_count_per_session or 0))
        clean_days = max(0, int(max_age_days or 0))

        # 获取所有包含聊天记录的会话
        async with self.conn.execute(
            """
            SELECT DISTINCT bot_name, group_or_user_id
            FROM chat_history
            """
        ) as cursor:
            sessions = await cursor.fetchall()

        total_deleted = 0
        for session in sessions:
            bot_name = session["bot_name"]
            group_or_user_id = session["group_or_user_id"]

            conds = []
            params = [bot_name, group_or_user_id, bot_name, group_or_user_id, clean_keep]

            if clean_count > 0:
                conds.append(
                    """
                    id NOT IN (
                        SELECT id FROM chat_history
                        WHERE bot_name = ? AND group_or_user_id = ?
                        ORDER BY created_at DESC, id DESC
                        LIMIT ?
                    )
                    """
                )
                params.extend([bot_name, group_or_user_id, clean_count])

            if clean_days > 0:
                conds.append(
                    f"created_at < datetime('now', '-{clean_days} days')"
                )

            if not conds:
                continue

            where_or = " OR ".join(conds)
            sql = f"""
                DELETE FROM chat_history
                WHERE bot_name = ? AND group_or_user_id = ?
                  AND id NOT IN (
                      SELECT id FROM chat_history
                      WHERE bot_name = ? AND group_or_user_id = ?
                      ORDER BY created_at DESC, id DESC
                      LIMIT ?
                  )
                  AND ({where_or})
            """

            async with self.conn.execute(sql, params) as cursor:
                total_deleted += cursor.rowcount

        await self.conn.commit()
        return total_deleted


