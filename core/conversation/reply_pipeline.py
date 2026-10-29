import asyncio
import random
import re
import uuid
from copy import deepcopy

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..llm.preset_prompts import build_tts_xml_instructions
from ..llm.prompt import (
    build_reply_prompt,
    build_reply_review_prompt,
    prepare_reply_message_context,
)
from ..memory.atomic import memory_metadata
from ..memory.recall_scope import (
    resolve_memory_recall_scope,
    select_automatic_recall_memories,
)
from ..utils.anti_drool import filter_duplicate_replies
from ..utils.schemas import MediaCaption, MessageData
from .media_captioner import MediaCaptioner
from .memory_recall import conf_float, conf_int, search_automatic_user_memories
from .tool_executor import ToolExecutor


class ReplyReviewRejected(RuntimeError):
    """Request one regeneration before the draft has produced any side effects."""


class ReplyPipeline:
    def __init__(self, plugin):
        self.plugin = plugin
        self.media_captioner = MediaCaptioner(plugin)
        self.tool_executor = ToolExecutor(plugin)

    @staticmethod
    def _has_non_message_work(llm_result) -> bool:
        """Whether an empty message still represents useful follow-up work."""
        action_fields = (
            "delete_message_ids",
            "emoji_ids",
            "repeat_message_ids",
            "likes",
            "poke",
            "ban",
            "kick",
            "leave",
            "tools_to_call",
            "native_tools_called",
            "add_stickers",
            "send_stickers",
            "task_board_actions",
            "slang_actions",
            "tts_segments",
            "set_call_names",
            "set_avatars",
            "set_custom_status",
        )
        return any(bool(getattr(llm_result, field, None)) for field in action_fields)

    @staticmethod
    def _conf_int(conf: dict, key: str, default: int) -> int:
        return conf_int(conf, key, default)

    def _session_recall_limits(self) -> tuple[int, int]:
        conf = self.plugin.embedding_conf
        return (
            self._conf_int(conf, "inject_limit", 20),
            self._conf_int(conf, "session_recall_ttl_seconds", 1800),
        )

    def commit_pending_session_recalled_memories(
        self,
        bot_name: str,
        group_or_user_id: str,
        pending_recall_memories: list[dict] | None,
        *,
        user_ids: list[str] | None = None,
    ) -> None:
        """Commit deduplicated recalls after a successful logical reply.

        Args:
            bot_name: Owning bot configuration.
            group_or_user_id: Current conversation.
            pending_recall_memories: Mutable pending records, cleared after merging.
            user_ids: Frozen batch participants receiving pool slots in full scope.
        """
        if not pending_recall_memories:
            return
        deduped_memories = {}
        fallback_index = 0
        for memory in pending_recall_memories:
            memory_id = str(memory.get("memory_id") or memory.get("id") or "").strip()
            if not memory_id:
                memory_id = f"__fallback_{fallback_index}"
                fallback_index += 1
            deduped_memories[memory_id] = memory
        max_items, ttl_seconds = self._session_recall_limits()
        self.plugin.data_cache.merge_session_recalled_memories(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            memories=list(deduped_memories.values()),
            max_items=max_items,
            ttl_seconds=ttl_seconds,
            user_ids=(
                user_ids if resolve_memory_recall_scope(self.plugin) == "all" else None
            ),
        )
        pending_recall_memories.clear()

    async def _search_current_recall_memories(
        self,
        bot_name: str,
        group_or_user_id: str,
        reply_messages: list[MessageData],
        remind_message: str | None,
        recent_messages: list[MessageData],
        media_captions: list[MediaCaption] | None = None,
    ) -> list[dict]:
        """Recall each participant's batch using text and completed media captions.

        Args:
            bot_name: Owning bot configuration.
            group_or_user_id: Owning conversation.
            reply_messages: Frozen pending messages in their original order.
            remind_message: Reminder text used when no pending messages exist.
            recent_messages: History identifying the active participants.
            media_captions: Already cached media descriptions; only caption is used.

        Returns:
            Ranked, deduplicated records within the automatic injection limit.
        """
        conf = self.plugin.embedding_conf
        max_items, _ = self._session_recall_limits()
        if not conf.get("enabled", False) or max_items <= 0:
            return []

        captions = {
            caption.hash_val: (caption.caption or "").strip()
            for caption in media_captions or []
            if caption.is_captioned and (caption.caption or "").strip()
        }
        messages = reply_messages or [MessageData(content=remind_message or "")]
        user_queries: dict[str, list[str]] = {}
        for message in messages:
            if message.is_recalled or message.role != "message":
                continue
            # Uncaptioned media never contributes a placeholder or an identifier.
            content = re.sub(
                r"\[(?:图片|语音|视频|合并转发)(?::([^\]\s]+))?\]",
                lambda match: (
                    "\n" + captions[match[1]] + "\n" if match[1] in captions else ""
                ),
                message.content or "",
            ).strip()
            if not content:
                continue
            if message.user_id:
                content = f"{message.nickname} ({message.user_id}): {content}"
            user_queries.setdefault(str(message.user_id), []).append(content)

        candidates = []
        recall_scope = resolve_memory_recall_scope(self.plugin)
        for user_id, parts in user_queries.items():
            candidates.extend(
                await search_automatic_user_memories(
                    self.plugin,
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                    query="\n".join(parts),
                    recent_messages=[*recent_messages, *reply_messages],
                    user_id=user_id,
                )
            )
        candidates.sort(
            key=lambda memory: conf_float(
                memory, "score", 1.0 - conf_float(memory, "_distance", 1.0)
            ),
            reverse=True,
        )
        deduped = {}
        for memory in candidates:
            memory_id = str(memory.get("memory_id") or memory.get("id") or "").strip()
            if memory_id:
                deduped.setdefault(memory_id, memory)
        memories = list(deduped.values())
        if recall_scope == "all":
            memories = select_automatic_recall_memories(
                memories, list(user_queries), max_items
            )
        else:
            memories = memories[:max_items]
        if memories:
            try:
                await self.plugin.data_cache.record_memory_hits(memories)
            except Exception as error:
                logger.warning(
                    "[Giftia] Failed to record automatic recall hits: %s", error
                )
        return memories

    async def dispatch_llm_reply_loop(
        self,
        event: AstrMessageEvent,
        bot_name: str,
        nickname: str,
        group_or_user_id: str,
        reply_messages: list[MessageData] | None = None,
        remind_message: str | None = None,
        relevant_memories: list[dict] | None = None,
        pending_recall_memories: list[dict] | None = None,
        tool_results: list[dict[str, str]] | None = None,
        other_data: list[str] | None = None,
        times: int = 0,
        sent_messages: list[str] | None = None,
        meme_tags: str | None = None,
        recent_messages: list[MessageData] | None = None,
        task_creator_names: dict[str, str] | None = None,
        tool_context: dict | None = None,
        review_after_id: int | None = None,
    ):
        """Generate frozen reply turns and review the first eligible unsent draft.

        Args:
            event: Platform routing event.
            bot_name: Owning bot configuration.
            nickname: Bot display name.
            group_or_user_id: Owning conversation.
            reply_messages: Frozen messages selected for this logical reply.
            remind_message: Scheduled reminder text when no input batch exists.
            relevant_memories: Memories selected by the decision model.
            pending_recall_memories: Recalls committed after successful completion.
            tool_results: Results accumulated across XML tool turns.
            other_data: Additional context retained across tool turns.
            times: Current XML tool-turn index.
            sent_messages: Previously sent text used for duplicate suppression.
            meme_tags: Decision-selected sticker search tags.
            recent_messages: Frozen user history, extended only with bot operations.
            task_creator_names: Trusted identities for task creation.
            tool_context: Trusted state shared by tool invocations.
            review_after_id: Input watermark; None disables review for this run.

        Yields:
            Parsed reply results ready for action dispatch.

        Raises:
            ReplyReviewRejected: The first draft conflicts with new messages.
            RuntimeError: Generation fails or exceeds the tool-turn limit.
            ValueError: A required persona or reply provider is missing.
        """
        reply_messages = reply_messages or []
        is_first_turn = sent_messages is None
        if sent_messages is None:
            sent_messages = []
        if pending_recall_memories is None:
            pending_recall_memories = []
        bot_conf = self.plugin.bot_map[bot_name]
        max_loop = self.plugin.tools_config.get("max_loop", 10)

        if times >= max_loop:
            raise RuntimeError("Reply exceeded the maximum number of tool turns")

        # User input stays frozen for the entire logical reply. Later tool
        # turns may only append bot output and operation logs from this session.
        if recent_messages is None:
            recent_messages = deepcopy(
                await self.plugin.data_cache.get_recent_message(
                    bot_name, group_or_user_id, self.plugin.msg_number
                )
            )
        elif not is_first_turn:
            latest = await self.plugin.data_cache.get_recent_message(
                bot_name, group_or_user_id, self.plugin.msg_number
            )
            seen_ids = {msg.message_id for msg in recent_messages}
            for msg in latest:
                if (
                    str(msg.user_id) == str(event.get_self_id())
                    or msg.role == "operation_log"
                ) and msg.message_id not in seen_ids:
                    recent_messages.append(deepcopy(msg))
                    seen_ids.add(msg.message_id)
        if is_first_turn:
            self_id = str(event.get_self_id())
            for msg in recent_messages:
                if msg.user_id and str(msg.user_id) == self_id and msg.content:
                    sent_messages.append(msg.content)

        media_captions = await self.media_captioner.get_cached_media_captions(
            bot_name=bot_name,
            recent_messages=[*recent_messages, *reply_messages],
        )

        if relevant_memories is None:
            relevant_memories = []

        if times == 0:
            current_recall_memories = await self._search_current_recall_memories(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                reply_messages=reply_messages,
                remind_message=remind_message,
                recent_messages=recent_messages,
                media_captions=media_captions,
            )
            if current_recall_memories:
                pending_recall_memories.extend(current_recall_memories)

        session_recalled_memories = []
        if self.plugin.embedding_conf.get("enabled", False):
            recall_scope = resolve_memory_recall_scope(self.plugin)
            max_items, ttl_seconds = self._session_recall_limits()
            recall_users = {
                str(msg.user_id)
                for msg in reply_messages
                if msg.user_id and msg.role == "message" and not msg.is_recalled
            } or {str(event.get_sender_id())}
            cached_memories = self.plugin.data_cache.get_session_recalled_memories(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                max_items=max_items,
                ttl_seconds=ttl_seconds,
                user_ids=list(recall_users) if recall_scope == "all" else None,
            )
            seen_ids = {
                str(memory.get("memory_id") or memory.get("id") or "")
                for memory in relevant_memories
            }
            # Pending recalls survive tool turns without mutating the pool before success.
            for memory in [*pending_recall_memories, *cached_memories]:
                if (
                    recall_scope != "all"
                    and len(session_recalled_memories) >= max_items
                ):
                    break
                memory_id = (
                    str(memory.get("memory_id") or memory.get("id") or "")
                    if isinstance(memory, dict)
                    else memory.memory_id
                )
                metadata = memory_metadata(
                    memory
                    if isinstance(memory, dict)
                    else {"metadata": memory.metadata}
                )
                source = str(
                    metadata.get("source_session_id")
                    or (
                        memory.get("group_or_user_id")
                        if isinstance(memory, dict)
                        else ""
                    )
                    or group_or_user_id
                )
                if recall_scope == "session" and source != str(group_or_user_id):
                    continue
                if recall_scope == "user":
                    associated = metadata.get("associated_user_ids", [])
                    if not isinstance(associated, list):
                        associated = []
                    if associated:
                        if not any(
                            type(uid) in (str, int) and str(uid) in recall_users
                            for uid in associated
                        ):
                            continue
                    elif source != str(group_or_user_id):
                        continue
                if memory_id and memory_id not in seen_ids:
                    seen_ids.add(memory_id)
                    session_recalled_memories.append(memory)
            if recall_scope == "all":
                session_recalled_memories = select_automatic_recall_memories(
                    session_recalled_memories, list(recall_users), max_items
                )

        # Load full profiles for every participant in the rendered reply timeline.
        bot_status = await self.plugin.data_cache.get_bot_status(
            bot_name=bot_name,
            group_id=group_or_user_id,
        )
        group_profile = await self.plugin.data_cache.get_group_profile(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
        )
        prompt_recent_messages, prompt_reply_messages = prepare_reply_message_context(
            recent_messages, reply_messages
        )
        reply_users = {}
        self_id = str(event.get_self_id())
        sender_id = str(event.get_sender_id() or "")
        for msg in prompt_reply_messages:
            uid = str(msg.user_id or "")
            if uid and uid != self_id and not msg.is_recalled and msg.role == "message":
                reply_users[uid] = msg.nickname or reply_users.get(uid, "")
        if not reply_messages and sender_id and sender_id != self_id:
            reply_users[sender_id] = event.get_sender_name() or ""
        common_groups = (
            await self.plugin.db.get_user_common_groups(
                bot_name, group_or_user_id, list(reply_users)
            )
            if reply_users
            else {}
        )
        user_profiles = []
        for uid, user_nickname in reply_users.items():
            (
                user_profile,
                user_relation,
                relation_changes,
                relation_score,
            ) = await asyncio.gather(
                self.plugin.data_cache.get_user_profile_record(
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                    user_id=uid,
                ),
                self.plugin.data_cache.get_user_relation(
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                    user_id=uid,
                ),
                self.plugin.db.get_relation_changes(
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                    user_id=uid,
                    limit=3,
                ),
                self.plugin.db.get_relation_score(
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                    user_id=uid,
                ),
            )
            user_profiles.append(
                {
                    "user_id": uid,
                    "nickname": user_nickname
                    or (event.get_sender_name() if uid == sender_id else "")
                    or "",
                    "user_profile": user_profile,
                    "user_relation": user_relation,
                    "relation_changes": relation_changes,
                    "relation_score": relation_score,
                    "common_groups": common_groups.get(uid, []),
                }
            )
        active_user_briefs = await self.plugin.data_cache.build_active_user_briefs(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            recent_messages=[
                msg
                for msg in [*recent_messages, *reply_messages]
                if str(msg.user_id) not in reply_users
                and not msg.is_recalled
                and msg.role == "message"
            ],
            current_user_id="",
            self_id=event.get_self_id(),
            limit=self.plugin.tools_config.get("active_user_brief_limit", 10),
        )
        short_tasks = []
        short_task_limit = self.plugin.tools_config.get("task_board_max_active", 3)
        if hasattr(self.plugin, "task_board"):
            short_tasks = await self.plugin.task_board.get_active_tasks(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
            )
            short_task_limit = self.plugin.task_board.max_active_tasks()

        if is_first_turn:
            # Freeze trusted creator identities with the input, including the
            # original owners of reminders whose messages have left history.
            task_creator_names = dict(task_creator_names or {})
            for task in short_tasks:
                if task.creator_user_id:
                    task_creator_names[str(task.creator_user_id)] = (
                        task.creator_nickname or str(task.creator_user_id)
                    )
            for msg in [*recent_messages, *reply_messages]:
                if msg.user_id and not msg.is_recalled and msg.role == "message":
                    task_creator_names[str(msg.user_id)] = (
                        msg.nickname
                        or task_creator_names.get(str(msg.user_id))
                        or str(msg.user_id)
                    )
            # These identities are also present in the prompt's routing context.
            sender_id = str(event.get_sender_id() or "")
            if sender_id:
                task_creator_names.setdefault(
                    sender_id, event.get_sender_name() or sender_id
                )
            task_creator_names[str(event.get_self_id())] = nickname

        if tool_context is None:
            tool_context = {
                "bot_name": bot_name,
                "session_id": str(group_or_user_id),
                "request_id": uuid.uuid4().hex,
                "creator_names": {
                    str(uid): str(name or uid)
                    for uid, name in (task_creator_names or {}).items()
                },
                "user_ids": sorted(
                    {
                        str(msg.user_id)
                        for msg in reply_messages
                        if msg.user_id
                        and str(msg.user_id) != str(event.get_self_id())
                        and msg.role == "message"
                        and not msg.is_recalled
                    }
                )
                or [str(event.get_sender_id())],
            }

        llm_reply_conf = bot_conf.get("llm_reply_conf", {})
        persona_id = str(llm_reply_conf.get("persona_id") or "default").strip()

        # 获取表情包池并抽取样本
        if (
            getattr(self.plugin, "use_meme_manager", False)
            and hasattr(self.plugin, "meme_manager_client")
            and self.plugin.meme_manager_client
        ):
            query = (
                meme_tags
                if (meme_tags and meme_tags.strip())
                else getattr(self.plugin, "default_meme_query", "日常, 开心, 互动")
            )
            count = getattr(self.plugin, "meme_candidate_count", 5)
            candidates = await self.plugin.meme_manager_client.get_candidate_memes(
                persona_id=persona_id,
                query=query,
                count=count,
                event=event,
            )
            bot_sticker_cache = (
                self.plugin.meme_manager_client.format_candidates_for_prompt(candidates)
            )
        else:
            bot_sticker_cache = await self.plugin.emoji_manager.get_random_stickers(
                bot_name
            )

        # 3. 构造回复提示词 Prompt
        user_prompt = build_reply_prompt(
            recent_messages=prompt_recent_messages,
            media_captions=media_captions,
            reply_messages=prompt_reply_messages,
            remind_message=remind_message,
            group_data=str(
                await event.get_group(event.get_group_id())
                if event.get_group_id()
                else ""
            ),
            bot_status=bot_status,
            tool_results=tool_results,
            session_recalled_memories=session_recalled_memories,
            relevant_memories=relevant_memories,
            user_profiles=user_profiles,
            group_profile=group_profile,
            active_user_briefs=active_user_briefs,
            short_tasks=short_tasks,
            short_task_limit=short_task_limit,
            other_data=other_data,
            bot_sticker=bot_sticker_cache,
            message_truncate_limit=getattr(
                self.plugin, "reply_message_truncate_limit", 1500
            ),
            slang_entries=await self.plugin.db.slang_repo.get_entries(
                bot_name, group_or_user_id
            ),
        )
        logger.debug(f"[Giftia] 触发正式回复,构造回复提示词：{user_prompt}")

        persona = None
        persona_tools = None
        if (
            hasattr(self.plugin.context, "persona_manager")
            and self.plugin.context.persona_manager
        ):
            persona = self.plugin.context.persona_manager.get_persona_v3_by_id(
                persona_id
            )

        if not persona:
            err_msg = f"[Giftia] {bot_name} 绑定的 AstrBot 人格 '{persona_id}' 在 AstrBot 中不存在或已被删除，请重新在控制台配置机器人人格。"
            logger.error(err_msg)
            raise ValueError(err_msg)

        system_prompt = str(persona.get("prompt") or "")
        persona_tools = persona.get("tools")

        provider_ids = llm_reply_conf.get("provider_ids")
        if not provider_ids:
            old_provider_id = llm_reply_conf.get("provider_id")
            if old_provider_id:
                provider_ids = [old_provider_id] + llm_reply_conf.get(
                    "fallback_provider_ids", []
                )
            else:
                logger.error(f"{bot_name} 未配置回复模型ID")
                raise ValueError("No reply provider configured")
        provider_ids = [p for p in provider_ids if p]
        if not provider_ids:
            logger.error(f"{bot_name} 未配置回复模型ID")
            raise ValueError("No reply provider configured")

        provider_selection_mode = llm_reply_conf.get(
            "provider_selection_mode", "fallback"
        )
        if provider_selection_mode == "random":
            random.shuffle(provider_ids)

        # 4. 调用 LLM 进行回复
        if getattr(self.plugin, "passive_memory_enabled", False):
            await self.plugin.passive_memory_manager.observe_messages(
                bot_name,
                group_or_user_id,
                event.get_self_id(),
                [*recent_messages, *reply_messages],
            )
        llm_result = await self.plugin.call_llm.call_llm_reply(
            event=event,
            group_or_user_id=group_or_user_id,
            provider_ids=provider_ids,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            use_source_tools=self.plugin.tools_config.get("use_source_tools", False),
            force_xml_tools=self.plugin.tools_config.get("force_xml_tools", False),
            enabled_features=bot_conf.get("enabled_interactive_features"),
            persona_tools=persona_tools,
            tts_instruction=(
                build_tts_xml_instructions(
                    self.plugin.tts_manager.provider_type(bot_conf),
                    self.plugin.tts_manager.language_options(bot_conf),
                )
                if hasattr(self.plugin, "tts_manager")
                and self.plugin.tts_manager.enabled(bot_conf)
                else ""
            ),
            timeout=self.plugin.tools_config.get("timeout", 120),
            tool_context=tool_context,
            use_meme_manager=getattr(self.plugin, "use_meme_manager", False),
        )

        if not llm_result:
            raise RuntimeError("Reply providers exhausted their retries")

        # Anti-drooling optimization for low-intelligence models:
        # Filter out messages that have already been sent in the current XML tool calling loop.
        if getattr(self.plugin, "enable_anti_drool", False):
            filter_duplicate_replies(llm_result, sent_messages)

        # Only the first unsent tool turn is eligible. Regeneration explicitly
        # omits the watermark, so arrivals cannot cause an unbounded review loop.
        review_provider = llm_reply_conf.get("review_provider_id")
        if (
            review_provider
            and review_after_id is not None
            and is_first_turn
            and times == 0
            and not llm_result.native_tools_called
            and llm_result.msg_chains
        ):
            passed = None
            try:
                parse_key = f"{bot_name}:{event.unified_msg_origin}"
                async with self.plugin.parse_locks[parse_key]:
                    latest_id = await self.plugin.db.get_max_message_id(
                        bot_name, group_or_user_id
                    )
                    new_messages = await self.plugin.db.get_messages_by_id_range(
                        bot_name, group_or_user_id, review_after_id + 1, latest_id
                    )
                    new_messages = deepcopy(
                        [
                            message
                            for message in new_messages
                            if message.role == "message"
                            and not message.is_recalled
                            and str(message.user_id) != str(event.get_self_id())
                        ]
                    )
                if new_messages:
                    review_user_profiles = list(user_profiles)
                    review_user_ids = {
                        str(profile["user_id"]) for profile in review_user_profiles
                    }
                    for message in new_messages:
                        uid = str(message.user_id or "")
                        if uid and uid not in review_user_ids:
                            review_user_ids.add(uid)
                            review_user_profiles.append(
                                {
                                    "user_id": uid,
                                    "user_profile": await self.plugin.data_cache.get_user_profile_record(
                                        bot_name=bot_name,
                                        group_or_user_id=group_or_user_id,
                                        user_id=uid,
                                    ),
                                }
                            )
                    review_captions = (
                        await self.media_captioner.get_cached_media_captions(
                            bot_name=bot_name,
                            recent_messages=[*prompt_reply_messages, *new_messages],
                        )
                    )
                    review_prompt = build_reply_review_prompt(
                        reply_messages=prompt_reply_messages,
                        new_messages=new_messages,
                        llm_result=llm_result,
                        media_captions=review_captions,
                        nickname=nickname,
                        self_id=str(event.get_self_id()),
                        session_id=group_or_user_id,
                        group_profile=group_profile,
                        bot_status=bot_status,
                        user_profiles=review_user_profiles,
                        active_user_briefs=active_user_briefs,
                        message_truncate_limit=getattr(
                            self.plugin, "reply_message_truncate_limit", 1500
                        ),
                    )
                    if review_prompt:
                        passed = await self.plugin.call_llm.call_llm_reply_review(
                            provider_id=review_provider,
                            user_prompt=review_prompt,
                            bot_name=bot_name,
                            group_or_user_id=group_or_user_id,
                        )
            except Exception as error:
                logger.warning(
                    "[Giftia] Could not prepare reply review for %s:%s; sending draft: %s",
                    bot_name,
                    group_or_user_id,
                    error,
                )
            if passed is False:
                raise ReplyReviewRejected("New messages conflict with the unsent draft")

        # 5. 空回复拦截处理
        if (
            not remind_message
            and times == 0
            and not llm_result.msg_chains
            and not self._has_non_message_work(llm_result)
        ):
            for message in reply_messages:
                await self.plugin.db.update_message_reply_decision(
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                    message_id=message.message_id,
                    reply_decision=0,
                )

        # 6. 更新机器人状态
        if llm_result.status:
            await self.plugin.data_cache.set_bot_status(
                bot_name=bot_name,
                group_id=group_or_user_id,
                status=llm_result.status,
            )

        # 7. 后台分析与添加表情包（仅原生模式生效，接入 meme_manager 时由其自行管理收集）
        if llm_result.add_stickers and not getattr(
            self.plugin, "use_meme_manager", False
        ):
            asyncio.create_task(
                self.media_captioner.analyze_and_add_stickers(
                    event=event,
                    bot_name=bot_name,
                    nickname=nickname,
                    group_or_user_id=group_or_user_id,
                    llm_result=llm_result,
                )
            )

        # 8. 产生 LLM 回复结果供 Dispatcher 处理
        task_creator_names = {
            **(task_creator_names or {}),
            **tool_context.get("creator_names", {}),
        }
        llm_result.task_creator_names = dict(task_creator_names)
        llm_result.tool_context = tool_context
        yield llm_result

        # 初始化参数用于潜在的下一次递归
        if tool_results is None:
            tool_results = []

        # Dispatcher 已经在对应输出顺序位置执行了 XML 工具。
        tool_results.extend(llm_result.xml_tool_results)

        # 10. 判断是否需要继续循环迭代
        if llm_result.tools_to_call or llm_result.xml_tool_results:
            logger.debug(f"{bot_name} llm step {times + 1} ...")
            async for chunk in self.dispatch_llm_reply_loop(
                event=event,
                bot_name=bot_name,
                nickname=nickname,
                group_or_user_id=group_or_user_id,
                reply_messages=reply_messages,
                relevant_memories=relevant_memories,
                pending_recall_memories=pending_recall_memories,
                tool_results=tool_results,
                remind_message=remind_message,
                times=times + 1,
                other_data=other_data,
                sent_messages=sent_messages,
                meme_tags=meme_tags,
                recent_messages=recent_messages,
                task_creator_names=llm_result.task_creator_names,
                tool_context=tool_context,
            ):
                yield chunk
