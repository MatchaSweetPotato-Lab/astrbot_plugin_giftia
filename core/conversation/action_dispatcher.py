import asyncio
import random
import re
import uuid
from datetime import datetime
from xml.sax.saxutils import quoteattr

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Reply
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ..utils.event_utils import get_adapter_id
from ..utils.qq_official_action import is_qq_official as is_qq_official_platform
from ..utils.schemas import FeatureKey, ImageSendType, MessageData, XmlLlmResult
from .tool_executor import ToolExecutor


class ActionDispatcher:
    def __init__(self, plugin):
        self.plugin = plugin
        self.tool_executor = ToolExecutor(plugin)

    async def _record_action_logs(
        self,
        event: AstrMessageEvent,
        bot_name: str,
        nickname: str,
        group_or_user_id: str,
        logs: list[str],
    ) -> None:
        """在派发下一个可见输出前，记录已执行完成的动作日志。

        Args:
            event: 标识机器人的事件对象。
            bot_name: 机器人配置名称。
            nickname: 机器人昵称。
            group_or_user_id: 当前会话 ID。
            logs: 已完成的动作描述列表，为空时忽略。
        """
        if logs:
            await self.plugin.data_cache.add_message(
                bot_name,
                group_or_user_id,
                MessageData(
                    nickname=nickname,
                    user_id=event.get_self_id(),
                    group_or_user_id=group_or_user_id,
                    time=datetime.now().isoformat(),
                    content="\n".join(logs),
                    role="operation_log",
                ),
            )

    async def _invoke_platform_action(
        self,
        event: AstrMessageEvent,
        action_name: str,
        is_qq_official: bool,
        *args,
        **kwargs,
    ):
        handler = (
            getattr(self.plugin, "qq_official", None)
            if is_qq_official
            else getattr(self.plugin, "aiocqhttp", None)
        )
        if not handler:
            return "handler_not_found"
        func = getattr(handler, action_name, None)
        if callable(func):
            return await func(event=event, *args, **kwargs)
        return "action_not_supported"

    def _interactive_feature_enabled(
        self, feature_name: str, bot_conf: dict | str = None
    ) -> bool:
        bot_dict = (
            self.plugin.get_bot_config(bot_conf)
            if hasattr(self.plugin, "get_bot_config")
            else {}
        )
        enabled_features = bot_dict.get("enabled_interactive_features")
        if enabled_features is None:
            return True
        return feature_name in enabled_features

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

    async def _dispatch_set_call_name_actions(
        self,
        event: AstrMessageEvent,
        bot_name: str,
        group_or_user_id: str,
        llm_result: XmlLlmResult,
    ) -> list[str]:
        if not llm_result.set_call_names:
            return []

        logs = []
        max_call_name_length = 20

        for item in llm_result.set_call_names:
            target_user_id = str(item.user_id or "").strip()
            if not target_user_id:
                logs.append("<set_call_name result='failed' reason='missing_user_id'/>")
                logger.warning("[Giftia] 设置用户称呼失败: 未在标签中显式提供 user_id")
                continue

            raw_name = str(item.name or "").strip()
            name = re.sub(r"\s+", " ", raw_name).strip() if raw_name else ""
            if len(name) > max_call_name_length:
                logger.warning(
                    f"[Giftia] 用户 {target_user_id} 设置的称呼 '{name}' 超出长度限制 ({max_call_name_length} 字)，已自动截断"
                )
                name = name[:max_call_name_length].strip()

            try:
                profile_fields = {"call_name": name if name else None}
                await self.plugin.data_cache.set_user_profile(
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                    user_id=target_user_id,
                    profile_fields=profile_fields,
                )
                action_str = (
                    f"set call_name to '{name}'" if name else "cleared call_name"
                )
                name_attr = f" name={quoteattr(name)}" if name else ""
                logs.append(
                    f"<set_call_name user_id={quoteattr(target_user_id)}{name_attr} result='success'/>"
                )
                logger.info(
                    f"[Giftia] 成功对用户 {target_user_id} 执行称呼设置: {action_str}"
                )
            except Exception as e:
                logger.error(
                    f"[Giftia] 设置用户 {target_user_id} 称呼失败: {e}",
                    exc_info=True,
                )
                logs.append(
                    f"<set_call_name user_id={quoteattr(target_user_id)} "
                    f"result='failed' reason={quoteattr(str(e))}/>"
                )

        return logs

    async def _dispatch_set_status_actions(
        self,
        event: AstrMessageEvent,
        bot_name: str,
        group_or_user_id: str,
        llm_result: XmlLlmResult,
    ) -> list[str]:
        """派发更新常驻/低频自定义状态的动作。"""
        if not llm_result.set_custom_status:
            return []

        if not self._interactive_feature_enabled(
            FeatureKey.PERSISTENT_STATUS, bot_name
        ):
            logger.info(
                f"[Giftia] Bot {bot_name} 未启用常驻状态管理，跳过 set_status 动作"
            )
            return []

        logs = []
        try:
            await self.plugin.data_cache.update_bot_custom_status(
                bot_name=bot_name,
                group_id=group_or_user_id,
                custom_status_updates=llm_result.set_custom_status,
            )
            for k, v in llm_result.set_custom_status.items():
                if v:
                    logs.append(
                        f"<set_status key={quoteattr(k)} value={quoteattr(v)} result='success'/>"
                    )
                    logger.info(
                        f"[Giftia] 会话 {group_or_user_id} 更新常驻状态: {k} = {v}"
                    )
                else:
                    logs.append(f"<set_status key={quoteattr(k)} result='deleted'/>")
                    logger.info(f"[Giftia] 会话 {group_or_user_id} 删除常驻状态: {k}")
        except Exception as e:
            logger.error(f"[Giftia] 更新常驻状态失败: {e}", exc_info=True)
            logs.append(f"<set_status result='failed' reason={quoteattr(str(e))}/>")

        return logs

    @staticmethod
    def get_output_order(llm_result: XmlLlmResult) -> list[tuple[str, int]]:
        """按 XML 中的出现顺序返回可见输出，并追加未追踪的动作。

        Args:
            llm_result: 解析后的回复结果，或未包含顺序元数据的结果。

        Returns:
            按派发顺序排列的 (输出类型, 索引) 元组列表。
        """
        if llm_result.output_order:
            order = list(llm_result.output_order)
        else:
            order = [("message", index) for index in range(len(llm_result.msg_chains))]
            order.extend(
                ("tts", index) for index in range(len(llm_result.tts_segments))
            )
        tracked = set(order)
        for item_type, items in (
            ("repeat", llm_result.repeat_message_ids),
            ("emoji_like", llm_result.emoji_ids),
            ("poke", llm_result.poke),
            ("delete", llm_result.delete_message_ids),
            ("like", llm_result.likes),
            ("ban", llm_result.ban),
            ("kick", llm_result.kick),
            ("tool_call", llm_result.tools_to_call),
        ):
            order.extend(
                (item_type, index)
                for index in range(len(items))
                if (item_type, index) not in tracked
            )
        return order

    async def build_tts_message_chain(
        self,
        event: AstrMessageEvent,
        llm_result: XmlLlmResult,
        index: int,
        bot_conf: dict | str = None,
    ):
        """
        [Internal Helper] 构建指定的 TTS 消息链。

        此方法属于内部辅助函数，主要供 ActionDispatcher 及 ChatManager（用于构建定时任务的 TTS 消息）调用。
        """
        if not hasattr(self.plugin, "tts_manager"):
            return None, ""
        bot_dict = self.plugin.get_bot_config(bot_conf or get_adapter_id(event) or None)
        if not self.plugin.tts_manager.enabled(bot_dict):
            return None, ""
        if index < 0 or index >= len(llm_result.tts_segments):
            return None, ""

        segment = llm_result.tts_segments[index]
        record = await self.plugin.tts_manager.build_record(event, segment, bot_dict)
        if record:
            chain = [record]
            if segment.quote_message_id:
                chain.insert(0, Reply(id=segment.quote_message_id))
            return chain, segment.text

        logger.warning("[Giftia TTS] 语音合成重试后依然失败，放弃发送该段语音消息。")
        return None, ""

    async def _build_send_chain(self, msg_chain: list, bot_conf: dict) -> list:
        """按该机器人的「表情包转 GIF」开关，返回用于发送的消息链副本。

        必须返回副本而非就地修改：发送之后调用方还要用**原链**调
        message_parser.chain_to_result() 生成入库内容，若原链被换成 GIF，
        入库的 [图片:<sticker_id>] 会变成 GIF 的新哈希，破坏聊天记录与表情包复用。
        开关关闭、或链里没有可转的表情包时，原样返回入参。
        """
        if not msg_chain or not bot_conf.get("send_sticker_as_gif"):
            return msg_chain
        converter = getattr(self.plugin, "gif_converter", None)
        if converter is None:
            return msg_chain
        try:
            return await converter.build_send_chain(msg_chain)
        except Exception as e:
            logger.warning(f"[Giftia] 表情包转 GIF 失败，按原格式发送: {e}")
            return msg_chain

    async def _dispatch_single_repeat(
        self,
        event: AstrMessageEvent,
        bot_name: str,
        nickname: str,
        group_or_user_id: str,
        message_id: str,
        bot_conf: dict,
        is_qq_official: bool,
        success_logs: list[str],
    ) -> bool:
        """派发单条消息复读，并在成功时写入历史缓存与日志"""
        repeat_enabled = self._interactive_feature_enabled("repeat", bot_conf)
        self_id = str(event.get_self_id() or "")
        message_id = str(message_id or "").strip()
        if not message_id:
            return False
        if not repeat_enabled:
            success_logs.append(
                f"<repeat message_id={quoteattr(message_id)} result='failed' reason='disabled'/>"
            )
            return False

        target_msg = self._find_recent_message(bot_name, group_or_user_id, message_id)
        if not target_msg:
            success_logs.append(
                f"<repeat message_id={quoteattr(message_id)} result='failed' reason='not_in_context_window'/>"
            )
            return False
        if getattr(target_msg, "role", "message") == "operation_log":
            success_logs.append(
                f"<repeat message_id={quoteattr(message_id)} result='failed' reason='operation_log'/>"
            )
            return False
        if self_id and str(target_msg.user_id or "") == self_id:
            success_logs.append(
                f"<repeat message_id={quoteattr(message_id)} result='failed' reason='self_message'/>"
            )
            return False
        if target_msg.is_recalled:
            success_logs.append(
                f"<repeat message_id={quoteattr(message_id)} result='failed' reason='recalled'/>"
            )
            return False

        if is_qq_official:
            m_id = message_id
        else:
            try:
                m_id = int(message_id)
            except ValueError:
                logger.error(f"{bot_name} 复读消息ID格式错误: {message_id}")
                success_logs.append(
                    f"<repeat message_id={quoteattr(message_id)} result='failed' reason='invalid_message_id'/>"
                )
                return False

        res = await self._invoke_platform_action(
            event, "repeat_message", is_qq_official, message_id=m_id
        )
        if res in ("handler_not_found", "action_not_supported"):
            logger.debug(f"[Giftia] repeat_message 动作暂不支持 [{res}]")
            return False

        if isinstance(res, tuple) and len(res) == 3:
            success, new_message_id, err_msg = res
        else:
            success, new_message_id, err_msg = False, None, str(res)

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
                return True
            else:
                success_logs.append(
                    f"<repeat message_id={quoteattr(message_id)} result='partial' reason={quoteattr(err_msg or 'missing_message_id')}/>"
                )
                return True
        else:
            success_logs.append(
                f"<repeat message_id={quoteattr(message_id)} result='failed' reason={quoteattr(err_msg or 'unknown')}/>"
            )
            return False

    async def _dispatch_aiocqhttp_outputs(
        self,
        event: AstrMessageEvent,
        bot_name: str,
        nickname: str,
        group_or_user_id: str,
        llm_result: XmlLlmResult,
    ) -> None:
        """按顺序派发 aiocqhttp 平台的可见输出、TTS 语音以及相关动作。

        Args:
            event: 消息事件对象。
            bot_name: 机器人配置名称。
            nickname: 机器人昵称。
            group_or_user_id: 当前会话 ID。
            llm_result: LLM 返回的解析结果。
        """
        bot_conf = self.plugin.get_bot_config(bot_name)
        sent_index = 0
        is_qq_official = is_qq_official_platform(event)

        for item_type, item_index in self.get_output_order(llm_result):
            if item_type in ("message", "sticker", "image"):
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
                send_image_type = (
                    ImageSendType.STICKER
                    if item_type == "sticker"
                    else ImageSendType.NORMAL
                )
            elif item_type == "tts":
                msg_chain, msg_str = await self.build_tts_message_chain(
                    event, llm_result, item_index, bot_conf
                )
                if not msg_chain:
                    continue
                send_image_type = ImageSendType.NORMAL
            elif item_type == "repeat":
                if item_index < 0 or item_index >= len(llm_result.repeat_message_ids):
                    continue
                if sent_index > 0:
                    interval = random.randint(
                        self.plugin.min_reply_interval, self.plugin.max_reply_interval
                    )
                    await asyncio.sleep(interval)
                target_msg_id = llm_result.repeat_message_ids[item_index]
                action_logs = []
                await self._dispatch_single_repeat(
                    event=event,
                    bot_name=bot_name,
                    nickname=nickname,
                    group_or_user_id=group_or_user_id,
                    message_id=target_msg_id,
                    bot_conf=bot_conf,
                    is_qq_official=is_qq_official,
                    success_logs=action_logs,
                )
                await self._record_action_logs(
                    event, bot_name, nickname, group_or_user_id, action_logs
                )
                sent_index += 1
                continue
            elif item_type in ("poke", "emoji_like", "delete", "like", "ban", "kick"):
                items = {
                    "poke": llm_result.poke,
                    "emoji_like": llm_result.emoji_ids,
                    "delete": llm_result.delete_message_ids,
                    "like": llm_result.likes,
                    "ban": llm_result.ban,
                    "kick": llm_result.kick,
                }[item_type]
                if item_index < 0 or item_index >= len(items):
                    continue
                log_type = item_type
                try:
                    if item_type == "poke":
                        group_id, user_id = items[item_index]
                        action_name = "group_poke"
                        action_args = {
                            "group_id": group_id if is_qq_official else int(group_id),
                            "user_id": user_id if is_qq_official else int(user_id),
                        }
                        log_attrs = f"user_id={user_id}"
                    elif item_type == "emoji_like":
                        message_id, emoji_id = items[item_index]
                        action_name = "msg_emoji_like"
                        action_args = {
                            "message_id": message_id
                            if is_qq_official
                            else int(message_id),
                            "emoji_id": int(emoji_id),
                        }
                        log_attrs = f"message_id={message_id} emoji_id={emoji_id}"
                    elif item_type == "delete":
                        message_id = items[item_index]
                        action_name = "delete_messages"
                        action_args = {
                            "message_ids": [
                                message_id if is_qq_official else int(message_id)
                            ]
                        }
                        log_type = "recall"
                        log_attrs = f"message_ids={[message_id]}"
                    elif item_type == "like":
                        user_id, count = items[item_index]
                        action_name = "like"
                        action_args = {
                            "user_id": user_id if is_qq_official else int(user_id),
                            "count": int(count),
                        }
                        log_attrs = f"user_id={user_id}"
                    else:
                        group_id, user_id = items[item_index][:2]
                        action_name = (
                            "group_ban" if item_type == "ban" else "group_kick"
                        )
                        action_args = {
                            "group_id": group_id if is_qq_official else int(group_id),
                            "user_id": user_id if is_qq_official else int(user_id),
                        }
                        log_attrs = f"user_id={user_id}"
                        if item_type == "ban":
                            duration = items[item_index][2]
                            action_args["duration"] = int(duration)
                            log_attrs += f" duration={duration}"
                except (TypeError, ValueError):
                    logger.error(
                        f"[Giftia] {bot_name} Invalid {item_type} arguments: {items[item_index]}"
                    )
                    continue

                if sent_index > 0:
                    interval = random.randint(
                        self.plugin.min_reply_interval, self.plugin.max_reply_interval
                    )
                    await asyncio.sleep(interval)
                err_msg = await self._invoke_platform_action(
                    event, action_name, is_qq_official, **action_args
                )
                if err_msg not in ("handler_not_found", "action_not_supported"):
                    if item_type == "delete" and not err_msg:
                        await self.plugin.data_cache.set_message_recalled(
                            bot_name, group_or_user_id, [message_id]
                        )
                    await self._record_action_logs(
                        event,
                        bot_name,
                        nickname,
                        group_or_user_id,
                        [f"<{log_type} {log_attrs} result={err_msg or 'success'}/>"],
                    )
                else:
                    logger.debug(f"[Giftia] {action_name} is unavailable [{err_msg}]")
                sent_index += 1
                continue
            elif item_type == "tool_call":
                if item_index < 0 or item_index >= len(llm_result.tools_to_call):
                    continue
                if sent_index > 0:
                    await asyncio.sleep(
                        random.randint(
                            self.plugin.min_reply_interval,
                            self.plugin.max_reply_interval,
                        )
                    )
                tool_name, tool_args = llm_result.tools_to_call[item_index]
                llm_result.xml_tool_results.append(
                    await self.tool_executor.execute_tool(
                        event,
                        bot_name,
                        nickname,
                        group_or_user_id,
                        tool_name,
                        tool_args,
                    )
                )
                sent_index += 1
                continue
            else:
                continue

            if sent_index > 0:
                interval = random.randint(
                    self.plugin.min_reply_interval, self.plugin.max_reply_interval
                )
                await asyncio.sleep(interval)

            # 发送用 GIF 副本，入库仍用原链（见 _build_send_chain 注释）。
            # 官方 QQ 尤其需要：那边不支持小图表情包外显，表情包会以大图发出占屏，
            # 转成 GIF 后客户端才会按表情包渲染。
            send_chain = await self._build_send_chain(msg_chain, bot_conf)

            if is_qq_official_platform(event):
                success, message_id = await self.plugin.qq_official.send_message(
                    event,
                    send_chain,
                )
                if not success:
                    try:
                        event._giftia_bypass_logging = True
                        await event.send(MessageChain(send_chain))
                        success = True
                        message_id = str(uuid.uuid4())
                    except Exception as err_fallback:
                        logger.error(
                            f"[Giftia] 官方 QQ 富媒体消息降级发送失败: {err_fallback}"
                        )
                    finally:
                        event._giftia_bypass_logging = False
            else:
                success, message_id = await self.plugin.aiocqhttp.send_message(
                    event,
                    send_chain,
                    image_type=send_image_type,
                )
            sent_index += 1
            if success and message_id:
                iso_string = datetime.now().isoformat()
                if item_type == "tts":
                    segment = llm_result.tts_segments[item_index]
                    attrs = []
                    if segment.lang:
                        attrs.append(f'lang="{segment.lang}"')
                    if segment.emotion:
                        attrs.append(f'emotion="{segment.emotion}"')
                    if segment.quote_message_id:
                        attrs.append(f"quote={quoteattr(segment.quote_message_id)}")
                    attrs_str = " " + " ".join(attrs) if attrs else ""
                    db_content = f"<tts{attrs_str}>{segment.text}</tts>"
                    msg_data = MessageData(
                        nickname=nickname,
                        user_id=event.get_self_id(),
                        group_or_user_id=group_or_user_id,
                        time=iso_string,
                        message_id=str(message_id),
                        content=db_content,
                        is_recalled=0,
                        media_id_list=[],
                    )
                else:
                    try:
                        parsed_msg = await self.plugin.message_parser.chain_to_result(
                            msg_chain, event=event
                        )
                        db_content = parsed_msg.content
                        media_id_list = parsed_msg.media_id_list
                        forward_messages = parsed_msg.forward_messages
                    except Exception as parse_err:
                        logger.error(
                            f"[Giftia] 解析已发送消息链入库失败: {parse_err}, 降级使用日志文本",
                            exc_info=True,
                        )
                        db_content = msg_str
                        media_id_list = re.findall(r"\[图片:(.*?)\]", msg_str)
                        forward_messages = []

                    msg_data = MessageData(
                        nickname=nickname,
                        user_id=event.get_self_id(),
                        group_or_user_id=group_or_user_id,
                        time=iso_string,
                        message_id=str(message_id),
                        content=db_content,
                        is_recalled=0,
                        media_id_list=media_id_list,
                        forward_messages=forward_messages,
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
        """按顺序派发通用平台的可见消息与 TTS 语音。

        Args:
            event: 消息事件对象。
            bot_name: 机器人配置名称。
            nickname: 机器人昵称。
            group_or_user_id: 当前会话 ID。
            llm_result: LLM 返回的解析结果。
        """
        bot_conf = self.plugin.get_bot_config(bot_name)
        sent_index = 0
        for item_type, item_index in self.get_output_order(llm_result):
            is_tts = item_type == "tts"
            if item_type in ("message", "sticker", "image"):
                if item_index < 0 or item_index >= len(llm_result.msg_chains):
                    continue
                msg_chain = llm_result.msg_chains[item_index]
                if not msg_chain:
                    continue
            elif is_tts:
                msg_chain, tts_text = await self.build_tts_message_chain(
                    event, llm_result, item_index, bot_conf
                )
                if not msg_chain:
                    continue
            elif item_type == "tool_call":
                if item_index < 0 or item_index >= len(llm_result.tools_to_call):
                    continue
                if sent_index > 0:
                    await asyncio.sleep(
                        random.randint(
                            self.plugin.min_reply_interval,
                            self.plugin.max_reply_interval,
                        )
                    )
                tool_name, tool_args = llm_result.tools_to_call[item_index]
                llm_result.xml_tool_results.append(
                    await self.tool_executor.execute_tool(
                        event,
                        bot_name,
                        nickname,
                        group_or_user_id,
                        tool_name,
                        tool_args,
                    )
                )
                sent_index += 1
                continue
            else:
                continue

            if sent_index > 0:
                interval = random.randint(
                    self.plugin.min_reply_interval, self.plugin.max_reply_interval
                )
                await asyncio.sleep(interval)

            try:
                # 发送用 GIF 副本，入库仍用原链（见 _build_send_chain 注释）
                send_chain = await self._build_send_chain(msg_chain, bot_conf)
                try:
                    event._giftia_bypass_logging = True
                    await event.send(MessageChain(send_chain))
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
                    if segment.quote_message_id:
                        attrs.append(f"quote={quoteattr(segment.quote_message_id)}")
                    attrs_str = " " + " ".join(attrs) if attrs else ""
                    db_content = f"<tts{attrs_str}>{segment.text}</tts>"
                    msg_data = MessageData(
                        nickname=nickname,
                        user_id=event.get_self_id(),
                        group_or_user_id=group_or_user_id,
                        time=iso_string,
                        message_id=str(uuid.uuid4()),
                        content=db_content,
                        is_recalled=0,
                        media_id_list=[],
                    )
                else:
                    msg_str = (
                        llm_result.msg_logs[item_index]
                        if llm_result.msg_logs and item_index < len(llm_result.msg_logs)
                        else ""
                    )
                    try:
                        parsed_msg = await self.plugin.message_parser.chain_to_result(
                            msg_chain, event=event
                        )
                        db_content = parsed_msg.content
                        media_id_list = parsed_msg.media_id_list
                        forward_messages = parsed_msg.forward_messages
                    except Exception as parse_err:
                        logger.error(
                            f"[Giftia] 通用平台解析已发送消息链入库失败: {parse_err}, 降级使用日志文本",
                            exc_info=True,
                        )
                        db_content = msg_str
                        media_id_list = re.findall(r"\[图片:(.*?)\]", msg_str)
                        forward_messages = []

                    msg_data = MessageData(
                        nickname=nickname,
                        user_id=event.get_self_id(),
                        group_or_user_id=group_or_user_id,
                        time=iso_string,
                        message_id=str(uuid.uuid4()),
                        content=db_content,
                        is_recalled=0,
                        media_id_list=media_id_list,
                        forward_messages=forward_messages,
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

        group_id = event.get_group_id()
        if (
            group_id
            and hasattr(self.plugin, "data_cache")
            and self.plugin.data_cache.is_bot_muted(bot_name, str(group_id))
        ):
            logger.info(
                f"[Giftia] Bot {bot_name} 在群 {group_id} 处于禁言静默状态，拦截所有动作派发"
            )
            return

        bot_conf = self.plugin.get_bot_config(bot_name)
        if hasattr(self.plugin, "tts_manager") and self.plugin.tts_manager.enabled(
            bot_conf
        ):
            self.plugin.tts_manager.preprocess_signatures(llm_result, bot_conf)

        task_board_logs = await self._dispatch_task_board_actions(
            event=event,
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            llm_result=llm_result,
        )
        set_call_name_logs = await self._dispatch_set_call_name_actions(
            event=event,
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            llm_result=llm_result,
        )
        set_status_logs = await self._dispatch_set_status_actions(
            event=event,
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            llm_result=llm_result,
        )
        common_logs = task_board_logs + set_call_name_logs + set_status_logs

        for item in llm_result.slang_actions:
            action = item.get("action", "")
            term = item.get("term", "")
            try:
                if not self._interactive_feature_enabled(FeatureKey.SLANG, bot_name):
                    raise ValueError("黑话管理工具未启用")
                if action == "set":
                    await self.plugin.db.slang_repo.set_entry(
                        bot_name, group_or_user_id, term, item.get("description", "")
                    )
                    result = "success"
                elif action == "delete":
                    deleted = await self.plugin.db.slang_repo.delete_entry(
                        bot_name, group_or_user_id, term
                    )
                    result = "deleted" if deleted else "not_found"
                else:
                    raise ValueError("黑话工具仅支持 set 和 delete")
                common_logs.append(
                    f"<slang action={quoteattr(action)} term={quoteattr(term)} "
                    f"result={quoteattr(result)}/>"
                )
            except Exception as e:
                logger.warning(f"[Giftia] Slang action failed: {e}")
                common_logs.append(
                    f"<slang action={quoteattr(action)} term={quoteattr(term)} "
                    f"result='failed' reason={quoteattr(str(e))}/>"
                )

        # 区分 aiocqhttp / qq_official 平台与其它通用平台
        is_aiocqhttp = event.get_platform_name() == "aiocqhttp" and isinstance(
            event, AiocqhttpMessageEvent
        )
        is_qq_official = is_qq_official_platform(event)

        if is_aiocqhttp or is_qq_official:
            success_logs = list(common_logs)
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

            # 8. 添加定时任务
            if llm_result.schedule_tasks:
                for group_id, time_expr, remind_content in llm_result.schedule_tasks:
                    task_id = f"{bot_name}_{group_or_user_id}_{uuid.uuid4().hex[:6]}"
                    kwargs = {
                        "unified_msg_origin": event.unified_msg_origin,
                        "adapter_id": get_adapter_id(event),
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

            await self._record_action_logs(
                event, bot_name, nickname, group_or_user_id, success_logs
            )
            await self._dispatch_aiocqhttp_outputs(
                event=event,
                bot_name=bot_name,
                nickname=nickname,
                group_or_user_id=group_or_user_id,
                llm_result=llm_result,
            )

            # 在所有有序输出和工具执行完成后再退群。
            if llm_result.leave:
                for group_id in llm_result.leave:
                    if is_qq_official:
                        g_id = group_id
                    else:
                        try:
                            g_id = int(group_id)
                        except ValueError:
                            g_id = None
                            logger.error(f"{bot_name} 退群数据格式错误: {group_id}")
                    if g_id is not None:
                        err_msg = await self._invoke_platform_action(
                            event, "group_leave", is_qq_official, group_id=g_id
                        )
                        if err_msg not in ("handler_not_found", "action_not_supported"):
                            await self._record_action_logs(
                                event,
                                bot_name,
                                nickname,
                                group_or_user_id,
                                [
                                    f"<leave user_id={event.get_self_id()} result={err_msg or 'success'}/>"
                                ],
                            )
                        else:
                            logger.debug(
                                f"[Giftia] group_leave 动作暂不支持 [{err_msg}]"
                            )

            return

        await self._record_action_logs(
            event, bot_name, nickname, group_or_user_id, common_logs
        )
        await self._dispatch_generic_outputs(
            event=event,
            bot_name=bot_name,
            nickname=nickname,
            group_or_user_id=group_or_user_id,
            llm_result=llm_result,
        )
