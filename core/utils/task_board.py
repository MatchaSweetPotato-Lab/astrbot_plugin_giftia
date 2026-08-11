import uuid
from datetime import datetime, timedelta

from astrbot.api import logger

from .schemas import ShortTask


class TaskBoardManager:
    """短期任务看板：按会话保存活跃待办，由 LLM 在被激活时处理。"""

    def __init__(self, plugin):
        self.plugin = plugin

    def is_enabled(self, bot_conf: dict | str = None) -> bool:
        bot_dict = self.plugin.get_bot_config(bot_conf) if hasattr(self.plugin, "get_bot_config") else {}
        enabled_features = bot_dict.get("enabled_interactive_features")
        if enabled_features is None:
            return True
        return "task_board" in enabled_features

    def max_active_tasks(self) -> int:
        try:
            value = int(self.plugin.tools_config.get("task_board_max_active", 3))
        except (TypeError, ValueError):
            value = 3
        return max(0, value)

    def default_expire_hours(self) -> int:
        try:
            value = int(self.plugin.tools_config.get("task_board_default_expire_hours", 24))
        except (TypeError, ValueError):
            value = 24
        return max(1, value)

    def _format_time(self, dt: datetime) -> str:
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def _normalize_expires_at(self, value: str | None) -> str:
        if not value:
            return self._format_time(
                datetime.now() + timedelta(hours=self.default_expire_hours())
            )
        text = str(value).strip()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
            return self._format_time(dt)
        except ValueError:
            logger.warning(f"[Giftia TaskBoard] 无法解析 expires_at: {text}")
            return self._format_time(
                datetime.now() + timedelta(hours=self.default_expire_hours())
            )

    async def get_active_tasks(
        self, bot_name: str, group_or_user_id: str
    ) -> list[ShortTask]:
        if not self.is_enabled():
            return []
        await self.plugin.db.expire_short_tasks(bot_name, group_or_user_id)
        return await self.plugin.db.get_short_tasks(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            statuses=["active"],
            limit=self.max_active_tasks(),
        )

    async def get_all_tasks(
        self, bot_name: str, group_or_user_id: str
    ) -> list[ShortTask]:
        await self.plugin.db.expire_short_tasks(bot_name, group_or_user_id)
        return await self.plugin.db.get_short_tasks(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            statuses=["active", "completed", "canceled", "expired"],
        )

    async def create_task(
        self,
        bot_name: str,
        group_or_user_id: str,
        creator_user_id: str,
        creator_nickname: str,
        content: str,
        expires_at: str | None = None,
    ) -> tuple[bool, str, ShortTask | None]:
        if not self.is_enabled():
            return False, "短期任务看板未启用", None

        clean_content = str(content or "").strip()
        if not clean_content:
            return False, "任务内容为空", None

        limit = self.max_active_tasks()
        if limit <= 0:
            return False, "活跃任务上限为 0，无法创建任务", None

        await self.plugin.db.expire_short_tasks(bot_name, group_or_user_id)
        active_count = await self.plugin.db.count_active_short_tasks(
            bot_name, group_or_user_id
        )
        if active_count >= limit:
            return False, f"活跃任务已达上限 {limit} 条", None

        now = self._format_time(datetime.now())
        task = ShortTask(
            task_id=f"task_{uuid.uuid4().hex[:8]}",
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            creator_user_id=str(creator_user_id or ""),
            creator_nickname=str(creator_nickname or ""),
            content=clean_content,
            status="active",
            expires_at=self._normalize_expires_at(expires_at),
            created_at=now,
            updated_at=now,
        )
        await self.plugin.db.insert_short_task(task)
        return True, "创建短期任务成功", task

    async def close_task(
        self,
        bot_name: str,
        group_or_user_id: str,
        task_id: str,
        status: str,
        actor_user_id: str,
        reason: str = "",
    ) -> tuple[bool, str, ShortTask | None]:
        if not self.is_enabled():
            return False, "短期任务看板未启用", None

        clean_task_id = str(task_id or "").strip()
        if not clean_task_id:
            return False, "缺少 task_id", None

        await self.plugin.db.expire_short_tasks(bot_name, group_or_user_id)
        task = await self.plugin.db.get_short_task(
            clean_task_id, bot_name, group_or_user_id
        )
        if not task:
            return False, f"未找到任务 {clean_task_id}", None
        if task.status != "active":
            return False, f"任务 {clean_task_id} 当前状态为 {task.status}", task

        if status not in {"completed", "canceled"}:
            return False, f"不支持的任务状态: {status}", task

        ok = await self.plugin.db.update_short_task_status(
            task_id=task.task_id,
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            status=status,
            closed_by_user_id=str(actor_user_id or ""),
            close_reason=str(reason or "").strip(),
        )
        if not ok:
            return False, f"任务 {task.task_id} 状态更新失败", task

        updated = await self.plugin.db.get_short_task(
            task.task_id, bot_name, group_or_user_id
        )
        action_text = "完成" if status == "completed" else "取消"
        return True, f"{action_text}短期任务成功", updated or task

    async def update_task_from_dashboard(
        self,
        bot_name: str,
        group_or_user_id: str,
        task_id: str,
        content: str,
        status: str,
        expires_at: str,
    ) -> tuple[bool, str, ShortTask | None]:
        clean_task_id = str(task_id or "").strip()
        clean_content = str(content or "").strip()
        clean_status = str(status or "").strip()

        if not clean_task_id:
            return False, "缺少 task_id", None
        if not clean_content:
            return False, "任务内容不能为空", None
        if clean_status not in {"active", "completed", "canceled", "expired"}:
            return False, f"不支持的任务状态: {clean_status}", None

        task = await self.plugin.db.get_short_task(
            clean_task_id, bot_name, group_or_user_id
        )
        if not task:
            return False, f"未找到任务 {clean_task_id}", None

        if clean_status == "active" and task.status != "active":
            active_count = await self.plugin.db.count_active_short_tasks(
                bot_name, group_or_user_id
            )
            if active_count >= self.max_active_tasks():
                return False, f"活跃任务已达上限 {self.max_active_tasks()} 条", task

        normalized_expires_at = self._normalize_expires_at(expires_at)
        closed_by_user_id = ""
        close_reason = ""
        if clean_status != "active":
            closed_by_user_id = task.closed_by_user_id or "dashboard"
            close_reason = task.close_reason or "前端管理"

        ok = await self.plugin.db.update_short_task(
            task_id=clean_task_id,
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            content=clean_content,
            status=clean_status,
            expires_at=normalized_expires_at,
            closed_by_user_id=closed_by_user_id,
            close_reason=close_reason,
        )
        if not ok:
            return False, f"任务 {clean_task_id} 更新失败", task

        updated = await self.plugin.db.get_short_task(
            clean_task_id, bot_name, group_or_user_id
        )
        return True, "更新短期任务成功", updated or task

    async def delete_task_from_dashboard(
        self, bot_name: str, group_or_user_id: str, task_id: str
    ) -> tuple[bool, str]:
        clean_task_id = str(task_id or "").strip()
        if not clean_task_id:
            return False, "缺少 task_id"
        ok = await self.plugin.db.delete_short_task(
            clean_task_id, bot_name, group_or_user_id
        )
        if not ok:
            return False, f"未找到任务 {clean_task_id}"
        return True, "删除短期任务成功"

    async def clear_tasks_from_dashboard(
        self, bot_name: str, group_or_user_id: str, status: str = "all"
    ) -> tuple[bool, str, int]:
        clean_status = str(status or "").strip().lower()
        statuses = None
        if clean_status == "active":
            statuses = ["active"]
        elif clean_status == "completed":
            statuses = ["completed"]
        elif clean_status == "archived":
            statuses = ["canceled", "expired"]
        elif clean_status == "all":
            statuses = None
        else:
            return False, f"不支持的状态分类: {status}", 0

        deleted_count = await self.plugin.db.clear_short_tasks(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            statuses=statuses,
        )
        return True, f"成功清空 {deleted_count} 条短期任务", deleted_count

    async def get_dashboard_summary(
        self, bot_name: str, group_or_user_id: str
    ) -> dict:
        if not self.is_enabled():
            return {
                "enabled": False,
                "limit": 0,
                "active_tasks": [],
                "stats": {},
            }

        active_tasks = await self.get_active_tasks(bot_name, group_or_user_id)
        stats = await self.plugin.db.get_short_task_stats(bot_name, group_or_user_id)
        return {
            "enabled": True,
            "limit": self.max_active_tasks(),
            "active_tasks": [
                {
                    "task_id": task.task_id,
                    "creator_user_id": task.creator_user_id,
                    "creator_nickname": task.creator_nickname,
                    "content": task.content,
                    "created_at": task.created_at,
                    "expires_at": task.expires_at,
                }
                for task in active_tasks
            ],
            "stats": stats,
        }
