import json
from datetime import datetime, timedelta

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from ..utils.schemas import FORWARD_MEDIA_PATTERN, FORWARD_NESTED_PATTERN


def _safe_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _decode_forward(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _shorten(text: str, limit: int = 220) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


class ForwardApi:
    """Read-only APIs for merged forward message records."""

    @staticmethod
    def _status_condition(status: str | None) -> tuple[str | None, list]:
        if status == "summarized":
            return "is_summarized = 1", []
        if status == "unsummarized":
            return "COALESCE(is_summarized, 0) = 0", []
        if status == "unresolved":
            return "(content LIKE ? OR content LIKE ?)", [
                '%"unresolved": true%',
                '%"unresolved":true%',
            ]
        if status == "truncated":
            return "(content LIKE ? OR content LIKE ?)", [
                '%"truncated": true%',
                '%"truncated":true%',
            ]
        return None, []

    def _build_forward_where(
        self,
        *,
        bot_name: str | None = None,
        group_or_user_id: str | None = None,
        status: str | None = None,
        search: str | None = None,
    ) -> tuple[str, list]:
        conditions = []
        params = []

        if bot_name:
            conditions.append("bot_name = ?")
            params.append(bot_name)
        if group_or_user_id:
            conditions.append("group_or_user_id = ?")
            params.append(group_or_user_id)

        status_sql, status_params = self._status_condition(status)
        if status_sql:
            conditions.append(status_sql)
            params.extend(status_params)

        if search:
            conditions.append(
                """
                (
                    forward_id LIKE ?
                    OR source_id LIKE ?
                    OR owner_message_id LIKE ?
                    OR summary LIKE ?
                    OR content LIKE ?
                )
                """
            )
            like = f"%{search}%"
            params.extend([like, like, like, like, like])

        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)
        return where_clause, params

    @staticmethod
    def _node_items(nodes: list, *, limit: int | None = None) -> list[dict]:
        if not isinstance(nodes, list):
            return []
        selected_nodes = nodes[:limit] if limit is not None else nodes
        items = []
        for pos, node in enumerate(selected_nodes, start=1):
            if not isinstance(node, dict):
                continue
            content = str(node.get("content") or "")
            items.append(
                {
                    "index": node.get("index") or pos,
                    "sender_name": str(node.get("sender_name") or ""),
                    "sender_id": str(node.get("sender_id") or ""),
                    "time": str(node.get("time") or ""),
                    "content": content,
                    "preview": _shorten(content, 180),
                    "media_ids": sorted(set(FORWARD_MEDIA_PATTERN.findall(content))),
                    "nested_ids": sorted(set(FORWARD_NESTED_PATTERN.findall(content))),
                }
            )
        return items

    @staticmethod
    def _preview_from_nodes(nodes: list) -> str:
        if not isinstance(nodes, list):
            return ""
        previews = []
        for node in nodes[:3]:
            if not isinstance(node, dict):
                continue
            sender = str(node.get("sender_name") or node.get("sender_id") or "").strip()
            content = str(node.get("content") or "").strip()
            if not content:
                continue
            prefix = f"{sender}: " if sender else ""
            previews.append(prefix + content)
        return _shorten("\n".join(previews), 260)

    def _row_to_forward_item(self, row, *, include_nodes: bool = False) -> dict:
        forward = _decode_forward(row["content"])
        nodes = forward.get("nodes") if isinstance(forward.get("nodes"), list) else []
        sender_names = {
            str(node.get("sender_name") or node.get("sender_id") or "").strip()
            for node in nodes
            if isinstance(node, dict)
            and str(node.get("sender_name") or node.get("sender_id") or "").strip()
        }
        summary = str(row["summary"] or "").strip()
        preview = summary or self._preview_from_nodes(nodes) or "暂无内容"
        item = {
            "id": row["id"],
            "forward_id": row["forward_id"],
            "bot_name": row["bot_name"],
            "group_or_user_id": row["group_or_user_id"],
            "owner_message_id": row["owner_message_id"],
            "source": row["source"],
            "source_id": row["source_id"],
            "node_count": int(row["node_count"] or len(nodes) or 0),
            "media_count": int(row["media_count"] or 0),
            "nested_count": int(row["nested_count"] or 0),
            "sender_count": len(sender_names),
            "summary": summary,
            "preview": preview,
            "is_summarized": bool(row["is_summarized"]),
            "query_times": int(row["query_times"] or 0),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "flags": {
                "truncated": bool(forward.get("truncated")),
                "unresolved": bool(forward.get("unresolved")),
            },
        }
        if include_nodes:
            item["nodes"] = self._node_items(nodes)
        else:
            item["nodes_preview"] = self._node_items(nodes, limit=3)
        return item

    async def get_forwards(self):
        """Get merged forward records with pagination and filters."""
        try:
            page = _safe_int(request.query.get("page", 1), 1, 1, 1000000)
            limit = _safe_int(request.query.get("limit", 20), 20, 1, 100)
            bot_name = request.query.get("bot_name")
            group_or_user_id = request.query.get("group_or_user_id")
            status = request.query.get("status")
            search = request.query.get("search")

            offset = (page - 1) * limit
            where_clause, params = self._build_forward_where(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                status=status,
                search=search,
            )

            count_sql = (
                f"SELECT COUNT(*) as total FROM forwarded_message {where_clause}"
            )
            async with self.giftia.db.conn.execute(count_sql, params) as cursor:
                row = await cursor.fetchone()
                total = row["total"] if row else 0

            data_sql = f"""
                SELECT id, forward_id, bot_name, group_or_user_id, owner_message_id,
                       source, source_id, node_count, media_count, nested_count,
                       content, summary, is_summarized, query_times, created_at, updated_at
                FROM forwarded_message
                {where_clause}
                ORDER BY COALESCE(updated_at, created_at) DESC, id DESC
                LIMIT ? OFFSET ?
            """
            items = []
            async with self.giftia.db.conn.execute(
                data_sql, params + [limit, offset]
            ) as cursor:
                rows = await cursor.fetchall()
                items = [
                    self._row_to_forward_item(row, include_nodes=False) for row in rows
                ]

            return json_response(
                {
                    "status": "success",
                    "data": {
                        "items": items,
                        "total": total,
                        "page": page,
                        "limit": limit,
                    },
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] get_forwards error: {e}")
            return error_response(f"获取合并转发列表失败: {str(e)}")

    async def get_forward_detail(self):
        """Get one merged forward record with full node list."""
        try:
            bot_name = request.query.get("bot_name")
            group_or_user_id = request.query.get("group_or_user_id")
            forward_id = request.query.get("forward_id")

            if not bot_name or not group_or_user_id or not forward_id:
                return error_response(
                    "缺少 bot_name、group_or_user_id 或 forward_id 参数"
                )

            async with self.giftia.db.conn.execute(
                """
                SELECT id, forward_id, bot_name, group_or_user_id, owner_message_id,
                       source, source_id, node_count, media_count, nested_count,
                       content, summary, is_summarized, query_times, created_at, updated_at
                FROM forwarded_message
                WHERE bot_name = ? AND group_or_user_id = ? AND forward_id = ?
                LIMIT 1
                """,
                (bot_name, group_or_user_id, forward_id),
            ) as cursor:
                row = await cursor.fetchone()

            if not row:
                return error_response("合并转发记录不存在")

            return json_response(
                {
                    "status": "success",
                    "data": self._row_to_forward_item(row, include_nodes=True),
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] get_forward_detail error: {e}")
            return error_response(f"获取合并转发详情失败: {str(e)}")

    async def get_forward_filter_options(self):
        """Get bot/session filter options for merged forward records."""
        try:
            bot_name = request.query.get("bot_name")
            status = request.query.get("status")
            search = request.query.get("search")

            async with self.giftia.db.conn.execute(
                """
                SELECT DISTINCT bot_name
                FROM forwarded_message
                WHERE bot_name IS NOT NULL AND bot_name != ''
                ORDER BY bot_name ASC
                """
            ) as cursor:
                rows = await cursor.fetchall()
                bots = [row["bot_name"] for row in rows if row["bot_name"]]

            selected_bot_name = (
                bot_name if bot_name in bots else (bots[0] if bots else "")
            )

            sessions = []
            if selected_bot_name:
                where_clause, params = self._build_forward_where(
                    bot_name=selected_bot_name,
                    status=status,
                    search=search,
                )
                async with self.giftia.db.conn.execute(
                    f"""
                    SELECT group_or_user_id, COUNT(*) as total,
                           MAX(COALESCE(updated_at, created_at)) as latest_at
                    FROM forwarded_message
                    {where_clause}
                    GROUP BY group_or_user_id
                    ORDER BY latest_at DESC, group_or_user_id ASC
                    """,
                    params,
                ) as cursor:
                    rows = await cursor.fetchall()
                    sessions = [
                        {
                            "group_or_user_id": row["group_or_user_id"],
                            "total": row["total"],
                        }
                        for row in rows
                        if row["group_or_user_id"]
                    ]

            return json_response(
                {
                    "status": "success",
                    "data": {
                        "bots": bots,
                        "selected_bot_name": selected_bot_name,
                        "sessions": sessions,
                    },
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] get_forward_filter_options error: {e}")
            return error_response(f"获取合并转发筛选项失败: {str(e)}")

    async def clean_old_forwards(self):
        """Delete merged forward records older than 24 hours."""
        try:
            cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
            async with self.giftia.db.conn.execute(
                """
                SELECT COUNT(*) as total
                FROM forwarded_message
                WHERE COALESCE(created_at, updated_at) < ?
                """,
                (cutoff,),
            ) as cursor:
                row = await cursor.fetchone()
                deleted_count = int(row["total"] or 0) if row else 0

            await self.giftia.db.conn.execute(
                """
                DELETE FROM forwarded_message
                WHERE COALESCE(created_at, updated_at) < ?
                """,
                (cutoff,),
            )
            await self.giftia.db.conn.commit()

            return json_response(
                {
                    "status": "success",
                    "count": deleted_count,
                    "cutoff": cutoff,
                    "message": f"已清理 {deleted_count} 条超过 24 小时的合并转发记录",
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] clean_old_forwards error: {e}")
            return error_response(f"清理合并转发记录失败: {str(e)}")
