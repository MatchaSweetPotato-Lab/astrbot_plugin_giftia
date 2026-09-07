from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request


class SlangApi:
    """Dashboard vocabulary management using the shared session repository."""

    async def get_slang(self):
        """List vocabulary with bot, session, literal search, and pagination filters.

        Returns:
            A JSON response containing entries and the filtered total.
        """
        try:
            bot_name = request.query.get("bot_name", "").strip()
            session = request.query.get("group_or_user_id", "").strip()
            search = request.query.get("search", "").strip()
            page = max(1, int(request.query.get("page", 1)))
            limit = max(1, min(100, int(request.query.get("limit", 15))))
            if not bot_name:
                return error_response("请选择机器人")
            conditions, params = ["bot_name = ?"], [bot_name]
            if session:
                conditions.append("group_or_user_id = ?")
                params.append(session)
            if search:
                conditions.append("(instr(term, ?) > 0 OR instr(description, ?) > 0)")
                params.extend([search, search])
            where = " AND ".join(conditions)
            async with self.giftia.db.conn.execute(
                f"SELECT COUNT(*) FROM slang WHERE {where}", params
            ) as cursor:
                total = (await cursor.fetchone())[0]
            async with self.giftia.db.conn.execute(
                f"SELECT * FROM slang WHERE {where} "
                "ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
                [*params, limit, (page - 1) * limit],
            ) as cursor:
                items = [dict(row) for row in await cursor.fetchall()]
            return json_response(
                {"status": "success", "data": {"items": items, "total": total}}
            )
        except (ValueError, TypeError):
            return error_response("分页参数必须是整数")
        except Exception as e:
            logger.error(f"[Giftia API] get_slang error: {e}")
            return error_response("获取黑话失败")

    async def get_slang_filter_options(self):
        """Get bot and session filter options for slang.

        Returns:
            Available bots and sessions with term counts, excluding sessions without slang.
        """
        try:
            async with self.giftia.db.conn.execute(
                "SELECT bot_name FROM slang UNION SELECT bot_name FROM chat_history"
            ) as cursor:
                stored_bots = {row[0] for row in await cursor.fetchall() if row[0]}
            configured_bots = {
                bot["name"] for bot in self.giftia.bot_config_manager.load_bots()
            }
            bots = sorted(stored_bots | configured_bots)
            selected = request.query.get("bot_name", "")
            if selected not in bots:
                selected = bots[0] if bots else ""
            sessions = []
            if selected:
                async with self.giftia.db.conn.execute(
                    """
                    SELECT group_or_user_id, COUNT(*) as total, MAX(updated_at) as latest_at
                    FROM slang
                    WHERE bot_name = ? AND group_or_user_id IS NOT NULL AND group_or_user_id != ''
                    GROUP BY group_or_user_id
                    ORDER BY latest_at DESC, group_or_user_id ASC
                    """,
                    (selected,),
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
                        "selected_bot_name": selected,
                        "sessions": sessions,
                    },
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] get_slang_filter_options error: {e}")
            return error_response("获取黑话筛选项失败")

    async def save_slang(self):
        """Add or replace a definition in the explicitly selected session.

        Returns:
            A success response or a validation error.
        """
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return error_response("请求内容必须是对象")
            bot_name = body.get("bot_name")
            configured_bots = {
                bot["name"] for bot in self.giftia.bot_config_manager.load_bots()
            }
            if not isinstance(bot_name, str) or bot_name not in configured_bots:
                return error_response("请选择有效的机器人")
            await self.giftia.db.slang_repo.set_entry(
                bot_name,
                body.get("group_or_user_id"),
                body.get("term"),
                body.get("description"),
            )
            return json_response({"status": "success", "message": "黑话已保存"})
        except ValueError as e:
            return error_response(str(e))
        except Exception as e:
            logger.error(f"[Giftia API] save_slang error: {e}")
            return error_response("保存黑话失败")

    async def delete_slang(self):
        """Delete a literal term without affecting any other session.

        Returns:
            A success response, a validation error, or a missing-entry error.
        """
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return error_response("请求内容必须是对象")
            deleted = await self.giftia.db.slang_repo.delete_entry(
                body.get("bot_name"),
                body.get("group_or_user_id"),
                body.get("term"),
            )
            if not deleted:
                return error_response("该会话中未找到这个词，可能已被删除")
            return json_response({"status": "success", "message": "黑话已删除"})
        except ValueError as e:
            return error_response(str(e))
        except Exception as e:
            logger.error(f"[Giftia API] delete_slang error: {e}")
            return error_response("删除黑话失败")
