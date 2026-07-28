import asyncio
import json
from collections import defaultdict

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain, Reply, Image
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.utils.session_lock import session_lock_manager

from .core.conversation.chat_manager import ChatManager
from .core.database.data_cache import DataCache
from .core.database.database import Database
from .core.handlers.commands import CommandHandler
from .core.llm.call_llm import CallLLM
from .core.llm.llm_tools import (
    GetMessageContextTool,
    InspectForwardMessageTool,
    InspectVideoTool,
    SearchChatHistoryTool,
    SearchUserProfileTool,
    remove_tools,
)
from .core.llm.xml_parse import XmlParse
from .core.memory.memory import LTM
from .core.memory.passive_memory import PassiveMemoryManager
from .core.tts.manager import TTSManager
from .core.utils.aiocqhttp_action import AIoCQHTTPAction
from .core.utils.emoji_manager import EmojiManager
from .core.utils.http_manager import HttpManager
from .core.utils.message_parse import MessageParser
from .core.utils.scheduler import Scheduler
from .core.utils.task_board import TaskBoardManager
from .core.utils.tools_func import ToolsFunc
from .core.web.webui_manager import WebUIManager
from .core.utils.schemas import XmlLlmResult


class Giftia(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context: Context = context  # type: ignore
        self.conf = config
        bot_list = self.conf.get("bot_template", [])
        # 机器人名称，映射机器人配置
        self.bot_map: dict[str, dict] = {}
        # 机器人可以有多个适配器，多个适配器映射同一个机器人名称
        self.adapter_id_map: dict[str, str] = {}
        for bot_conf in bot_list:
            if not bot_conf.get("enabled", False):
                continue
            self.bot_map[bot_conf["name"]] = bot_conf
            for adapter_id in bot_conf.get("adapter_ids", []):
                self.adapter_id_map[adapter_id] = bot_conf["name"]

        # 图片转述提供商
        self.caption_config = self.conf.get("caption_config", {})
        # 聊天记录配置
        msg_history = self.conf.get("msg_history", {})
        self.msg_number = msg_history.get("msg_number", 300)

        # 白名单配置
        self.whitelist_config = self.conf.get("whitelist_config", {})
        self.group_whitelist_enabled = self.whitelist_config.get(
            "group_whitelist_enabled", False
        )
        self.group_whitelist = self.whitelist_config.get("group_whitelist", [])
        self.user_whitelist_enabled = self.whitelist_config.get(
            "user_whitelist_enabled", False
        )
        self.user_whitelist = self.whitelist_config.get("user_whitelist", [])
        self.private_chat_bypass = self.whitelist_config.get(
            "private_chat_bypass_decision_and_whitelist", True
        )
        self.private_user_whitelist_enabled = self.whitelist_config.get(
            "private_user_whitelist_enabled", False
        )
        self.private_user_whitelist = self.whitelist_config.get(
            "private_user_whitelist", []
        )

        # 并发策略
        self.concurrent_config = self.conf.get("concurrent_config", {})
        self.concurrent_strategy = self.concurrent_config.get(
            "concurrent_strategy", "discard"
        )
        self.concurrent_limit = self.concurrent_config.get("concurrent_limit", 2)
        # 节流配置
        self.user_throttle_time = self.concurrent_config.get("user_throttle_time", 10)
        self.group_throttle_time = self.concurrent_config.get("group_throttle_time", 5)
        self.throttle_map: dict[str, float] = {}
        # 接话分析窗口计数器 bot_name:group_or_user_id -> remaining_messages
        self.active_reply_counters: dict[str, int] = {}
        # 防抖字典
        self.user_debounce_time = self.concurrent_config.get("user_debounce_time", 3)
        self.user_max_debounce_time = self.concurrent_config.get(
            "user_max_debounce_time", 12
        )
        self.debounce_map: dict[str, float] = {}
        self.debounce_start_map: dict[str, float] = {}
        self.debounce_at_map: dict[str, bool] = {}
        # 表情包配置
        self.sticker_config = self.conf.get("sticker_config", {})
        self.random_sticker_count = self.sticker_config.get("random_sticker_count", 20)
        # 常规配置
        self.normal_config = self.conf.get("normal_config", {})
        self.min_reply_interval = self.normal_config.get("min_reply_interval", 2)
        self.max_reply_interval = self.normal_config.get("max_reply_interval", 4)
        self.energy_recovery_interval = self.normal_config.get(
            "energy_recovery_interval", 90
        )
        self.reply_message_truncate_limit = self.normal_config.get(
            "reply_message_truncate_limit", 1500
        )
        self.safety_intercept_keywords = self.normal_config.get(
            "safety_intercept_keywords", []
        )
        # 记忆配置
        memory_config = self.conf.get("memory_config", {})
        self.embedding_conf = memory_config.get("embedding_conf", {})
        self.rerank_conf = memory_config.get("rerank_conf", {})
        self.passive_memory_enabled = memory_config.get("passive_memory_enabled", False)
        self.passive_memory_provider_ids = memory_config.get(
            "passive_memory_provider_ids", []
        )
        self.passive_memory_silence_threshold = memory_config.get(
            "passive_memory_silence_threshold", 10
        )
        self.passive_memory_overflow_threshold = memory_config.get(
            "passive_memory_overflow_threshold", 100
        )

        # LLM工具配置
        self.tools_config = self.conf.get("tools_config", {})
        # TTS XML 配置
        self.tts_config = self.conf.get("tts_config", {})
        # 并发锁
        self.group_locks = defaultdict(lambda: asyncio.Semaphore(self.concurrent_limit))
        # 用户并发锁
        self.user_locks = defaultdict(asyncio.Lock)
        # 消息解析锁
        self.parse_locks = defaultdict(asyncio.Lock)
        # 表情包并发锁
        self.sticker_locks = defaultdict(asyncio.Lock)

        # 实例化
        self.http_manager = HttpManager(self.conf)
        sticker_summaries = self.sticker_config.get(
            "sticker_summaries", ["这是一张表情包"]
        )
        self.aiocqhttp = AIoCQHTTPAction(sticker_summaries=sticker_summaries)

        # 缓存
        self._recall_tasks = set()

        # 正在运行的任务映射
        self.running_tasks: dict[str, asyncio.Task] = {}

        # 正在回复的状态映射
        self.replying_status: dict[str, int] = {}

        self._original_send_message = self.context.send_message

    def get_caption_config(self, bot_conf: dict | None = None) -> dict:
        """Return global media-caption config with optional per-bot overrides."""
        caption_config = dict(self.caption_config or {})
        if bot_conf:
            caption_config.update(bot_conf.get("caption_config") or {})
        return caption_config

    async def initialize(self):
        """插件初始化方法"""
        # 实例化底座服务
        self.ltm = LTM(self.context, self.embedding_conf, self.rerank_conf)
        self.db = await Database.connect()
        self.data_cache = DataCache(
            db=self.db,
            http_manager=self.http_manager,
            ltm=self.ltm,
            msg_number=self.msg_number,
            energy_recovery_interval=self.energy_recovery_interval,
            plugin=self,
        )
        self.emoji_manager = EmojiManager(
            self.db, random_sticker_count=self.random_sticker_count
        )
        sticker_summaries = self.conf.get("sticker_config", {}).get(
            "sticker_summaries", ["这是一张表情包"]
        )
        self.xml_parse = XmlParse(
            self.data_cache, self.emoji_manager, sticker_summaries
        )
        self.call_llm = CallLLM(
            context=self.context,
            xml_parse=self.xml_parse,
            caption_config=self.conf.get("caption_config", {}),
            network_config=self.conf.get("network_config", {}),
            plugin=self,
        )
        self.message_parser = MessageParser(
            data_cache=self.data_cache,
            http_manager=self.http_manager,
            image_caption_enabled=self.caption_config.get(
                "image_caption_enabled", True
            ),
            audio_caption_enabled=self.caption_config.get(
                "audio_caption_enabled", True
            ),
            call_llm=self.call_llm,
        )
        # 定时任务
        self.task_manager = Scheduler()
        self.tools_func = ToolsFunc(
            self.conf, self.task_manager, self.db, self.http_manager, self.data_cache
        )

        # 实例化逻辑管理器
        self.task_board = TaskBoardManager(self)
        self.passive_memory_manager = PassiveMemoryManager(self)
        self.tts_manager = TTSManager(self)
        self.cmd_handler = CommandHandler(self)
        self.chat_manager = ChatManager(self)

        # 注册定时任务提醒函数
        self.task_manager.register_func("remind", self.remind_task)

        # 注册函数调用工具
        if self.conf.get("tools_config", {}).get("search_chat_history_enabled", True):
            self.context.add_llm_tools(SearchChatHistoryTool(plugin=self))
            logger.info("已注册函数调用工具: search_chat_history")
        if self.conf.get("tools_config", {}).get("get_message_context_enabled", True):
            self.context.add_llm_tools(GetMessageContextTool(plugin=self))
            logger.info("已注册函数调用工具: get_message_context")
        if self.conf.get("tools_config", {}).get("search_user_profile_enabled", True):
            self.context.add_llm_tools(SearchUserProfileTool(plugin=self))
            logger.info("已注册函数调用工具: search_user_profile")
        if self.conf.get("tools_config", {}).get("inspect_forward_message_enabled", True):
            self.context.add_llm_tools(InspectForwardMessageTool(plugin=self))
            logger.info("已注册函数调用工具: inspect_forward_message")
        if self.conf.get("tools_config", {}).get("inspect_video_enabled", True):
            self.context.add_llm_tools(InspectVideoTool(plugin=self))
            logger.info("已注册函数调用工具: inspect_video")
        # 注册 Web UI 及 API 路由
        self.webui_manager = WebUIManager(self)
        self.webui_manager.register_routes()
        self.web_api = self.webui_manager.web_api

        # Intercept context.send_message to capture bot replies sent via context
        original_context_send_message = self._original_send_message

        async def intercepted_context_send_message(session, message_chain) -> bool:
            ret = await original_context_send_message(session, message_chain)
            if getattr(self, "_terminated", False):
                return ret
            try:
                from datetime import datetime

                from astrbot.core.platform.message_session import MessageSesion

                from .core.utils.schemas import MessageData

                if isinstance(session, str):
                    session_obj = MessageSesion.from_str(session)
                else:
                    session_obj = session

                bot_name = self.adapter_id_map.get(session_obj.platform_name)

                if bot_name:
                    bot_conf = self.bot_map.get(bot_name, {})
                    nickname = bot_conf.get("nickname", bot_name)
                    parsed_msg = await self.message_parser.chain_to_result(
                        message_chain.chain, defer_caption=False
                    )

                    self_id = ""
                    for adapter_id in bot_conf.get("adapter_ids", []):
                        if ":" in adapter_id and adapter_id.startswith(
                            session_obj.platform_name + ":"
                        ):
                            self_id = adapter_id.split(":", 1)[1]
                            break

                    if not self_id:
                        for platform in self.context.platform_manager.platform_insts:
                            if platform.meta().id == session_obj.platform_name:
                                for attr in (
                                    "self_id",
                                    "bot_self_id",
                                    "client_self_id",
                                ):
                                    if hasattr(platform, attr):
                                        val = getattr(platform, attr)
                                        if (
                                            val
                                            and isinstance(val, str)
                                            and not callable(val)
                                            and not hasattr(val, "func")
                                        ):
                                            self_id = val
                                            break

                    await self.data_cache.add_message(
                        bot_name,
                        session_obj.session_id,
                        MessageData(
                            nickname=nickname,
                            user_id=self_id or "bot",
                            group_or_user_id=session_obj.session_id,
                            time=datetime.now().isoformat(),
                            message_id="",
                            content=parsed_msg.content,
                            is_recalled=0,
                            media_id_list=parsed_msg.media_id_list,
                            forward_messages=parsed_msg.forward_messages,
                        ),
                    )
            except Exception as e:
                logger.error(
                    f"[Giftia] Error logging intercepted context send_message: {e}",
                    exc_info=True,
                )
            return ret

        self.context.send_message = intercepted_context_send_message

    # ==================== 命令监听与分发 ====================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("工具列表")
    async def tool_list(self, event: AstrMessageEvent, index: int = 1):
        """工具列表"""
        async for chunk in self.cmd_handler.tool_list(event, index):
            yield chunk

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("工具解析")
    async def tool_xml(self, event: AstrMessageEvent, name: str):
        """将函数调用工具解析成xml格式"""
        async for chunk in self.cmd_handler.tool_xml(event, name):
            yield chunk

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("打印embedding模型")
    async def get_embedding_models(self, event: AstrMessageEvent):
        """打印所有支持的模型信息"""
        await self.cmd_handler.get_embedding_models(event)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("打印rerank模型")
    async def get_rerank_models(self, event: AstrMessageEvent):
        """打印所有支持的模型信息"""
        await self.cmd_handler.get_rerank_models(event)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("读取记忆")
    async def get_memory(
        self,
        event: AstrMessageEvent,
        bot_name: str,
        group_or_user_id: str,
        rag_queries: str,
    ):
        """根据ID获取记忆"""
        async for chunk in self.cmd_handler.get_memory(
            event, bot_name, group_or_user_id, rag_queries
        ):
            yield chunk

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("读取近期记忆")
    async def get_early_memory(
        self,
        event: AstrMessageEvent,
        bot_name: str,
        group_or_user_id: str,
        limit: int = 10,
    ):
        """根据ID获取记忆"""
        async for chunk in self.cmd_handler.get_early_memory(
            event, bot_name, group_or_user_id, limit
        ):
            yield chunk

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("删除消息")
    async def delete_message(self, event: AstrMessageEvent):
        """根据ID删除消息"""
        async for chunk in self.cmd_handler.delete_message(event):
            yield chunk

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("删除记忆")
    async def delete_memory(self, event: AstrMessageEvent, memory_id: str):
        """根据ID删除记忆"""
        async for chunk in self.cmd_handler.delete_memory(event, memory_id):
            yield chunk

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("清空记忆")
    async def delete_all_memories(
        self, event: AstrMessageEvent, bot_name: str, group_or_user_id: str
    ):
        """删除全部记忆"""
        async for chunk in self.cmd_handler.delete_all_memories(
            event, bot_name, group_or_user_id
        ):
            yield chunk

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("加满能量")
    async def fill_energy(self, event: AstrMessageEvent, bot_name: str):
        """给当前群的指定机器人加满能量"""
        async for chunk in self.cmd_handler.fill_energy(event, bot_name):
            yield chunk

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("清空媒体缓存")
    async def delete_all_media_cache(self, event: AstrMessageEvent):
        """清空全部媒体缓存"""
        async for chunk in self.cmd_handler.delete_all_media_cache(event):
            yield chunk

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("定时任务列表")
    async def task_list(self, event: AstrMessageEvent, index: int = 1):
        """获取全部定时任务"""
        async for chunk in self.cmd_handler.task_list(event, index):
            yield chunk

    @filter.command("获取定时任务")
    async def get_task_by_group(self, event: AstrMessageEvent, prefix: str):
        """根据botname+group_or_user_id获取定时任务"""
        async for chunk in self.cmd_handler.get_task_by_group(event, prefix):
            yield chunk

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("删除定时任务")
    async def delete_task(self, event: AstrMessageEvent, task_id: str):
        """删除定时任务"""
        async for chunk in self.cmd_handler.delete_task(event, task_id):
            yield chunk

    @filter.command("读取媒体转述", alias={"媒体转述"})
    async def get_media_caption(self, event: AstrMessageEvent):
        """读取媒体转述"""
        async for chunk in self.cmd_handler.get_media_caption(event):
            yield chunk

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("删除数据表")
    async def delete_table(self, event: AstrMessageEvent, table_name: str):
        """删除数据表"""
        async for chunk in self.cmd_handler.delete_table(event, table_name):
            yield chunk

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("强制总结")
    async def force_summarize(self, event: AstrMessageEvent):
        """强制总结当前会话的未处理聊天记录"""
        group_or_user_id = event.get_group_id() or event.get_sender_id()
        bot_name = self.adapter_id_map.get(event.platform_meta.id)
        if not bot_name:
            yield await event.send(MessageChain([Plain("未找到对应的 Bot 实例。")]))
            return
        async for chunk in self.cmd_handler.force_summarize(
            event, bot_name, group_or_user_id
        ):
            yield chunk

    @filter.command("搜番")
    async def search_anime(self, event: AstrMessageEvent):
        """以图搜番"""
        async for chunk in self.cmd_handler.search_anime_cmd(event):
            yield chunk

    @filter.command("搜插画", alias={"搜图", "搜画", "搜来源"})
    async def search_illust(self, event: AstrMessageEvent):
        """以图搜插画/来源 (SauceNAO)"""
        async for chunk in self.cmd_handler.search_illust_cmd(event):
            yield chunk

    # ==================== 消息事件接收 ====================

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-1000)
    async def on_message(self, event: AstrMessageEvent):
        """接收消息事件"""
        # 忽略机器人自身发送的消息（例如平台回显/echo事件）
        try:
            if event.get_sender_id() == event.get_self_id():
                return
        except Exception:
            pass

        # 搜番发图等待拦截
        sender_id = event.get_sender_id()
        if self.cmd_handler.has_anime_demand(sender_id):
            if await self.cmd_handler.fulfill_anime_demand(event):
                return

        await self.chat_manager.handle_message(event)

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
        """定时任务提醒入口"""
        await self.chat_manager.remind_task(
            unified_msg_origin=unified_msg_origin,
            adapter_id=adapter_id,
            bot_name=bot_name,
            nickname=nickname,
            self_id=self_id,
            platform_name=platform_name,
            user_id=user_id,
            user_name=user_name,
            group_id=group_id,
            group_or_user_id=group_or_user_id,
            remind_message=remind_message,
        )

    async def on_drawing_complete(
        self,
        event: AstrMessageEvent,
        result: MessageChain,
        params: dict,
        unified_msg_origin: str,
        **kwargs,
    ):
        """后台绘图完成回调"""

        # 1. 过滤出真正的图片组件
        images = [comp for comp in result.chain if isinstance(comp, Image)]
        
        # 2. 检查结果文本
        result_text = ""
        for comp in result.chain:
            if isinstance(comp, Plain):
                result_text += comp.text or ""

        # 3. 判定是否成功
        # 优先从 kwargs 获取显式成功状态（如果未来绘图后端支持传入 success 参数）
        is_success = kwargs.get("success")
        if is_success is None:
            # 兼容模式：若无显式状态参数，通过消息链内容进行判定。
            # 大香蕉生成失败时，会向消息链放入包含 "执行失败" 的 Plain 文本，且无图片组件；
            # 成功时，若不直接发送则包含图片组件，若直接发送则包含 "图片已成功发送给用户" 的 Plain 文本。
            if len(images) > 0:
                is_success = True
            elif "执行失败" in result_text:
                is_success = False
            elif "成功" in result_text:
                is_success = True
            else:
                is_success = False
        
        error_msg = ""
        if not is_success:
            error_msg = result_text if result_text else "未知错误"

        bot_name = self.adapter_id_map.get(event.platform_meta.id)
        if not bot_name:
            logger.warning("[Giftia] 未找到对应的 Bot 实例，跳过处理。")
            return
        
        bot_conf = self.bot_map.get(bot_name, {})
        nickname = bot_conf.get("nickname", bot_name)
        group_or_user_id = event.get_group_id() or event.get_sender_id()
        reply_key = f"{bot_name}:{group_or_user_id}"

        # 使用系统内置会话锁排队，确保等当前发言结束后再发送图片和激活回复
        async with session_lock_manager.acquire_lock(event.unified_msg_origin):
            # 4. 如果成功，且包含图片（大香蕉未开启直接发送时），构造只包含图片的 MessageChain 并直接发送给用户
            if is_success:
                if len(images) > 0:
                    logger.info(f"[Giftia] 后台绘图完成，正在直接发送生成的图片...")
                    send_chain = []
                    if event.message_obj and hasattr(event.message_obj, "message_id") and event.message_obj.message_id:
                        send_chain.append(Reply(id=event.message_obj.message_id))
                    send_chain.extend(images)
                    await event.send(event.chain_result(send_chain))
                else:
                    logger.info(f"[Giftia] 后台绘图完成（大香蕉已直接发送图片），状态: {result_text}")
            else:
                logger.warning(f"[Giftia] 后台绘图失败: {error_msg}")

            # 5. 唤醒 Bot 进行后继发言/点评
            try:
                if is_success:
                    remind_msg = (
                        f"[系统通知] 绘图已完成并且已经发送给用户。"
                        f"请根据上下文（包含刚刚发送的图片及转述描述），以你的角色口吻和语气进行拟人化对话确认或后续互动。"
                        f"注意：图片已经被系统自动发出了，不要在你的回复中输出任何图片链接、图片组件或再次调用画图工具，只输出你的文字/角色回复即可。"
                    )
                else:
                    remind_msg = (
                        f"[系统通知] 绘图任务执行失败（参数: {json.dumps(params, ensure_ascii=False)}，错误原因: {error_msg}）。"
                        f"请以你的角色口吻和语气，向用户表达歉意或给出合理的解释，告知绘图失败了。"
                        f"注意：只需进行普通的对话回复，不要再次调用绘图工具。"
                    )

                # 运行回复流水线
                logger.info(f"[Giftia] 正在唤醒 Bot {bot_name} 进行回复/点评...")
                
                # 手动维护正在回复状态，防止其他正常消息插队
                self.replying_status[reply_key] = self.replying_status.get(reply_key, 0) + 1
                pending_recall_memories = []
                try:
                    async for chunk in self.chat_manager.reply_pipeline.dispatch_llm_reply_loop(
                        event=event,
                        bot_name=bot_name,
                        nickname=nickname,
                        group_or_user_id=group_or_user_id,
                        remind_message=remind_msg,
                        image_urls=[],
                        pending_recall_memories=pending_recall_memories,
                    ):
                        if chunk:
                            if isinstance(chunk, XmlLlmResult):
                                # 派发动作和消息发送
                                await self.chat_manager.action_dispatcher.dispatch_actions(
                                    event=event,
                                    bot_name=bot_name,
                                    nickname=nickname,
                                    group_or_user_id=group_or_user_id,
                                    llm_result=chunk,
                                )
                finally:
                    self.replying_status[reply_key] = max(
                        0, self.replying_status.get(reply_key, 0) - 1
                    )

                # 提交记忆
                self.chat_manager.reply_pipeline.commit_pending_session_recalled_memories(
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                    pending_recall_memories=pending_recall_memories,
                )
            except Exception as e:
                logger.error(f"[Giftia] 唤醒 Bot 回复失败: {e}", exc_info=True)

    async def terminate(self):
        """销毁方法"""
        self._terminated = True
        if hasattr(self, "_original_send_message"):
            self.context.send_message = self._original_send_message

        for task in list(self.running_tasks.values()):
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.running_tasks.values(), return_exceptions=True)
        self.running_tasks.clear()

        await self.http_manager.close_session()
        await self.db.close()
        self.task_manager.shutdown()
        self.ltm.close()
        remove_tools(self.context)
