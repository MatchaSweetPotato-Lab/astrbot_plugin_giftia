import asyncio
import random
import re
import uuid
from datetime import datetime
from xml.sax.saxutils import quoteattr

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ..utils.schemas import MessageData, XmlLlmResult
from ..utils.anime_search import search_anime_by_media_id
from ..utils.saucenao_search import search_illust_by_media_id


class ActionDispatcher:
    def __init__(self, plugin):
        self.plugin = plugin

    def _interactive_feature_enabled(self, feature_name: str) -> bool:
        enabled_features = self.plugin.tools_config.get("enabled_interactive_features")
        if enabled_features is None:
            return True
        return any(str(item).startswith(feature_name) for item in enabled_features)

    def _find_recent_message(
        self, bot_name: str, group_or_user_id: str, message_id: str
    ) -> MessageData | None:
        fmt_key = f"{bot_name}:{group_or_user_id}"
        messages = self.plugin.data_cache.recent_messages.get(fmt_key, [])
        for msg in reversed(messages):
            if str(msg.message_id) == str(message_id):
                return msg
        return None

    async def _dispatch_task_board_actions(
        self,
        event: AstrMessageEvent,
        bot_name: str,
        group_or_user_id: str,
        llm_result: XmlLlmResult,
    ) -> list[str]:
        if not llm_result.task_board_actions:
            return []

        if not hasattr(self.plugin, "task_board"):
            return [
                "<task_board action='unknown' result='failed' reason='task board unavailable'/>"
            ]

        logs = []
        actor_user_id = str(event.get_sender_id() or "")
        actor_name = event.get_sender_name() or ""

        for item in llm_result.task_board_actions:
            action = str(item.get("action") or "").strip().lower()
            if action == "create":
                ok, message, task = await self.plugin.task_board.create_task(
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                    creator_user_id=actor_user_id,
                    creator_nickname=actor_name,
                    content=item.get("content") or "",
                    expires_at=item.get("expires_at") or "",
                )
                task_id = task.task_id if task else ""
                logs.append(
                    f"<task_board action='create' task_id={quoteattr(task_id)} "
                    f"result={quoteattr('success' if ok else 'failed')} "
                    f"message={quoteattr(message)}/>"
                )
                continue

            if action in {"complete", "cancel"}:
                status = "completed" if action == "complete" else "canceled"
                ok, message, task = await self.plugin.task_board.close_task(
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                    task_id=item.get("task_id") or "",
                    status=status,
                    actor_user_id=actor_user_id,
                    reason=item.get("reason") or "",
                )
                task_id = task.task_id if task else str(item.get("task_id") or "")
                logs.append(
                    f"<task_board action={quoteattr(action)} task_id={quoteattr(task_id)} "
                    f"result={quoteattr('success' if ok else 'failed')} "
                    f"message={quoteattr(message)}/>"
                )
                continue

            logs.append(
                f"<task_board action={quoteattr(action)} result='failed' "
                "message='不支持的任务操作'/>"
            )

        return logs

    @staticmethod
    def get_output_order(llm_result: XmlLlmResult) -> list[tuple[str, int]]:
        """
        [Internal Helper] 获取 LLM 输出的顺序列表。

        此方法属于内部辅助函数，主要供 ActionDispatcher 及 ChatManager（用于派发定时任务输出）调用。
        """
        if llm_result.output_order:
            return list(llm_result.output_order)
        order = [("message", index) for index in range(len(llm_result.msg_chains))]
        order.extend(("tts", index) for index in range(len(llm_result.tts_segments)))
        return order

    async def build_tts_message_chain(
        self,
        event: AstrMessageEvent,
        llm_result: XmlLlmResult,
        index: int,
    ):
        """
        [Internal Helper] 构建指定的 TTS 消息链。

        此方法属于内部辅助函数，主要供 ActionDispatcher 及 ChatManager（用于构建定时任务的 TTS 消息）调用。
        """
        if not hasattr(self.plugin, "tts_manager"):
            return None, ""
        if not self.plugin.tts_manager.enabled():
            return None, ""
        if index < 0 or index >= len(llm_result.tts_segments):
            return None, ""

        segment = llm_result.tts_segments[index]
        record = await self.plugin.tts_manager.build_record(event, segment)
        if record:
            return [record], segment.text

        logger.warning("[Giftia TTS] 语音合成重试后依然失败，放弃发送该段语音消息。")
        return None, ""

    async def _dispatch_aiocqhttp_outputs(
        self,
        event: AstrMessageEvent,
        bot_name: str,
        nickname: str,
        group_or_user_id: str,
        llm_result: XmlLlmResult,
    ) -> None:
        sent_index = 0
        for item_type, item_index in self.get_output_order(llm_result):
            if item_type == "message":
                if item_index < 0 or item_index >= len(llm_result.msg_chains):
                    continue
                msg_chain = llm_result.msg_chains[item_index]
                if not msg_chain:
                    continue
                msg_str = (
                    llm_result.msg_logs[item_index]
                    if llm_result.msg_logs and item_index < len(llm_result.msg_logs)
                    else ""
                )
            elif item_type == "tts":
                msg_chain, msg_str = await self.build_tts_message_chain(
                    event, llm_result, item_index
                )
                if not msg_chain:
                    continue
            else:
                continue

            if sent_index > 0:
                interval = random.randint(
                    self.plugin.min_reply_interval, self.plugin.max_reply_interval
                )
                await asyncio.sleep(interval)

            success, message_id = await self.plugin.aiocqhttp.send_message(
                event,
                msg_chain,
            )
            sent_index += 1
            if success and message_id:
                iso_string = datetime.now().isoformat()
                media_id_list = re.findall(r"\[图片:(.*?)\]", msg_str)
                if item_type == "tts":
                    segment = llm_result.tts_segments[item_index]
                    attrs = []
                    if segment.lang:
                        attrs.append(f'lang="{segment.lang}"')
                    if segment.emotion:
                        attrs.append(f'emotion="{segment.emotion}"')
                    attrs_str = " " + " ".join(attrs) if attrs else ""
                    db_content = f"<tts{attrs_str}>{segment.text}</tts>"
                else:
                    db_content = msg_str
                msg_data = MessageData(
                    nickname=nickname,
                    user_id=event.get_self_id(),
                    group_or_user_id=group_or_user_id,
                    time=iso_string,
                    message_id=str(message_id),
                    content=db_content,
                    is_recalled=False,
                    media_id_list=media_id_list,
                )
                await self.plugin.data_cache.add_message(
                    bot_name, group_or_user_id, msg_data
                )

    async def _dispatch_generic_outputs(
        self,
        event: AstrMessageEvent,
        bot_name: str,
        nickname: str,
        group_or_user_id: str,
        llm_result: XmlLlmResult,
    ) -> None:
        sent_index = 0
        for item_type, item_index in self.get_output_order(llm_result):
            is_tts = item_type == "tts"
            if item_type == "message":
                if item_index < 0 or item_index >= len(llm_result.msg_chains):
                    continue
                msg_chain = llm_result.msg_chains[item_index]
                if not msg_chain:
                    continue
            elif is_tts:
                msg_chain, tts_text = await self.build_tts_message_chain(
                    event, llm_result, item_index
                )
                if not msg_chain:
                    continue
            else:
                continue

            if sent_index > 0:
                interval = random.randint(
                    self.plugin.min_reply_interval, self.plugin.max_reply_interval
                )
                await asyncio.sleep(interval)

            try:
                try:
                    event._giftia_bypass_logging = True
                    await event.send(MessageChain(msg_chain))
                finally:
                    event._giftia_bypass_logging = False
                iso_string = datetime.now().isoformat()
                if is_tts:
                    segment = llm_result.tts_segments[item_index]
                    attrs = []
                    if segment.lang:
                        attrs.append(f'lang="{segment.lang}"')
                    if segment.emotion:
                        attrs.append(f'emotion="{segment.emotion}"')
                    attrs_str = " " + " ".join(attrs) if attrs else ""
                    db_content = f"<tts{attrs_str}>{segment.text}</tts>"
                    msg_data = MessageData(
                        nickname=nickname,
                        user_id=event.get_self_id(),
                        group_or_user_id=group_or_user_id,
                        time=iso_string,
                        message_id="",
                        content=db_content,
                        is_recalled=False,
                        media_id_list=[],
                    )
                else:
                    parsed_msg = await self.plugin.message_parser.chain_to_result(
                        msg_chain, event=event
                    )
                    msg_data = MessageData(
                        nickname=nickname,
                        user_id=event.get_self_id(),
                        group_or_user_id=group_or_user_id,
                        time=iso_string,
                        message_id="",
                        content=parsed_msg.content,
                        is_recalled=False,
                        media_id_list=parsed_msg.media_id_list,
                        forward_messages=parsed_msg.forward_messages,
                    )
                await self.plugin.data_cache.add_message(
                    bot_name, group_or_user_id, msg_data
                )
                sent_index += 1
            except Exception as e:
                logger.error(f"{bot_name} 通用平台发送消息失败: {e}")

    async def dispatch_actions(
        self,
        event: AstrMessageEvent,
        bot_name: str,
        nickname: str,
        group_or_user_id: str,
        llm_result: XmlLlmResult,
    ):
        """
        根据 LLM 结果派发具体写操作（OneBot API 或是通用消息发送）。
        """
        if not isinstance(llm_result, XmlLlmResult):
            logger.warning(
                f"派发动作遇到非 XmlLlmResult 类型的结果，类型为: {type(llm_result)}，跳过动作派发"
            )
            return

        if hasattr(self.plugin, "tts_manager") and self.plugin.tts_manager.enabled():
            self.plugin.tts_manager.preprocess_signatures(llm_result)

        task_board_logs = await self._dispatch_task_board_actions(
            event=event,
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            llm_result=llm_result,
        )

        # 区分 aiocqhttp 平台与其它通用平台
        if event.get_platform_name() == "aiocqhttp" and isinstance(
            event, AiocqhttpMessageEvent
        ):
            success_logs = list(task_board_logs)
            iso_string = datetime.now().isoformat()

            # 1. 删除长期记忆
            if llm_result.delete_memories and self.plugin.embedding_conf.get(
                "enabled", False
            ):
                for memory_id in llm_result.delete_memories:
                    result = await self.plugin.data_cache.delete_memory(
                        memory_id=memory_id
                    )
                    if result:
                        success_logs.append(
                            f"<delete_memory memory_id={memory_id} result='success'/>"
                        )
                    else:
                        success_logs.append(
                            f"<delete_memory memory_id={memory_id} result='failed'/>"
                        )

            # 2. 撤回消息
            if llm_result.delete_message_ids:
                try:
                    ids = [int(msg_id) for msg_id in llm_result.delete_message_ids]
                    err_msg = await self.plugin.aiocqhttp.delete_messages(
                        event=event, message_ids=ids
                    )
                    await self.plugin.data_cache.set_message_recalled(
                        bot_name, group_or_user_id, llm_result.delete_message_ids
                    )
                    success_logs.append(
                        f"<recall message_ids={llm_result.delete_message_ids} result={err_msg or 'success'}/>"
                    )
                except ValueError:
                    logger.error(
                        f"{bot_name} 撤回消息数据格式错误: {llm_result.delete_message_ids}"
                    )

            # 3. 消息贴表情点赞
            if llm_result.emoji_ids:
                for message_id, emoji_id in llm_result.emoji_ids:
                    try:
                        message_id_int = int(message_id)
                        emoji_id_int = int(emoji_id)
                        err_msg = await self.plugin.aiocqhttp.msg_emoji_like(
                            event=event,
                            message_id=message_id_int,
                            emoji_id=emoji_id_int,
                        )
                        success_logs.append(
                            f"<emoji_like message_id={message_id} emoji_id={emoji_id} result={err_msg or 'success'}/>"
                        )
                    except ValueError:
                        logger.error(
                            f"{bot_name} 贴表情数据格式错误: {message_id}, {emoji_id}"
                        )

            # 4. 消息复读
            if llm_result.repeat_message_ids:
                repeat_enabled = self._interactive_feature_enabled("repeat")
                self_id = str(event.get_self_id() or "")
                for message_id in llm_result.repeat_message_ids:
                    message_id = str(message_id or "").strip()
                    if not message_id:
                        continue
                    if not repeat_enabled:
                        success_logs.append(
                            f"<repeat message_id={quoteattr(message_id)} result='failed' reason='disabled'/>"
                        )
                        continue

                    target_msg = self._find_recent_message(
                        bot_name, group_or_user_id, message_id
                    )
                    if not target_msg:
                        success_logs.append(
                            f"<repeat message_id={quoteattr(message_id)} result='failed' reason='not_in_context_window'/>"
                        )
                        continue
                    if getattr(target_msg, "role", "message") == "operation_log":
                        success_logs.append(
                            f"<repeat message_id={quoteattr(message_id)} result='failed' reason='operation_log'/>"
                        )
                        continue
                    if self_id and str(target_msg.user_id or "") == self_id:
                        success_logs.append(
                            f"<repeat message_id={quoteattr(message_id)} result='failed' reason='self_message'/>"
                        )
                        continue
                    if target_msg.is_recalled:
                        success_logs.append(
                            f"<repeat message_id={quoteattr(message_id)} result='failed' reason='recalled'/>"
                        )
                        continue

                    try:
                        message_id_int = int(message_id)
                    except ValueError:
                        logger.error(f"{bot_name} 复读消息ID格式错误: {message_id}")
                        success_logs.append(
                            f"<repeat message_id={quoteattr(message_id)} result='failed' reason='invalid_message_id'/>"
                        )
                        continue

                    success, new_message_id, err_msg = (
                        await self.plugin.aiocqhttp.repeat_message(
                            event=event,
                            message_id=message_id_int,
                        )
                    )
                    if success:
                        if new_message_id:
                            success_logs.append(
                                f"<repeat message_id={quoteattr(message_id)} new_message_id={quoteattr(str(new_message_id))} result='success'/>"
                            )
                            msg_data = MessageData(
                                nickname=nickname,
                                user_id=event.get_self_id(),
                                group_or_user_id=group_or_user_id,
                                time=datetime.now().isoformat(),
                                message_id=str(new_message_id),
                                content=target_msg.content,
                                is_recalled=False,
                                media_id_list=list(target_msg.media_id_list or []),
                                forward_messages=list(target_msg.forward_messages or []),
                            )
                            await self.plugin.data_cache.add_message(
                                bot_name, group_or_user_id, msg_data
                            )
                        else:
                            success_logs.append(
                                f"<repeat message_id={quoteattr(message_id)} result='partial' reason={quoteattr(err_msg or 'missing_message_id')}/>"
                            )
                    else:
                        success_logs.append(
                            f"<repeat message_id={quoteattr(message_id)} result='failed' reason={quoteattr(err_msg or 'unknown')}/>"
                        )

            # 5. 点赞
            if llm_result.likes:
                for user_id, count in llm_result.likes:
                    try:
                        user_id_int = int(user_id)
                        count_int = int(count)
                        err_msg = await self.plugin.aiocqhttp.like(
                            event=event,
                            user_id=user_id_int,
                            count=count_int,
                        )
                        success_logs.append(
                            f"<like user_id={user_id} result={err_msg or 'success'}/>"
                        )
                    except ValueError:
                        logger.error(f"{bot_name} 点赞数据格式错误: {user_id}, {count}")

            # 6. 戳一戳
            if llm_result.poke:
                for group_id, user_id in llm_result.poke:
                    try:
                        group_id_int = int(group_id)
                        user_id_int = int(user_id)
                        err_msg = await self.plugin.aiocqhttp.group_poke(
                            event=event,
                            group_id=group_id_int,
                            user_id=user_id_int,
                        )
                        success_logs.append(
                            f"<poke user_id={user_id} result={err_msg or 'success'}/>"
                        )
                    except ValueError:
                        logger.error(
                            f"{bot_name} 戳一戳数据格式错误: {group_id}, {user_id}"
                        )

            # 7. 禁言
            if llm_result.ban:
                for group_id, user_id, duration in llm_result.ban:
                    try:
                        group_id_int = int(group_id)
                        user_id_int = int(user_id)
                        duration_int = int(duration)
                        err_msg = await self.plugin.aiocqhttp.group_ban(
                            event=event,
                            group_id=group_id_int,
                            user_id=user_id_int,
                            duration=duration_int,
                        )
                        success_logs.append(
                            f"<ban user_id={user_id} duration={duration} result={err_msg or 'success'}/>"
                        )
                    except ValueError:
                        logger.error(
                            f"{bot_name} 禁言数据格式错误: {group_id}, {user_id}, {duration}"
                        )

            # 8. 添加定时任务
            if llm_result.schedule_tasks:
                for group_id, time_expr, remind_content in llm_result.schedule_tasks:
                    task_id = f"{bot_name}_{group_or_user_id}_{uuid.uuid4().hex[:6]}"
                    kwargs = {
                        "unified_msg_origin": event.unified_msg_origin,
                        "adapter_id": event.platform_meta.id,
                        "bot_name": bot_name,
                        "nickname": nickname,
                        "self_id": event.get_self_id(),
                        "platform_name": event.get_platform_name(),
                        "user_id": event.get_sender_id(),
                        "user_name": event.get_sender_name(),
                        "group_id": event.get_group_id(),
                        "group_or_user_id": group_or_user_id,
                        "remind_message": remind_content,
                    }
                    err_msg = self.plugin.task_manager.add_job(
                        task_id,
                        "remind",
                        time_expr,
                        kwargs=kwargs,
                    )
                    success_logs.append(
                        f"<schedule_task task_id={task_id} time_expr={time_expr} result={err_msg or 'success'}/>"
                    )

            # 9. 删除定时任务
            if llm_result.delete_schedule_tasks:
                for task_id in llm_result.delete_schedule_tasks:
                    err_msg = self.plugin.task_manager.remove_job(task_id)
                    success_logs.append(
                        f"<delete_task task_id={task_id} result={err_msg or 'success'}/>"
                    )

            # 10. 添加表情包日志
            if llm_result.add_stickers:
                for sticker_id in llm_result.add_stickers:
                    success_logs.append(
                        f"<add_sticker media_id={sticker_id} result='success'/>"
                    )

            # 11. 发送消息链 / TTS 语音
            await self._dispatch_aiocqhttp_outputs(
                event=event,
                bot_name=bot_name,
                nickname=nickname,
                group_or_user_id=group_or_user_id,
                llm_result=llm_result,
            )

            # 12. 踢人
            if llm_result.kick:
                for group_id, user_id in llm_result.kick:
                    try:
                        group_id_int = int(group_id)
                        user_id_int = int(user_id)
                        err_msg = await self.plugin.aiocqhttp.group_kick(
                            event=event,
                            group_id=group_id_int,
                            user_id=user_id_int,
                        )
                        success_logs.append(
                            f"<kick user_id={user_id} result={err_msg or 'success'}/>"
                        )
                    except ValueError:
                        logger.error(
                            f"{bot_name} 踢人数据格式错误: {group_id}, {user_id}"
                        )

            # 13. 退群
            if llm_result.leave:
                for group_id in llm_result.leave:
                    try:
                        group_id_int = int(group_id)
                        err_msg = await self.plugin.aiocqhttp.group_leave(
                            event=event,
                            group_id=group_id_int,
                        )
                        success_logs.append(
                            f"<leave user_id={event.get_self_id()} result={err_msg or 'success'}/>"
                        )
                    except ValueError:
                        logger.error(f"{bot_name} 退群数据格式错误: {group_id}")

            # 14. 以图搜番与以图搜插画 (SauceNAO)
            if (
                self._interactive_feature_enabled("search_anime")
                and hasattr(llm_result, "search_anime_requests")
                and llm_result.search_anime_requests
            ):
                bot_id = event.get_self_id()
                for req in llm_result.search_anime_requests:
                    media_id = req.get("media_id", "")
                    stype = str(req.get("type", "anime")).lower().strip()
                    limit = int(req.get("limit", 3))
                    if media_id:
                        if stype in {"illust", "saucenao"}:
                            api_key = self.plugin.tools_config.get("saucenao_api_key", "")
                            ok, chain, err_msg = await search_illust_by_media_id(
                                self.plugin,
                                media_id,
                                limit=limit,
                                api_key=api_key,
                                bot_id=bot_id,
                                bot_name=bot_name,
                            )
                        else:
                            ok, chain, err_msg = await search_anime_by_media_id(
                                self.plugin,
                                media_id,
                                limit=limit,
                                bot_id=bot_id,
                                bot_name=bot_name,
                            )

                        if ok:
                            await event.send(chain)
                            success_logs.append(
                                f"<search_anime media_id={quoteattr(media_id)} type={quoteattr(stype)} limit={limit} result='success'/>"
                            )
                        else:
                            await event.send(chain)
                            success_logs.append(
                                f"<search_anime media_id={quoteattr(media_id)} type={quoteattr(stype)} limit={limit} result='failed' reason={quoteattr(err_msg)}/>"
                            )

            # 15. 记录总体操作日志
            if len(success_logs) > 0:
                await self.plugin.data_cache.add_message(
                    bot_name,
                    group_or_user_id,
                    MessageData(
                        nickname=nickname,
                        user_id=event.get_self_id(),
                        group_or_user_id=group_or_user_id,
                        time=iso_string,
                        message_id="",
                        content="\n".join(success_logs),
                        is_recalled=False,
                        media_id_list=[],
                        role="operation_log",
                    ),
                )
            return

        # 其它平台普通消息 / TTS 语音发送
        if llm_result.msg_chains or llm_result.tts_segments:
            await self._dispatch_generic_outputs(
                event=event,
                bot_name=bot_name,
                nickname=nickname,
                group_or_user_id=group_or_user_id,
                llm_result=llm_result,
            )

        if task_board_logs:
            await self.plugin.data_cache.add_message(
                bot_name,
                group_or_user_id,
                MessageData(
                    nickname=nickname,
                    user_id=event.get_self_id(),
                    group_or_user_id=group_or_user_id,
                    time=datetime.now().isoformat(),
                    message_id="",
                    content="\n".join(task_board_logs),
                    is_recalled=False,
                    media_id_list=[],
                    role="operation_log",
                ),
            )
