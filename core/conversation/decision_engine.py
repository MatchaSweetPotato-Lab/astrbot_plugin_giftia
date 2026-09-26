import random
import re

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

    def is_session_allowed(
        self, bot_name: str, session_id: str, *, is_private: bool
    ) -> bool:
        """Check the bot's session list according to its configured mode.

        Args:
            bot_name: Name of an enabled bot configuration.
            session_id: Group ID or private-chat user ID.
            is_private: Whether to use the private-chat list instead of the group list.

        Returns:
            Whether this bot is enabled for the session. In blacklist mode the
            list contains denied sessions; in whitelist mode it contains allowed
            sessions.
        """
        bot_conf = self.plugin.bot_map.get(bot_name, {})
        decision_conf = bot_conf.get("decision_conf", {})
        kind = "private" if is_private else "group"
        default_mode = "whitelist" if is_private else "blacklist"
        sessions = decision_conf.get(f"{kind}_whitelist", [])
        mode = decision_conf.get(f"{kind}_whitelist_mode", default_mode)
        if isinstance(mode, bool):
            mode = "whitelist" if mode else "blacklist"
        else:
            mode = str(mode or default_mode).strip().lower()
        in_list = str(session_id) in sessions
        return in_list if mode in ("whitelist", "白名单", "白名单模式") else not in_list

    def check_whitelists(self, event: AstrMessageEvent) -> bool:
        """Check session access and user exclusions before processing a message.

        Args:
            event: Incoming message event.

        Returns:
            Whether the message may be parsed, stored, and considered for replies.
        """
        bot_name = self.plugin.adapter_id_map.get(event.platform_meta.id)
        group_id = event.get_group_id()
        session_id = group_id or event.get_sender_id()
        if not bot_name or not self.is_session_allowed(
            bot_name, session_id, is_private=not bool(group_id)
        ):
            return False
        blocked_users = self.plugin.bot_map[bot_name].get("blocked_users", {})
        return str(event.get_sender_id()) not in blocked_users.get(
            event.unified_msg_origin, []
        )

    def get_trigger(
        self,
        event: AstrMessageEvent,
        bot_name: str,
        group_or_user_id: str,
        current_message: MessageData,
        *,
        continuation: bool = False,
    ) -> bool | None:
        """Choose admission once, without waiting or calling a model.

        Args:
            event: Incoming event, retained for routing and mention policy.
            bot_name: Bot configuration name.
            group_or_user_id: Stored conversation identifier.
            current_message: Parsed incoming message.
            continuation: Whether a session already has an active processing task.

        Returns:
            True for a direct reply, False for a model decision, or None when idle.
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
            return None

        is_just_at = any(
            isinstance(c, At) and str(c.qq) == event.get_self_id()
            for c in event.get_messages()
        )
        is_private = not event.get_group_id()
        private_decision_enabled = bool(
            decision_conf.get("private_chat_decision_enabled", False)
        )
        if is_private and not private_decision_enabled:
            is_just_at = True
        elif is_private:
            # Private messages opt into the same decision model without treating
            # the message as an @ mention, so every allowed message is evaluated.
            is_just_at = False

        fmt_key = f"{bot_name}:{group_or_user_id}"
        active_counter = self.plugin.active_reply_counters.get(fmt_key, 0)
        is_active_window = active_counter > 0

        # 是否针对当前消息强制直接回复（不走前置决策）
        should_force_reply = False

        if is_just_at:
            if is_private:
                should_force_reply = True
            else:
                at_behavior = decision_conf.get("at_behavior", "force_reply")
                has_decision_provider = bool(
                    decision_conf.get("provider_ids")
                    or decision_conf.get("provider_id")
                )
                if not has_decision_provider:
                    if at_behavior != "force_reply":
                        logger.warning(
                            f"[Giftia] {bot_name}: @ behavior '{at_behavior}' falls back to a forced reply because no decision provider is configured"
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

                # 如果 @ 行为交由前置决策，则立即刷新/激活活跃窗口计数
                if not should_force_reply:
                    window_size = decision_conf.get("reply_active_window", 10)
                    self.plugin.active_reply_counters[fmt_key] = window_size
                    logger.info(
                        f"[Giftia] {bot_name} 收到 @ 消息，根据 @ 行为策略刷新接话分析窗口为 {window_size} 并交由小模型进行判断"
                    )
        else:
            if not (
                decision_conf.get("provider_ids") or decision_conf.get("provider_id")
            ):
                logger.debug(
                    "Skipping unmentioned message: no decision provider configured"
                )
                return None
            # 活跃窗口与主动接话概率检查
            proactive_prob = decision_conf.get("proactive_probability", 0)
            is_proactive_hit = False
            is_keyword_hit = False
            should_decide_private = is_private and private_decision_enabled

            if should_decide_private:
                is_proactive_hit = True
            elif is_active_window or continuation:
                pass
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

            if (
                not continuation
                and not is_active_window
                and not is_proactive_hit
                and not is_keyword_hit
            ):
                logger.debug(
                    "没有at机器人且不满足接话分析窗口、主动概率或关键词触发，跳过处理"
                )
                return None

        # 跳过空消息
        if (
            not current_message.content
            and not current_message.media_id_list
            and not current_message.forward_messages
        ):
            logger.debug("消息为空，跳过处理")
            return None

        # 跳过已唤醒的消息
        if getattr(event, "_has_send_oper", False):
            logger.debug(f"{bot_name} 跳过已唤醒的消息: {current_message.content}")
            return None

        return should_force_reply

    async def evaluate_decision(
        self,
        event: AstrMessageEvent,
        bot_name: str,
        nickname: str,
        group_or_user_id: str,
        pending_messages: list[MessageData],
        recent_messages: list[MessageData],
        *,
        force_reply: bool = False,
    ) -> tuple[bool, list[str] | None, list[dict] | None, str | None]:
        """Evaluate a frozen batch; admission and scheduling belong to the caller.

        Args:
            event: Routing event for this batch.
            bot_name: Bot configuration name.
            nickname: Bot display name.
            group_or_user_id: Stored conversation identifier.
            pending_messages: Every message being decided in this round.
            recent_messages: Frozen history, excluding messages queued for later.
            force_reply: Whether a batch member requests a direct reply.

        Returns:
            Reply choice, recalled texts, recalled records, and optional meme tags.

        Raises:
            RuntimeError: All model attempts failed; this is not a silent decision.
            ValueError: No decision provider is configured.
        """
        bot_conf = self.plugin.bot_map[bot_name]
        decision_conf = bot_conf.get("decision_conf", {})
        if force_reply:
            for message in pending_messages:
                await self.plugin.db.update_message_decision(
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                    message_id=message.message_id,
                    reply_decision=3,
                    use_rag=2,
                )
            return True, None, None, None

        relevant_memories = None
        # 获取决策所需上下文（小模型采用轻量级条数）
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
        active_user_briefs = await self.plugin.data_cache.build_active_user_briefs(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            recent_messages=[*recent_messages, *pending_messages],
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

        # 读取已有缓存的媒体转述（仅读缓存，非阻塞），用于轻量内联
        caption_config = self.plugin.get_caption_config(bot_conf)
        all_for_caption = []
        if recent_messages:
            all_for_caption.extend(recent_messages)
        all_for_caption.extend(pending_messages)

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
            pending_messages=pending_messages,
            bot_status=bot_status,
            group_profile=group_profile,
            user_profile=user_profile,
            user_relation=user_relation,
            active_user_briefs=active_user_briefs,
            short_tasks=short_tasks,
            short_task_limit=short_task_limit,
            message_truncate_limit=getattr(
                self.plugin, "reply_message_truncate_limit", 1500
            ),
            media_captions=media_captions,
            slang_entries=await self.plugin.db.slang_repo.get_entries(
                bot_name, group_or_user_id
            ),
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
                raise ValueError("No decision provider configured")
        provider_ids = [p for p in provider_ids if p]
        if not provider_ids:
            logger.error(f"{bot_name} 未配置决策模型ID")
            raise ValueError("No decision provider configured")

        # Consume one analysis round per batch, never once per sender.
        fmt_key = f"{bot_name}:{group_or_user_id}"
        self.plugin.active_reply_counters[fmt_key] = max(
            0, self.plugin.active_reply_counters.get(fmt_key, 0) - 1
        )

        # 调用 LLM 决策（纯文本化调用，不传递多模态原始 URL）
        result = await self.plugin.call_llm.call_llm_decision(
            provider_ids=provider_ids,
            system_prompt=decision_conf.get("decision_prompt"),
            user_prompt=user_prompt,
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            use_meme_manager=getattr(self.plugin, "use_meme_manager", False),
        )

        if result is None or result.reply_decision not in (0, 1):
            raise RuntimeError("Decision providers exhausted their retries")

        for message in pending_messages:
            await self.plugin.db.update_message_decision(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                message_id=message.message_id,
                reply_decision=result.reply_decision,
                use_rag=result.use_rag,
            )

        if result.reply_decision == 0 or result.reply_decision == 2:
            logger.info(f"{bot_name} LLM决策判定：不回复")
            return False, None, None, None

        logger.info(f"{bot_name} LLM决策判定：回复")

        # 重置接话活跃分析窗口
        fmt_key = f"{bot_name}:{group_or_user_id}"
        window_size = decision_conf.get("reply_active_window", 10)
        self.plugin.active_reply_counters[fmt_key] = window_size
        logger.info(f"{bot_name} LLM决策判定回复，重置接话分析窗口计数为 {window_size}")

        # 如果命中 RAG，执行记忆搜索
        if result.use_rag == 1 and self.plugin.embedding_conf.get("enabled", False):
            memory_results = await search_memories_with_rerank(
                self.plugin,
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                query=result.rag_query,
                recent_messages=recent_messages,
                log_context="决策 RAG 记忆召回",
            )
            relevant_memories = [m["text"] for m in memory_results]
            return True, relevant_memories, memory_results, result.meme_tags

        return True, relevant_memories, None, result.meme_tags
