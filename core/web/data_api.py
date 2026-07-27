import json
import time
from datetime import datetime, timedelta

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from ..utils.schemas import normalize_memory_importance


USER_PROFILE_FIELD_KEYS = (
    "call_name",
    "personality",
    "interests",
    "attitude",
    "agreements",
    "extra",
)


class DataApi:
    """Data management APIs: chat history, memories, bot status, profiles."""

    def __init__(self, giftia):
        self.giftia = giftia

    def _invalidate_user_profile_record_cache(
        self, bot_name: str, group_or_user_id: str, user_id: str
    ) -> None:
        fmt_key = f"{bot_name}:{group_or_user_id}:{user_id}"
        self.giftia.data_cache.user_profile_records.pop(fmt_key, None)

    @staticmethod
    def _optional_int(value, default: int | None = None, min_value: int | None = None):
        if value is None or value == "":
            return default
        try:
            result = int(value)
        except (TypeError, ValueError):
            return default
        if min_value is not None:
            result = max(min_value, result)
        return result

    @staticmethod
    def _optional_bool(value, default: bool = False) -> bool:
        if value is None or value == "":
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
        return bool(value)

    @staticmethod
    def _safe_json_dict(raw) -> dict:
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _memory_row_to_dict(self, row) -> dict:
        return {
            "id": row["id"],
            "bot_name": row["bot_name"],
            "group_or_user_id": row["group_or_user_id"],
            "memory_id": row["memory_id"],
            "text": row["text"],
            "metadata": self._safe_json_dict(row["metadata"]),
            "importance": normalize_memory_importance(row["importance"]),
            "hit_count": int(row["hit_count"] or 0),
            "last_hit_at": row["last_hit_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _memory_clean_conditions(self, body: dict) -> tuple[list[str], list, dict]:
        bot_name = str(body.get("bot_name") or "").strip()
        group_or_user_id = str(body.get("group_or_user_id") or "").strip()
        associated_user_id = str(body.get("associated_user_id") or "").strip()
        search = str(body.get("search") or "").strip()

        max_importance = self._optional_int(
            body.get("max_importance"), default=3, min_value=1
        )
        if max_importance is not None:
            max_importance = min(10, max_importance)
        max_hit_count = self._optional_int(
            body.get("max_hit_count"), default=1, min_value=0
        )
        min_age_days = self._optional_int(
            body.get("min_age_days"), default=60, min_value=0
        )
        last_hit_before_days = self._optional_int(
            body.get("last_hit_before_days"), default=30, min_value=0
        )
        include_never_hit = self._optional_bool(
            body.get("include_never_hit"), default=True
        )

        conditions = ["bot_name = ?"]
        params = [bot_name]

        if group_or_user_id:
            conditions.append("group_or_user_id = ?")
            params.append(group_or_user_id)
        if associated_user_id:
            conditions.append("metadata LIKE ?")
            params.append(f"%{associated_user_id}%")
        if search:
            conditions.append("text LIKE ?")
            params.append(f"%{search}%")
        if max_importance is not None:
            conditions.append("COALESCE(importance, 5) <= ?")
            params.append(max_importance)
        if max_hit_count is not None:
            conditions.append("COALESCE(hit_count, 0) <= ?")
            params.append(max_hit_count)
        if min_age_days and min_age_days > 0:
            created_cutoff = (
                datetime.now() - timedelta(days=min_age_days)
            ).strftime("%Y-%m-%d %H:%M:%S")
            conditions.append("datetime(created_at) <= datetime(?)")
            params.append(created_cutoff)
        if last_hit_before_days and last_hit_before_days > 0:
            hit_cutoff = (
                datetime.now() - timedelta(days=last_hit_before_days)
            ).strftime("%Y-%m-%d %H:%M:%S")
            if include_never_hit:
                conditions.append(
                    "((last_hit_at IS NULL OR last_hit_at = '') OR datetime(last_hit_at) <= datetime(?))"
                )
            else:
                conditions.append(
                    "last_hit_at IS NOT NULL AND last_hit_at != '' AND datetime(last_hit_at) <= datetime(?)"
                )
            params.append(hit_cutoff)

        clean_rules = {
            "max_importance": max_importance,
            "max_hit_count": max_hit_count,
            "min_age_days": min_age_days,
            "last_hit_before_days": last_hit_before_days,
            "include_never_hit": include_never_hit,
        }
        return conditions, params, clean_rules

    @staticmethod
    def _memory_clean_reasons(item: dict, clean_rules: dict) -> list[str]:
        reasons = []
        max_importance = clean_rules.get("max_importance")
        max_hit_count = clean_rules.get("max_hit_count")
        min_age_days = clean_rules.get("min_age_days")
        last_hit_before_days = clean_rules.get("last_hit_before_days")

        if max_importance is not None:
            reasons.append(f"重要度 {item['importance']} <= {max_importance}")
        if max_hit_count is not None:
            reasons.append(f"命中 {item['hit_count']} 次 <= {max_hit_count} 次")
        if min_age_days and min_age_days > 0:
            reasons.append(f"创建超过 {min_age_days} 天")
        if last_hit_before_days and last_hit_before_days > 0:
            if item.get("last_hit_at"):
                reasons.append(f"最近命中超过 {last_hit_before_days} 天")
            else:
                reasons.append("从未命中")
        return reasons

    async def get_chat_history(self):
        """Get chat logs with pagination and filters."""
        try:
            page = int(request.query.get("page", 1))
            limit = int(request.query.get("limit", 20))
            bot_name = request.query.get("bot_name")
            group_or_user_id = request.query.get("group_or_user_id")
            user_id = request.query.get("user_id")
            reply_decision = request.query.get("reply_decision")
            use_rag = request.query.get("use_rag")
            search = request.query.get("search")

            offset = (page - 1) * limit
            conditions = []
            params = []

            if bot_name:
                conditions.append("bot_name = ?")
                params.append(bot_name)
            if group_or_user_id:
                conditions.append("group_or_user_id = ?")
                params.append(group_or_user_id)
            if user_id:
                conditions.append("user_id = ?")
                params.append(user_id)
            if reply_decision is not None and reply_decision != "":
                conditions.append("reply_decision = ?")
                params.append(int(reply_decision))
            if use_rag is not None and use_rag != "":
                conditions.append("use_rag = ?")
                params.append(int(use_rag))
            if search:
                conditions.append("content LIKE ?")
                params.append(f"%{search}%")

            where_clause = ""
            if conditions:
                where_clause = "WHERE " + " AND ".join(conditions)

            # Query count
            count_sql = f"SELECT COUNT(*) as total FROM chat_history {where_clause}"
            async with self.giftia.db.conn.execute(count_sql, params) as cursor:
                row = await cursor.fetchone()
                total = row["total"] if row else 0

            # Query data
            data_sql = f"""
                SELECT id, bot_name, group_or_user_id, nickname, user_id, message_id,
                       content, media_ids, role, reply_decision, use_rag, is_recalled, created_at
                FROM chat_history
                {where_clause}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            """
            data_params = params + [limit, offset]
            items = []
            async with self.giftia.db.conn.execute(data_sql, data_params) as cursor:
                rows = await cursor.fetchall()
                for r in rows:
                    items.append(
                        {
                            "id": r["id"],
                            "bot_name": r["bot_name"],
                            "group_or_user_id": r["group_or_user_id"],
                            "nickname": r["nickname"],
                            "user_id": r["user_id"],
                            "message_id": r["message_id"],
                            "content": r["content"],
                            "media_ids": json.loads(r["media_ids"])
                            if r["media_ids"]
                            else [],
                            "role": r["role"],
                            "reply_decision": r["reply_decision"],
                            "use_rag": r["use_rag"],
                            "is_recalled": r["is_recalled"],
                            "created_at": r["created_at"],
                        }
                    )

            last_summarized_id = 0
            if bot_name and group_or_user_id:
                last_summarized_id = await self.giftia.db.get_kv_data(
                    f"passive_memory:last_summarized_id:{bot_name}:{group_or_user_id}",
                    0,
                )

            return json_response(
                {
                    "status": "success",
                    "data": {
                        "items": items,
                        "total": total,
                        "page": page,
                        "limit": limit,
                        "last_summarized_id": last_summarized_id,
                    },
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] get_chat_history error: {e}")
            return error_response(f"获取聊天记录失败: {str(e)}")

    async def get_chat_history_filter_options(self):
        """Get bot/session filter options for chat history."""
        try:
            bot_name = request.query.get("bot_name")
            user_id = request.query.get("user_id")
            reply_decision = request.query.get("reply_decision")
            use_rag = request.query.get("use_rag")
            search = request.query.get("search")

            bots = []
            async with self.giftia.db.conn.execute(
                """
                SELECT DISTINCT bot_name
                FROM chat_history
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
                conditions = ["bot_name = ?"]
                params = [selected_bot_name]
                if user_id:
                    conditions.append("user_id = ?")
                    params.append(user_id)
                if reply_decision is not None and reply_decision != "":
                    conditions.append("reply_decision = ?")
                    params.append(int(reply_decision))
                if use_rag is not None and use_rag != "":
                    conditions.append("use_rag = ?")
                    params.append(int(use_rag))
                if search:
                    conditions.append("content LIKE ?")
                    params.append(f"%{search}%")

                where_clause = "WHERE " + " AND ".join(conditions)
                async with self.giftia.db.conn.execute(
                    f"""
                    SELECT group_or_user_id, COUNT(*) as total, MAX(created_at) as latest_at
                    FROM chat_history
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
            logger.error(f"[Giftia API] get_chat_history_filter_options error: {e}")
            return error_response(f"获取决策审计筛选项失败: {str(e)}")

    async def delete_chat_history(self):
        """Delete chat history for a session."""
        try:
            body = await request.json()
            bot_name = body.get("bot_name")
            group_or_user_id = body.get("group_or_user_id")

            if not bot_name or not group_or_user_id:
                return error_response("缺少 bot_name 或 group_or_user_id 参数")

            await self.giftia.db.delete_chat_history(bot_name, group_or_user_id)

            # Reset last_summarized_id
            await self.giftia.db.delete_kv_data(
                f"passive_memory:last_summarized_id:{bot_name}:{group_or_user_id}"
            )

            return json_response({"status": "success", "message": "清空当前会话消息成功"})
        except Exception as e:
            logger.error(f"[Giftia API] delete_chat_history error: {e}")
            return error_response(f"清空当前会话消息失败: {str(e)}")

    # ── Memory APIs ─────────────────────────────────────────────────────

    async def get_memories(self):
        """Get memories with pagination and filters."""
        try:
            page = int(request.query.get("page", 1))
            limit = int(request.query.get("limit", 20))
            bot_name = request.query.get("bot_name")
            group_or_user_id = request.query.get("group_or_user_id")
            associated_user_id = request.query.get("associated_user_id")
            search = request.query.get("search")

            offset = (page - 1) * limit
            conditions = []
            params = []

            if bot_name:
                conditions.append("bot_name = ?")
                params.append(bot_name)
            if group_or_user_id:
                conditions.append("group_or_user_id = ?")
                params.append(group_or_user_id)
            if associated_user_id:
                conditions.append("metadata LIKE ?")
                params.append(f"%{associated_user_id}%")
            if search:
                conditions.append("text LIKE ?")
                params.append(f"%{search}%")

            where_clause = ""
            if conditions:
                where_clause = "WHERE " + " AND ".join(conditions)

            # Query count
            count_sql = f"SELECT COUNT(*) as total FROM memories {where_clause}"
            async with self.giftia.db.conn.execute(count_sql, params) as cursor:
                row = await cursor.fetchone()
                total = row["total"] if row else 0

            # Query data
            data_sql = f"""
                SELECT id, bot_name, group_or_user_id, memory_id, text, metadata,
                       importance, hit_count, last_hit_at, created_at, updated_at
                FROM memories
                {where_clause}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            """
            data_params = params + [limit, offset]
            items = []
            async with self.giftia.db.conn.execute(data_sql, data_params) as cursor:
                rows = await cursor.fetchall()
                for r in rows:
                    items.append(self._memory_row_to_dict(r))

            # Query user nicknames for items on the page
            user_id_to_name = {}
            if bot_name and items:
                user_ids = set()
                for item in items:
                    meta = item.get("metadata") or {}
                    uid = meta.get("user_id")
                    if uid:
                        user_ids.add(str(uid))
                    associated = meta.get("associated_user_ids")
                    if isinstance(associated, list):
                        for auid in associated:
                            if auid:
                                user_ids.add(str(auid))

                if user_ids:
                    placeholders = ",".join(["?"] * len(user_ids))
                    sql = f"""
                        SELECT user_id, call_name 
                        FROM user_profiles 
                        WHERE bot_name = ? AND user_id IN ({placeholders})
                    """
                    sql_params = [bot_name] + list(user_ids)
                    try:
                        async with self.giftia.db.conn.execute(sql, sql_params) as cursor:
                            p_rows = await cursor.fetchall()
                            for p_r in p_rows:
                                if p_r["call_name"]:
                                    user_id_to_name[str(p_r["user_id"])] = str(p_r["call_name"]).strip()
                    except Exception as e:
                        logger.error(f"[Giftia API] Failed to query user call names for memories: {e}")

            return json_response(
                {
                    "status": "success",
                    "data": {
                        "items": items,
                        "total": total,
                        "page": page,
                        "limit": limit,
                        "user_id_to_name": user_id_to_name,
                    },
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] get_memories error: {e}")
            return error_response(f"获取记忆列表失败: {str(e)}")

    async def get_memory_filter_options(self):
        """Get bot/session filter options for memories."""
        try:
            bot_name = request.query.get("bot_name")
            associated_user_id = request.query.get("associated_user_id")
            search = request.query.get("search")

            bots = []
            async with self.giftia.db.conn.execute(
                """
                SELECT DISTINCT bot_name
                FROM memories
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
                conditions = ["bot_name = ?"]
                params = [selected_bot_name]
                if associated_user_id:
                    conditions.append("metadata LIKE ?")
                    params.append(f"%{associated_user_id}%")
                if search:
                    conditions.append("text LIKE ?")
                    params.append(f"%{search}%")

                where_clause = "WHERE " + " AND ".join(conditions)
                async with self.giftia.db.conn.execute(
                    f"""
                    SELECT group_or_user_id, COUNT(*) as total, MAX(created_at) as latest_at
                    FROM memories
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
            logger.error(f"[Giftia API] get_memory_filter_options error: {e}")
            return error_response(f"获取长期记忆筛选项失败: {str(e)}")

    async def add_memory(self):
        """Add new memory item."""
        try:
            body = await request.json()
            bot_name = body.get("bot_name")
            group_or_user_id = body.get("group_or_user_id")
            text = body.get("text")
            user_id = body.get("user_id") or "admin"
            associated_user_ids = body.get("associated_user_ids")
            importance = normalize_memory_importance(body.get("importance"), 5)

            if isinstance(associated_user_ids, str):
                associated_user_ids = [
                    uid.strip() for uid in associated_user_ids.split(",") if uid.strip()
                ]

            if not bot_name or not group_or_user_id or not text:
                return error_response("缺少必要参数 (bot_name, group_or_user_id, text)")

            memory_id = await self.giftia.data_cache.add_memory(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                text=text,
                user_id=user_id,
                associated_user_ids=associated_user_ids,
                importance=importance,
            )

            if not memory_id:
                return error_response("添加记忆失败，大模型 Embedding 出错")

            return json_response(
                {
                    "status": "success",
                    "message": "添加记忆成功",
                    "data": {"memory_id": memory_id},
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] add_memory error: {e}")
            return error_response(f"添加记忆失败: {str(e)}")

    async def update_memory(self):
        """Update existing memory text by replacing it."""
        try:
            body = await request.json()
            memory_id = body.get("memory_id")
            bot_name = body.get("bot_name")
            group_or_user_id = body.get("group_or_user_id")
            text = body.get("text")
            user_id = body.get("user_id") or "admin"
            associated_user_ids = body.get("associated_user_ids")
            importance_raw = body.get("importance")

            if isinstance(associated_user_ids, str):
                associated_user_ids = [
                    uid.strip() for uid in associated_user_ids.split(",") if uid.strip()
                ]

            if not memory_id or not bot_name or not group_or_user_id or not text:
                return error_response("缺少必要参数")

            async with self.giftia.db.conn.execute(
                "SELECT importance, hit_count, last_hit_at FROM memories WHERE memory_id = ?",
                (memory_id,),
            ) as cursor:
                old_row = await cursor.fetchone()

            if importance_raw is None or importance_raw == "":
                importance = normalize_memory_importance(
                    old_row["importance"] if old_row else None, 5
                )
            else:
                importance = normalize_memory_importance(importance_raw, 5)
            hit_count = int(old_row["hit_count"] or 0) if old_row else 0
            last_hit_at = (old_row["last_hit_at"] or "") if old_row else ""

            # Delete old memory first
            await self.giftia.data_cache.delete_memory(memory_id)

            # Re-insert with updated text
            new_memory_id = await self.giftia.data_cache.add_memory(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                text=text,
                user_id=user_id,
                associated_user_ids=associated_user_ids,
                importance=importance,
                hit_count=hit_count,
                last_hit_at=last_hit_at,
            )

            if not new_memory_id:
                return error_response("更新记忆失败，大模型 Embedding 出错")

            return json_response(
                {
                    "status": "success",
                    "message": "更新记忆成功",
                    "data": {"memory_id": new_memory_id},
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] update_memory error: {e}")
            return error_response(f"更新记忆失败: {str(e)}")

    async def delete_memory(self):
        """Delete memory item."""
        try:
            body = await request.json()
            memory_id = body.get("memory_id")

            if not memory_id:
                return error_response("缺少 memory_id 参数")

            success = await self.giftia.data_cache.delete_memory(memory_id)
            if not success:
                return error_response("删除记忆失败")

            return json_response({"status": "success", "message": "删除记忆成功"})
        except Exception as e:
            logger.error(f"[Giftia API] delete_memory error: {e}")
            return error_response(f"删除记忆失败: {str(e)}")

    async def get_memory_clean_candidates(self):
        """Preview memory cleanup candidates by criteria."""
        try:
            body = await request.json()
            bot_name = str(body.get("bot_name") or "").strip()
            if not bot_name:
                return error_response("请选择 Bot 名称")

            limit = self._optional_int(body.get("limit"), default=300, min_value=1)
            limit = min(limit or 300, 500)
            conditions, params, clean_rules = self._memory_clean_conditions(body)
            where_clause = "WHERE " + " AND ".join(conditions)

            count_sql = f"SELECT COUNT(*) as total FROM memories {where_clause}"
            async with self.giftia.db.conn.execute(count_sql, params) as cursor:
                row = await cursor.fetchone()
                total = row["total"] if row else 0

            data_sql = f"""
                SELECT id, bot_name, group_or_user_id, memory_id, text, metadata,
                       importance, hit_count, last_hit_at, created_at, updated_at
                FROM memories
                {where_clause}
                ORDER BY COALESCE(importance, 5) ASC,
                         COALESCE(hit_count, 0) ASC,
                         datetime(created_at) ASC
                LIMIT ?
            """
            items = []
            async with self.giftia.db.conn.execute(
                data_sql, [*params, limit]
            ) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    item = self._memory_row_to_dict(row)
                    item["clean_reasons"] = self._memory_clean_reasons(
                        item, clean_rules
                    )
                    items.append(item)

            return json_response(
                {
                    "status": "success",
                    "data": {
                        "items": items,
                        "total": total,
                        "limit": limit,
                        "truncated": total > len(items),
                    },
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] get_memory_clean_candidates error: {e}")
            return error_response(f"筛选待清理记忆失败: {str(e)}")

    async def clean_selected_memories(self):
        """Delete selected memories from cleanup preview."""
        try:
            body = await request.json()
            memory_ids = body.get("memory_ids")
            if not isinstance(memory_ids, list):
                return error_response("缺少 memory_ids 列表")

            clean_ids = []
            seen = set()
            for memory_id in memory_ids:
                memory_id = str(memory_id or "").strip()
                if memory_id and memory_id not in seen:
                    seen.add(memory_id)
                    clean_ids.append(memory_id)

            if not clean_ids:
                return error_response("没有选中的记忆")
            if len(clean_ids) > 500:
                return error_response("单次最多清理 500 条记忆")

            deleted_count = 0
            failed_ids = []
            for memory_id in clean_ids:
                success = await self.giftia.data_cache.delete_memory(memory_id)
                if success:
                    deleted_count += 1
                else:
                    failed_ids.append(memory_id)

            if deleted_count == 0:
                return error_response("清理失败，未删除任何记忆")

            return json_response(
                {
                    "status": "success",
                    "message": f"已清理 {deleted_count} 条长期记忆",
                    "deleted_count": deleted_count,
                    "failed_ids": failed_ids,
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] clean_selected_memories error: {e}")
            return error_response(f"清理记忆失败: {str(e)}")

    async def get_auto_clean_memory_config(self):
        """获取长期记忆自动清理配置。"""
        try:
            raw_cfg = await self.giftia.db.get_kv_data("auto_clean_memory_config")
            cfg = self.giftia.tools_func.normalize_auto_clean_memory_config(raw_cfg)
            return json_response({"status": "success", "config": cfg})
        except Exception as e:
            logger.error(f"[Giftia API] get_auto_clean_memory_config error: {e}")
            return error_response(f"获取长期记忆自动清理配置失败: {str(e)}")

    async def set_auto_clean_memory_config(self):
        """保存长期记忆自动清理配置。"""
        try:
            body = await request.json()
            cfg = self.giftia.tools_func.normalize_auto_clean_memory_config(body)
            await self.giftia.db.upsert_kv_data(
                "auto_clean_memory_config", json.dumps(cfg)
            )
            self.giftia.tools_func.update_auto_clean_memory_job()
            return json_response(
                {"status": "success", "message": "配置保存成功", "config": cfg}
            )
        except Exception as e:
            logger.error(f"[Giftia API] set_auto_clean_memory_config error: {e}")
            return error_response(f"保存长期记忆自动清理配置失败: {str(e)}")

    async def trigger_auto_clean_memories(self):
        """立即执行一次长期记忆自动清理。"""
        try:
            res = await self.giftia.tools_func.auto_clean_memories()
            return json_response(res)
        except Exception as e:
            logger.error(f"[Giftia API] trigger_auto_clean_memories error: {e}")
            return error_response(f"执行长期记忆自动清理失败: {str(e)}")

    # ── Bot Status APIs ─────────────────────────────────────────────────

    async def get_bot_status(self):
        """Get active bot status list."""
        try:
            sql = """
                SELECT id, bot_name, group_or_user_id, mood, state, memory, action, energy, created_at, updated_at
                FROM bot_status
                ORDER BY updated_at DESC
            """
            items = []
            async with self.giftia.db.conn.execute(sql) as cursor:
                rows = await cursor.fetchall()

            for r in rows:
                task_board = {
                    "enabled": False,
                    "limit": 0,
                    "active_tasks": [],
                    "stats": {},
                }
                if hasattr(self.giftia, "task_board"):
                    task_board = await self.giftia.task_board.get_dashboard_summary(
                        bot_name=r["bot_name"],
                        group_or_user_id=r["group_or_user_id"],
                    )
                items.append(
                    {
                        "id": r["id"],
                        "bot_name": r["bot_name"],
                        "group_or_user_id": r["group_or_user_id"],
                        "mood": r["mood"],
                        "state": r["state"],
                        "memory": r["memory"],
                        "action": r["action"],
                        "energy": r["energy"],
                        "created_at": r["created_at"],
                        "updated_at": r["updated_at"],
                        "task_board": task_board,
                    }
                )

            return json_response({"status": "success", "data": items})
        except Exception as e:
            logger.error(f"[Giftia API] get_bot_status error: {e}")
            return error_response(f"获取状态列表失败: {str(e)}")

    async def fill_energy(self):
        """Replenish bot energy to max 100."""
        try:
            body = await request.json()
            bot_name = body.get("bot_name")
            group_or_user_id = body.get("group_or_user_id")

            if not bot_name or not group_or_user_id:
                return error_response("缺少必要参数")

            fmt_key = f"{bot_name}:{group_or_user_id}"
            status = self.giftia.data_cache.bot_status.get(fmt_key)
            if not status:
                status = await self.giftia.db.get_bot_status(group_or_user_id, bot_name)

            status.energy = "100.0"
            status.timestamp = time.time()
            self.giftia.data_cache.bot_status[fmt_key] = status

            # Persist to database
            await self.giftia.db.upsert_bot_status(group_or_user_id, bot_name, status)

            return json_response(
                {"status": "success", "message": f"成功为 {bot_name} 补充能量"}
            )
        except Exception as e:
            logger.error(f"[Giftia API] fill_energy error: {e}")
            return error_response(f"补充能量失败: {str(e)}")

    async def update_bot_status(self):
        """Update bot mood, state, memory, or action."""
        try:
            body = await request.json()
            bot_name = body.get("bot_name")
            group_or_user_id = body.get("group_or_user_id")
            mood = body.get("mood")
            state = body.get("state")
            memory = body.get("memory")
            action = body.get("action")

            if not bot_name or not group_or_user_id:
                return error_response("缺少必要参数")

            fmt_key = f"{bot_name}:{group_or_user_id}"
            status = self.giftia.data_cache.bot_status.get(fmt_key)
            if not status:
                status = await self.giftia.db.get_bot_status(group_or_user_id, bot_name)

            if mood is not None:
                status.mood = mood
            if state is not None:
                status.state = state
            if memory is not None:
                status.memory = memory
            if action is not None:
                status.action = action

            self.giftia.data_cache.bot_status[fmt_key] = status
            await self.giftia.db.upsert_bot_status(group_or_user_id, bot_name, status)

            return json_response({"status": "success", "message": "更新 Bot 状态成功"})
        except Exception as e:
            logger.error(f"[Giftia API] update_bot_status error: {e}")
            return error_response(f"更新 Bot 状态失败: {str(e)}")

    # ── Short Task Board APIs ───────────────────────────────────────────

    def _serialize_short_task(self, task) -> dict:
        return {
            "task_id": task.task_id,
            "bot_name": task.bot_name,
            "group_or_user_id": task.group_or_user_id,
            "creator_user_id": task.creator_user_id,
            "creator_nickname": task.creator_nickname,
            "content": task.content,
            "status": task.status,
            "closed_by_user_id": task.closed_by_user_id,
            "close_reason": task.close_reason,
            "expires_at": task.expires_at,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        }

    async def get_task_board(self):
        """Get short task board for a session."""
        try:
            bot_name = request.query.get("bot_name")
            group_or_user_id = request.query.get("group_or_user_id")

            if not bot_name or not group_or_user_id:
                return error_response("缺少 bot_name 或 group_or_user_id 参数")
            if not hasattr(self.giftia, "task_board"):
                return error_response("短期任务看板不可用")

            tasks = await self.giftia.task_board.get_all_tasks(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
            )
            stats = await self.giftia.db.get_short_task_stats(bot_name, group_or_user_id)
            return json_response(
                {
                    "status": "success",
                    "data": {
                        "enabled": self.giftia.task_board.is_enabled(),
                        "limit": self.giftia.task_board.max_active_tasks(),
                        "stats": stats,
                        "items": [self._serialize_short_task(task) for task in tasks],
                    },
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] get_task_board error: {e}")
            return error_response(f"获取短期任务失败: {str(e)}")

    async def update_task_board(self):
        """Update a short task from dashboard without creator permission checks."""
        try:
            body = await request.json()
            bot_name = body.get("bot_name")
            group_or_user_id = body.get("group_or_user_id")
            task_id = body.get("task_id")
            content = body.get("content")
            status = body.get("status")
            expires_at = body.get("expires_at")

            if not bot_name or not group_or_user_id or not task_id:
                return error_response("缺少必要参数")
            if not hasattr(self.giftia, "task_board"):
                return error_response("短期任务看板不可用")

            ok, message, task = await self.giftia.task_board.update_task_from_dashboard(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                task_id=task_id,
                content=content or "",
                status=status or "",
                expires_at=expires_at or "",
            )
            if not ok:
                return error_response(message)
            return json_response(
                {
                    "status": "success",
                    "message": message,
                    "data": self._serialize_short_task(task) if task else None,
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] update_task_board error: {e}")
            return error_response(f"更新短期任务失败: {str(e)}")

    async def delete_task_board(self):
        """Delete a short task from dashboard without creator permission checks."""
        try:
            body = await request.json()
            bot_name = body.get("bot_name")
            group_or_user_id = body.get("group_or_user_id")
            task_id = body.get("task_id")

            if not bot_name or not group_or_user_id or not task_id:
                return error_response("缺少必要参数")
            if not hasattr(self.giftia, "task_board"):
                return error_response("短期任务看板不可用")

            ok, message = await self.giftia.task_board.delete_task_from_dashboard(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                task_id=task_id,
            )
            if not ok:
                return error_response(message)
            return json_response({"status": "success", "message": message})
        except Exception as e:
            logger.error(f"[Giftia API] delete_task_board error: {e}")
            return error_response(f"删除短期任务失败: {str(e)}")

    async def clear_task_board(self):
        """Clear short tasks by status filter for a session."""
        try:
            body = await request.json()
            bot_name = body.get("bot_name")
            group_or_user_id = body.get("group_or_user_id")
            status = body.get("status") or "all"

            if not bot_name or not group_or_user_id:
                return error_response("缺少 bot_name 或 group_or_user_id 参数")
            if not hasattr(self.giftia, "task_board"):
                return error_response("短期任务看板不可用")

            ok, message, count = await self.giftia.task_board.clear_tasks_from_dashboard(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                status=status,
            )
            if not ok:
                return error_response(message)
            return json_response({"status": "success", "message": message, "cleared_count": count})
        except Exception as e:
            logger.error(f"[Giftia API] clear_task_board error: {e}")
            return error_response(f"清空短期任务失败: {str(e)}")

    # ── Scheduled Task APIs ─────────────────────────────────────────────

    async def get_scheduled_tasks(self):
        """Get scheduled tasks for a session."""
        try:
            bot_name = request.query.get("bot_name")
            group_or_user_id = request.query.get("group_or_user_id")

            if not bot_name or not group_or_user_id:
                return error_response("缺少 bot_name 或 group_or_user_id 参数")
            if not hasattr(self.giftia, "task_manager"):
                return error_response("定时任务调度器不可用")

            jobs = self.giftia.task_manager.get_session_jobs_data(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
            )
            return json_response(
                {
                    "status": "success",
                    "data": {
                        "items": jobs,
                        "total": len(jobs),
                    },
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] get_scheduled_tasks error: {e}")
            return error_response(f"获取定时任务列表失败: {str(e)}")

    async def delete_scheduled_task(self):
        """Delete a single scheduled task for a session with session ownership verification."""
        try:
            body = await request.json()
            bot_name = body.get("bot_name")
            group_or_user_id = body.get("group_or_user_id")
            task_id = body.get("task_id")

            if not bot_name or not group_or_user_id or not task_id:
                return error_response("缺少必要参数")
            if not hasattr(self.giftia, "task_manager"):
                return error_response("定时任务调度器不可用")

            # 会话隔离校验：验证该定时任务是否确实属于指定的 Bot 与会话
            session_jobs = self.giftia.task_manager.get_session_jobs_data(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
            )
            if not any(job["task_id"] == task_id for job in session_jobs):
                return error_response("指定的定时任务不存在或无权删除")

            res_msg = self.giftia.task_manager.remove_job(task_id)
            return json_response({"status": "success", "message": res_msg})
        except Exception as e:
            logger.error(f"[Giftia API] delete_scheduled_task error: {e}")
            return error_response(f"删除定时任务失败: {str(e)}")

    async def clear_scheduled_tasks(self):
        """Clear all scheduled tasks for a session."""
        try:
            body = await request.json()
            bot_name = body.get("bot_name")
            group_or_user_id = body.get("group_or_user_id")

            if not bot_name or not group_or_user_id:
                return error_response("缺少 bot_name 或 group_or_user_id 参数")
            if not hasattr(self.giftia, "task_manager"):
                return error_response("定时任务调度器不可用")

            count = self.giftia.task_manager.remove_session_jobs(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
            )
            return json_response({
                "status": "success",
                "message": f"成功清空 {count} 条定时任务",
                "cleared_count": count,
            })
        except Exception as e:
            logger.error(f"[Giftia API] clear_scheduled_tasks error: {e}")
            return error_response(f"清空定时任务失败: {str(e)}")

    # ── User Profile APIs ───────────────────────────────────────────────

    async def get_user_profiles(self):
        """Get user profiles with pagination and filters."""
        try:
            page = int(request.query.get("page", 1))
            limit = int(request.query.get("limit", 20))
            bot_name = request.query.get("bot_name")
            group_or_user_id = request.query.get("group_or_user_id")
            user_id = request.query.get("user_id")
            search = request.query.get("search")

            offset = (page - 1) * limit
            conditions = []
            params = []

            if bot_name:
                conditions.append("up.bot_name = ?")
                params.append(bot_name)
            if group_or_user_id:
                conditions.append("up.group_or_user_id = ?")
                params.append(group_or_user_id)
            if user_id:
                conditions.append("up.user_id = ?")
                params.append(user_id)
            if search:
                like_fields = ["up.profile"] + [
                    f"up.{field}" for field in USER_PROFILE_FIELD_KEYS
                ]
                alias_exists = """
                    EXISTS (
                        SELECT 1
                        FROM user_aliases ua
                        WHERE ua.bot_name = up.bot_name
                          AND ua.group_or_user_id = up.group_or_user_id
                          AND ua.user_id = up.user_id
                          AND ua.alias LIKE ?
                    )
                """
                conditions.append(
                    "("
                    + " OR ".join(
                        [f"{field} LIKE ?" for field in like_fields] + [alias_exists]
                    )
                    + ")"
                )
                params.extend([f"%{search}%"] * (len(like_fields) + 1))

            where_clause = ""
            if conditions:
                where_clause = "WHERE " + " AND ".join(conditions)

            # Query count
            count_sql = f"SELECT COUNT(*) as total FROM user_profiles up {where_clause}"
            async with self.giftia.db.conn.execute(count_sql, params) as cursor:
                row = await cursor.fetchone()
                total = row["total"] if row else 0

            # Query data
            data_sql = f"""
                SELECT up.id, up.bot_name, up.group_or_user_id, up.user_id,
                       up.profile, up.call_name, up.personality,
                       up.interests, up.attitude, up.agreements, up.extra,
                       up.created_at, up.updated_at,
                       COALESCE(up.relation, r.relation) AS relation,
                       CASE WHEN up.title IS NOT NULL THEN up.title ELSE r.title END AS title
                FROM user_profiles up
                LEFT JOIN relations r ON up.bot_name = r.bot_name AND up.group_or_user_id = r.group_or_user_id AND up.user_id = r.user_id
                {where_clause}
                ORDER BY up.updated_at DESC
                LIMIT ? OFFSET ?
            """
            data_params = params + [limit, offset]
            items = []
            async with self.giftia.db.conn.execute(data_sql, data_params) as cursor:
                rows = await cursor.fetchall()
                for r in rows:
                    items.append(
                        {
                            "id": r["id"],
                            "bot_name": r["bot_name"],
                            "group_or_user_id": r["group_or_user_id"],
                            "user_id": r["user_id"],
                            "profile": r["profile"],
                            "call_name": r["call_name"] or "",
                            "aliases": await self.giftia.db.get_user_aliases_text(
                                bot_name=r["bot_name"],
                                group_or_user_id=r["group_or_user_id"],
                                user_id=r["user_id"],
                                limit=6,
                            ),
                            "personality": r["personality"] or "",
                            "interests": r["interests"] or "",
                            "attitude": r["attitude"] or "",
                            "agreements": r["agreements"] or "",
                            "extra": r["extra"] or "",
                            "relation": r["relation"]
                            if r["relation"] is not None
                            else 0,
                            "title": r["title"] if r["title"] is not None else "",
                            "created_at": r["created_at"],
                            "updated_at": r["updated_at"],
                        }
                    )

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
            logger.error(f"[Giftia API] get_user_profiles error: {e}")
            return error_response(f"获取用户画像列表失败: {str(e)}")

    async def get_user_profile_filter_options(self):
        """Get bot/session filter options for user profiles."""
        try:
            bot_name = request.query.get("bot_name")
            user_id = request.query.get("user_id")
            search = request.query.get("search")

            bots = []
            async with self.giftia.db.conn.execute(
                """
                SELECT DISTINCT bot_name
                FROM user_profiles
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
                conditions = ["bot_name = ?"]
                params = [selected_bot_name]
                if user_id:
                    conditions.append("user_id = ?")
                    params.append(user_id)
                if search:
                    like_fields = ["profile"] + list(USER_PROFILE_FIELD_KEYS)
                    alias_exists = """
                        EXISTS (
                            SELECT 1
                            FROM user_aliases ua
                            WHERE ua.bot_name = user_profiles.bot_name
                              AND ua.group_or_user_id = user_profiles.group_or_user_id
                              AND ua.user_id = user_profiles.user_id
                              AND ua.alias LIKE ?
                        )
                    """
                    conditions.append(
                        "("
                        + " OR ".join(
                            [f"{field} LIKE ?" for field in like_fields]
                            + [alias_exists]
                        )
                        + ")"
                    )
                    params.extend([f"%{search}%"] * (len(like_fields) + 1))

                where_clause = "WHERE " + " AND ".join(conditions)
                async with self.giftia.db.conn.execute(
                    f"""
                    SELECT group_or_user_id, COUNT(*) as total, MAX(updated_at) as latest_at
                    FROM user_profiles
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
            logger.error(f"[Giftia API] get_user_profile_filter_options error: {e}")
            return error_response(f"获取用户画像筛选项失败: {str(e)}")

    async def update_user_profile(self):
        """Update/Upsert user profile."""
        try:
            body = await request.json()
            bot_name = body.get("bot_name")
            group_or_user_id = body.get("group_or_user_id")
            user_id = body.get("user_id")
            profile = body.get("profile")
            relation = body.get("relation")
            title = body.get("title")
            profile_fields = {
                field: body.get(field)
                for field in USER_PROFILE_FIELD_KEYS
                if field in body
            }

            if not bot_name or not group_or_user_id or not user_id:
                return error_response(
                    "缺少必要参数 (bot_name, group_or_user_id, user_id)"
                )

            parsed_relation = (
                int(relation) if relation is not None and relation != "" else None
            )
            parsed_title = str(title) if title is not None else None

            await self.giftia.data_cache.set_user_profile(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                user_id=user_id,
                profile=profile,
                relation=parsed_relation,
                title=parsed_title,
                profile_fields=profile_fields,
                alias_increment_count=False,
            )

            return json_response({"status": "success", "message": "更新用户画像成功"})
        except Exception as e:
            logger.error(f"[Giftia API] update_user_profile error: {e}")
            return error_response(f"更新用户画像失败: {str(e)}")

    async def delete_user_profile(self):
        """Delete user profile."""
        try:
            body = await request.json()
            bot_name = body.get("bot_name")
            group_or_user_id = body.get("group_or_user_id")
            user_id = body.get("user_id")

            if not bot_name or not group_or_user_id or not user_id:
                return error_response(
                    "缺少必要参数 (bot_name, group_or_user_id, user_id)"
                )

            await self.giftia.db.delete_user_profile(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                user_id=user_id,
            )
            fmt_key = f"{bot_name}:{group_or_user_id}:{user_id}"
            self.giftia.data_cache.user_profiles.pop(fmt_key, None)
            self.giftia.data_cache.user_profile_records.pop(fmt_key, None)
            self.giftia.data_cache.relations.pop(fmt_key, None)
            return json_response({"status": "success", "message": "删除用户画像成功"})
        except Exception as e:
            logger.error(f"[Giftia API] delete_user_profile error: {e}")
            return error_response(f"删除用户画像失败: {str(e)}")

    async def get_user_aliases(self):
        """Get all aliases for a user profile."""
        try:
            bot_name = request.query.get("bot_name")
            group_or_user_id = request.query.get("group_or_user_id")
            user_id = request.query.get("user_id")
            limit_raw = request.query.get("limit")

            if not bot_name or not group_or_user_id or not user_id:
                return error_response(
                    "缺少必要参数 (bot_name, group_or_user_id, user_id)"
                )

            limit = int(limit_raw) if limit_raw else None
            aliases = await self.giftia.db.get_user_aliases(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                user_id=user_id,
                limit=limit,
                ignore_count_filter=True,
            )
            return json_response(
                {
                    "status": "success",
                    "data": {
                        "items": aliases,
                    },
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] get_user_aliases error: {e}")
            return error_response(f"获取用户外号失败: {str(e)}")

    async def add_user_alias(self):
        """Add one or more aliases for a user without increasing existing counts."""
        try:
            body = await request.json()
            bot_name = body.get("bot_name")
            group_or_user_id = body.get("group_or_user_id")
            user_id = body.get("user_id")
            alias = str(body.get("alias") or "").strip()

            if not bot_name or not group_or_user_id or not user_id:
                return error_response(
                    "缺少必要参数 (bot_name, group_or_user_id, user_id)"
                )
            if not alias:
                return error_response("外号不能为空")

            await self.giftia.db.upsert_user_aliases(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                user_id=user_id,
                aliases=alias,
                increment_count=False,
            )
            self._invalidate_user_profile_record_cache(
                bot_name, group_or_user_id, user_id
            )
            return json_response({"status": "success", "message": "新增外号成功"})
        except Exception as e:
            logger.error(f"[Giftia API] add_user_alias error: {e}")
            return error_response(f"新增用户外号失败: {str(e)}")

    async def update_user_alias_count(self):
        """Update alias count for a user."""
        try:
            body = await request.json()
            bot_name = body.get("bot_name")
            group_or_user_id = body.get("group_or_user_id")
            user_id = body.get("user_id")
            alias = str(body.get("alias") or "").strip()
            alias_count = body.get("alias_count")

            if not bot_name or not group_or_user_id or not user_id:
                return error_response(
                    "缺少必要参数 (bot_name, group_or_user_id, user_id)"
                )
            if not alias:
                return error_response("外号不能为空")
            try:
                parsed_count = int(alias_count)
            except (TypeError, ValueError):
                return error_response("统计次数必须是正整数")
            if parsed_count < 1:
                return error_response("统计次数必须大于 0")

            updated = await self.giftia.db.set_user_alias_count(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                user_id=user_id,
                alias=alias,
                alias_count=parsed_count,
            )
            if not updated:
                return error_response("外号不存在")
            self._invalidate_user_profile_record_cache(
                bot_name, group_or_user_id, user_id
            )
            return json_response({"status": "success", "message": "更新外号次数成功"})
        except Exception as e:
            logger.error(f"[Giftia API] update_user_alias_count error: {e}")
            return error_response(f"更新用户外号次数失败: {str(e)}")

    async def delete_user_alias(self):
        """Delete one alias for a user."""
        try:
            body = await request.json()
            bot_name = body.get("bot_name")
            group_or_user_id = body.get("group_or_user_id")
            user_id = body.get("user_id")
            alias = str(body.get("alias") or "").strip()

            if not bot_name or not group_or_user_id or not user_id:
                return error_response(
                    "缺少必要参数 (bot_name, group_or_user_id, user_id)"
                )
            if not alias:
                return error_response("外号不能为空")

            deleted = await self.giftia.db.delete_user_alias(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                user_id=user_id,
                alias=alias,
            )
            if not deleted:
                return error_response("外号不存在")
            self._invalidate_user_profile_record_cache(
                bot_name, group_or_user_id, user_id
            )
            return json_response({"status": "success", "message": "删除外号成功"})
        except Exception as e:
            logger.error(f"[Giftia API] delete_user_alias error: {e}")
            return error_response(f"删除用户外号失败: {str(e)}")

    # ── Chat History Auto Clean APIs ────────────────────────────────────

    async def get_auto_clean_chat_history_config(self):
        """获取聊天记录自动清理配置。"""
        try:
            raw_cfg = await self.giftia.db.get_kv_data("auto_clean_chat_history_config")
            cfg = self.giftia.tools_func.normalize_auto_clean_chat_history_config(raw_cfg)
            return json_response({"status": "success", "config": cfg})
        except Exception as e:
            logger.error(f"[Giftia API] get_auto_clean_chat_history_config error: {e}")
            return error_response(f"获取聊天记录自动清理配置失败: {str(e)}")

    async def set_auto_clean_chat_history_config(self):
        """保存聊天记录自动清理配置。"""
        try:
            body = await request.json()
            cfg = self.giftia.tools_func.normalize_auto_clean_chat_history_config(body)
            await self.giftia.db.upsert_kv_data(
                "auto_clean_chat_history_config", json.dumps(cfg)
            )
            return json_response({"status": "success", "config": cfg, "message": "已保存聊天记录自动清理配置"})
        except Exception as e:
            logger.error(f"[Giftia API] set_auto_clean_chat_history_config error: {e}")
            return error_response(f"保存聊天记录自动清理配置失败: {str(e)}")

    async def trigger_auto_clean_chat_history(self):
        """立即手动触发聊天记录自动清理。"""
        try:
            res = await self.giftia.tools_func.auto_clean_chat_history()
            return json_response(res)
        except Exception as e:
            logger.error(f"[Giftia API] trigger_auto_clean_chat_history error: {e}")
            return error_response(f"执行聊天记录自动清理失败: {str(e)}")

    # ── User Aliases Auto Clean APIs ───────────────────────────────────

    async def get_auto_clean_aliases_config(self):
        """获取过期外号自动清理配置。"""
        try:
            raw_cfg = await self.giftia.db.get_kv_data("auto_clean_user_aliases_config")
            cfg = self.giftia.tools_func.normalize_auto_clean_aliases_config(raw_cfg)
            return json_response({"status": "success", "config": cfg})
        except Exception as e:
            logger.error(f"[Giftia API] get_auto_clean_aliases_config error: {e}")
            return error_response(f"获取过期外号自动清理配置失败: {str(e)}")

    async def set_auto_clean_aliases_config(self):
        """保存过期外号自动清理配置。"""
        try:
            body = await request.json()
            cfg = self.giftia.tools_func.normalize_auto_clean_aliases_config(body)
            await self.giftia.db.upsert_kv_data(
                "auto_clean_user_aliases_config", json.dumps(cfg)
            )
            return json_response({"status": "success", "config": cfg, "message": "已保存过期外号自动清理配置"})
        except Exception as e:
            logger.error(f"[Giftia API] set_auto_clean_aliases_config error: {e}")
            return error_response(f"保存过期外号自动清理配置失败: {str(e)}")

    async def trigger_auto_clean_aliases(self):
        """立即手动触发过期外号自动清理。"""
        try:
            res = await self.giftia.tools_func.auto_clean_expired_user_aliases()
            return json_response(res)
        except Exception as e:
            logger.error(f"[Giftia API] trigger_auto_clean_aliases error: {e}")
            return error_response(f"执行过期外号自动清理失败: {str(e)}")

    # ── Group Profile APIs ──────────────────────────────────────────────


    async def get_group_profiles(self):
        """Get group profiles with pagination and filters."""
        try:
            page = int(request.query.get("page", 1))
            limit = int(request.query.get("limit", 20))
            bot_name = request.query.get("bot_name")
            group_or_user_id = request.query.get("group_or_user_id")
            search = request.query.get("search")

            offset = (page - 1) * limit
            conditions = []
            params = []

            if bot_name:
                conditions.append("bot_name = ?")
                params.append(bot_name)
            if group_or_user_id:
                conditions.append("group_or_user_id = ?")
                params.append(group_or_user_id)
            if search:
                conditions.append("profile LIKE ?")
                params.append(f"%{search}%")

            where_clause = ""
            if conditions:
                where_clause = "WHERE " + " AND ".join(conditions)

            # Query count
            count_sql = f"SELECT COUNT(*) as total FROM group_profiles {where_clause}"
            async with self.giftia.db.conn.execute(count_sql, params) as cursor:
                row = await cursor.fetchone()
                total = row["total"] if row else 0

            # Query data
            data_sql = f"""
                SELECT id, bot_name, group_or_user_id, profile, created_at, updated_at
                FROM group_profiles
                {where_clause}
                ORDER BY updated_at DESC
                LIMIT ? OFFSET ?
            """
            data_params = params + [limit, offset]
            items = []
            async with self.giftia.db.conn.execute(data_sql, data_params) as cursor:
                rows = await cursor.fetchall()
                for r in rows:
                    items.append(
                        {
                            "id": r["id"],
                            "bot_name": r["bot_name"],
                            "group_or_user_id": r["group_or_user_id"],
                            "profile": r["profile"],
                            "created_at": r["created_at"],
                            "updated_at": r["updated_at"],
                        }
                    )

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
            logger.error(f"[Giftia API] get_group_profiles error: {e}")
            return error_response(f"获取群聊画像列表失败: {str(e)}")

    async def get_group_profile_filter_options(self):
        """Get bot/session filter options for group profiles."""
        try:
            bot_name = request.query.get("bot_name")
            search = request.query.get("search")

            bots = []
            async with self.giftia.db.conn.execute(
                """
                SELECT DISTINCT bot_name
                FROM group_profiles
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
                conditions = ["bot_name = ?"]
                params = [selected_bot_name]
                if search:
                    conditions.append("profile LIKE ?")
                    params.append(f"%{search}%")

                where_clause = "WHERE " + " AND ".join(conditions)
                async with self.giftia.db.conn.execute(
                    f"""
                    SELECT group_or_user_id, COUNT(*) as total, MAX(updated_at) as latest_at
                    FROM group_profiles
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
            logger.error(f"[Giftia API] get_group_profile_filter_options error: {e}")
            return error_response(f"获取群画像筛选项失败: {str(e)}")

    async def update_group_profile(self):
        """Update/Upsert group profile."""
        try:
            body = await request.json()
            bot_name = body.get("bot_name")
            group_or_user_id = body.get("group_or_user_id")
            profile = body.get("profile")

            if not bot_name or not group_or_user_id or profile is None:
                return error_response(
                    "缺少必要参数 (bot_name, group_or_user_id, profile)"
                )

            await self.giftia.db.upsert_group_profile(
                group_or_user_id=group_or_user_id,
                bot_name=bot_name,
                profile=profile,
            )
            return json_response({"status": "success", "message": "更新群聊画像成功"})
        except Exception as e:
            logger.error(f"[Giftia API] update_group_profile error: {e}")
            return error_response(f"更新群聊画像失败: {str(e)}")

    async def delete_group_profile(self):
        """Delete group profile."""
        try:
            body = await request.json()
            bot_name = body.get("bot_name")
            group_or_user_id = body.get("group_or_user_id")

            if not bot_name or not group_or_user_id:
                return error_response("缺少必要参数 (bot_name, group_or_user_id)")

            await self.giftia.db.delete_group_profile(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
            )
            return json_response({"status": "success", "message": "删除群聊画像成功"})
        except Exception as e:
            logger.error(f"[Giftia API] delete_group_profile error: {e}")
            return error_response(f"删除群聊画像失败: {str(e)}")
