import aiosqlite

from astrbot.api import logger
from astrbot.api.star import StarTools

from .profile_store import ProfileStoreMixin
from .schema import initialize_database

from ..utils.schemas import (
    MediaCaption,
    MemoryItem,
    MessageData,
    ShortTask,
    Status,
    Sticker,
)

from .repositories.chat_history import ChatHistoryRepository
from .repositories.forwarded_messages import ForwardedMessagesRepository
from .repositories.media_captions import MediaCaptionsRepository
from .repositories.short_tasks import ShortTasksRepository
from .repositories.bot_status import BotStatusRepository
from .repositories.memories import MemoriesRepository
from .repositories.kv_store import KVStoreRepository
from .repositories.stickers import StickersRepository
from .repositories.token_usage import TokenUsageRepository


class Database(ProfileStoreMixin):
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn
        self.db_path = (
            StarTools.get_data_dir("astrbot_plugin_giftia") / "chat_history.db"
        )

        # Instantiate repositories
        self.chat_history_repo = ChatHistoryRepository(conn)
        self.forwarded_messages_repo = ForwardedMessagesRepository(conn, self.chat_history_repo)
        self.media_captions_repo = MediaCaptionsRepository(conn)
        self.short_tasks_repo = ShortTasksRepository(conn)
        self.bot_status_repo = BotStatusRepository(conn)
        self.memories_repo = MemoriesRepository(conn)
        self.kv_store_repo = KVStoreRepository(conn)
        self.stickers_repo = StickersRepository(conn)
        self.token_usage_repo = TokenUsageRepository(conn)

    @classmethod
    async def connect(cls):
        db_path = StarTools.get_data_dir("astrbot_plugin_giftia") / "chat_history.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        await initialize_database(conn)
        return cls(conn)

    # =========================================================================
    # Chat History Delegations
    # =========================================================================
    async def insert_message(self, bot_name: str, message: MessageData):
        return await self.chat_history_repo.insert_message(bot_name, message)

    async def get_messages(self, group_or_user_id: str, bot_name: str, limit: int = 100) -> list[MessageData]:
        return await self.chat_history_repo.get_messages(group_or_user_id, bot_name, limit)

    async def get_max_message_id(self, bot_name: str, group_or_user_id: str) -> int:
        return await self.chat_history_repo.get_max_message_id(bot_name, group_or_user_id)

    async def get_messages_by_id_range(
        self, bot_name: str, group_or_user_id: str, start_id: int, end_id: int
    ) -> list[MessageData]:
        return await self.chat_history_repo.get_messages_by_id_range(bot_name, group_or_user_id, start_id, end_id)

    async def get_user_messages_after_id(
        self, bot_name: str, group_or_user_id: str, user_id: str, after_id: int, limit: int = 200
    ) -> list[MessageData]:
        return await self.chat_history_repo.get_user_messages_after_id(bot_name, group_or_user_id, user_id, after_id, limit)

    async def get_message_count_by_id_range(
        self, bot_name: str, group_or_user_id: str, start_id: int, end_id: int
    ) -> int:
        return await self.chat_history_repo.get_message_count_by_id_range(bot_name, group_or_user_id, start_id, end_id)

    async def get_boundary_message_id(self, bot_name: str, group_or_user_id: str, offset: int) -> int:
        return await self.chat_history_repo.get_boundary_message_id(bot_name, group_or_user_id, offset)

    async def get_message_by_id(
        self, message_id: str, group_or_user_id: str, bot_name: str
    ) -> MessageData | None:
        return await self.chat_history_repo.get_message_by_id(message_id, group_or_user_id, bot_name)

    async def get_database_id_by_message_id(
        self, message_id: str, group_or_user_id: str, bot_name: str
    ) -> int | None:
        return await self.chat_history_repo.get_database_id_by_message_id(message_id, group_or_user_id, bot_name)

    async def search_messages(
        self,
        group_or_user_id: str,
        bot_name: str,
        user_id: str | None = None,
        keyword: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        sort_order: str = "desc",
        limit: int = 100,
    ) -> list[MessageData]:
        return await self.chat_history_repo.search_messages(
            group_or_user_id, bot_name, user_id, keyword, start_time, end_time, sort_order, limit
        )

    async def get_message_context(
        self, message_id: str, group_or_user_id: str, bot_name: str, limit: int = 30
    ) -> list[MessageData]:
        return await self.chat_history_repo.get_message_context(message_id, group_or_user_id, bot_name, limit)

    async def delete_chat_history(self, bot_name: str, group_or_user_id: str):
        return await self.chat_history_repo.delete_chat_history(bot_name, group_or_user_id)

    async def auto_clean_chat_history(
        self,
        max_count_per_session: int = 1000,
        max_age_days: int = 30,
        min_keep_per_session: int = 20,
    ) -> int:
        return await self.chat_history_repo.auto_clean_chat_history(
            max_count_per_session=max_count_per_session,
            max_age_days=max_age_days,
            min_keep_per_session=min_keep_per_session,
        )


    async def update_message_decision(
        self, bot_name: str, group_or_user_id: str, message_id: str, reply_decision: int, use_rag: int
    ):
        return await self.chat_history_repo.update_message_decision(
            bot_name, group_or_user_id, message_id, reply_decision, use_rag
        )

    async def update_message_reply_decision(
        self, bot_name: str, group_or_user_id: str, message_id: str, reply_decision: int
    ):
        return await self.chat_history_repo.update_message_reply_decision(
            bot_name, group_or_user_id, message_id, reply_decision
        )

    async def update_message_recall(
        self, bot_name: str, group_or_user_id: str, message_ids: list[str], is_recalled: int
    ):
        return await self.chat_history_repo.update_message_recall(bot_name, group_or_user_id, message_ids, is_recalled)

    async def delete_message(
        self,
        bot_name: str,
        group_or_user_id: str,
        message_id: str,
    ) -> bool:
        return await self.chat_history_repo.delete_message(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            message_id=message_id,
        )

    async def delete_message_by_db_id(self, message_db_id: int):
        return await self.chat_history_repo.delete_message_by_db_id(message_db_id)

    # =========================================================================
    # Forwarded Messages Delegations
    # =========================================================================
    async def find_forward_message_by_id(
        self, group_or_user_id: str, bot_name: str, forward_id: str, limit: int = 50
    ) -> tuple[MessageData | None, dict | None]:
        return await self.forwarded_messages_repo.find_forward_message_by_id(
            group_or_user_id, bot_name, forward_id, limit
        )

    async def get_forward_summary(self, bot_name: str, group_or_user_id: str, forward_id: str) -> str | None:
        return await self.forwarded_messages_repo.get_forward_summary(bot_name, group_or_user_id, forward_id)

    async def update_forward_summary(self, bot_name: str, group_or_user_id: str, forward_id: str, summary: str):
        return await self.forwarded_messages_repo.update_forward_summary(bot_name, group_or_user_id, forward_id, summary)

    async def increment_forward_query_times(self, bot_name: str, group_or_user_id: str, forward_id: str):
        return await self.forwarded_messages_repo.increment_forward_query_times(bot_name, group_or_user_id, forward_id)

    async def clean_old_forwards(self, max_age_hours: int = 24) -> int:
        return await self.forwarded_messages_repo.clean_old_forwards(max_age_hours)


    # =========================================================================
    # Media Captions Delegations
    # =========================================================================
    async def insert_media_caption(self, media_caption: MediaCaption):
        return await self.media_captions_repo.insert_media_caption(media_caption)

    async def get_media_caption_by_hash(self, hash_val: str) -> MediaCaption | None:
        return await self.media_captions_repo.get_media_caption_by_hash(hash_val)

    async def get_media_caption_by_filename(self, file_name: str) -> MediaCaption | None:
        return await self.media_captions_repo.get_media_caption_by_filename(file_name)

    async def update_media_caption(self, media_caption: MediaCaption):
        return await self.media_captions_repo.update_media_caption(media_caption)

    async def increment_media_query_times(self, hash_val: str):
        return await self.media_captions_repo.increment_media_query_times(hash_val)

    async def update_media_url(self, hash_val: str, url: str):
        return await self.media_captions_repo.update_media_url(hash_val, url)

    async def clear_media_caption(self):
        return await self.media_captions_repo.clear_media_caption()

    # =========================================================================
    # Short Tasks Delegations
    # =========================================================================
    async def insert_short_task(self, task: ShortTask) -> None:
        return await self.short_tasks_repo.insert_short_task(task)

    async def expire_short_tasks(self, bot_name: str | None = None, group_or_user_id: str | None = None) -> int:
        return await self.short_tasks_repo.expire_short_tasks(bot_name, group_or_user_id)

    async def get_short_tasks(
        self, bot_name: str, group_or_user_id: str, statuses: list[str] | None = None, limit: int | None = None
    ) -> list[ShortTask]:
        return await self.short_tasks_repo.get_short_tasks(bot_name, group_or_user_id, statuses, limit)

    async def get_short_task(self, task_id: str, bot_name: str, group_or_user_id: str) -> ShortTask | None:
        return await self.short_tasks_repo.get_short_task(task_id, bot_name, group_or_user_id)

    async def count_active_short_tasks(self, bot_name: str, group_or_user_id: str) -> int:
        return await self.short_tasks_repo.count_active_short_tasks(bot_name, group_or_user_id)

    async def update_short_task_status(
        self, task_id: str, bot_name: str, group_or_user_id: str, status: str, closed_by_user_id: str = "", close_reason: str = ""
    ) -> bool:
        return await self.short_tasks_repo.update_short_task_status(
            task_id, bot_name, group_or_user_id, status, closed_by_user_id, close_reason
        )

    async def update_short_task(
        self, task_id: str, bot_name: str, group_or_user_id: str, content: str, status: str, expires_at: str, closed_by_user_id: str = "", close_reason: str = ""
    ) -> bool:
        return await self.short_tasks_repo.update_short_task(
            task_id, bot_name, group_or_user_id, content, status, expires_at, closed_by_user_id, close_reason
        )

    async def delete_short_task(self, task_id: str, bot_name: str, group_or_user_id: str) -> bool:
        return await self.short_tasks_repo.delete_short_task(task_id, bot_name, group_or_user_id)

    async def get_short_task_stats(self, bot_name: str, group_or_user_id: str) -> dict:
        return await self.short_tasks_repo.get_short_task_stats(bot_name, group_or_user_id)

    # =========================================================================
    # Bot Status Delegations
    # =========================================================================
    async def get_bot_status(self, group_or_user_id: str, bot_name: str) -> Status:
        return await self.bot_status_repo.get_bot_status(group_or_user_id, bot_name)

    async def upsert_bot_status(self, group_or_user_id: str, bot_name: str, status: Status):
        return await self.bot_status_repo.upsert_bot_status(group_or_user_id, bot_name, status)

    async def delete_bot_status(self, group_or_user_id: str, bot_name: str):
        return await self.bot_status_repo.delete_bot_status(group_or_user_id, bot_name)

    # =========================================================================
    # Memories Delegations
    # =========================================================================
    async def insert_memory(self, bot_name: str, group_or_user_id: str, memory: MemoryItem):
        return await self.memories_repo.insert_memory(bot_name, group_or_user_id, memory)

    async def get_memories(self, group_or_user_id: str, bot_name: str, limit: int = 10) -> list[MemoryItem]:
        return await self.memories_repo.get_memories(group_or_user_id, bot_name, limit)

    async def record_memory_hits(self, memory_ids: list[str], hit_at: str | None = None):
        return await self.memories_repo.record_memory_hits(memory_ids, hit_at)

    async def delete_memory(self, memory_id: str):
        return await self.memories_repo.delete_memory(memory_id)

    async def delete_all_memories(self, bot_name: str, group_or_user_id: str):
        return await self.memories_repo.delete_all_memories(bot_name, group_or_user_id)

    # =========================================================================
    # KV Store Delegations
    # =========================================================================
    async def get_kv_data(self, key: str, default=None):
        return await self.kv_store_repo.get_kv_data(key, default)

    async def upsert_kv_data(self, key: str, value: str | int | float | bool):
        return await self.kv_store_repo.upsert_kv_data(key, value)

    async def delete_kv_data(self, key: str):
        return await self.kv_store_repo.delete_kv_data(key)

    # =========================================================================
    # Stickers Delegations
    # =========================================================================
    async def insert_sticker(
        self, sticker_id: str, name: str, category: str, tags: list[str], description: str, filename: str = ""
    ):
        return await self.stickers_repo.insert_sticker(sticker_id, name, category, tags, description, filename)

    async def get_sticker(self) -> list[Sticker]:
        return await self.stickers_repo.get_sticker()

    async def delete_sticker(self, sticker_id: str):
        return await self.stickers_repo.delete_sticker(sticker_id)

    async def insert_sticker_bot(self, sticker_id: str, bot_name: str):
        return await self.stickers_repo.insert_sticker_bot(sticker_id, bot_name)

    async def get_sticker_categories(self) -> list[str]:
        return await self.stickers_repo.get_sticker_categories()

    async def get_sticker_bot(self, bot_name: str) -> list[str]:
        return await self.stickers_repo.get_sticker_bot(bot_name)

    async def update_sticker(
        self, sticker_id: str, name: str, category: str, tags: list[str], description: str
    ) -> bool:
        return await self.stickers_repo.update_sticker(sticker_id, name, category, tags, description)

    async def get_sticker_by_id(self, sticker_id: str) -> Sticker | None:
        return await self.stickers_repo.get_sticker_by_id(sticker_id)

    async def query_stickers(
        self,
        page: int = 1,
        limit: int = 12,
        category: str = "",
        tag: str = "",
        search: str = "",
        sticker_ids: list[str] | None = None,
        sort: str = "created_desc",
    ) -> tuple[list[dict], int]:
        return await self.stickers_repo.query_stickers(
            page, limit, category, tag, search, sticker_ids, sort
        )

    async def delete_sticker_bot(self, sticker_id: str, bot_name: str) -> bool:
        return await self.stickers_repo.delete_sticker_bot(sticker_id, bot_name)

    async def remove_sticker_from_all_bots(self, sticker_id: str) -> list[str]:
        return await self.stickers_repo.remove_sticker_from_all_bots(sticker_id)

    async def get_all_bot_sticker_map(self) -> dict[str, list[str]]:
        return await self.stickers_repo.get_all_bot_sticker_map()

    async def get_all_sticker_tags(self) -> list[str]:
        return await self.stickers_repo.get_all_sticker_tags()

    async def get_sticker_category_stats(self) -> list[dict]:
        return await self.stickers_repo.get_category_stats()

    async def get_sticker_tag_stats(self) -> list[dict]:
        return await self.stickers_repo.get_tag_stats()

    async def rename_sticker_category(self, old_name: str, new_name: str) -> int:
        return await self.stickers_repo.rename_category(old_name, new_name)

    async def rename_sticker_tag(self, old_tag: str, new_tag: str) -> int:
        return await self.stickers_repo.rename_tag(old_tag, new_tag)

    async def delete_sticker_tag(self, tag: str) -> int:
        return await self.stickers_repo.delete_tag(tag)

    async def batch_update_sticker_category(self, sticker_ids: list[str], category: str) -> int:
        return await self.stickers_repo.batch_update_category(sticker_ids, category)

    async def batch_add_sticker_tags(self, sticker_ids: list[str], tags: list[str]) -> int:
        return await self.stickers_repo.batch_add_tags(sticker_ids, tags)

    async def batch_remove_sticker_tags(self, sticker_ids: list[str], tags: list[str]) -> int:
        return await self.stickers_repo.batch_remove_tags(sticker_ids, tags)

    # =========================================================================
    # Token Usage Delegations
    # =========================================================================
    async def log_token_usage(
        self, bot_name: str, group_or_user_id: str, type: str, provider_id: str, model_name: str,
        prompt_tokens: int, completion_tokens: int, total_tokens: int, extra_info: dict | None = None
    ):
        return await self.token_usage_repo.log_token_usage(
            bot_name, group_or_user_id, type, provider_id, model_name,
            prompt_tokens, completion_tokens, total_tokens, extra_info
        )

    async def get_token_stats(self, bot_name: str = None, group_or_user_id: str = None, time_range: str = None) -> dict:
        return await self.token_usage_repo.get_token_stats(bot_name, group_or_user_id, time_range)

    async def get_token_logs(
        self, page: int = 1, page_size: int = 20, type: str = None, bot_name: str = None, group_or_user_id: str = None, time_range: str = None
    ) -> tuple[list[dict], int]:
        return await self.token_usage_repo.get_token_logs(page, page_size, type, bot_name, group_or_user_id, time_range)

    async def squash_token_logs(self) -> int:
        return await self.token_usage_repo.squash_token_logs()

    async def clear_token_logs(
        self, before_days: int = None, bot_name: str = None, group_or_user_id: str = None, time_range: str = None
    ) -> int:
        return await self.token_usage_repo.clear_token_logs(before_days, bot_name, group_or_user_id, time_range)

    # =========================================================================
    # Misc Maintenance (Retained in main database class)
    # =========================================================================
    async def backup_chat_history_db(self, last_backup_time: float):
        """备份数据库，VACUUM INTO"""
        wal_path = self.db_path.with_name(f"{self.db_path.name}-wal")
        current_mtime = self.db_path.stat().st_mtime
        if wal_path.exists():
            current_mtime = max(current_mtime, wal_path.stat().st_mtime)

        if current_mtime <= last_backup_time:
            logger.debug("数据库自上次备份以来无变动，跳过备份。")
            return None

        backup_temp_path = (
            StarTools.get_data_dir("astrbot_plugin_giftia") / "chat_history_backup.db"
        )
        try:
            if backup_temp_path.exists():
                backup_temp_path.unlink()
            safe_path = backup_temp_path.as_posix()
            async with aiosqlite.connect(self.db_path) as backup_conn:
                await backup_conn.execute(f"VACUUM INTO '{safe_path}'")
            logger.info(f"本地临时备份已创建: {backup_temp_path}")
            return backup_temp_path
        except Exception as e:
            logger.error(f"创建本地备份临时文件失败: {e}")
            return None

    async def close(self):
        if self.conn:
            await self.conn.close()
