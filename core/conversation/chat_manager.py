import asyncio
from unittest.mock import AsyncMock, MagicMock

from aiocqhttp import CQHttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At, Node, Nodes, Plain
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.utils.session_lock import session_lock_manager
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
    AiocqhttpAdapter,
)

from ..utils.message_media import format_node_components
from ..utils.notice_parse import NoticeParseResult
from ..utils.qq_official_action import is_qq_official
from ..utils.schemas import XmlLlmResult
from .action_dispatcher import ActionDispatcher
from .decision_engine import DecisionEngine
from .reply_pipeline import ReplyPipeline


class ChatManager:
    def __init__(self, plugin):
        self.plugin = plugin
        self.decision_engine = DecisionEngine(plugin)
        self.reply_pipeline = ReplyPipeline(plugin)
        self.action_dispatcher = ActionDispatcher(plugin)

    async def handle_message(self, event: AstrMessageEvent):
        """接收并处理消息"""
        # 1. 检查白名单拦截
        if not self.decision_engine.check_whitelists(event):
            return

        # 2. 处理撤回消息通知
        msg_obj = getattr(event, "message_obj", None)
        raw_message = getattr(msg_obj, "raw_message", None) if msg_obj else None
        if raw_message:
            message_name = getattr(raw_message, "name", "")
            if message_name in ["notice.group_recall", "notice.friend_recall"]:
                recalled_message_id = str(getattr(raw_message, "message_id", ""))
                bot_name = self.plugin.adapter_id_map.get(event.platform_meta.id)
                if bot_name and recalled_message_id:
                    group_or_user_id = event.get_group_id() or event.get_sender_id()
                    try:
                        await self.plugin.data_cache.set_message_recalled(
                            bot_name, group_or_user_id, [recalled_message_id]
                        )
                        logger.debug(
                            f"{bot_name} 收到撤回消息事件，已标注消息 {recalled_message_id} 为撤回"
                        )
                    except Exception as e:
                        logger.error(f"处理撤回消息失败: {e}")
                return

        # 3. 跳过机器人自己的发言（通知类事件除外）
        raw_msg = getattr(msg_obj, "raw_message", None) if msg_obj else None
        post_type = (
            raw_msg.get("post_type", "")
            if hasattr(raw_msg, "get")
            else getattr(raw_msg, "post_type", "")
        ) if raw_msg else ""
        if post_type != "notice" and event.get_sender_id() == event.get_self_id():
            logger.debug(f"{event.platform_meta.id} 消息为机器人自己的消息，跳过处理")
            return

        # Intercept event.send to capture bot replies (non-LLM responses/tool messages)
        original_send = event.send

        async def intercepted_send(message: MessageChain):
            logger.debug(f"[Giftia] intercepted_send triggered for message: {message}")
            bypass = getattr(event, "_giftia_bypass_logging", False)
            ret = await original_send(message)
            if getattr(self.plugin, "_terminated", False):
                return ret
            if bypass:
                logger.debug("[Giftia] intercepted_send bypass=True, skipping log")
                return ret

            try:
                from datetime import datetime

                from ..utils.schemas import MessageData

                bot_name = self.plugin.adapter_id_map.get(event.platform_meta.id)
                group_or_user_id = event.get_group_id() or event.get_sender_id()
                logger.debug(
                    f"[Giftia] intercepted_send: bot_name={bot_name}, group_or_user_id={group_or_user_id}"
                )
                if bot_name:
                    bot_conf = self.plugin.bot_map.get(bot_name, {})
                    nickname = bot_conf.get("nickname", bot_name)

                    # 动态判定活跃窗口状态
                    fmt_key = f"{bot_name}:{group_or_user_id}"
                    active_counter = self.plugin.active_reply_counters.get(fmt_key, 0)
                    is_active_window = active_counter > 0
                    defer_caption = not is_active_window

                    parsed_msg = await self.plugin.message_parser.chain_to_result(
                        message.chain, defer_caption=defer_caption, event=event
                    )
                    logger.debug(
                        f"[Giftia] intercepted_send logging message content: {parsed_msg.content}"
                    )
                    await self.plugin.data_cache.add_message(
                        bot_name,
                        group_or_user_id,
                        MessageData(
                            nickname=nickname,
                            user_id=event.get_self_id(),
                            group_or_user_id=group_or_user_id,
                            time=datetime.now().isoformat(),
                            message_id="",
                            content=parsed_msg.content,
                            is_recalled=0,
                            media_id_list=parsed_msg.media_id_list,
                            forward_messages=parsed_msg.forward_messages,
                        ),
                    )
                    logger.debug(
                        "[Giftia] intercepted_send successfully logged to database"
                    )
            except Exception as e:
                logger.error(
                    f"[Giftia] Error logging intercepted bot message: {e}",
                    exc_info=True,
                )
            return ret

        event.send = intercepted_send

        # 4. 创建后台回复任务
        task = asyncio.create_task(self.job(event))
        task_id = str(id(task))
        self.plugin.running_tasks[task_id] = task
        try:
            await task

            # 被动记忆后台触发检查
            if self.plugin.passive_memory_enabled:
                bot_name = self.plugin.adapter_id_map.get(event.platform_meta.id)
                group_or_user_id = event.get_group_id() or event.get_sender_id()
                if bot_name:
                    asyncio.create_task(
                        self.plugin.passive_memory_manager.check_and_trigger_passive_memory(
                            bot_name=bot_name,
                            group_or_user_id=group_or_user_id,
                            self_id=event.get_self_id(),
                        )
                    )
        except asyncio.CancelledError:
            logger.info(f"{task_id} 任务被取消")
        except Exception as e:
            logger.error(f"{task_id} 任务执行失败: {e}", exc_info=True)
        finally:
            self.plugin.running_tasks.pop(task_id, None)

    async def job(self, event: AstrMessageEvent):
        # 获取基础信息
        bot_name = self.plugin.adapter_id_map[event.platform_meta.id]
        bot_conf = self.plugin.bot_map[bot_name]
        nickname = bot_conf.get("nickname", bot_name)
        group_or_user_id = event.get_group_id() or event.get_sender_id()

        # 检查是否开启延迟多媒体转述 (仅在没有 @ 且不在发言窗口时延迟)
        caption_config = self.plugin.get_caption_config(bot_conf)
        defer_enabled = caption_config.get("defer_caption_enabled", True)

        should_defer = False
        if defer_enabled:
            is_just_at = any(
                isinstance(c, At) and str(c.qq) == event.get_self_id()
                for c in event.get_messages()
            )
            is_private = not event.get_group_id()
            if is_private and self.plugin.private_chat_bypass:
                is_just_at = True

            fmt_key = f"{bot_name}:{group_or_user_id}"
            active_counter = self.plugin.active_reply_counters.get(fmt_key, 0)
            is_active_window = active_counter > 0

            if not is_just_at and not is_active_window:
                should_defer = True

        # 解析用户消息并缓存多媒体
        async with self.plugin.parse_locks[f"{bot_name}:{group_or_user_id}"]:
            (
                current_message,
                image_urls,
                audio_urls,
            ) = await self.plugin.message_parser.parse_user_message(
                event, bot_name, defer_caption=should_defer
            )

        # Check if the message is a command-type message
        is_command = False
        activated_handlers = event.get_extra("activated_handlers", [])
        for handler in activated_handlers:
            if handler.handler_name == "on_message":
                continue
            for filter_obj in handler.event_filters:
                if filter_obj.__class__.__name__ in (
                    "CommandFilter",
                    "CommandGroupFilter",
                ):
                    is_command = True
                    break
            if is_command:
                break

        if is_command:
            logger.debug(
                f"{bot_name} command message detected, logged to database, skipping LLM reply"
            )
            return

        # 检查是否为系统通知类事件，若是则统一在此处更新禁言状态，并跳过 LLM 回复（贴表情除外）
        notice_result: NoticeParseResult | None = getattr(event, "_notice_result", None)
        if notice_result is None:
            raw_msg = getattr(getattr(event, "message_obj", None), "raw_message", None)
            if (
                raw_msg
                and hasattr(self.plugin, "message_parser")
                and hasattr(self.plugin.message_parser, "notice_parser")
            ):
                notice_result = await self.plugin.message_parser.notice_parser.parse_notice(
                    event, raw_msg, bot_name
                )
                setattr(event, "_notice_result", notice_result)

        if notice_result and notice_result.is_notice:
            # 禁言事件：更新缓存并根据条件触发告状
            if notice_result.is_ban_event:
                if notice_result.is_all_member_ban:
                    if hasattr(self.plugin, "data_cache"):
                        self.plugin.data_cache.set_bot_muted(bot_name, notice_result.group_id, -1)
                        logger.info(
                            f"[Giftia] 群 {notice_result.group_id} 开启全员禁言，Bot {bot_name} 进入静默状态"
                        )
                elif notice_result.is_target_self:
                    if hasattr(self.plugin, "data_cache"):
                        self.plugin.data_cache.set_bot_muted(
                            bot_name, notice_result.group_id, notice_result.duration
                        )
                        logger.info(
                            f"[Giftia] Bot {bot_name} 在群 {notice_result.group_id} 被禁言 {notice_result.duration} 秒，进入静默状态"
                        )

                if notice_result.is_all_member_ban or notice_result.is_target_self:
                    asyncio.create_task(
                        self.report_ban_to_stronghold(
                            bot_name=bot_name,
                            event=event,
                            notice_result=notice_result,
                        )
                    )
            # 解禁事件：更新缓存
            elif notice_result.is_lift_ban_event:
                if (notice_result.is_all_member_ban or notice_result.is_target_self) and hasattr(self.plugin, "data_cache"):
                    self.plugin.data_cache.lift_bot_mute(bot_name, notice_result.group_id)
                    logger.info(
                        f"[Giftia] 群 {notice_result.group_id} 禁言已解除，Bot {bot_name} 恢复正常发言状态"
                    )

            if notice_result.role == "system":
                logger.debug(
                    f"{bot_name} 系统通知事件({notice_result.notice_type or notice_result.sub_type})已记录入库，跳过 LLM 自动回复"
                )
                return

        # 5. 调用决策引擎进行发言判断
        (
            should_reply,
            relevant_memories,
            is_just_at,
            pending_recall_memories,
        ) = await self.decision_engine.evaluate_decision(
            event=event,
            bot_name=bot_name,
            nickname=nickname,
            group_or_user_id=group_or_user_id,
            current_message=current_message,
            image_urls=image_urls,
            audio_urls=audio_urls,
        )

        if not should_reply:
            return

        # 6. 进入 LLM 回复流水线
        reply_key = f"{bot_name}:{group_or_user_id}"
        async with session_lock_manager.acquire_lock(event.unified_msg_origin):
            self.plugin.replying_status[reply_key] = (
                self.plugin.replying_status.get(reply_key, 0) + 1
            )
            if pending_recall_memories is None:
                pending_recall_memories = []

            try:
                has_sent_reply = False
                async for chunk in self.reply_pipeline.dispatch_llm_reply_loop(
                    event=event,
                    bot_name=bot_name,
                    nickname=nickname,
                    group_or_user_id=group_or_user_id,
                    current_message=current_message,
                    image_urls=image_urls,
                    audio_urls=audio_urls,
                    relevant_memories=relevant_memories,
                    pending_recall_memories=pending_recall_memories,
                ):
                    if chunk:
                        if isinstance(chunk, XmlLlmResult):
                            # 派发具体写操作和消息发送
                            await self.action_dispatcher.dispatch_actions(
                                event=event,
                                bot_name=bot_name,
                                nickname=nickname,
                                group_or_user_id=group_or_user_id,
                                llm_result=chunk,
                            )
                            has_tts_reply = (
                                bool(chunk.tts_segments)
                                and hasattr(self.plugin, "tts_manager")
                                and self.plugin.tts_manager.enabled(bot_conf)
                            )
                            if chunk.msg_chains or has_tts_reply or chunk.repeat_message_ids:
                                has_sent_reply = True
                    else:
                        logger.error(f"{bot_name} 生成消息失败，收到空消息块")

                if has_sent_reply:
                    fmt_key = f"{bot_name}:{group_or_user_id}"
                    active_counter = self.plugin.active_reply_counters.get(fmt_key, 0)
                    decision_conf = bot_conf.get("decision_conf", {})
                    window_size = decision_conf.get("reply_active_window", 10)
                    self.plugin.active_reply_counters[fmt_key] = window_size

                    trigger_msg_id = None
                    if (
                        active_counter == 0
                        and "current_message" in locals()
                        and current_message
                    ):
                        trigger_msg_id = current_message.message_id

                    await self.plugin.passive_memory_manager.mark_silence_summary_armed(
                        bot_name=bot_name,
                        group_or_user_id=group_or_user_id,
                        trigger_msg_id=trigger_msg_id,
                    )
                    logger.info(
                        f"{bot_name} 机器人发言，重置接话分析窗口计数为 {window_size}"
                    )
                self.reply_pipeline.commit_pending_session_recalled_memories(
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                    pending_recall_memories=pending_recall_memories,
                )
            finally:
                self.plugin.replying_status[reply_key] = max(
                    0, self.plugin.replying_status.get(reply_key, 0) - 1
                )

    def get_platform_adapter(
        self, adapter_id: str
    ) -> tuple[CQHttp, PlatformMetadata] | None:
        """获取平台适配器实例，目前仅支持aiocqhttp"""
        platforms = self.plugin.context.platform_manager.get_insts()
        for p in platforms:
            if isinstance(p, AiocqhttpAdapter) and p.metadata.id == adapter_id:
                return p.bot, p.metadata
        return None

    async def remind_task(
        self,
        unified_msg_origin: str,
        adapter_id: str,
        bot_name: str,
        nickname: str,
        self_id: str,
        platform_name: str,
        user_id: str,
        user_name: str,
        group_id: str,
        group_or_user_id: str,
        remind_message: str,
    ):
        """处理定时任务调度提醒"""
        # 检查是否处于禁言静默状态
        if (
            group_id
            and hasattr(self.plugin, "data_cache")
            and self.plugin.data_cache.is_bot_muted(bot_name, str(group_id))
        ):
            logger.info(
                f"[Giftia] Bot {bot_name} 在群 {group_id} 处于禁言静默状态，跳过定时任务提醒"
            )
            return

        reply_key = f"{bot_name}:{group_or_user_id}"
        bot_conf = self.plugin.get_bot_config(bot_name)
        async with session_lock_manager.acquire_lock(unified_msg_origin):
            self.plugin.replying_status[reply_key] = (
                self.plugin.replying_status.get(reply_key, 0) + 1
            )
            try:
                mock_event = self.fake_event(
                    self_id=self_id,
                    sender_id=user_id,
                    sender_name=user_name,
                    group_id=group_id,
                    unified_msg_origin=unified_msg_origin,
                    adapter_id=adapter_id,
                )
                has_sent_reply = False
                pending_recall_memories = []
                async for chunk in self.reply_pipeline.dispatch_llm_reply_loop(
                    event=mock_event,
                    bot_name=bot_name,
                    nickname=nickname,
                    group_or_user_id=group_or_user_id,
                    remind_message=f"[定时任务唤醒] {user_name}({user_id}): {remind_message}",
                    pending_recall_memories=pending_recall_memories,
                ):
                    if chunk:
                        if isinstance(chunk, XmlLlmResult):
                            if hasattr(self.plugin, "tts_manager") and self.plugin.tts_manager.enabled(bot_conf):
                                self.plugin.tts_manager.preprocess_signatures(chunk, bot_conf)
                            if platform_name == "aiocqhttp" or is_qq_official(platform_name):
                                if mock_event:
                                    await self.action_dispatcher.dispatch_actions(
                                        event=mock_event,
                                        bot_name=bot_name,
                                        nickname=nickname,
                                        group_or_user_id=group_or_user_id,
                                        llm_result=chunk,
                                    )
                                    has_tts_reply = (
                                        bool(chunk.tts_segments)
                                        and hasattr(self.plugin, "tts_manager")
                                        and self.plugin.tts_manager.enabled(bot_conf)
                                    )
                                    if (
                                        chunk.msg_chains
                                        or has_tts_reply
                                        or chunk.repeat_message_ids
                                    ):
                                        has_sent_reply = True
                                    continue
                            # 降级到普通消息发送
                            if not chunk.msg_chains and not chunk.tts_segments:
                                continue
                            for item_type, item_index in self.action_dispatcher.get_output_order(
                                chunk
                            ):
                                if item_type == "message":
                                    if item_index < 0 or item_index >= len(chunk.msg_chains):
                                        continue
                                    msg_chain = chunk.msg_chains[item_index]
                                elif item_type == "tts":
                                    msg_chain, _ = (
                                        await self.action_dispatcher.build_tts_message_chain(
                                            mock_event, chunk, item_index, bot_conf
                                        )
                                    )
                                else:
                                    continue
                                if not msg_chain:
                                    continue
                                await self.plugin.context.send_message(
                                    unified_msg_origin, MessageChain(msg_chain)
                                )
                                has_sent_reply = True
                    else:
                        logger.error(f"{bot_name} 定时任务调度失败，未获取到回复内容")

                if has_sent_reply:
                    fmt_key = f"{bot_name}:{group_or_user_id}"
                    bot_conf = self.plugin.bot_map.get(bot_name, {})
                    decision_conf = bot_conf.get("decision_conf", {})
                    window_size = decision_conf.get("reply_active_window", 10)
                    self.plugin.active_reply_counters[fmt_key] = window_size
                    await self.plugin.passive_memory_manager.mark_silence_summary_armed(
                        bot_name=bot_name,
                        group_or_user_id=group_or_user_id,
                    )
                    logger.info(
                        f"{bot_name} 定时任务发言，重置接话分析窗口计数为 {window_size}"
                    )
                self.reply_pipeline.commit_pending_session_recalled_memories(
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                    pending_recall_memories=pending_recall_memories,
                )
            finally:
                self.plugin.replying_status[reply_key] = max(
                    0, self.plugin.replying_status.get(reply_key, 0) - 1
                )

    def fake_event(
        self,
        self_id: str,
        sender_id: str,
        sender_name: str,
        group_id: str,
        unified_msg_origin: str,
        adapter_id: str,
    ) -> AstrMessageEvent:
        """伪造一个aiocqhttp的event，用于主动消息复用被动消息函数"""
        mock_event = MagicMock(spec=AiocqhttpMessageEvent)
        adapter = self.get_platform_adapter(adapter_id)
        if adapter:
            bot, metadata = adapter
            mock_event.bot = bot
            mock_event.platform_meta = metadata
        mock_event.get_platform_name = MagicMock(return_value="aiocqhttp")
        mock_event.get_group = AsyncMock(return_value="")
        mock_event.get_self_id = MagicMock(return_value=self_id)
        mock_event.get_group_id = MagicMock(return_value=group_id)
        mock_event.get_sender_id = MagicMock(return_value=sender_id)
        mock_event.get_sender_name = MagicMock(return_value=sender_name)
        mock_event.unified_msg_origin = unified_msg_origin
        return mock_event

    async def report_ban_to_stronghold(
        self,
        bot_name: str,
        event: AstrMessageEvent,
        notice_result: NoticeParseResult | None = None,
        raw_event: dict | None = None,
    ):
        """当 Bot 被禁言时，向预先设置的据点会话告状并发送前20条消息合并转发"""
        try:
            # 1. 检查是否配置了据点
            if not hasattr(self.plugin, "data_cache"):
                return
            stronghold = await self.plugin.data_cache.get_stronghold()
            if not stronghold or not stronghold.get("unified_msg_origin"):
                logger.debug("[Giftia] 未设置通知据点，跳过禁言告状")
                return

            if notice_result is None and raw_event:
                if hasattr(self.plugin, "message_parser") and hasattr(self.plugin.message_parser, "notice_parser"):
                    notice_result = await self.plugin.message_parser.notice_parser.parse_notice(
                        event, raw_event, bot_name
                    )

            if not notice_result:
                return

            # 只有在 ban 且 (目标是Bot自己 或 全员禁言) 时才触发告状（解禁时不触发告状）
            if not notice_result.is_ban_event or not (
                notice_result.is_target_self or notice_result.is_all_member_ban
            ):
                return

            group_id = notice_result.group_id or str(event.get_group_id() or "")
            # 避免向当前已被禁言的群聊重复发送
            stronghold_origin = stronghold.get("unified_msg_origin")
            stronghold_group = str(stronghold.get("group_id") or "")
            if stronghold_group and stronghold_group == group_id:
                logger.warning(
                    f"[Giftia] 通知据点正是当前被禁言群 {group_id}，无法在该群发送告状消息"
                )
                return

            # 3. 解析群名称与操作者名称
            group_name = ""
            if (
                hasattr(event, "bot")
                and hasattr(event.bot, "get_group_info")
                and group_id.isdigit()
            ):
                try:
                    info = await event.bot.get_group_info(
                        group_id=int(group_id), no_cache=False
                    )
                    if isinstance(info, dict):
                        group_name = info.get("group_name", "")
                except Exception:
                    pass
            group_display = (
                f"【{group_name}】({group_id})"
                if group_name
                else f"群聊({group_id})"
            )

            op_display = (
                notice_result.operator_ref
                or notice_result.operator_id
                or "管理员"
            )

            # 4. 构造告状文本
            if notice_result.is_all_member_ban:
                complaint_text = f"呜呜呜~我在{group_display}被【{op_display}】开启全员禁言了！"
            else:
                from ..utils.notice_parse import NoticeParser

                dur_text = NoticeParser.format_duration(notice_result.duration)
                complaint_text = f"呜呜呜~我在{group_display}被【{op_display}】禁言了（时长：{dur_text}）！"

            # 5. 发送告状文本
            logger.info(
                f"[Giftia] 正在向据点 {stronghold_origin} 发送禁言告状: {complaint_text}"
            )
            await self.plugin.context.send_message(
                stronghold_origin, MessageChain([Plain(complaint_text)])
            )

            # 6. 获取禁言前 20 条真实聊天消息并构造合并转发（排除系统通知消息）
            recent_msgs = await self.plugin.db.get_messages(
                group_or_user_id=group_id, bot_name=bot_name, limit=40
            )
            # 过滤系统通知，确保转发的全为真实用户与Bot的聊天记录
            chat_msgs = [
                m
                for m in (recent_msgs or [])
                if str(getattr(m, "user_id", "") or "").lower() != "system"
                and getattr(m, "role", "") != "system"
                and not str(getattr(m, "content", "") or "").startswith(
                    "【系统消息】"
                )
            ]
            # 截取案发前最新的 20 条消息（切片修复）
            recent_chat_msgs = chat_msgs[-20:]

            if recent_chat_msgs:
                nodes = []
                media_cache_map: dict = {}
                for msg in recent_chat_msgs:
                    u_id = str(msg.user_id or "10000")
                    u_name = str(msg.nickname or msg.user_id or "用户")
                    content_str = msg.content or ""
                    node_components = await format_node_components(
                        content_str,
                        db=self.plugin.db,
                        media_cache_map=media_cache_map,
                    )
                    nodes.append(
                        Node(
                            uin=u_id,
                            name=u_name,
                            content=node_components,
                        )
                    )
                if nodes:
                    await self.plugin.context.send_message(
                        stronghold_origin, MessageChain([Nodes(nodes)])
                    )
                    logger.info(
                        f"[Giftia] 已向据点 {stronghold_origin} 发送禁言前 {len(nodes)} 条线索消息合并转发"
                    )
        except Exception as e:
            logger.error(f"[Giftia] 向据点发送禁言告状失败: {e}", exc_info=True)

