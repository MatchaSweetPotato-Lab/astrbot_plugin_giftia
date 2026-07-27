from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request


class TaskApi:
    """Short task board & scheduled task APIs."""

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
