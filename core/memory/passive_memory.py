import asyncio
import json

from astrbot.api import logger

from .passive_tasks import PassiveSummaryTaskMixin


class PassiveMemoryManager(PassiveSummaryTaskMixin):
    def __init__(self, plugin):
        self.plugin = plugin
        self.passive_memory_locks: dict[str, asyncio.Lock] = {}
        self.summary_tasks: dict[str, asyncio.Task] = {}
        self.consolidation_tasks: dict[str, asyncio.Task] = {}
        self.consolidation_locks: dict[str, asyncio.Lock] = {}
        self.consolidation_wakeups: set[str] = set()
        self.maintenance_lock = asyncio.Lock()
        self.maintenance_tasks: set[asyncio.Task] = set()
        self.registered_sessions = set()
        self.activity_versions: dict[str, int] = {}
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
            *self.consolidation_tasks.values(),
            *self.maintenance_tasks,
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.summary_tasks.clear()
        self.consolidation_tasks.clear()
        self.consolidation_wakeups.clear()

    def _schedule_memory_consolidation(
        self, bot_name: str, session_id: str, self_id: str
    ) -> None:
        """Start one independent consumer for durable new-memory jobs.

        Args:
            bot_name: Owning bot configuration.
            session_id: Owning conversation.
            self_id: Observing bot account.
        """
        key = f"{bot_name}:{session_id}"
        if self._closed:
            return
        if key in self.consolidation_tasks:
            self.consolidation_wakeups.add(key)
            return
        self.consolidation_tasks[key] = asyncio.create_task(
            self._consolidate_session(bot_name, session_id, self_id)
        )

    async def _consolidate_session(
        self, bot_name: str, session_id: str, self_id: str
    ) -> None:
        """Consume queued additions without rerunning extraction or scanning history.

        Args:
            bot_name: Owning bot configuration.
            session_id: Owning conversation.
            self_id: Observing bot account.
        """
        key = f"{bot_name}:{session_id}"
        lock = self.consolidation_locks.setdefault(key, asyncio.Lock())
        try:
            while not self._closed:
                self.consolidation_wakeups.discard(key)
                async with lock:
                    handled = await self.consolidate_memories(
                        bot_name, session_id, self_id
                    )
                if not handled and key not in self.consolidation_wakeups:
                    break
                await asyncio.sleep(0)
        except Exception:
            logger.exception("[Giftia Memory] Consolidation worker failed for %s", key)
        finally:
            self.consolidation_tasks.pop(key, None)
            self.consolidation_wakeups.discard(key)

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

    async def mark_silence_summary_armed(
        self, bot_name: str, group_or_user_id: str, trigger_msg_id: str = None
    ) -> None:
        """Arm silent-session extraction without skipping pending source messages.

        Args:
            bot_name: Owning bot configuration.
            group_or_user_id: Owning conversation.
            trigger_msg_id: Optional message waking a previously idle bot.
        """
        if not self.plugin.passive_memory_enabled:
            return
        fmt_key = f"{bot_name}:{group_or_user_id}"
        self.activity_versions[fmt_key] = self.activity_versions.get(fmt_key, 0) + 1
        lock = self.passive_memory_locks.setdefault(fmt_key, asyncio.Lock())
        can_skip = bool(
            trigger_msg_id and fmt_key not in self.summary_tasks and not lock.locked()
        )
        # Serialize an idle cursor advance with extraction. Busy sessions still
        # record activity immediately without waiting for a model call to finish.
        if can_skip:
            await lock.acquire()
        try:
            was_armed = await self.plugin.db.get_kv_data(
                f"passive_memory:silence_armed:{fmt_key}", 0
            )
            await self.plugin.db.upsert_kv_data(
                f"passive_memory:silence_armed:{fmt_key}", 1
            )
            await self.plugin.db.upsert_kv_data(
                f"passive_memory:silent_count:{fmt_key}", 0
            )
            # An armed session may have failed or unfinished batches. A wake-up
            # may skip unobserved history only when no such work is pending.
            if can_skip and not was_armed:
                db_msg_id = await self.plugin.db.get_database_id_by_message_id(
                    message_id=trigger_msg_id,
                    group_or_user_id=group_or_user_id,
                    bot_name=bot_name,
                )
                if db_msg_id:
                    last_summarized_id = await self.plugin.db.get_kv_data(
                        f"passive_memory:last_summarized_id:{fmt_key}", 0
                    )
                    if db_msg_id - 1 > last_summarized_id:
                        logger.info(
                            "[Giftia Passive Memory] Skipping unobserved history for %s: cursor %s -> %s",
                            fmt_key,
                            last_summarized_id,
                            db_msg_id - 1,
                        )
                        await self.plugin.db.upsert_kv_data(
                            f"passive_memory:last_summarized_id:{fmt_key}",
                            db_msg_id - 1,
                        )
        finally:
            if can_skip:
                lock.release()

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
        """Search vectors and recheck authoritative validity before recall.

        Args:
            bot_name: Owning bot configuration.
            group_or_user_id: Owning conversation.
            query: Semantic recall query.
            recent_messages: Messages defining the active participants.
            limit: Maximum vector candidates.
            threshold: Maximum accepted vector distance.
            filter_active_users: Limit automatic recall to current participants.

        Returns:
            Current active memories with matching index revisions.
        """
        embedding_memories = await self.plugin.ltm.search_memory(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            query=query,
            limit=limit,
            threshold=threshold,
        )
        if not embedding_memories:
            return []

        ids = [str(memory.get("id") or "") for memory in embedding_memories]
        placeholders = ",".join("?" for _ in ids)
        async with self.plugin.db.conn.execute(
            f"SELECT memory_id, text, metadata FROM memories WHERE bot_name=? AND group_or_user_id=? "
            f"AND memory_id IN ({placeholders}) AND status='active' AND revision=indexed_revision "
            "AND (expires_at IS NULL OR julianday(expires_at)>julianday('now'))",
            (bot_name, group_or_user_id, *ids),
        ) as cursor:
            current = {row["memory_id"]: dict(row) for row in await cursor.fetchall()}
        embedding_memories = [
            {
                **memory,
                "text": current[memory["id"]]["text"],
                "metadata": current[memory["id"]]["metadata"],
            }
            for memory in embedding_memories
            if memory.get("id") in current
        ]

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
        self.activity_versions[key] = self.activity_versions.get(key, 0) + 1
        await self._remember_session(bot_name, group_or_user_id, self_id)
        if key in self.summary_tasks:
            await self.plugin.db.upsert_kv_data(
                f"passive_memory:silence_armed:{key}", 1
            )
            return
        lock = self.passive_memory_locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            await self.plugin.db.upsert_kv_data(
                f"passive_memory:silence_armed:{key}", 1
            )
            return
        async with lock:
            active = self.plugin.active_reply_counters.get(key, 0)
            armed = await self.plugin.db.get_kv_data(
                f"passive_memory:silence_armed:{key}", 0
            )
            if not active and not armed:
                return
            max_id = await self.plugin.db.get_max_message_id(bot_name, group_or_user_id)
            cursor = int(
                await self.plugin.db.get_kv_data(
                    f"passive_memory:last_summarized_id:{key}", 0
                )
            )
            if cursor > max_id:
                cursor = max_id
                await self.plugin.db.upsert_kv_data(
                    f"passive_memory:last_summarized_id:{key}", cursor
                )
            if not cursor and not armed:
                await self.plugin.db.upsert_kv_data(
                    f"passive_memory:last_summarized_id:{key}", max_id
                )
                return
            if max_id <= cursor:
                return
            boundary = await self.plugin.db.get_boundary_message_id(
                bot_name, group_or_user_id, self.plugin.msg_number
            )
            end_id = 0
            if boundary > cursor:
                overflow = await self.plugin.db.get_message_count_by_id_range(
                    bot_name, group_or_user_id, cursor + 1, boundary
                )
                if overflow >= self.plugin.passive_memory_overflow_threshold:
                    end_id = boundary
            if not end_id and not active and armed:
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
                    end_id = max_id
            elif active:
                await self.plugin.db.upsert_kv_data(
                    f"passive_memory:silent_count:{key}", 0
                )
            if end_id:
                self.summary_tasks[key] = asyncio.create_task(
                    self._summarize_session(
                        bot_name, group_or_user_id, str(self_id), cursor + 1, end_id
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
            await self._remember_session(bot_name, group_or_user_id, self_id)
            key = f"{bot_name}:{group_or_user_id}"
            lock = self.passive_memory_locks.setdefault(key, asyncio.Lock())
            if lock.locked() or key in self.summary_tasks:
                return "当前会话正在进行记忆维护，请稍后再试。"
            async with lock:
                maximum = await self.plugin.db.get_max_message_id(
                    bot_name, group_or_user_id
                )
                cursor_key = f"passive_memory:last_summarized_id:{key}"
                cursor = int(await self.plugin.db.get_kv_data(cursor_key, 0))
                if cursor > maximum:
                    cursor = maximum
                    await self.plugin.db.upsert_kv_data(cursor_key, cursor)
                if maximum <= cursor:
                    return "当前会话暂无未总结的消息。"
                success = await self._run_background_summarize(
                    bot_name, group_or_user_id, str(self_id), cursor + 1, maximum
                )
                if not success:
                    return "记忆归纳失败，未推进处理进度，可稍后重试。"
                processed_end = await self.plugin.db.get_kv_data(cursor_key, cursor)
                return f"已归纳消息 id {cursor + 1} 到 {processed_end}；剩余消息会继续分批处理。"
        finally:
            self.maintenance_tasks.discard(current)

    async def force_consolidate_memories(
        self, bot_name: str, session_id: str, self_id: str
    ) -> str:
        """Retry a queued new-memory batch on explicit request.

        Args:
            bot_name: Owning bot.
            session_id: Owning conversation.
            self_id: Observing bot account.

        Returns:
            User-facing maintenance outcome.
        """
        if self._closed or not self.plugin.passive_memory_enabled:
            return "记忆维护未启用。"
        current = asyncio.current_task()
        self.maintenance_tasks.add(current)
        try:
            await self._remember_session(bot_name, session_id, self_id)
            key = f"{bot_name}:{session_id}"
            lock = self.consolidation_locks.setdefault(key, asyncio.Lock())
            if lock.locked() or key in self.consolidation_tasks:
                return "当前会话正在进行记忆维护，请稍后再试。"
            async with lock:
                if not await self.plugin.db.memory_maintenance_repo.pending_consolidation(
                    bot_name, session_id
                ):
                    return "当前会话暂无待整理的新增记忆批次。"
                if await self.consolidate_memories(bot_name, session_id, str(self_id)):
                    self._schedule_memory_consolidation(
                        bot_name, session_id, str(self_id)
                    )
                    return "已处理一批新增记忆；无相关历史记忆的条目已跳过整理，原始来源保留。"
                return "记忆整理失败，原记录与进度保留，可稍后重试。"
        finally:
            self.maintenance_tasks.discard(current)
