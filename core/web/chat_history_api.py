import json

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request


class ChatHistoryApi:
    """Chat history APIs: query, filter options, delete, auto clean configs/triggers."""

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

    async def delete_single_message(self):
        """Delete a single chat history message by its database id."""
        try:
            body = await request.json()
            msg_id = body.get("id")
            if msg_id is None:
                return error_response("缺少 id 参数")

            try:
                msg_id = int(msg_id)
            except (ValueError, TypeError):
                return error_response("无效的消息 id")

            success = await self.giftia.data_cache.delete_message_by_db_id(msg_id)
            if not success:
                return error_response("未找到该消息或已被删除")

            return json_response({"status": "success", "message": "删除消息成功"})
        except Exception as e:
            logger.error(f"[Giftia API] delete_single_message error: {e}")
            return error_response(f"删除消息失败: {str(e)}")

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
