import time

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request


class BotStatusApi:
    """Bot status APIs: get status list, fill energy, update mood/state/memory/action."""

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
