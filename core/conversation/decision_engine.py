import asyncio
import random
import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At

from ..llm.prompt import build_decision_prompt
from ..utils.schemas import MessageData
from .media_captioner import MediaCaptioner
from .memory_recall import search_memories_with_rerank


class DecisionEngine:
    def __init__(self, plugin):
        self.plugin = plugin
        self.media_captioner = MediaCaptioner(plugin)

    def check_whitelists(self, event: AstrMessageEvent) -> bool:
        """检查白名单配置。返回 True 表示通过，False 表示拦截。"""
        is_private = not event.get_group_id()
        bypass_whitelist = is_private and self.plugin.private_chat_bypass

        # 群白名单判断
        if (
            not bypass_whitelist
            and self.plugin.group_whitelist_enabled
            and event.unified_msg_origin not in self.plugin.group_whitelist
        ):
            logger.debug(f"群 {event.unified_msg_origin} 不在白名单内，跳过处理")
            return False

        # 用户白名单判断
        if (
            not bypass_whitelist
            and self.plugin.user_whitelist_enabled
            and event.get_sender_id() not in self.plugin.user_whitelist
        ):
            logger.debug(f"用户 {event.get_sender_id()} 不在白名单内，跳过处理")
            return False

        # 私聊用户白名单判断
        if (
            is_private
            and self.plugin.private_user_whitelist_enabled
            and event.get_sender_id() not in self.plugin.private_user_whitelist
        ):
            logger.debug(f"私聊用户 {event.get_sender_id()} 不在私聊白名单内，跳过处理")
            return False

        # 判断是否为本插件管理的机器人收到的消息
        if event.platform_meta.id not in self.plugin.adapter_id_map:
            logger.debug(
                f"{event.platform_meta.id} 消息不是本插件管理的机器人收到的消息，跳过处理"
            )
            return False

        return True

    def can_execute(self, key: str, throttle_time: float) -> bool:
        """节流检查"""
        now = time.time()
        last_time = self.plugin.throttle_map.get(key, 0)
        if now - last_time >= throttle_time:
            self.plugin.throttle_map[key] = now
            return True
        return False

    async def evaluate_decision(
        self,
        event: AstrMessageEvent,
        bot_name: str,
        nickname: str,
        group_or_user_id: str,
        current_message: MessageData,
    ) -> tuple[bool, list[str] | None, bool, list[dict] | None]:
        """
        进行接话决策。
        返回: (should_reply, relevant_memories, is_just_at, pending_recall_memories)
        """
        bot_conf = self.plugin.bot_map[bot_name]
        decision_conf = bot_conf.get("decision_conf", {})

        group_id = event.get_group_id()
        if (
            group_id
            and hasattr(self.plugin, "data_cache")
            and self.plugin.data_cache.is_bot_muted(bot_name, str(group_id))
        ):
            remaining = self.plugin.data_cache.get_bot_mute_remaining(
                bot_name, str(group_id)
            )
            rem_str = (
                "全员禁言"
                if remaining == float("inf")
                else f"剩余约 {int(remaining)} 秒"
            )
            logger.info(
                f"[Giftia] Bot {bot_name} 在群 {group_id} 处于禁言静默状态（{rem_str}），跳过回复决策"
            )
            return False, None, False, None

        is_just_at = any(
            isinstance(c, At) and str(c.qq) == event.get_self_id()
            for c in event.get_messages()
        )
        is_private = not event.get_group_id()
        if is_private and self.plugin.private_chat_bypass:
            is_just_at = True

        debounce_key = f"{bot_name}:{group_or_user_id}:{event.get_sender_id()}"

        # 预先处理防抖状态（如果是防抖的一部分，将 is_just_at 状态积累）
        if self.plugin.user_debounce_time > 0:
            if debounce_key in self.plugin.debounce_start_map:
                self.plugin.debounce_at_map[debounce_key] = (
                    self.plugin.debounce_at_map.get(debounce_key, False) or is_just_at
                )
                is_just_at = self.plugin.debounce_at_map[debounce_key]
            else:
                self.plugin.debounce_at_map[debounce_key] = is_just_at

        fmt_key = f"{bot_name}:{group_or_user_id}"
        active_counter = self.plugin.active_reply_counters.get(fmt_key, 0)
        is_active_window = active_counter > 0

        # 是否针对当前消息强制直接回复（不走小模型判断）
        should_force_reply = False
        # 是否需要递减接话分析窗口的标志
        decrement_counter = False

        if is_just_at:
            if is_private and self.plugin.private_chat_bypass:
                should_force_reply = True
            else:
                at_behavior = decision_conf.get("at_behavior", "force_reply")
                has_decision_provider = decision_conf.get("enabled", True) and bool(
                    decision_conf.get("provider_ids") or decision_conf.get("provider_id")
                )
                is_in_whitelist = (
                    not decision_conf.get("group_whitelist")
                    or group_or_user_id in decision_conf.get("group_whitelist")
                )

                if not has_decision_provider or not is_in_whitelist:
                    if at_behavior != "force_reply":
                        logger.warning(
                            f"[Giftia] {bot_name} 配置了 @ 行为为 '{at_behavior}'，但小模型决策未启用、无可用提供商或不在白名单内，自动降级为强制回复"
                        )
                    should_force_reply = True
                elif at_behavior == "force_reply":
                    should_force_reply = True
                elif at_behavior == "decide_in_window_force_outside":
                    if is_active_window:
                        should_force_reply = False
                    else:
                        should_force_reply = True
                elif at_behavior == "activate_and_decide":
                    should_force_reply = False
                else:
                    should_force_reply = True

                # 如果 @ 行为交由小模型判断，则立即刷新/激活活跃窗口计数
                if not should_force_reply:
                    window_size = decision_conf.get("reply_active_window", 10)
                    self.plugin.active_reply_counters[fmt_key] = window_size
                    logger.info(
                        f"[Giftia] {bot_name} 收到 @ 消息，根据 @ 行为策略刷新接话分析窗口为 {window_size} 并交由小模型进行判断"
                    )
        else:
            if not decision_conf.get("enabled", True) or not (
                decision_conf.get("provider_ids") or decision_conf.get("provider_id")
            ):
                logger.debug("没有at机器人且未开启决策，跳过处理")
                return False, None, False, None
            if decision_conf.get(
                "group_whitelist"
            ) and group_or_user_id not in decision_conf.get("group_whitelist"):
                logger.debug("没有at机器人且当前群组不在决策白名单内，跳过处理")
                return False, None, False, None

            # 活跃窗口与主动接话概率检查
            proactive_prob = decision_conf.get("proactive_probability", 0)
            is_proactive_hit = False
            is_keyword_hit = False

            if is_active_window:
                decrement_counter = True
            else:
                is_proactive_hit = (
                    proactive_prob > 0 and random.randint(1, 100) <= proactive_prob
                )

                # 关键词触发检查
                if (
                    not is_proactive_hit
                    and decision_conf.get("keyword_trigger_enabled", False)
                    and current_message.content
                ):
                    content_lower = current_message.content.lower()
                    keyword_rules = decision_conf.get("keyword_rules", [])
                    default_prob = decision_conf.get("keyword_default_probability", 100)

                    for rule_str in keyword_rules:
                        if not rule_str or not isinstance(rule_str, str):
                            continue
                        if ":" in rule_str:
                            keywords_str, prob_str = rule_str.split(":", 1)
                            prob = prob_str.strip()
                        else:
                            keywords_str = rule_str
                            prob = default_prob

                        kw_list = [
                            k.strip()
                            for k in re.split(r"[,，]", keywords_str)
                            if k.strip()
                        ]
                        for kw in kw_list:
                            if kw.lower() in content_lower:
                                try:
                                    prob_val = int(prob)
                                except (ValueError, TypeError):
                                    prob_val = default_prob

                                if random.randint(1, 100) <= prob_val:
                                    is_keyword_hit = True
                                    logger.info(
                                        f"{bot_name} 匹配到兴趣关键词 '{kw}'，触发接话决策"
                                    )
                                break
                        if is_keyword_hit:
                            break

            if not is_active_window and not is_proactive_hit and not is_keyword_hit:
                logger.debug(
                    "没有at机器人且不满足接话分析窗口、主动概率或关键词触发，跳过处理"
                )
                return False, None, False, None

        # 跳过空消息
        if (
            not current_message.content
            and not current_message.media_id_list
            and not current_message.forward_messages
        ):
            logger.debug("消息为空，跳过处理")
            return False, None, False, None

        # 跳过已唤醒的消息
        if event._has_send_oper:
            logger.debug(f"{bot_name} 跳过已唤醒的消息: {current_message.content}")
            return False, None, False, None

        # 防抖延迟等待
        if self.plugin.user_debounce_time > 0:
            current_time = time.time()

            if debounce_key not in self.plugin.debounce_start_map:
                self.plugin.debounce_start_map[debounce_key] = current_time

            time_since_start = (
                current_time - self.plugin.debounce_start_map[debounce_key]
            )

            if time_since_start >= self.plugin.user_max_debounce_time:
                logger.debug(
                    f"{bot_name} 消息 {debounce_key} 达到最大防抖时间，强制执行"
                )
                self.plugin.debounce_start_map.pop(debounce_key, None)
                self.plugin.debounce_at_map.pop(debounce_key, None)
                self.plugin.debounce_map[debounce_key] = current_time
            else:
                self.plugin.debounce_map[debounce_key] = current_time
                await asyncio.sleep(self.plugin.user_debounce_time)
                if self.plugin.debounce_map.get(debounce_key) != current_time:
                    logger.debug(f"{bot_name} 消息 {debounce_key} 触发防抖，跳过处理")
                    return False, None, False, None
                else:
                    self.plugin.debounce_start_map.pop(debounce_key, None)
                    self.plugin.debounce_at_map.pop(debounce_key, None)

        logger.debug(f"{bot_name} 处理消息: {current_message.content}")
        reply_key = f"{bot_name}:{group_or_user_id}"

        # 节流拦截更新
        if should_force_reply:
            now = time.time()
            if self.plugin.user_throttle_time > 0:
                user_throttle_key = f"{bot_name}:{event.get_sender_id()}"
                self.plugin.throttle_map[user_throttle_key] = now
            if self.plugin.group_throttle_time > 0:
                group_throttle_key = f"{bot_name}:{event.get_group_id()}"
                self.plugin.throttle_map[group_throttle_key] = now

        # 强制回复直接跳过后续 LLM 决策，更新决策表为 3 (直接回复)
        if should_force_reply:
            if is_private and self.plugin.replying_status.get(reply_key, 0) > 0:
                logger.debug(
                    f"{bot_name} 消息 {reply_key} 正在回复中，私聊防并发单线程拦截"
                )
                return False, None, True, None

            await self.plugin.db.update_message_decision(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                message_id=current_message.message_id,
                reply_decision=3,
                use_rag=2,
            )
            return True, None, True, None

        # 非强制回复消息进行 LLM 决策
        if self.plugin.replying_status.get(reply_key, 0) > 0:
            logger.debug(f"{bot_name} 消息 {reply_key} 正在回复中，跳过决策")
            return False, None, False, None

        # 节流判断
        if not is_private:
            user_throttle_key = f"{bot_name}:{event.get_sender_id()}"
            if self.plugin.user_throttle_time > 0 and not self.can_execute(
                user_throttle_key, self.plugin.user_throttle_time
            ):
                logger.info(f"{bot_name} 消息用户{user_throttle_key}节流中，跳过处理")
                return False, None, False, None

            group_throttle_key = f"{bot_name}:{event.get_group_id()}"
            if self.plugin.group_throttle_time > 0 and not self.can_execute(
                group_throttle_key, self.plugin.group_throttle_time
            ):
                logger.info(f"{bot_name} 消息群组{group_throttle_key}节流中，跳过处理")
                return False, None, False, None

        # 并发锁判断
        fmt_user_lock = f"{bot_name}:{group_or_user_id}:{event.get_sender_id()}"
        user_lock = self.plugin.user_locks[fmt_user_lock]
        if user_lock.locked():
            logger.info(f"{bot_name} 用户{fmt_user_lock}正在决策中，跳过处理")
            return False, None, False, None

        fmt_lock = f"{bot_name}:{group_or_user_id}"
        lock = self.plugin.group_locks[fmt_lock]
        if self.plugin.concurrent_strategy == "discard" and lock.locked():
            logger.info(f"{bot_name} 消息群组{fmt_lock}并发数已达上限，跳过处理")
            return False, None, False, None

        relevant_memories = None

        async with user_lock:
            async with lock:
                # 双重检查
                if self.plugin.replying_status.get(reply_key, 0) > 0:
                    logger.debug(
                        f"{bot_name} 消息 {reply_key} 正在回复中，跳过决策 (队列拦截)"
                    )
                    return False, None, False, None

                # 获取决策所需上下文（小模型采用轻量级条数）
                recent_messages = await self.plugin.data_cache.get_recent_message(
                    bot_name=bot_name,
                    group_id=group_or_user_id,
                    limit=12,
                )
                bot_status = await self.plugin.data_cache.get_bot_status(
                    bot_name=bot_name,
                    group_id=group_or_user_id,
                )
                group_profile = await self.plugin.data_cache.get_group_profile(
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                )
                user_profile = await self.plugin.data_cache.get_user_profile_record(
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                    user_id=event.get_sender_id(),
                )
                user_relation = await self.plugin.data_cache.get_user_relation(
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                    user_id=event.get_sender_id(),
                )
                active_user_briefs = (
                    await self.plugin.data_cache.build_active_user_briefs(
                        bot_name=bot_name,
                        group_or_user_id=group_or_user_id,
                        recent_messages=recent_messages,
                        current_user_id=event.get_sender_id(),
                        self_id=event.get_self_id(),
                        limit=self.plugin.tools_config.get(
                            "active_user_brief_limit", 10
                        ),
                    )
                )
                short_tasks = []
                short_task_limit = self.plugin.tools_config.get(
                    "task_board_max_active", 3
                )
                if hasattr(self.plugin, "task_board"):
                    short_tasks = await self.plugin.task_board.get_active_tasks(
                        bot_name=bot_name,
                        group_or_user_id=group_or_user_id,
                    )
                    short_task_limit = self.plugin.task_board.max_active_tasks()

                # 读取已有缓存的媒体转述（仅读缓存，非阻塞），用于轻量内联
                caption_config = self.plugin.get_caption_config(bot_conf)
                all_for_caption = []
                if recent_messages:
                    all_for_caption.extend(recent_messages)
                if current_message:
                    all_for_caption.append(current_message)

                media_captions = await self.media_captioner.get_cached_media_captions(
                    bot_name=bot_name,
                    recent_messages=all_for_caption,
                    caption_config=caption_config,
                    group_or_user_id=group_or_user_id,
                )

                user_prompt = build_decision_prompt(
                    user_id=event.get_sender_id(),
                    group_data=str(
                        await event.get_group(event.get_group_id())
                        if event.get_group_id()
                        else ""
                    ),
                    recent_messages=recent_messages,
                    current_message=current_message,
                    bot_status=bot_status,
                    group_profile=group_profile,
                    user_profile=user_profile,
                    user_relation=user_relation,
                    active_user_briefs=active_user_briefs,
                    short_tasks=short_tasks,
                    short_task_limit=short_task_limit,
                    message_truncate_limit=getattr(self.plugin, "reply_message_truncate_limit", 1500),
                    media_captions=media_captions,
                )

                provider_ids = decision_conf.get("provider_ids")
                if not provider_ids:
                    old_provider_id = decision_conf.get("provider_id")
                    if old_provider_id:
                        provider_ids = [old_provider_id] + decision_conf.get(
                            "fallback_provider_ids", []
                        )
                    else:
                        logger.error(f"{bot_name} 未配置决策模型ID")
                        return False, None, False, None
                provider_ids = [p for p in provider_ids if p]
                if not provider_ids:
                    logger.error(f"{bot_name} 未配置决策模型ID")
                    return False, None, False, None

                # 递减分析窗口
                if decrement_counter:
                    fmt_key = f"{bot_name}:{group_or_user_id}"
                    self.plugin.active_reply_counters[fmt_key] = max(
                        0, self.plugin.active_reply_counters.get(fmt_key, 0) - 1
                    )
                    logger.debug(
                        f"{bot_name} 消耗接话分析窗口次数，当前群组剩余分析次数: {self.plugin.active_reply_counters[fmt_key]}"
                    )

                # 调用 LLM 决策（纯文本化调用，不传递多模态原始 URL）
                result = await self.plugin.call_llm.call_llm_decision(
                    provider_ids=provider_ids,
                    system_prompt=decision_conf.get("decision_prompt"),
                    user_prompt=user_prompt,
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                )

                if result is None:
                    logger.error(f"{bot_name} LLM决策失败，默认判定为不回复")
                    return False, None, False, None

                # 更新消息决策表
                if result.reply_decision != 2 or result.use_rag != 2:
                    await self.plugin.db.update_message_decision(
                        bot_name=bot_name,
                        group_or_user_id=group_or_user_id,
                        message_id=current_message.message_id,
                        reply_decision=result.reply_decision,
                        use_rag=result.use_rag,
                    )

                if result.reply_decision == 0 or result.reply_decision == 2:
                    logger.info(f"{bot_name} LLM决策判定：不回复")
                    return False, None, False, None

                logger.info(f"{bot_name} LLM决策判定：回复")

                # 重置接话活跃分析窗口
                fmt_key = f"{bot_name}:{group_or_user_id}"
                window_size = decision_conf.get("reply_active_window", 10)
                self.plugin.active_reply_counters[fmt_key] = window_size
                logger.info(
                    f"{bot_name} LLM决策判定回复，重置接话分析窗口计数为 {window_size}"
                )

                # 如果命中 RAG，执行记忆搜索
                if result.use_rag == 1 and self.plugin.embedding_conf.get(
                    "enabled", False
                ):
                    memory_results = await search_memories_with_rerank(
                        self.plugin,
                        bot_name=bot_name,
                        group_or_user_id=group_or_user_id,
                        query=result.rag_query,
                        recent_messages=recent_messages,
                        log_context="决策 RAG 记忆召回",
                    )
                    relevant_memories = [m["text"] for m in memory_results]
                    return True, relevant_memories, False, memory_results

                return True, relevant_memories, False, None
