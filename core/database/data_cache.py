import asyncio
import json
import time
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import asdict
from datetime import datetime

from cachetools import LRUCache

from astrbot.api import logger

from ..memory.memory import LTM
from ..utils.http_manager import HttpManager
from ..utils.schemas import (
    MediaCaption,
    MemoryItem,
    MessageData,
    SessionRecallMemory,
    Status,
    normalize_memory_importance,
)
from .database import Database

MAX_CAPTION_CACHE_SIZE = 500


class DataCache:
    """数据缓存"""

    # 键是文件二进制数据的xxhash，而不是URL的xxhash
    caption: LRUCache[str, MediaCaption]  # xxhash:caption
    # filename:xxhash，需要一个文件名到hash的映射
    filename_to_hash: LRUCache[str, str]
    # 聊天记录缓存
    recent_messages: defaultdict[
        str, deque[MessageData]
    ]  # bot_name:group_id:MessageData
    # 机器人状态缓存
    bot_status: dict[str, Status]  # bot_name:group_id:status

    def __init__(
        self,
        db: Database,
        http_manager: HttpManager,
        ltm: LTM,
        memory_number: int = 20,
        msg_number: int = 50,
        energy_recovery_interval: int = 90,
        plugin=None,
    ):
        self.db = db
        self.http_manager = http_manager
        self.ltm = ltm
        self.memory_number = memory_number
        self.msg_number = msg_number
        self.energy_recovery_interval = energy_recovery_interval
        self.plugin = plugin
        self.caption = LRUCache(maxsize=MAX_CAPTION_CACHE_SIZE)
        self.filename_to_hash = LRUCache(maxsize=MAX_CAPTION_CACHE_SIZE)
        # 用户画像缓存
        self.user_profiles: dict[str, str] = {}
        self.user_profile_records: dict[str, dict] = {}
        # 群画像缓存
        self.group_profiles: dict[str, str] = {}
        self.bot_status: dict[str, Status] = {}

        # 关系缓存
        self.relations: dict[
            str, tuple[int, str]
        ] = {}  # bot_name:group_or_user_id:user_id -> (relation, title)

        # 使用 defaultdict 自动管理每个会话的 deque
        # lambda 确保每个新 key 都会得到一个指定长度的 deque
        self.recent_messages: defaultdict[str, deque[MessageData]] = defaultdict(
            lambda: deque(maxlen=self.msg_number)
        )
        # 记忆缓存
        self.memories: defaultdict[str, deque[MemoryItem]] = defaultdict(
            lambda: deque(maxlen=self.memory_number)
        )
        # 会话级临时召回记忆池，重启后自然清空
        self.session_recalled_memories: defaultdict[
            str, dict[str, SessionRecallMemory]
        ] = defaultdict(dict)

    async def get_recent_message(
        self, bot_name: str, group_id: str, limit: int = 50
    ) -> list[MessageData]:
        """获取近期聊天记录"""
        fmt_key = f"{bot_name}:{group_id}"
        messages = self.recent_messages.get(fmt_key)
        # 只有在缓存满足需求量时，才直接使用缓存
        if messages and (len(messages) >= limit or len(messages) == self.msg_number):
            return list(messages)[-limit:]
        # 否则从数据库中获取
        fetch_limit = max(limit, self.msg_number)
        db_messages = await self.db.get_messages(
            group_or_user_id=group_id, bot_name=bot_name, limit=fetch_limit
        )
        # 缓存最新的一批
        self.recent_messages[fmt_key] = deque(db_messages, maxlen=self.msg_number)
        return db_messages[-limit:]

    async def get_message_by_id(
        self, bot_name: str, group_id: str, message_id: str
    ) -> MessageData | None:
        """通过消息ID获取消息"""
        fmt_key = f"{bot_name}:{group_id}"
        messages = self.recent_messages.get(fmt_key)
        if messages:
            for msg in messages:
                if msg.message_id == message_id:
                    return msg
        return await self.db.get_message_by_id(
            group_or_user_id=group_id, bot_name=bot_name, message_id=message_id
        )

    def _intercept_message(self, msg_data: MessageData) -> None:
        if msg_data.content and self.plugin:
            keywords = getattr(self.plugin, "safety_intercept_keywords", None)
            if keywords:
                for kw in keywords:
                    if kw and kw in msg_data.content:
                        logger.info(f"[Giftia Safety Intercept] 拦截到敏感词: {kw}，已进行消息屏蔽处理")
                        msg_data.content = "【该消息触发了安全拦截，已被屏蔽】"
                        break

    async def add_message(
        self, bot_name: str, group_id: str, msg_data: MessageData
    ) -> None:
        """添加消息，先加入缓存，再写入数据库"""
        import time

        self._intercept_message(msg_data)

        now = time.time()
        if not hasattr(self, "_recent_adds"):
            self._recent_adds = []

        # Clean entries older than 2 seconds
        self._recent_adds = [x for x in self._recent_adds if now - x[0] < 2.0]

        # Check for duplicate
        for ts, b, g, mid, c in self._recent_adds:
            if b == bot_name and g == group_id:
                # If both have non-empty message_id, precisely match by message_id
                if mid and msg_data.message_id and mid == msg_data.message_id:
                    logger.debug(
                        f"[Giftia] Duplicate message write bypassed (message_id match): {msg_data.message_id}"
                    )
                    return
                # If either message lacks a message_id, fall back to content matching
                if (not mid or not msg_data.message_id) and c == msg_data.content:
                    logger.debug(
                        f"[Giftia] Duplicate message write bypassed (content match): {msg_data.content}"
                    )
                    return

        # Record this write
        self._recent_adds.append((now, bot_name, group_id, msg_data.message_id, msg_data.content))

        if not msg_data.message_id:
            import uuid

            msg_data.message_id = f"local_{uuid.uuid4().hex}"
        fmt_key = f"{bot_name}:{group_id}"
        self.recent_messages[fmt_key].append(msg_data)
        # 将消息写入数据库
        await self.db.insert_message(bot_name=bot_name, message=msg_data)

    async def add_cache_message(
        self, bot_name: str, group_id: str, msg_data: MessageData
    ) -> None:
        """添加消息到缓存，不持久化，如点赞失败、撤回、戳一戳等操作，防止AI重复触发"""
        self._intercept_message(msg_data)
        fmt_key = f"{bot_name}:{group_id}"
        self.recent_messages[fmt_key].append(msg_data)

    async def get_bot_status(self, bot_name: str, group_id: str) -> Status:
        fmt_key = f"{bot_name}:{group_id}"
        status = self.bot_status.get(fmt_key)

        if not status:
            status = await self.db.get_bot_status(
                bot_name=bot_name, group_or_user_id=group_id
            )
            self.bot_status[fmt_key] = status

        # 当前时间
        current_time = time.time()

        if status.timestamp > 0:
            # 恢复的能量
            recovered_energy = (
                current_time - status.timestamp
            ) / self.energy_recovery_interval
            clean_energy = status.energy.strip().strip('"').strip("'")
            try:
                status.energy = str(min(float(clean_energy) + recovered_energy, 100.0))
            except Exception as e:
                logger.error(f"能量数据异常：{e}，自动重置为100")
                status.energy = "100.0"

        status.timestamp = current_time
        return status

    async def set_message_recalled(
        self, bot_name: str, group_or_user_id: str, message_ids: list[str]
    ) -> None:
        """将消息标记为撤回"""
        fmt_key = f"{bot_name}:{group_or_user_id}"
        messages = self.recent_messages.get(fmt_key)
        if messages:
            for msg in messages:
                if msg.message_id in message_ids:
                    msg.is_recalled = 1
        await self.db.update_message_recall(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            message_ids=message_ids,
            is_recalled=1,
        )

    async def delete_message(
        self, bot_name: str, group_or_user_id: str, message_id: str
    ) -> None:
        """删除消息"""
        fmt_key = f"{bot_name}:{group_or_user_id}"
        messages = self.recent_messages.get(fmt_key)
        if messages:
            for msg in messages:
                if msg.message_id == message_id:
                    messages.remove(msg)
                    break
        await self.db.delete_message(
            bot_name=bot_name, group_or_user_id=group_or_user_id, message_id=message_id
        )

    async def set_bot_status(
        self, bot_name: str, group_id: str, status: Status
    ) -> None:
        current_status = await self.get_bot_status(bot_name, group_id)
        # 使用 asdict 将 dataclass 转换为字典，即便开启了 slots 也能正常工作
        status_data = asdict(status)
        # 仅对非空值进行差分覆盖
        for key, value in status_data.items():
            if key not in ("timestamp", "last_updated") and value is not None and value != "":
                setattr(current_status, key, value)
        await self.db.upsert_bot_status(
            bot_name=bot_name,
            group_or_user_id=group_id,
            status=current_status,
        )

    async def get_caption_by_hash(self, hash_val: str) -> MediaCaption | None:
        media_caption = self.caption.get(hash_val)
        if media_caption:
            return media_caption
        # 从数据库中获取
        media_caption = await self.db.get_media_caption_by_hash(hash_val)
        if media_caption:
            await self.set_caption(media_caption, False)
            return media_caption
        return None

    async def get_caption_by_filename(
        self, filename: str
    ) -> tuple[str | None, MediaCaption | None]:
        if not filename or is_temp_or_local_path(filename):
            return None, None
        hash_val = self.filename_to_hash.get(filename)
        if hash_val and self.caption.get(hash_val):
            return hash_val, self.caption[hash_val]
        # 从数据库中获取
        media_caption = await self.db.get_media_caption_by_filename(filename)
        if media_caption:
            await self.set_caption(media_caption, False)
            return media_caption.hash_val, media_caption
        return None, None

    async def set_caption(self, caption: MediaCaption, is_new: bool = True) -> None:
        self.caption[caption.hash_val] = caption
        if caption.file_name and not is_temp_or_local_path(caption.file_name):
            self.filename_to_hash[caption.file_name] = caption.hash_val
        if is_new:
            await self.db.insert_media_caption(media_caption=caption)

    async def update_caption(self, caption: MediaCaption) -> None:
        self.caption[caption.hash_val] = caption
        if caption.file_name and not is_temp_or_local_path(caption.file_name):
            self.filename_to_hash[caption.file_name] = caption.hash_val
        await self.db.update_media_caption(media_caption=caption)

    async def clear_caption(self):
        self.caption.clear()
        self.filename_to_hash.clear()
        await self.db.clear_media_caption()

    async def get_user_profile(
        self, bot_name: str, group_or_user_id: str, user_id: str
    ) -> str | None:
        """获取用户画像"""
        fmt_key = f"{bot_name}:{group_or_user_id}:{user_id}"
        profile = self.user_profiles.get(fmt_key)
        if profile:
            return profile
        # 从数据库中获取
        profile_data = await self.db.get_user_profile(
            user_id=user_id,
            group_or_user_id=group_or_user_id,
            bot_name=bot_name,
        )
        if profile_data:
            self.user_profiles[fmt_key] = profile_data
            return profile_data
        return None

    async def get_user_profile_record(
        self, bot_name: str, group_or_user_id: str, user_id: str
    ) -> dict | None:
        """获取用户画像完整记录"""
        fmt_key = f"{bot_name}:{group_or_user_id}:{user_id}"
        record = self.user_profile_records.get(fmt_key)
        if record:
            return record

        record = await self.db.get_user_profile_record(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            user_id=user_id,
        )
        if record:
            self.user_profile_records[fmt_key] = record
            profile = record.get("profile")
            if profile:
                self.user_profiles[fmt_key] = profile
        return record

    async def set_user_profile(
        self,
        bot_name: str,
        group_or_user_id: str,
        user_id: str,
        profile: str | None = None,
        relation: int | None = None,
        title: str | None = None,
        profile_fields: dict[str, str | None] | None = None,
        alias_increment_count: bool = True,
    ) -> None:
        """设置用户画像"""
        fmt_key = f"{bot_name}:{group_or_user_id}:{user_id}"
        profile_fields = dict(profile_fields or {})
        aliases = profile_fields.pop("aliases", None)
        db_relation = relation
        db_title = title
        if relation is not None or title is not None:
            current_relation, current_title = await self.get_user_relation(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                user_id=user_id,
            )
            if relation is not None:
                db_relation = relation
            else:
                db_relation = current_relation
            db_title = title if title is not None else current_title
            self.relations[fmt_key] = (
                db_relation,
                db_title,
            )
        if aliases is not None:
            if aliases or alias_increment_count:
                await self.db.upsert_user_aliases(
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                    user_id=user_id,
                    aliases=aliases,
                    increment_count=alias_increment_count,
                )
            else:
                await self.db.delete_user_aliases(
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                    user_id=user_id,
                )
        await self.db.upsert_user_profile(
            user_id=user_id,
            group_or_user_id=group_or_user_id,
            bot_name=bot_name,
            profile=profile,
            relation=db_relation,
            title=db_title,
            profile_fields=profile_fields,
        )
        self.user_profile_records.pop(fmt_key, None)
        self.user_profiles.pop(fmt_key, None)

    async def get_group_profile(
        self, bot_name: str, group_or_user_id: str
    ) -> str | None:
        """获取群画像"""
        fmt_key = f"{bot_name}:{group_or_user_id}"
        profile = self.group_profiles.get(fmt_key)
        if profile:
            return profile
        # 从数据库中获取
        profile_data = await self.db.get_group_profile(
            group_or_user_id=group_or_user_id, bot_name=bot_name
        )
        if profile_data:
            self.group_profiles[fmt_key] = profile_data
            return profile_data
        return None

    async def set_group_profile(
        self, bot_name: str, group_or_user_id: str, profile: str
    ) -> None:
        """设置群画像"""
        fmt_key = f"{bot_name}:{group_or_user_id}"
        self.group_profiles[fmt_key] = profile
        await self.db.upsert_group_profile(
            group_or_user_id=group_or_user_id,
            bot_name=bot_name,
            profile=profile,
        )

    async def update_relation(
        self, bot_name: str, group_or_user_id: str, user_id: str, relation: int
    ) -> None:
        """更新关系"""
        fmt_key = f"{bot_name}:{group_or_user_id}:{user_id}"
        current_relation, current_title = await self.get_user_relation(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            user_id=user_id,
        )
        new_relation = current_relation + relation
        self.relations[fmt_key] = (new_relation, current_title)
        if fmt_key in self.user_profile_records:
            self.user_profile_records[fmt_key]["relation"] = new_relation
        await self.db.upsert_relation(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            user_id=user_id,
            relation=new_relation,
        )

    async def set_relation_title(
        self, bot_name: str, group_or_user_id: str, user_id: str, title: str
    ) -> None:
        """设置关系头衔"""
        fmt_key = f"{bot_name}:{group_or_user_id}:{user_id}"
        current_relation, _ = await self.get_user_relation(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            user_id=user_id,
        )
        self.relations[fmt_key] = (current_relation, title)
        if fmt_key in self.user_profile_records:
            self.user_profile_records[fmt_key]["title"] = title
        await self.db.upsert_relation_title(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            user_id=user_id,
            title=title,
        )

    async def get_user_relation(
        self, bot_name: str, group_or_user_id: str, user_id: str
    ) -> tuple[int, str]:
        """获取用户关系"""
        fmt_key = f"{bot_name}:{group_or_user_id}:{user_id}"
        relation = self.relations.get(fmt_key)
        if relation:
            return relation
        # 从数据库中获取
        relation_data = await self.db.get_relation(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            user_id=user_id,
        )
        if relation_data:
            self.relations[fmt_key] = relation_data
            return relation_data
        return 0, ""

    async def build_active_user_briefs(
        self,
        bot_name: str,
        group_or_user_id: str,
        recent_messages: list[MessageData],
        current_user_id: str = "",
        self_id: str = "",
        limit: int = 10,
    ) -> list[dict]:
        """构建消息窗口内其他活跃用户的轻量画像摘要"""
        current_user_id = str(current_user_id) if current_user_id else ""
        self_id = str(self_id) if self_id else ""

        active_users = []
        seen = set()
        for msg in reversed(recent_messages or []):
            uid = str(msg.user_id) if msg.user_id else ""
            if not uid or uid == current_user_id or uid == self_id or uid in seen:
                continue
            seen.add(uid)
            active_users.append((uid, msg.nickname or ""))
            if len(active_users) >= limit:
                break

        briefs = []
        for uid, nickname in active_users:
            record = await self.get_user_profile_record(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                user_id=uid,
            )
            relation, title = await self.get_user_relation(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                user_id=uid,
            )

            brief = {
                "user_id": uid,
                "nickname": nickname,
                "relation": relation,
                "title": title,
                "call_name": "",
                "aliases": "",
            }
            if record:
                brief["call_name"] = record.get("call_name") or ""
                brief["aliases"] = record.get("aliases") or ""
                if not title and record.get("title"):
                    brief["title"] = record.get("title") or ""
                if relation == 0 and record.get("relation") is not None:
                    brief["relation"] = record.get("relation")

            has_content = any(
                brief.get(key)
                for key in ("relation", "title", "call_name", "aliases")
            )
            if has_content:
                briefs.append(brief)

        return briefs

    @staticmethod
    def _safe_float(value, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _session_recall_score(memory: dict) -> tuple[float, float]:
        distance = DataCache._safe_float(memory.get("_distance"), 1.0)
        if "score" in memory:
            score = DataCache._safe_float(memory.get("score"), 0.0)
        else:
            score = max(0.0, 1.0 - distance)
        return score, distance

    @staticmethod
    def _session_recall_rank(memory: SessionRecallMemory, now: float) -> float:
        age_minutes = max(0.0, (now - memory.last_recalled_at) / 60.0)
        recency_bonus = 0.2 / (1.0 + age_minutes)
        hit_bonus = min(memory.hit_count, 6) * 0.04
        return memory.score + recency_bonus + hit_bonus

    def _prune_session_recalled_memories(
        self,
        fmt_key: str,
        max_items: int,
        ttl_seconds: int = 0,
    ) -> None:
        pool = self.session_recalled_memories.get(fmt_key)
        if not pool:
            return

        now = time.time()
        if ttl_seconds and ttl_seconds > 0:
            expired_ids = [
                memory_id
                for memory_id, memory in pool.items()
                if now - memory.last_recalled_at > ttl_seconds
            ]
            for memory_id in expired_ids:
                pool.pop(memory_id, None)

        if max_items <= 0:
            pool.clear()
            return

        if len(pool) <= max_items:
            return

        ranked = sorted(
            pool.values(),
            key=lambda memory: self._session_recall_rank(memory, now),
            reverse=True,
        )
        keep_ids = {memory.memory_id for memory in ranked[:max_items]}
        for memory_id in list(pool.keys()):
            if memory_id not in keep_ids:
                pool.pop(memory_id, None)

    def merge_session_recalled_memories(
        self,
        bot_name: str,
        group_or_user_id: str,
        memories: list[dict] | None,
        max_items: int = 20,
        ttl_seconds: int = 0,
    ) -> list[SessionRecallMemory]:
        """合并当前会话的语义召回结果，并在超限时淘汰低价值旧召回。"""
        if not memories:
            return self.get_session_recalled_memories(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                max_items=max_items,
                ttl_seconds=ttl_seconds,
            )

        fmt_key = f"{bot_name}:{group_or_user_id}"
        pool = self.session_recalled_memories[fmt_key]
        now = time.time()

        for raw_memory in memories:
            memory_id = str(
                raw_memory.get("memory_id") or raw_memory.get("id") or ""
            ).strip()
            text = str(raw_memory.get("text") or "").strip()
            if not memory_id or not text:
                continue

            score, distance = self._session_recall_score(raw_memory)
            metadata = str(raw_memory.get("metadata") or "{}")
            updated_at = str(raw_memory.get("updated_at") or "")
            created_at = str(raw_memory.get("created_at") or "")

            if memory_id in pool:
                memory = pool[memory_id]
                memory.text = text
                memory.metadata = metadata
                memory.score = max(memory.score, score)
                memory.distance = min(memory.distance, distance)
                memory.hit_count += 1
                memory.last_recalled_at = now
                memory.updated_at = updated_at or memory.updated_at
                memory.created_at = created_at or memory.created_at
            else:
                pool[memory_id] = SessionRecallMemory(
                    memory_id=memory_id,
                    text=text,
                    metadata=metadata,
                    score=score,
                    distance=distance,
                    hit_count=1,
                    first_recalled_at=now,
                    last_recalled_at=now,
                    updated_at=updated_at,
                    created_at=created_at,
                )

        self._prune_session_recalled_memories(fmt_key, max_items, ttl_seconds)
        return self.get_session_recalled_memories(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            max_items=max_items,
            ttl_seconds=ttl_seconds,
        )

    def get_session_recalled_memories(
        self,
        bot_name: str,
        group_or_user_id: str,
        max_items: int = 20,
        ttl_seconds: int = 0,
    ) -> list[SessionRecallMemory]:
        """获取当前会话可注入的临时召回记忆。"""
        fmt_key = f"{bot_name}:{group_or_user_id}"
        self._prune_session_recalled_memories(fmt_key, max_items, ttl_seconds)
        pool = self.session_recalled_memories.get(fmt_key)
        if not pool or max_items <= 0:
            return []

        now = time.time()
        ranked = sorted(
            pool.values(),
            key=lambda memory: self._session_recall_rank(memory, now),
            reverse=True,
        )

        selected = []
        for memory in ranked:
            if len(selected) >= max_items:
                break
            selected.append(memory)
        return selected

    def remove_session_recalled_memory(self, memory_id: str) -> None:
        """从所有会话临时召回池中移除一条长期记忆。"""
        if not memory_id:
            return
        for pool in self.session_recalled_memories.values():
            pool.pop(memory_id, None)

    async def get_memories(
        self, bot_name: str, group_or_user_id: str, limit: int = 20
    ) -> list[MemoryItem]:
        """获取记忆"""
        fmt_key = f"{bot_name}:{group_or_user_id}"
        memories = self.memories.get(fmt_key)
        if memories and len(memories) >= limit:
            return list(memories)[-limit:]
        # 从数据库中获取
        db_memories = await self.db.get_memories(
            group_or_user_id=group_or_user_id, bot_name=bot_name, limit=limit
        )
        # 缓存最新的一批
        self.memories[fmt_key] = deque(db_memories, maxlen=limit)
        return db_memories

    async def add_memory(
        self,
        bot_name: str,
        group_or_user_id: str,
        text: str,
        user_id: str,
        associated_user_ids: list[str] = None,
        importance: int = 5,
        hit_count: int = 0,
        last_hit_at: str = "",
    ) -> str | None:
        """添加记忆"""
        fmt_key = f"{bot_name}:{group_or_user_id}"
        now = datetime.now().isoformat()
        importance = normalize_memory_importance(importance)
        try:
            hit_count = max(0, int(hit_count or 0))
        except (TypeError, ValueError):
            hit_count = 0
        last_hit_at = str(last_hit_at or "")
        meta_dict = {"user_id": user_id, "importance": importance}
        if associated_user_ids:
            meta_dict["associated_user_ids"] = associated_user_ids
        meta_str = json.dumps(meta_dict)

        result = await self.ltm.add_memory(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            text=text,
            time=now,
            metadata=meta_str,
        )
        if result is None:
            return
        memory_id, vector = result
        memory = MemoryItem(
            memory_id=memory_id,
            text=text,
            vector=vector,
            metadata=meta_str,
            updated_at=now,
            created_at=now,
            importance=importance,
            hit_count=hit_count,
            last_hit_at=last_hit_at,
        )
        self.memories[fmt_key].append(memory)
        # 将记忆写入数据库
        await self.db.insert_memory(
            bot_name=bot_name, group_or_user_id=group_or_user_id, memory=memory
        )
        return memory_id

    async def record_memory_hits(self, memories: Iterable[dict | object] | None) -> None:
        """记录长期记忆的有效召回命中。"""
        if not memories:
            return

        memory_ids = []
        seen = set()
        for memory in memories:
            if isinstance(memory, dict):
                raw_memory_id = memory.get("memory_id") or memory.get("id")
            else:
                raw_memory_id = getattr(memory, "memory_id", None) or getattr(
                    memory, "id", None
                )
            memory_id = str(raw_memory_id or "").strip()
            if memory_id and memory_id not in seen:
                seen.add(memory_id)
                memory_ids.append(memory_id)

        if not memory_ids:
            return

        hit_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await self.db.record_memory_hits(memory_ids, hit_at=hit_at)

        memory_id_set = set(memory_ids)
        for memory_deque in self.memories.values():
            for memory in memory_deque:
                if memory.memory_id in memory_id_set:
                    memory.hit_count = int(memory.hit_count or 0) + 1
                    memory.last_hit_at = hit_at

    async def delete_memory(self, memory_id: str) -> bool:
        """删除记忆"""
        result = await self.ltm.delete_memory(memory_id=memory_id)
        if result is None:
            return False
        await self.db.delete_memory(memory_id=memory_id)
        # 从缓存中删除
        for memories in self.memories.values():
            for memory in memories:
                if memory.memory_id == memory_id:
                    memories.remove(memory)
                    break
        self.remove_session_recalled_memory(memory_id)
        return True

    async def delete_all_memories(self, bot_name: str, group_or_user_id: str):
        """删除全部记忆"""
        fmt_key = f"{bot_name}:{group_or_user_id}"
        targets = [
            self.group_profiles,
            self.memories,
            self.bot_status,
            self.recent_messages,
            self.session_recalled_memories,
        ]
        for cache in targets:
            cache.pop(fmt_key, None)

        prefix = f"{fmt_key}:"
        for cache_dict in [self.user_profiles, self.relations]:
            keys_to_delete = [k for k in cache_dict.keys() if k.startswith(prefix)]
            for k in keys_to_delete:
                cache_dict.pop(k, None)
        keys_to_delete = [
            k for k in self.user_profile_records.keys() if k.startswith(prefix)
        ]
        for k in keys_to_delete:
            self.user_profile_records.pop(k, None)

        await asyncio.gather(
            self.db.delete_group_user_profiles(
                bot_name=bot_name, group_or_user_id=group_or_user_id
            ),
            self.db.delete_group_profile(
                bot_name=bot_name, group_or_user_id=group_or_user_id
            ),
            self.ltm.delete_all_memories(
                bot_name=bot_name, group_or_user_id=group_or_user_id
            ),
            self.db.delete_all_memories(
                bot_name=bot_name, group_or_user_id=group_or_user_id
            ),
            self.db.delete_bot_status(
                group_or_user_id=group_or_user_id, bot_name=bot_name
            ),
            self.db.delete_all_relations(
                bot_name=bot_name, group_or_user_id=group_or_user_id
            ),
            self.db.delete_chat_history(
                group_or_user_id=group_or_user_id, bot_name=bot_name
            ),
            return_exceptions=True,
        )


def is_temp_or_local_path(s: str | None) -> bool:
    """Determines whether a file path or filename is temporary or local.

    Args:
        s: The file path or filename to check.

    Returns:
        True if the path or filename points to a local file or temporary pattern,
        False otherwise.
    """
    if not s:
        return False
    if s.startswith(("http://", "https://")):
        return False
    if s.startswith("file://") or any(
        marker in s
        for marker in [
            "media_image_",
            "media_audio_",
            "media_file_",
            "io_temp_img_",
            "compressed_",
        ]
    ):
        return True
    import os

    try:
        if os.path.isabs(s) or os.path.exists(s):
            return True
    except Exception:
        pass
    return False
