import asyncio
import json

from .atomic import memory_metadata
from .passive_tasks import PassiveSummaryTaskMixin


class PassiveMemoryManager(PassiveSummaryTaskMixin):
    def __init__(self, plugin):
        self.plugin = plugin
        self.passive_memory_locks: dict[str, asyncio.Lock] = {}
        self.summary_tasks: dict[str, asyncio.Task] = {}
        self.maintenance_lock = asyncio.Lock()
        self.maintenance_tasks: set[asyncio.Task] = set()
        self.registered_sessions = set()
        self.vector_locks: dict[str, asyncio.Lock] = {}
        self._closed = False

    def register_jobs(self) -> None:
        """Register the plugin's periodic memory maintenance job."""
        self.plugin.task_manager.register_func(
            "memory_maintenance", self.maintain_memories
        )
        self.plugin.task_manager.add_job(
            task_id="system_memory_maintenance",
            func_name="memory_maintenance",
            time_expr="*/5 * * * *",
        )

    async def close(self) -> None:
        """Cancel and await memory workers before their resources are closed."""
        self._closed = True
        tasks = [
            *self.summary_tasks.values(),
            *self.maintenance_tasks,
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.summary_tasks.clear()

    async def _remember_session(
        self, bot_name: str, session_id: str, self_id: str
    ) -> None:
        """Persist the observer identity needed by future maintenance runs.

        Args:
            bot_name: Owning bot configuration.
            session_id: Owning conversation.
            self_id: Observing bot account.
        """
        key = f"{bot_name}:{session_id}"
        if key not in self.registered_sessions:
            await self.plugin.db.upsert_kv_data(
                f"atomic_memory:session:{key}",
                json.dumps(
                    {
                        "bot_name": bot_name,
                        "session_id": session_id,
                        "self_id": str(self_id),
                    }
                ),
            )
            self.registered_sessions.add(key)

    async def _summarize_session(
        self, bot_name, session_id, self_id, start_id, end_id
    ) -> None:
        """Serialize a background summary within its conversation.

        Args:
            bot_name: Owning bot.
            session_id: Owning conversation.
            self_id: Observing bot account.
            start_id: First pending source row.
            end_id: Last observed source row.
        """
        key = f"{bot_name}:{session_id}"
        try:
            async with self.passive_memory_locks[key]:
                await self._run_background_summarize(
                    bot_name, session_id, self_id, start_id, end_id
                )
        finally:
            self.summary_tasks.pop(key, None)

    async def observe_messages(
        self, bot_name: str, session_id: str, self_id: str, messages: list
    ) -> None:
        """Queue the exact message snapshot submitted to a decision or reply model.

        Args:
            bot_name: Owning bot configuration.
            session_id: Owning conversation.
            self_id: Observing bot account.
            messages: Frozen history and current messages included in the request.
        """
        if self._closed or not self.plugin.passive_memory_enabled:
            return
        # Register the route first so a crash cannot strand newly queued messages.
        await self._remember_session(bot_name, session_id, self_id)
        await self.plugin.db.chat_history_repo.mark_messages_observed(
            bot_name, session_id, [msg.message_id for msg in messages]
        )

    async def reset_silence_counter(self, bot_name: str, session_id: str) -> None:
        """Restart silence counting after bot output without changing source eligibility.

        Args:
            bot_name: Owning bot configuration.
            session_id: Owning conversation.
        """
        if not self._closed and self.plugin.passive_memory_enabled:
            await self.plugin.db.upsert_kv_data(
                f"passive_memory:silent_count:{bot_name}:{session_id}", 0
            )

    async def search_and_filter_memories(
        self,
        bot_name: str,
        group_or_user_id: str,
        query: str,
        recent_messages: list = None,
        limit: int = 5,
        threshold: float = 0.7,
        filter_active_users: bool = True,
    ) -> list[dict]:
        """Recall indexed factual memories from the current conversation.

        Args:
            bot_name: Owning bot configuration.
            group_or_user_id: Owning conversation.
            query: Semantic recall query.
            recent_messages: Messages defining the active participants.
            limit: Maximum vector candidates.
            threshold: Maximum accepted vector distance.
            filter_active_users: Limit automatic recall to current participants.

        Returns:
            Factual memories with matching index revisions.
        """
        async with self.plugin.db.conn.execute(
            "SELECT memory_id FROM memories WHERE bot_name=? AND group_or_user_id=? "
            "AND revision=indexed_revision",
            (bot_name, group_or_user_id),
        ) as cursor:
            eligible_ids = [row[0] for row in await cursor.fetchall()]
        if not eligible_ids:
            return []
        embedding_memories = await self.plugin.ltm.search_memory(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            query=query,
            limit=limit,
            threshold=threshold,
            include_ids=eligible_ids,
        )
        if not embedding_memories:
            return []

        ids = [str(memory.get("id") or "") for memory in embedding_memories]
        placeholders = ",".join("?" for _ in ids)
        async with self.plugin.db.conn.execute(
            "SELECT memory_id, text, metadata, created_at, updated_at "
            "FROM memories WHERE bot_name=? AND group_or_user_id=? "
            f"AND memory_id IN ({placeholders}) AND revision=indexed_revision",
            (bot_name, group_or_user_id, *ids),
        ) as cursor:
            current = {row["memory_id"]: dict(row) for row in await cursor.fetchall()}
        recalled = []
        for memory in embedding_memories:
            row = current.get(memory.get("id"))
            if row is None:
                continue
            metadata = memory_metadata(row)
            recalled.append(
                {
                    **memory,
                    **row,
                    "metadata": json.dumps(metadata, ensure_ascii=False),
                }
            )
        embedding_memories = recalled

        if not filter_active_users:
            return embedding_memories

        if recent_messages is None:
            recent_messages = await self.plugin.data_cache.get_recent_message(
                bot_name=bot_name,
                group_id=group_or_user_id,
                limit=self.plugin.msg_number,
            )

        active_users = {str(msg.user_id) for msg in recent_messages if msg.user_id}

        filtered_memories = []
        for memory in embedding_memories:
            metadata_str = memory.get("metadata", "{}")
            try:
                meta = json.loads(metadata_str) if metadata_str else {}
            except Exception:
                meta = {}

            associated_ids = meta.get("associated_user_ids", [])
            if not associated_ids:
                filtered_memories.append(memory)
                continue

            associated_ids_str = {str(uid) for uid in associated_ids}
            if associated_ids_str & active_users:
                filtered_memories.append(memory)

        return filtered_memories

    async def check_and_trigger_passive_memory(
        self,
        bot_name: str,
        group_or_user_id: str,
        self_id: str,
    ) -> None:
        """Schedule one pending source interval without advancing its cursor.

        Args:
            bot_name: Owning bot configuration.
            group_or_user_id: Owning conversation.
            self_id: Observing bot account.
        """
        if self._closed or not self.plugin.passive_memory_enabled:
            return
        key = f"{bot_name}:{group_or_user_id}"
        if key in self.summary_tasks:
            return
        lock = self.passive_memory_locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            repo = self.plugin.db.chat_history_repo
            first_id, last_id, pending_count = await repo.get_pending_memory_stats(
                bot_name, group_or_user_id
            )
            if not pending_count:
                return
            active = self.plugin.active_reply_counters.get(key, 0)
            boundary = await self.plugin.db.get_boundary_message_id(
                bot_name, group_or_user_id, self.plugin.msg_number
            )
            end_id = 0
            if boundary > first_id:
                _, overflow_end, overflow = await repo.get_pending_memory_stats(
                    bot_name, group_or_user_id, end_id=boundary - 1
                )
                if overflow >= self.plugin.passive_memory_overflow_threshold:
                    end_id = overflow_end
            if not end_id and not active:
                silent_count = (
                    int(
                        await self.plugin.db.get_kv_data(
                            f"passive_memory:silent_count:{key}", 0
                        )
                    )
                    + 1
                )
                await self.plugin.db.upsert_kv_data(
                    f"passive_memory:silent_count:{key}", silent_count
                )
                if silent_count >= self.plugin.passive_memory_silence_threshold:
                    end_id = last_id
            elif active:
                await self.plugin.db.upsert_kv_data(
                    f"passive_memory:silent_count:{key}", 0
                )
            if end_id:
                self.summary_tasks[key] = asyncio.create_task(
                    self._summarize_session(
                        bot_name, group_or_user_id, str(self_id), first_id, end_id
                    )
                )

    async def force_trigger_passive_memory(
        self,
        bot_name: str,
        group_or_user_id: str,
        self_id: str,
    ) -> str:
        """Summarize one bounded batch and report its durable outcome.

        Args:
            bot_name: Owning bot configuration.
            group_or_user_id: Owning conversation.
            self_id: Observing bot account.

        Returns:
            User-facing success, failure, or busy status.
        """
        if not self.plugin.passive_memory_enabled:
            return "被动记忆功能未启用"
        if self._closed:
            return "记忆服务已停止"
        current = asyncio.current_task()
        self.maintenance_tasks.add(current)
        try:
            key = f"{bot_name}:{group_or_user_id}"
            lock = self.passive_memory_locks.setdefault(key, asyncio.Lock())
            if lock.locked() or key in self.summary_tasks:
                return "当前会话正在进行记忆维护，请稍后再试。"
            async with lock:
                (
                    first_id,
                    last_id,
                    pending,
                ) = await self.plugin.db.chat_history_repo.get_pending_memory_stats(
                    bot_name, group_or_user_id
                )
                if not pending:
                    return "当前会话暂无已观察且未总结的消息。"
                success = await self._run_background_summarize(
                    bot_name, group_or_user_id, str(self_id), first_id, last_id
                )
                if not success:
                    return "记忆归纳失败，未推进处理进度，可稍后重试。"
                return "已归纳一批已观察消息；剩余消息会继续分批处理。"
        finally:
            self.maintenance_tasks.discard(current)
