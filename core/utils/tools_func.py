import asyncio
import json
import time
from datetime import datetime, timedelta

from astrbot.api import logger
from astrbot.core import AstrBotConfig

from ..database.database import Database
from .http_manager import HttpManager
from .scheduler import Scheduler


class ToolsFunc:
    DEFAULT_AUTO_CLEAN_MEMORY_CONFIG = {
        "enabled": False,
        "max_importance": 3,
        "max_hit_count": 1,
        "min_age_days": 60,
        "last_hit_before_days": 30,
        "include_never_hit": True,
        "max_delete_per_run": 20,
        "cron": "30 3 * * *",
    }
    DEFAULT_AUTO_CLEAN_USER_ALIASES_CONFIG = {
        "enabled": True,
        "min_age_days": 7,
        "count_threshold": 3,
    }
    DEFAULT_AUTO_CLEAN_CHAT_HISTORY_CONFIG = {
        "enabled": False,
        "max_count_per_session": 1000,
        "max_age_days": 30,
        "min_keep_per_session": 20,
    }


    def __init__(
        self,
        config: AstrBotConfig,
        task_manager: Scheduler,
        db: Database,
        http_manager: HttpManager,
        data_cache=None,
    ):
        self.config = config
        self.task_manager = task_manager
        self.db = db
        self.http_manager = http_manager
        self.data_cache = data_cache
        self.register_funcs()
        self.task_manager.start()
        # 启动时添加备份任务
        if self.config.get("r2_config", {}).get("r2_enabled", False):
            self.add_backup_message_to_r2()
        # 启动时更新自动清理任务
        self.update_auto_cleanup_jobs()

    def register_funcs(self):
        """注册定时任务函数"""
        self.task_manager.register_func(
            "backup_message_to_r2", self.backup_message_to_r2
        )
        self.task_manager.register_func(
            "auto_clean_media_cache", self.auto_clean_media_cache
        )
        self.task_manager.register_func(
            "auto_clean_memories", self.auto_clean_memories
        )
        self.task_manager.register_func(
            "auto_clean_token_usage", self.auto_clean_token_usage
        )
        self.task_manager.register_func(
            "auto_cleanup_unified", self.auto_cleanup_unified
        )

    def add_backup_message_to_r2(self):
        """添加备份消息到R2的定时任务"""
        # 先检查是否已经添加过该任务
        if self.task_manager.get_job_info("system_r2_backup_message"):
            return
        self.task_manager.add_job(
            task_id="system_r2_backup_message",
            func_name="backup_message_to_r2",
            time_expr=self.config.get("r2_config", {}).get("backup_time", "0 * * * *"),
        )

    async def backup_message_to_r2(self):
        """备份聊天记录到R2"""
        raw_time = await self.db.get_kv_data("last_backup_message_time", 0.0)
        last_backup_time = float(raw_time) if raw_time is not None else 0.0
        backup_path = await self.db.backup_chat_history_db(last_backup_time)
        if backup_path:
            success = await self.http_manager.upload_file(backup_path)
            if success:
                await self.db.upsert_kv_data("last_backup_message_time", time.time())

    async def _get_media_clean_cfg(self) -> dict:
        """
        读取并解析 auto_clean_media_config 配置，统一默认值与错误处理。
        """
        raw_media_cfg = await self.db.get_kv_data("auto_clean_media_config")
        try:
            return (
                json.loads(raw_media_cfg)
                if raw_media_cfg
                else {"enabled": False, "keep_genres": ["表情包", "sticker"]}
            )
        except Exception:
            return {"enabled": False, "keep_genres": ["表情包", "sticker"]}

    async def _get_token_clean_cfg(self) -> dict:
        """
        读取并解析 auto_clean_token_config 配置，统一默认值与错误处理。
        """
        raw_token_cfg = await self.db.get_kv_data("auto_clean_token_config")
        try:
            return (
                json.loads(raw_token_cfg)
                if raw_token_cfg
                else {"enabled": True, "days": 365}
            )
        except Exception:
            return {"enabled": True, "days": 365}

    def update_auto_cleanup_jobs(self):
        """统一管理所有的自动清理定时任务"""
        async def _update():
            # 1. 读取各任务配置
            media_cfg = await self._get_media_clean_cfg()
            
            raw_memory_cfg = await self.db.get_kv_data("auto_clean_memory_config")
            memory_cfg = self.normalize_auto_clean_memory_config(raw_memory_cfg)
            
            token_cfg = await self._get_token_clean_cfg()
                
            media_enabled = bool(media_cfg.get("enabled", False))
            memory_enabled = bool(memory_cfg.get("enabled", False))
            token_enabled = bool(token_cfg.get("enabled", True))
            
            # 是否需要注册统一清理 (只要有任意一个清理开启)
            any_enabled = media_enabled or memory_enabled or token_enabled
            
            if any_enabled:
                self.task_manager.add_job(
                    task_id="system_auto_cleanup_unified",
                    func_name="auto_cleanup_unified",
                    time_expr="0 3 * * *",
                )
            else:
                if self.task_manager.get_job_info("system_auto_cleanup_unified"):
                    self.task_manager.remove_job("system_auto_cleanup_unified")
                
            # 物理清理/移除原有的三个历史分散定时任务（仅在存在时清理，避免 scheduler 打印警告日志）
            for old_task in ["system_auto_clean_media_cache", "system_auto_clean_memories", "system_auto_clean_token_usage"]:
                if self.task_manager.get_job_info(old_task):
                    self.task_manager.remove_job(old_task)

        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                loop.create_task(_update())
            else:
                asyncio.run(_update())
        except RuntimeError:
            asyncio.run(_update())

    def update_auto_clean_media_job(self):
        """兼容层：统一自动清理更新"""
        self.update_auto_cleanup_jobs()

    @staticmethod
    def _clean_bool(val, default: bool) -> bool:
        if val is None:
            return default
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            lowered = val.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
        return bool(val)

    @classmethod
    def normalize_auto_clean_memory_config(cls, raw_cfg=None) -> dict:
        """归一化长期记忆自动清理配置。"""
        if isinstance(raw_cfg, str):
            try:
                raw_cfg = json.loads(raw_cfg) if raw_cfg else {}
            except Exception:
                raw_cfg = {}
        if not isinstance(raw_cfg, dict):
            raw_cfg = {}

        cfg = dict(cls.DEFAULT_AUTO_CLEAN_MEMORY_CONFIG)
        cfg.update(raw_cfg)

        def clean_int(key: str, default: int, min_value: int, max_value: int | None = None):
            try:
                value = int(cfg.get(key, default))
            except (TypeError, ValueError):
                value = default
            value = max(min_value, value)
            if max_value is not None:
                value = min(max_value, value)
            cfg[key] = value

        cfg["enabled"] = cls._clean_bool(cfg.get("enabled"), False)
        cfg["include_never_hit"] = cls._clean_bool(cfg.get("include_never_hit"), True)
        clean_int("max_importance", 3, 1, 7)
        clean_int("max_hit_count", 1, 0, None)
        clean_int("min_age_days", 60, 7, None)
        clean_int("last_hit_before_days", 30, 7, None)
        clean_int("max_delete_per_run", 20, 1, 200)
        cron = str(cfg.get("cron") or "30 3 * * *").strip()
        cfg["cron"] = cron or "30 3 * * *"
        return cfg

    @classmethod
    def normalize_auto_clean_aliases_config(cls, raw_cfg=None) -> dict:
        """归一化过期外号自动清理配置。"""
        if isinstance(raw_cfg, str):
            try:
                raw_cfg = json.loads(raw_cfg) if raw_cfg else {}
            except Exception:
                raw_cfg = {}
        if not isinstance(raw_cfg, dict):
            raw_cfg = {}

        cfg = dict(cls.DEFAULT_AUTO_CLEAN_USER_ALIASES_CONFIG)
        cfg.update(raw_cfg)

        cfg["enabled"] = cls._clean_bool(cfg.get("enabled"), True)
        try:
            cfg["min_age_days"] = max(1, int(cfg.get("min_age_days", 7)))
        except (TypeError, ValueError):
            cfg["min_age_days"] = 7

        try:
            cfg["count_threshold"] = max(1, int(cfg.get("count_threshold", 3)))
        except (TypeError, ValueError):
            cfg["count_threshold"] = 3

        return cfg

    @classmethod
    def normalize_auto_clean_chat_history_config(cls, raw_cfg=None) -> dict:
        """归一化聊天记录自动清理配置。"""
        if isinstance(raw_cfg, str):
            try:
                raw_cfg = json.loads(raw_cfg) if raw_cfg else {}
            except Exception:
                raw_cfg = {}
        if not isinstance(raw_cfg, dict):
            raw_cfg = {}

        cfg = dict(cls.DEFAULT_AUTO_CLEAN_CHAT_HISTORY_CONFIG)
        cfg.update(raw_cfg)

        cfg["enabled"] = cls._clean_bool(cfg.get("enabled"), False)
        try:
            cfg["max_count_per_session"] = max(0, int(cfg.get("max_count_per_session", 1000)))
        except (TypeError, ValueError):
            cfg["max_count_per_session"] = 1000

        try:
            cfg["max_age_days"] = max(0, int(cfg.get("max_age_days", 30)))
        except (TypeError, ValueError):
            cfg["max_age_days"] = 30

        try:
            cfg["min_keep_per_session"] = max(1, int(cfg.get("min_keep_per_session", 20)))
        except (TypeError, ValueError):
            cfg["min_keep_per_session"] = 20

        return cfg


    def update_auto_clean_memory_job(self):
        """兼容层：统一自动清理更新"""
        self.update_auto_cleanup_jobs()

    async def auto_clean_expired_user_aliases(self, config: dict | None = None) -> dict:
        """自动清理未达标且过期的外号记录。"""
        if config is None:
            raw_cfg = await self.db.get_kv_data("auto_clean_user_aliases_config")
            config = self.normalize_auto_clean_aliases_config(raw_cfg)
        else:
            config = self.normalize_auto_clean_aliases_config(config)

        if not config.get("enabled", True):
            return {"status": "skipped", "message": "过期外号自动清理未启用", "deleted_count": 0}

        deleted_count = await self.db.delete_expired_user_aliases(
            min_age_days=config["min_age_days"],
            count_threshold=config["count_threshold"],
        )
        msg = f"过期外号自动清理完成，已清理 {deleted_count} 条低于 {config['count_threshold']} 次且早于 {config['min_age_days']} 天的外号"
        logger.info(f"[Giftia] {msg}")
        return {"status": "success", "message": msg, "deleted_count": deleted_count}

    async def auto_clean_chat_history(self, config: dict | None = None) -> dict:
        """自动清理聊天记录（方案 A 并集删除，保护保底条数）。"""
        if config is None:
            raw_cfg = await self.db.get_kv_data("auto_clean_chat_history_config")
            config = self.normalize_auto_clean_chat_history_config(raw_cfg)
        else:
            config = self.normalize_auto_clean_chat_history_config(config)

        if not config.get("enabled", False):
            return {"status": "skipped", "message": "聊天记录自动清理未启用", "deleted_count": 0}

        deleted_count = await self.db.auto_clean_chat_history(
            max_count_per_session=config["max_count_per_session"],
            max_age_days=config["max_age_days"],
            min_keep_per_session=config["min_keep_per_session"],
        )
        msg = f"聊天记录自动清理完成，共清理 {deleted_count} 条消息记录"
        logger.info(f"[Giftia] {msg}")
        return {"status": "success", "message": msg, "deleted_count": deleted_count}

    async def auto_cleanup_unified(self):
        """统一自动清理任务，按顺序线性执行各项已启用的清理子任务。"""
        logger.info("[Giftia] 开始执行统一自动清理流程...")
        
        # 1. 媒体缓存清理
        media_cfg = await self._get_media_clean_cfg()
            
        if media_cfg.get("enabled", False):
            logger.info("[Giftia] 1/6 正在清理媒体缓存...")
            try:
                await self.auto_clean_media_cache()
            except Exception as e:
                logger.error(f"[Giftia] 媒体缓存自动清理执行异常: {e}")
        else:
            logger.info("[Giftia] 1/6 自动清理媒体缓存未启用，跳过")
            
        # 2. 长期记忆清理
        raw_memory_cfg = await self.db.get_kv_data("auto_clean_memory_config")
        memory_cfg = self.normalize_auto_clean_memory_config(raw_memory_cfg)
        
        if memory_cfg.get("enabled", False):
            logger.info("[Giftia] 2/6 正在清理长期记忆...")
            try:
                await self.auto_clean_memories()
            except Exception as e:
                logger.error(f"[Giftia] 长期记忆自动清理执行异常: {e}")
        else:
            logger.info("[Giftia] 2/6 自动清理长期记忆未启用，跳过")

        # 3. 过期外号清理（默认开启，无需 UI 配置）
        raw_alias_cfg = await self.db.get_kv_data("auto_clean_user_aliases_config")
        alias_cfg = self.normalize_auto_clean_aliases_config(raw_alias_cfg)

        if alias_cfg.get("enabled", True):
            logger.info("[Giftia] 3/6 正在清理过期外号...")
            try:
                await self.auto_clean_expired_user_aliases(config=alias_cfg)
            except Exception as e:
                logger.error(f"[Giftia] 过期外号自动清理执行异常: {e}")
        else:
            logger.info("[Giftia] 3/6 自动清理过期外号未启用，跳过")

        # 4. 聊天记录清理（控制台开关，默认关闭）
        raw_chat_cfg = await self.db.get_kv_data("auto_clean_chat_history_config")
        chat_cfg = self.normalize_auto_clean_chat_history_config(raw_chat_cfg)

        if chat_cfg.get("enabled", False):
            logger.info("[Giftia] 4/6 正在清理聊天记录...")
            try:
                await self.auto_clean_chat_history(config=chat_cfg)
            except Exception as e:
                logger.error(f"[Giftia] 聊天记录自动清理执行异常: {e}")
        else:
            logger.info("[Giftia] 4/6 自动清理聊天记录未启用，跳过")


        # 5. 合并转发记录清理（超过 24h 默认自动清理，无需 UI 配置）
        logger.info("[Giftia] 5/6 正在清理合并转发记录...")
        try:
            deleted_forwards = await self.db.clean_old_forwards(max_age_hours=24)
            logger.info(f"[Giftia] 合并转发记录清理完成，已清理 {deleted_forwards} 条超过 24h 的记录")
        except Exception as e:
            logger.error(f"[Giftia] 合并转发自动清理执行异常: {e}")

        # 6. Token 消耗日志清理
        token_cfg = await self._get_token_clean_cfg()
            
        if token_cfg.get("enabled", True):
            logger.info("[Giftia] 6/6 正在清理 Token 消耗日志...")
            try:
                await self.auto_clean_token_usage()
            except Exception as e:
                logger.error(f"[Giftia] Token 消耗日志自动清理执行异常: {e}")
        else:
            logger.info("[Giftia] 6/6 自动清理 Token 消耗日志未启用，跳过")

            
        logger.info("[Giftia] 统一自动清理流程执行结束。")


    def _build_auto_clean_memory_query(self, cfg: dict) -> tuple[str, list]:
        max_importance = min(int(cfg.get("max_importance", 3)), 7)
        max_hit_count = max(0, int(cfg.get("max_hit_count", 1)))
        min_age_days = max(7, int(cfg.get("min_age_days", 60)))
        last_hit_before_days = max(7, int(cfg.get("last_hit_before_days", 30)))
        include_never_hit = bool(cfg.get("include_never_hit", True))
        max_delete_per_run = max(1, min(200, int(cfg.get("max_delete_per_run", 20))))

        created_cutoff = (
            datetime.now() - timedelta(days=min_age_days)
        ).strftime("%Y-%m-%d %H:%M:%S")
        hit_cutoff = (
            datetime.now() - timedelta(days=last_hit_before_days)
        ).strftime("%Y-%m-%d %H:%M:%S")

        conditions = [
            "COALESCE(importance, 5) <= ?",
            "COALESCE(hit_count, 0) <= ?",
            "datetime(created_at) <= datetime(?)",
        ]
        params = [max_importance, max_hit_count, created_cutoff]

        if include_never_hit:
            conditions.append(
                "((last_hit_at IS NULL OR last_hit_at = '') OR datetime(last_hit_at) <= datetime(?))"
            )
        else:
            conditions.append(
                "last_hit_at IS NOT NULL AND last_hit_at != '' AND datetime(last_hit_at) <= datetime(?)"
            )
        params.append(hit_cutoff)

        sql = f"""
            SELECT memory_id, bot_name, group_or_user_id, text, importance,
                   hit_count, last_hit_at, created_at
            FROM memories
            WHERE {' AND '.join(conditions)}
            ORDER BY COALESCE(importance, 5) ASC,
                     COALESCE(hit_count, 0) ASC,
                     datetime(created_at) ASC
            LIMIT ?
        """
        params.append(max_delete_per_run)
        return sql, params

    async def auto_clean_memories(self) -> dict:
        """自动清理低重要度、低活跃度、足够旧的长期记忆。"""
        try:
            raw_cfg = await self.db.get_kv_data("auto_clean_memory_config")
            cfg = self.normalize_auto_clean_memory_config(raw_cfg)
            sql, params = self._build_auto_clean_memory_query(cfg)

            async with self.db.conn.execute(sql, params) as cursor:
                rows = await cursor.fetchall()

            candidates = [
                {
                    "memory_id": row["memory_id"],
                    "bot_name": row["bot_name"],
                    "group_or_user_id": row["group_or_user_id"],
                    "text": row["text"],
                    "importance": row["importance"],
                    "hit_count": row["hit_count"],
                    "last_hit_at": row["last_hit_at"],
                    "created_at": row["created_at"],
                }
                for row in rows
                if row["memory_id"]
            ]

            deleted_count = 0
            failed_ids = []
            if not self.data_cache:
                return {
                    "status": "error",
                    "message": "自动清理长期记忆失败: data_cache 未初始化",
                }

            for item in candidates:
                memory_id = item["memory_id"]
                try:
                    success = await self.data_cache.delete_memory(memory_id)
                except Exception as e:
                    logger.error(f"[Giftia] 自动清理长期记忆失败 {memory_id}: {e}")
                    success = False

                if success:
                    deleted_count += 1
                else:
                    failed_ids.append(memory_id)

            msg = f"长期记忆自动清理完成，共删除 {deleted_count} 条长期记忆"
            if failed_ids:
                msg += f"，{len(failed_ids)} 条删除失败"
            logger.info(f"[Giftia] {msg}")
            return {
                "status": "success",
                "count": deleted_count,
                "deleted_count": deleted_count,
                "failed_ids": failed_ids,
                "candidates": candidates,
                "message": msg,
            }
        except Exception as e:
            logger.error(f"[Giftia] 自动清理长期记忆失败: {e}")
            return {"status": "error", "message": f"自动清理长期记忆失败: {str(e)}"}

    async def auto_clean_media_cache(self) -> dict:
        """自动清理非表情包类型的超出会话窗口的媒体文件"""
        try:
            from astrbot.core.star.star_tools import StarTools

            # 1. 读取自动清理配置
            cfg = await self._get_media_clean_cfg()

            keep_genres = cfg.get("keep_genres", ["表情包", "sticker"])

            # 2. 查询所有应保留的媒体哈希
            # A. 自定义表情包（stickers 表）
            async with self.db.conn.execute(
                "SELECT sticker_id FROM stickers"
            ) as cursor:
                rows = await cursor.fetchall()
                sticker_hashes = {r["sticker_id"] for r in rows if r["sticker_id"]}

            # B. 媒体转述中属于保留风格类型的哈希
            caption_keep_hashes = set()
            if keep_genres:
                has_unspecified = "" in keep_genres
                specified_genres = [g for g in keep_genres if g != ""]

                conditions = []
                params = []
                if specified_genres:
                    placeholders = ",".join(["?"] * len(specified_genres))
                    if has_unspecified:
                        conditions.append(
                            f"(genre IN ({placeholders}) OR genre IS NULL OR genre = '')"
                        )
                    else:
                        conditions.append(f"genre IN ({placeholders})")
                    params.extend(specified_genres)
                elif has_unspecified:
                    conditions.append("(genre IS NULL OR genre = '')")

                if conditions:
                    conditions.append("caption IS NOT NULL AND caption != ''")
                    sql = f"SELECT hash_val FROM media_caption WHERE {' AND '.join(conditions)}"
                    async with self.db.conn.execute(sql, params) as cursor:
                        rows = await cursor.fetchall()
                        caption_keep_hashes = {
                            r["hash_val"] for r in rows if r["hash_val"]
                        }

            # C. 活跃会话消息引用的媒体哈希
            active_media_hashes = set()
            # 获取所有会话列表
            async with self.db.conn.execute(
                "SELECT DISTINCT bot_name, group_or_user_id FROM chat_history"
            ) as cursor:
                sessions = await cursor.fetchall()

            msg_number = min(
                self.config.get("message_history", {}).get("msg_number", 300), 100
            )

            for session in sessions:
                bot_name = session["bot_name"]
                group_or_user_id = session["group_or_user_id"]
                if not bot_name or not group_or_user_id:
                    continue
                async with self.db.conn.execute(
                    "SELECT media_ids FROM chat_history WHERE bot_name = ? AND group_or_user_id = ? ORDER BY created_at DESC LIMIT ?",
                    (bot_name, group_or_user_id, msg_number),
                ) as cursor:
                    history_rows = await cursor.fetchall()
                    for hr in history_rows:
                        if hr["media_ids"]:
                            try:
                                h_list = json.loads(hr["media_ids"])
                                if isinstance(h_list, list):
                                    active_media_hashes.update(h_list)
                            except Exception:
                                pass

            # D. 查询所有已经有成功转述（caption 非空）的媒体哈希，为了做时间窗口和清理判定
            async with self.db.conn.execute(
                "SELECT hash_val FROM media_caption WHERE caption IS NOT NULL AND caption != ''"
            ) as cursor:
                rows = await cursor.fetchall()
                all_transcribed_hashes = {r["hash_val"] for r in rows if r["hash_val"]}

            # 合并所有保留哈希集
            retain_hashes = sticker_hashes | caption_keep_hashes | active_media_hashes

            # 3. 遍历媒体文件物理缓存执行清理
            cache_dir = StarTools.get_data_dir("astrbot_plugin_giftia") / "media_cache"
            cleaned_count = 0
            freed_bytes = 0

            if cache_dir.exists():
                # 安全时间窗口：保留最近 24 小时修改过的任何已转述文件，未转述仅保留 1 小时
                now = time.time()
                twenty_four_hours_ago = now - 24 * 3600
                one_hour_ago = now - 3600

                for cache_file in cache_dir.iterdir():
                    # 跳过子目录（如 thumbnails）
                    if cache_file.is_dir():
                        continue

                    hash_val = cache_file.name
                    if hash_val not in retain_hashes:
                        try:
                            # 检查文件修改时间
                            mtime = cache_file.stat().st_mtime
                            is_transcribed = hash_val in all_transcribed_hashes
                            safety_limit = (
                                twenty_four_hours_ago
                                if is_transcribed
                                else one_hour_ago
                            )

                            if mtime < safety_limit:
                                file_size = cache_file.stat().st_size
                                cache_file.unlink()
                                cleaned_count += 1
                                freed_bytes += file_size

                                # 删除对应的缩略图
                                thumb_file = cache_dir / "thumbnails" / hash_val
                                if thumb_file.exists():
                                    thumb_file.unlink()

                                # 如果是无有效转述的死媒体记录，同步清理数据库行与内存 LRUCache
                                if not is_transcribed:
                                    await self.db.conn.execute(
                                        "DELETE FROM media_caption WHERE hash_val = ?",
                                        (hash_val,),
                                    )
                                    await self.db.conn.commit()
                                    if self.data_cache:
                                        self.data_cache.caption.pop(hash_val, None)
                        except Exception as e:
                            logger.error(
                                f"[Giftia] 自动清理缓存文件失败 {hash_val}: {e}"
                            )

            msg = f"媒体缓存自动清理完成，共物理删除 {cleaned_count} 个过期媒体文件，释放空间 {freed_bytes} 字节"
            logger.info(f"[Giftia] {msg}")
            return {
                "status": "success",
                "count": cleaned_count,
                "size_bytes": freed_bytes,
                "message": msg,
            }
        except Exception as e:
            logger.error(f"[Giftia] 自动清理媒体缓存失败: {e}")
            return {"status": "error", "message": f"自动清理媒体缓存失败: {str(e)}"}

    def update_auto_clean_token_job(self):
        """兼容层：统一自动清理更新"""
        self.update_auto_cleanup_jobs()

    async def auto_clean_token_usage(self):
        """自动清理过期 Token 日志"""
        try:
            cfg = await self._get_token_clean_cfg()
            
            if cfg.get("enabled", True):
                # 1. 先将昨日及以前的数据拍扁汇总归档
                await self.db.squash_token_logs()
                
                # 2. 清理超过保留天数的历史归档
                days = int(cfg.get("days", 365))
                cleaned_count = await self.db.clear_token_logs(before_days=days)
                logger.info(f"[Giftia] Token 消耗日志自动清理完成，共物理删除 {cleaned_count} 条过期 Token 记录 (保留最近 {days} 天)")
        except Exception as e:
            logger.error(f"[Giftia] 自动清理 Token 记录失败: {e}")
