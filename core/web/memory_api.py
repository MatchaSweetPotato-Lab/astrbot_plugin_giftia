import json
from datetime import datetime, timedelta

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from ..utils.schemas import normalize_memory_importance
from .web_helpers import optional_bool, optional_int, safe_json_dict


class MemoryApi:
    """Long-term memory APIs: CRUD, preview candidates, batch clean, auto clean configs/triggers."""

    def _memory_row_to_dict(self, row) -> dict:
        return {
            "id": row["id"],
            "bot_name": row["bot_name"],
            "group_or_user_id": row["group_or_user_id"],
            "memory_id": row["memory_id"],
            "text": row["text"],
            "metadata": safe_json_dict(row["metadata"]),
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

        max_importance = optional_int(
            body.get("max_importance"), default=3, min_value=1
        )
        if max_importance is not None:
            max_importance = min(10, max_importance)
        max_hit_count = optional_int(
            body.get("max_hit_count"), default=1, min_value=0
        )
        min_age_days = optional_int(
            body.get("min_age_days"), default=60, min_value=0
        )
        last_hit_before_days = optional_int(
            body.get("last_hit_before_days"), default=30, min_value=0
        )
        include_never_hit = optional_bool(
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

            limit = optional_int(body.get("limit"), default=300, min_value=1)
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
