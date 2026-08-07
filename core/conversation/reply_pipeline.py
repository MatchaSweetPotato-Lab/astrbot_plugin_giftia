import asyncio
import random
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image

from ..llm.preset_prompts import build_tts_xml_instructions
from ..llm.prompt import build_reply_prompt
from ..utils.anti_drool import filter_duplicate_replies
from ..utils.schemas import MessageData
from .media_captioner import MediaCaptioner
from .memory_recall import conf_int, search_memories_with_rerank
from .tool_executor import ToolExecutor


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
            "search_memories",
            "delete_memories",
            "tools_to_call",
            "native_tools_called",
            "schedule_tasks",
            "delete_schedule_tasks",
            "all_tasks",
            "add_stickers",
            "send_stickers",
            "search_histories",
            "get_message_contexts",
            "task_board_actions",
            "tts_segments",
            "recaption_requests",
            "set_call_names",
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

    @staticmethod
    def _append_relevant_memory_texts(
        relevant_memories: list[str],
        memories: list[dict] | None,
    ) -> None:
        if not memories:
            return
        seen = set(relevant_memories)
        for memory in memories:
            text = str(memory.get("text") or "").strip()
            if text and text not in seen:
                relevant_memories.append(text)
                seen.add(text)

    def commit_pending_session_recalled_memories(
        self,
        bot_name: str,
        group_or_user_id: str,
        pending_recall_memories: list[dict] | None,
    ) -> None:
        if not pending_recall_memories or not self.plugin.embedding_conf.get(
            "session_recall_enabled", True
        ):
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
        )
        pending_recall_memories.clear()

    async def _search_current_recall_memories(
        self,
        bot_name: str,
        group_or_user_id: str,
        current_message: MessageData | None,
        remind_message: str | None,
        recent_messages: list[MessageData],
    ) -> list[dict]:
        conf = self.plugin.embedding_conf
        if not conf.get("enabled", False) or not conf.get(
            "session_recall_enabled", True
        ):
            return []

        query = ""
        if current_message and current_message.content:
            query = current_message.content.strip()
        elif remind_message:
            query = remind_message.strip()
        if not query:
            return []

        return await search_memories_with_rerank(
            self.plugin,
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            query=query,
            recent_messages=recent_messages,
            limit_key="session_recall_search_limit",
            log_context="会话记忆自动召回",
        )

    async def dispatch_llm_reply_loop(
        self,
        event: AstrMessageEvent,
        bot_name: str,
        nickname: str,
        group_or_user_id: str,
        current_message: MessageData | None = None,
        remind_message: str | None = None,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        relevant_memories: list[str] | None = None,
        pending_recall_memories: list[dict] | None = None,
        tool_results: list[dict[str, str]] | None = None,
        other_data: list[str] | None = None,
        times: int = 0,
        sent_messages: list[str] | None = None,
    ):
        """
        集成用户提示词构建、LLM调用、生成表情包与工具执行，支持递归的工具执行循环。
        """
        is_first_turn = sent_messages is None
        if sent_messages is None:
            sent_messages = []
        if pending_recall_memories is None:
            pending_recall_memories = []
        bot_conf = self.plugin.bot_map[bot_name]
        iso_string = datetime.now().isoformat()
        max_loop = self.plugin.tools_config.get("max_loop", 10)

        if times >= max_loop:
            logger.warning(
                f"{bot_name} 达到最大工具调用次数 ({max_loop})，强制退出循环"
            )
            # 记录系统指令提示到数据库
            await self.plugin.data_cache.add_message(
                bot_name,
                group_or_user_id,
                MessageData(
                    nickname=nickname,
                    user_id=event.get_self_id(),
                    group_or_user_id=group_or_user_id,
                    time=iso_string,
                    message_id="",
                    content=f"系统提示：当前已经达到最大工具调用次数 {max_loop}，请立即停止调用工具，并以现有信息作为最终结果进行回复。",
                    is_recalled=False,
                    media_id_list=[],
                    role="operation_log",
                ),
            )

        # 1. 获取近期消息上下文并处理延迟转述
        recent_messages = await self.plugin.data_cache.get_recent_message(
            bot_name, group_or_user_id, self.plugin.msg_number
        )
        if is_first_turn:
            self_id = str(event.get_self_id())
            for msg in recent_messages:
                if msg.user_id and str(msg.user_id) == self_id and msg.content:
                    sent_messages.append(msg.content)

        caption_config = self.plugin.get_caption_config(bot_conf)
        media_captions = await self.media_captioner.transcribe_media_if_deferred(
            bot_name=bot_name,
            recent_messages=recent_messages,
            caption_config=caption_config,
            group_or_user_id=group_or_user_id,
        )

        if relevant_memories is None:
            relevant_memories = []

        if times == 0:
            current_recall_memories = await self._search_current_recall_memories(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                current_message=current_message,
                remind_message=remind_message,
                recent_messages=recent_messages,
            )
            if current_recall_memories:
                pending_recall_memories.extend(current_recall_memories)
                self._append_relevant_memory_texts(
                    relevant_memories, current_recall_memories
                )

        session_recalled_memories = []
        if self.plugin.embedding_conf.get("session_recall_enabled", True):
            max_items, ttl_seconds = self._session_recall_limits()
            session_recalled_memories = (
                self.plugin.data_cache.get_session_recalled_memories(
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                    max_items=max_items,
                    ttl_seconds=ttl_seconds,
                )
            )

        # 2. 读取画像、关系与状态数据
        bot_status = await self.plugin.data_cache.get_bot_status(
            bot_name=bot_name,
            group_id=group_or_user_id,
        )
        user_profile = await self.plugin.data_cache.get_user_profile_record(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            user_id=event.get_sender_id(),
        )
        group_profile = await self.plugin.data_cache.get_group_profile(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
        )
        user_relation = await self.plugin.data_cache.get_user_relation(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            user_id=event.get_sender_id(),
        )
        active_user_briefs = await self.plugin.data_cache.build_active_user_briefs(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            recent_messages=recent_messages,
            current_user_id=event.get_sender_id(),
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

        # 读取长期记忆
        long_memories = []
        inject_limit = self._conf_int(self.plugin.embedding_conf, "inject_limit", 20)
        if (
            self.plugin.embedding_conf.get("enabled", False)
            and not self.plugin.embedding_conf.get("session_recall_enabled", True)
            and inject_limit > 0
        ):
            long_memories = await self.plugin.data_cache.get_memories(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                limit=inject_limit,
            )

        # 获取表情包池并抽取随机样本
        bot_sticker_cache = await self.plugin.emoji_manager.get_random_stickers(
            bot_name
        )

        # 3. 构造回复提示词 Prompt
        user_prompt = build_reply_prompt(
            recent_messages=recent_messages,
            media_captions=media_captions,
            current_message=current_message,
            remind_message=remind_message,
            group_data=str(
                await event.get_group(event.get_group_id())
                if event.get_group_id()
                else ""
            ),
            user_id=event.get_sender_id(),
            nickname=event.get_sender_name() or "",
            bot_status=bot_status,
            tool_results=tool_results,
            long_memories=long_memories,
            session_recalled_memories=session_recalled_memories,
            relevant_memories=relevant_memories,
            user_profile=user_profile,
            group_profile=group_profile,
            active_user_briefs=active_user_briefs,
            short_tasks=short_tasks,
            short_task_limit=short_task_limit,
            other_data=other_data,
            user_relation=user_relation,
            bot_sticker=bot_sticker_cache,
            message_truncate_limit=getattr(self.plugin, "reply_message_truncate_limit", 1500),
        )
        logger.debug(f"[Giftia] 触发大模型回复,构造回复提示词：{user_prompt}")

        llm_reply_conf = bot_conf.get("llm_reply_conf", {})
        provider_ids = llm_reply_conf.get("provider_ids")
        if not provider_ids:
            old_provider_id = llm_reply_conf.get("provider_id")
            if old_provider_id:
                provider_ids = [old_provider_id] + llm_reply_conf.get(
                    "fallback_provider_ids", []
                )
            else:
                logger.error(f"{bot_name} 未配置回复模型ID")
                return
        provider_ids = [p for p in provider_ids if p]
        if not provider_ids:
            logger.error(f"{bot_name} 未配置回复模型ID")
            return

        provider_selection_mode = llm_reply_conf.get(
            "provider_selection_mode", "fallback"
        )
        if provider_selection_mode == "random":
            random.shuffle(provider_ids)

        # 4. 调用 LLM 进行回复
        llm_result = await self.plugin.call_llm.call_llm_reply(
            event=event,
            group_or_user_id=group_or_user_id,
            provider_ids=provider_ids,
            system_prompt=llm_reply_conf.get("llm_reply_prompt"),
            user_prompt=user_prompt,
            use_source_tools=self.plugin.tools_config.get("use_source_tools", False),
            force_xml_tools=self.plugin.tools_config.get("force_xml_tools", False),
            enabled_features=self.plugin.tools_config.get(
                "enabled_interactive_features"
            ),
            tts_instruction=(
                build_tts_xml_instructions(
                    self.plugin.tts_manager.provider_type(),
                    self.plugin.tts_manager.language_options(),
                )
                if hasattr(self.plugin, "tts_manager")
                and self.plugin.tts_manager.enabled()
                else ""
            ),
            image_urls=image_urls,
            audio_urls=audio_urls,
            timeout=self.plugin.tools_config.get("timeout", 120),
        )

        if not llm_result:
            logger.error(f"{bot_name} LLM回复失败")
            return

        # Anti-drooling optimization for low-intelligence models:
        # Filter out messages that have already been sent in the current XML tool calling loop.
        filter_duplicate_replies(llm_result, sent_messages)

        # 5. 空回复拦截处理
        if (
            not remind_message
            and times == 0
            and not llm_result.msg_chains
            and not self._has_non_message_work(llm_result)
        ):
            await self.plugin.db.update_message_reply_decision(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                message_id=current_message.message_id if current_message else "",
                reply_decision=0,
            )

        # 6. 更新机器人状态
        if llm_result.status:
            await self.plugin.data_cache.set_bot_status(
                bot_name=bot_name,
                group_id=group_or_user_id,
                status=llm_result.status,
            )

        # 7. 后台分析与添加表情包
        if llm_result.add_stickers:
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
        yield llm_result

        # 初始化参数用于潜在的下一次递归
        if other_data is None:
            other_data = []
        if tool_results is None:
            tool_results = []

        # 9. 调用 ToolExecutor 执行工具和搜索查询
        image_base64 = await self.tool_executor.execute_tools_and_queries(
            event=event,
            bot_name=bot_name,
            nickname=nickname,
            group_or_user_id=group_or_user_id,
            llm_result=llm_result,
            recent_messages=recent_messages,
            relevant_memories=relevant_memories,
            other_data=other_data,
            tool_results=tool_results,
            times=times,
        )

        # 如果工具调用产生了图片，立即发送
        if image_base64:
            yield await event.send(
                MessageChain([Image.fromBase64(b64) for b64 in image_base64])
            )
            logger.info(
                f"{bot_name} 从MCP工具收到 {len(image_base64)} 张图片，直接发出去了"
            )

        # 10. 判断是否需要继续循环迭代
        if (
            len(llm_result.tools_to_call) > 0
            or len(llm_result.search_memories) > 0
            or len(llm_result.all_tasks) > 0
            or len(llm_result.search_histories) > 0
            or len(llm_result.get_message_contexts) > 0
            or (hasattr(llm_result, "recaption_requests") and len(llm_result.recaption_requests) > 0)
        ):
            logger.debug(f"{bot_name} llm step {times + 1} ...")
            async for chunk in self.dispatch_llm_reply_loop(
                event=event,
                bot_name=bot_name,
                nickname=nickname,
                group_or_user_id=group_or_user_id,
                current_message=current_message,
                relevant_memories=relevant_memories,
                pending_recall_memories=pending_recall_memories,
                tool_results=tool_results,
                remind_message=remind_message,
                image_urls=image_base64,
                times=times + 1,
                other_data=other_data,
                sent_messages=sent_messages,
            ):
                yield chunk
