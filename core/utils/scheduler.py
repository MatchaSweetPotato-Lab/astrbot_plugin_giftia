import inspect
from collections.abc import Callable
from datetime import datetime

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from astrbot.api import logger
from astrbot.api.star import StarTools

# 全局函数映射表
GLOBAL_FUNC_MAP: dict[str, Callable] = {}


async def job_proxy(func_name: str, *args, **kwargs):
    func = GLOBAL_FUNC_MAP.get(func_name)
    if func:
        if inspect.iscoroutinefunction(func):
            await func(*args, **kwargs)
        else:
            func(*args, **kwargs)
    else:
        logger.error(f"定时任务执行失败：未找到注册的函数 '{func_name}'")


class Scheduler:
    def __init__(self):
        self.registered_funcs = GLOBAL_FUNC_MAP
        # 使用 apscheduler 自带的 job store
        job_db_path = StarTools.get_data_dir("astrbot_plugin_giftia") / "jobs.db"
        job_db_path.parent.mkdir(parents=True, exist_ok=True)
        jobstores = {"default": SQLAlchemyJobStore(url=f"sqlite:///{job_db_path}")}
        self.scheduler = AsyncIOScheduler(jobstores=jobstores)

    def register_func(self, name: str, func: Callable):
        """
        注册可供定时任务调用的函数
        """
        self.registered_funcs[name] = func

    def add_job(
        self,
        task_id: str,
        func_name: str,
        time_expr: str,
        args: list | None = None,
        kwargs: dict | None = None,
    ) -> str:
        """
        添加一个定时任务，支持 Cron 表达式和具体日期时间
        """
        if func_name not in self.registered_funcs:
            logger.error(f"添加任务失败：函数 '{func_name}' 未注册")
            return f"添加任务失败：函数 '{func_name}' 未注册"

        args = args or []
        kwargs = kwargs or {}

        # 智能识别触发器类型
        trigger = None
        try:
            run_date = datetime.fromisoformat(time_expr)
            trigger = DateTrigger(run_date=run_date)
        except ValueError:
            try:
                trigger = CronTrigger.from_crontab(time_expr)
            except ValueError:
                logger.error(
                    f"任务 {task_id} 时间格式错误 (既不是日期也不是合法的 Cron): {time_expr}"
                )
                return f"任务 {task_id} 时间格式错误 (既不是日期也不是合法的 Cron): {time_expr}"

        try:
            self.scheduler.add_job(
                job_proxy,
                trigger,
                args=[func_name, *args],
                kwargs=kwargs,
                id=task_id,
                name=f"{func_name}|{time_expr}",
                replace_existing=True,
                misfire_grace_time=3600,  # 1小时容错时间，防止意外宕机错过任务
            )
            logger.info(f"添加定时任务成功: {task_id} (时间规则: {time_expr})")
            return f"添加定时任务成功: {task_id} (时间规则: {time_expr})"
        except Exception as e:
            logger.error(f"添加定时任务 {task_id} 失败: {e}")
            return f"添加定时任务 {task_id} 失败: {e}"

    def remove_job(self, task_id: str) -> str:
        """
        删除定时任务
        """
        if task_id == "system_r2_backup_message":
            logger.warning(f"禁止删除系统定时任务: {task_id}")
            return f"禁止删除系统定时任务: {task_id}"
        job = self.scheduler.get_job(task_id)
        if job:
            try:
                self.scheduler.remove_job(task_id)
                logger.info(f"已删除定时任务: {task_id}")
                return f"已删除定时任务: {task_id}"
            except Exception as e:
                logger.warning(f"从 APScheduler 移除任务 {task_id} 失败: {e}")
                return f"从 APScheduler 移除任务 {task_id} 失败: {e}"
        else:
            logger.warning(f"尝试删除不存在的定时任务: {task_id}")
            return f"尝试删除不存在的定时任务: {task_id}"

    def _format_job(self, job) -> str:
        func_name = job.name
        time_expr = ""
        if job.name and "|" in job.name:
            parts = job.name.split("|", 1)
            func_name = parts[0]
            time_expr = parts[1]

        actual_args = list(job.args)[1:] if job.args else []
        next_run = str(job.next_run_time) if job.next_run_time else "None"

        return f"""task_id: {job.id}
func_name: {func_name}
time_expr: {time_expr}
args: {actual_args}
kwargs: {job.kwargs}
next_run_time: {next_run}"""

    def get_job_info(self, task_id: str) -> str:
        """
        获取某个定时任务的基础信息
        """
        job = self.scheduler.get_job(task_id)
        if not job:
            return ""
        return self._format_job(job)

    def get_all_jobs(self) -> list[str]:
        """
        获取所有定时任务信息
        """
        jobs = self.scheduler.get_jobs()
        return [self._format_job(job) for job in jobs]

    def get_prefix_jobs(self, task_id_prefix: str) -> list[str]:
        """
        获取某个前缀的所有定时任务信息
        """
        if not task_id_prefix.endswith("_"):
            task_id_prefix += "_"
        jobs = self.scheduler.get_jobs()
        return [
            f"任务ID：{job.id} | 任务名称：{job.name} | 任务内容：{job.kwargs.get('remind_message', '')}"
            for job in jobs
            if str(job.id).startswith(task_id_prefix)
        ]

    def get_session_jobs_data(self, bot_name: str, group_or_user_id: str) -> list[dict]:
        """
        获取属于特定 Bot 和会话的定时任务结构化列表（自动跳过系统级/全局任务）
        """
        prefix = f"{bot_name}_{group_or_user_id}_"
        jobs = self.scheduler.get_jobs()
        items = []
        for job in jobs:
            # 跳过系统级/全局任务
            if str(job.id).startswith("system_") or (job.kwargs and job.kwargs.get("system_job", False)):
                continue

            is_match = False
            if str(job.id).startswith(prefix):
                is_match = True
            elif job.kwargs and job.kwargs.get("bot_name") == bot_name and job.kwargs.get("group_or_user_id") == group_or_user_id:
                is_match = True

            if is_match:
                func_name = job.name
                time_expr = ""
                if job.name and "|" in job.name:
                    parts = job.name.split("|", 1)
                    func_name = parts[0]
                    time_expr = parts[1]

                next_run = str(job.next_run_time) if job.next_run_time else "None"
                items.append({
                    "task_id": str(job.id),
                    "bot_name": bot_name,
                    "group_or_user_id": group_or_user_id,
                    "func_name": func_name,
                    "time_expr": time_expr,
                    "remind_message": job.kwargs.get("remind_message", "") if job.kwargs else "",
                    "user_id": job.kwargs.get("user_id", "") if job.kwargs else "",
                    "user_name": job.kwargs.get("user_name", "") if job.kwargs else "",
                    "next_run_time": next_run,
                })
        return items

    def remove_session_jobs(self, bot_name: str, group_or_user_id: str) -> int:
        """
        批量删除指定会话的所有非系统级定时任务
        """
        session_jobs = self.get_session_jobs_data(bot_name, group_or_user_id)
        count = 0
        for item in session_jobs:
            try:
                self.scheduler.remove_job(item["task_id"])
                count += 1
            except Exception as e:
                logger.warning(f"从 APScheduler 移除会话任务 {item['task_id']} 失败: {e}")
        return count

    def start(self):
        """
        启动调度器
        """
        if not self.scheduler.running:
            self.scheduler.start()
            logger.info("Scheduler 已启动")

    def shutdown(self):
        """
        关闭调度器
        """
        if self.scheduler.running:
            self.scheduler.shutdown()
            logger.info("Scheduler 已关闭")
