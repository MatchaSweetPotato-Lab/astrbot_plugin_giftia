import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import (
    At,
    File,
    Image,
    Node,
    Nodes,
    Plain,
    Record,
    Reply,
)

from ..reports.status import build_status_report
from ..reports.user_profile import build_user_profile_report
from ..utils.schemas import Status


class CommandHandler:
    def __init__(self, plugin):
        self.plugin = plugin

    async def tool_list(self, event: AstrMessageEvent, index: int = 1):
        """工具列表"""
        tool_set = (
            self.plugin.context.get_llm_tool_manager()
            .get_full_tool_set()
            .get_light_tool_set()
        )
        # 分页
        total_pages = (len(tool_set) + 10 - 1) // 10
        # 获取当前页工具
        start = (index - 1) * 10
        current_page_tools = tool_set.tools[start : start + 10]
        if not current_page_tools:
            yield await event.send(
                MessageChain([Plain(f"第 {index} 页没有更多工具了。")])
            )
            return
        nodes = []
        for tool in current_page_tools:
            nodes.append(
                Node(
                    uin=event.get_sender_id(),
                    name=event.get_sender_name(),
                    content=[
                        Plain(f"工具名称: {tool.name}\n工具描述: {tool.description}")
                    ],
                )
            )
        nodes.append(
            Node(
                uin=event.get_sender_id(),
                name=event.get_sender_name(),
                content=[
                    Plain(
                        f"第 {index} 页，{len(current_page_tools)} 个工具；共 {total_pages} 页，{len(tool_set)} 个工具"
                    )
                ],
            )
        )
        if index < total_pages:
            nodes.append(
                Node(
                    uin=event.get_sender_id(),
                    name=event.get_sender_name(),
                    content=[Plain(f"/工具列表 {index + 1} 查看下一页")],
                )
            )
        yield await event.send(MessageChain([Nodes(nodes)]))

    async def tool_xml(self, event: AstrMessageEvent, name: str):
        """将函数调用工具解析成xml格式"""
        tool = (
            self.plugin.context.get_llm_tool_manager()
            .get_full_tool_set()
            .get_tool(name)
        )
        if not tool:
            yield await event.send(MessageChain([Plain(f"未找到工具: {name}")]))
            return
        # 解析成xml
        xml = f'<tool_call name="{tool.name}" description="{tool.description}">{json.dumps(tool.parameters, ensure_ascii=False)}</tool_call>'
        node = Node(
            uin=event.get_sender_id(),
            name=event.get_sender_name(),
            content=[Plain(xml)],
        )
        yield await event.send(MessageChain([Nodes([node])]))

    async def get_embedding_models(self, event: AstrMessageEvent):
        """打印所有支持的模型信息"""
        if not self.plugin.embedding_conf.get("enabled", False):
            logger.error("未启用embedding功能")
            return
        models = self.plugin.ltm.get_all_models()
        logger.info(models)

    async def get_rerank_models(self, event: AstrMessageEvent):
        """打印所有支持的模型信息"""
        if not self.plugin.rerank_conf.get("enabled", False):
            logger.error("未启用rerank功能")
            return
        models = self.plugin.ltm.get_all_rerank_models()
        logger.info(models)

    async def get_memory(
        self,
        event: AstrMessageEvent,
        bot_name: str,
        group_or_user_id: str,
        rag_queries: str,
    ):
        """根据ID获取记忆"""
        if not self.plugin.embedding_conf.get("enabled", False):
            logger.error("未启用embedding功能")
            yield await event.send(MessageChain([Plain("未启用embedding功能")]))
            return
        embedding_memories = await self.plugin.ltm.search_memory(
            bot_name,
            group_or_user_id,
            rag_queries,
            limit=self.plugin.embedding_conf.get(
                "limit", self.plugin.embedding_conf.get("top_k", 5)
            ),
            threshold=self.plugin.embedding_conf.get("threshold", 0.7),
        )
        if self.plugin.rerank_conf.get("enabled", False):
            rerank_memories = await self.plugin.ltm.rerank_memories(
                rag_queries,
                embedding_memories,
                top_k=self.plugin.rerank_conf.get("top_k", 5),
                threshold=self.plugin.rerank_conf.get("threshold", 0.45),
            )
        else:
            rerank_memories = embedding_memories
        nodes = []
        for mem in rerank_memories:
            data = {
                "id": mem["id"],
                "bot_name": mem["bot_name"],
                "text": mem["text"],
                "created_at": mem["created_at"],
                "_distance": mem["_distance"],
                "_rerank_score": mem.get("score"),
            }
            nodes.append(
                Node(
                    uin=event.get_self_id(),
                    name="Firefly",
                    content=[Plain(json.dumps(data, indent=4, ensure_ascii=False))],
                )
            )
        if not nodes:
            yield await event.send(MessageChain([Plain("未找到相关记忆")]))
            return
        yield await event.send(MessageChain([Nodes(nodes)]))

    async def get_early_memory(
        self,
        event: AstrMessageEvent,
        bot_name: str,
        group_or_user_id: str,
        limit: int = 10,
    ):
        """根据ID获取记忆"""
        if not self.plugin.embedding_conf.get("enabled", False):
            logger.error("未启用embedding功能")
            yield await event.send(MessageChain([Plain("未启用embedding功能")]))
            return

        long_memories = await self.plugin.data_cache.get_memories(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            limit=limit,
        )
        nodes = []
        for mem in long_memories:
            data = {
                "memory_id": mem.memory_id,
                "text": mem.text,
                "importance": mem.importance,
                "hit_count": mem.hit_count,
                "last_hit_at": mem.last_hit_at,
                "created_at": mem.created_at,
            }
            nodes.append(
                Node(
                    uin=event.get_self_id(),
                    name="Firefly",
                    content=[Plain(json.dumps(data, indent=4, ensure_ascii=False))],
                )
            )
        if not nodes:
            yield await event.send(MessageChain([Plain("未找到相关记忆")]))
            return
        yield await event.send(MessageChain([Nodes(nodes)]))

    async def delete_message(self, event: AstrMessageEvent):
        """根据ID删除消息"""
        message_id = None
        for comp in event.get_messages():
            if isinstance(comp, Reply):
                message_id = comp.id
                break
        if not message_id:
            yield await event.send(MessageChain([Plain("未找到引用消息的消息ID")]))
            return
        bot_name = self.plugin.adapter_id_map.get(event.platform_meta.id)
        if not bot_name:
            return
        group_or_user_id = event.get_group_id() or event.get_sender_id()
        success = await self.plugin.data_cache.delete_message(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            message_id=str(message_id),
        )
        if success:
            yield await event.send(MessageChain([Plain("删除消息成功")]))
        else:
            yield await event.send(
                MessageChain(
                    [
                        Plain(
                            "删除消息失败：指令响应消息请前往 WebUI 决策审计页面手动删除。"
                        )
                    ]
                )
            )

    async def delete_memory(self, event: AstrMessageEvent, memory_id: str):
        """根据ID删除记忆"""
        if not self.plugin.embedding_conf.get("enabled", False):
            logger.error("未启用embedding功能")
            yield await event.send(MessageChain([Plain("未启用embedding功能")]))
            return
        await self.plugin.data_cache.delete_memory(memory_id)
        yield await event.send(MessageChain([Plain("删除记忆成功")]))

    async def delete_all_memories(
        self, event: AstrMessageEvent, bot_name: str, group_or_user_id: str
    ):
        """删除全部记忆"""
        if not self.plugin.embedding_conf.get("enabled", False):
            logger.error("未启用embedding功能")
            yield await event.send(MessageChain([Plain("未启用embedding功能")]))
            return
        try:
            await self.plugin.data_cache.delete_all_memories(
                bot_name=bot_name, group_or_user_id=group_or_user_id
            )
        except Exception:
            logger.error("删除全部记忆失败")
        yield await event.send(MessageChain([Plain("删除全部记忆成功")]))

    async def fill_energy(self, event: AstrMessageEvent, bot_name: str):
        """给当前群的指定机器人加满能量"""
        group_or_user_id = event.get_group_id() or event.get_sender_id()
        if not bot_name:
            yield await event.send(MessageChain([Plain("请输入机器人名称")]))
            return

        status = Status(energy="100.0")
        await self.plugin.data_cache.set_bot_status(
            bot_name=bot_name, group_id=group_or_user_id, status=status
        )
        yield await event.send(MessageChain([Plain(f"已为机器人 {bot_name} 加满能量")]))

    async def delete_all_media_cache(self, event: AstrMessageEvent):
        """清空全部媒体缓存"""
        try:
            await self.plugin.data_cache.clear_caption()
            yield await event.send(MessageChain([Plain("清空媒体缓存成功")]))
        except Exception as e:
            logger.error(f"清空媒体缓存失败，报错：{e}")
            yield await event.send(MessageChain([Plain("清空媒体缓存失败")]))

    async def task_list(self, event: AstrMessageEvent, index: int = 1):
        """获取全部定时任务"""
        tasks = self.plugin.task_manager.get_all_jobs()
        # 分页
        total_pages = (len(tasks) + 10 - 1) // 10
        # 获取当前页任务
        start = (index - 1) * 10
        current_page_tasks = tasks[start : start + 10]
        if not current_page_tasks:
            yield await event.send(
                MessageChain([Plain(f"第 {index} 页没有更多任务了。")])
            )
            return
        nodes = []
        for task in current_page_tasks:
            nodes.append(
                Node(
                    uin=event.get_sender_id(),
                    name=event.get_sender_name(),
                    content=[Plain(task)],
                )
            )
        nodes.extend(
            [
                Node(
                    uin=event.get_sender_id(),
                    name=event.get_sender_name(),
                    content=[Plain(f"共 {len(tasks)} 个任务，当前为第 {index} 页")],
                ),
                Node(
                    uin=event.get_sender_id(),
                    name=event.get_sender_name(),
                    content=[Plain("/删除定时任务 <task_id> 删除定时任务")],
                ),
            ]
        )
        if index < total_pages:
            nodes.append(
                Node(
                    uin=event.get_sender_id(),
                    name=event.get_sender_name(),
                    content=[Plain(f"/定时任务列表 {index + 1} 查看下一页")],
                )
            )
        yield await event.send(MessageChain([Nodes(nodes)]))

    async def get_task_by_group(self, event: AstrMessageEvent, prefix: str):
        """根据botname+group_or_user_id获取定时任务"""
        tasks = self.plugin.task_manager.get_prefix_jobs(prefix)
        if not tasks:
            yield await event.send(MessageChain([Plain("没有找到相关定时任务")]))
            return
        nodes = []
        for task in tasks:
            nodes.append(
                Node(
                    uin=event.get_sender_id(),
                    name=event.get_sender_name(),
                    content=[Plain(task)],
                )
            )
        yield await event.send(MessageChain([Nodes(nodes)]))

    async def delete_task(self, event: AstrMessageEvent, task_id: str):
        """删除定时任务"""
        result = self.plugin.task_manager.remove_job(task_id)
        yield await event.send(MessageChain([Plain(result)]))

    async def get_media_caption(self, event: AstrMessageEvent):
        """读取媒体转述"""
        bot_name = self.plugin.adapter_id_map.get(event.platform_meta.id, "")
        group_or_user_id = event.get_group_id() or event.get_sender_id()

        file_name = ""
        media_hash = ""

        for comp in event.get_messages():
            if isinstance(comp, Reply):
                if bot_name:
                    msg_data = await self.plugin.data_cache.get_message_by_id(
                        bot_name, group_or_user_id, str(comp.id)
                    )
                    if msg_data and msg_data.media_id_list:
                        media_hash = msg_data.media_id_list[0]

                if comp.chain:
                    for quote in comp.chain:
                        if isinstance(quote, Image) and quote.file:
                            file_name = quote.file
                            break
                        elif isinstance(quote, Record) and quote.file:
                            file_name = quote.file
                            break
                        elif isinstance(quote, File) and quote.file:
                            file_name = quote.file
                            break
            elif isinstance(comp, Image) and comp.file:
                file_name = comp.file
                break
            elif isinstance(comp, Record) and comp.file:
                file_name = comp.file
                break
            elif isinstance(comp, File) and comp.file:
                file_name = comp.file
                break

        media_caption = None
        if media_hash:
            media_caption = await self.plugin.data_cache.get_caption_by_hash(media_hash)

        if not media_caption and file_name:
            _, media_caption = await self.plugin.data_cache.get_caption_by_filename(
                file_name
            )

        if media_caption:
            msg = f"""hash_val: {media_caption.hash_val}
media_type: {media_caption.media_type}
file_name: {media_caption.file_name}
genre: {media_caption.genre}
character: {media_caption.character}
source: {media_caption.source}
text: {media_caption.text}
caption: {media_caption.caption}"""
            yield await event.send(MessageChain([Plain(msg)]))
        else:
            if not media_hash and not file_name:
                yield await event.send(
                    MessageChain([Plain("没有获取到文件或引用消息")])
                )
            else:
                yield await event.send(MessageChain([Plain("未找到媒体转述缓存")]))

    async def set_persistent_status(
        self, event: AstrMessageEvent, status_name: str, status_value: str = ""
    ):
        """设置或删除当前会话Bot的常驻状态"""
        bot_name = self.plugin.adapter_id_map.get(event.platform_meta.id)
        if not bot_name:
            yield await event.send(MessageChain([Plain("未找到对应的 Bot 实例。")]))
            return

        status_name = str(status_name or "").strip()
        if not status_name:
            yield await event.send(
                MessageChain(
                    [Plain("状态名不能为空。用法：/设置常驻状态 <状态名> [状态值]")]
                )
            )
            return

        status_value = str(status_value or "").strip()
        group_or_user_id = event.get_group_id() or event.get_sender_id()

        # 若状态值为空，或为 "删除" / "清空" / "无"，则删除该常驻状态项
        if not status_value or status_value in ("删除", "清空", "无"):
            await self.plugin.data_cache.update_bot_custom_status(
                bot_name=bot_name,
                group_id=group_or_user_id,
                custom_status_updates={status_name: ""},
            )
            yield await event.send(
                MessageChain(
                    [Plain(f"已清除 Bot【{bot_name}】的常驻状态「{status_name}」")]
                )
            )
        else:
            await self.plugin.data_cache.update_bot_custom_status(
                bot_name=bot_name,
                group_id=group_or_user_id,
                custom_status_updates={status_name: status_value},
            )
            yield await event.send(
                MessageChain(
                    [
                        Plain(
                            f"已设置 Bot【{bot_name}】的常驻状态「{status_name}」为：{status_value}"
                        )
                    ]
                )
            )

    async def slang(self, event: AstrMessageEvent, text: str = ""):
        """Look up a term or let an administrator replace its definition.

        Args:
            event: Command event identifying the bot, session, and permissions.
            text: Term followed by an optional complete definition.

        Yields:
            The result of sending the definition, confirmation, or error.
        """
        command_parts = event.get_message_str().lstrip().split(maxsplit=1)
        if command_parts and command_parts[0].endswith("黑话"):
            text = command_parts[1] if len(command_parts) > 1 else ""
        parts = text.strip().split(maxsplit=1)
        bot_name = self.plugin.adapter_id_map.get(event.platform_meta.id)
        if not bot_name:
            message = "未找到对应的 Bot 实例。"
        elif not parts:
            message = (
                "用法：/黑话 <词> [描述]\n仅输入词时查询；管理员输入描述时新增或覆盖。"
            )
        else:
            session_id = event.get_group_id() or event.get_sender_id()
            term = parts[0]
            repo = self.plugin.db.slang_repo
            if len(parts) == 1:
                entries = await repo.get_entries(bot_name, session_id)
                entry = next((item for item in entries if item["term"] == term), None)
                message = (
                    f"{term}：{entry['description']}"
                    if entry
                    else f"当前会话尚未收录黑话「{term}」。"
                )
            elif not event.is_admin():
                message = "仅 AstrBot 管理员可以通过指令新增或覆盖黑话。"
            else:
                try:
                    await repo.set_entry(bot_name, session_id, term, parts[1])
                    message = f"已保存当前会话的黑话「{term}」：\n{parts[1]}"
                except ValueError as e:
                    message = str(e)
        yield await event.send(MessageChain([Plain(message)]))

    async def set_group_rules(self, event: AstrMessageEvent, rules: str = ""):
        """Replace or display the rules for the current bot and session.

        Args:
            event: Command event identifying the bot and session.
            rules: Complete replacement text; blank input shows usage and current rules.

        Yields:
            The result of sending a confirmation, current rules, or usage message.
        """
        # CommandFilter collapses whitespace; recover the original multiline text.
        command_parts = event.get_message_str().lstrip().split(maxsplit=1)
        if command_parts and command_parts[0].endswith("群规"):
            rules = command_parts[1] if len(command_parts) > 1 else ""
        rules = rules.strip()
        bot_name = self.plugin.adapter_id_map.get(event.platform_meta.id)
        if not bot_name:
            yield await event.send(MessageChain([Plain("未找到对应的 Bot 实例。")]))
            return
        session_id = event.get_group_id() or event.get_sender_id()
        if not rules:
            message = "用法：/群规 具体规则（覆写当前会话的全部群规）"
            current_rules = await self.plugin.data_cache.get_group_profile(
                bot_name=bot_name, group_or_user_id=session_id
            )
            if current_rules and current_rules.strip():
                message += f"\n\n当前会话的群规：\n{current_rules}"
            yield await event.send(MessageChain([Plain(message)]))
            return
        await self.plugin.data_cache.set_group_profile(
            bot_name=bot_name,
            group_or_user_id=session_id,
            profile=rules,
        )
        yield await event.send(
            MessageChain([Plain(f"已覆写当前会话的群规：\n{rules}")])
        )

    async def get_bot_status(self, event: AstrMessageEvent):
        """获取当前会话的临时+常驻状态"""
        bot_name = self.plugin.adapter_id_map.get(event.platform_meta.id)
        if not bot_name:
            yield await event.send(MessageChain([Plain("未找到对应的 Bot 实例。")]))
            return

        bot_conf = self.plugin.bot_map.get(bot_name, {})
        nickname = bot_conf.get("nickname", bot_name)
        group_or_user_id = event.get_group_id() or event.get_sender_id()

        status = await self.plugin.data_cache.get_bot_status(bot_name, group_or_user_id)

        report = build_status_report(bot_name, nickname, group_or_user_id, status)
        mode = self.plugin.conf.get("report_config", {}).get("render_mode", "文本响应")
        if mode == "图片响应":
            try:
                path = await self.plugin.reports.render_image("status", report)
            except Exception:
                logger.exception(
                    "[Giftia Reports] Status image failed; falling back to text"
                )
            else:
                try:
                    yield await event.send(MessageChain([Image.fromFileSystem(path)]))
                finally:
                    Path(path).unlink(missing_ok=True)
                return

        custom_lines = "\n".join(
            f"• {k}：{v}" for k, v in report["custom_status"].items()
        )
        custom_lines = custom_lines or "• 暂无常驻状态"

        msg = (
            f"【Bot 状态看板】\n"
            f"🤖 机器人：{nickname} ({bot_name})\n\n"
            f"📊 临时状态：\n"
            f"• 心情：{report['mood']}\n"
            f"• 状态：{report['state']}\n"
            f"• 动作：{report['action']}\n"
            f"• 思考：{report['memory']}\n"
            f"• 能量：{report['energy']}\n\n"
            f"📌 常驻状态：\n"
            f"{custom_lines}"
        )
        yield await event.send(MessageChain([Plain(msg)]))

    async def get_user_profile(self, event: AstrMessageEvent, target: str = ""):
        """Send a stored user profile using the shared report rendering mode.

        Args:
            event: Command event, including structured mention components.
            target: Remaining command text; empty input defaults to the sender.

        Yields:
            The result of sending the profile or a usage/not-found message.
        """
        try:
            user_id = self._profile_target(event, target)
        except ValueError as exc:
            yield await event.send(MessageChain([Plain(str(exc))]))
            return

        bot_name = self.plugin.adapter_id_map.get(event.platform_meta.id)
        if not bot_name:
            yield await event.send(MessageChain([Plain("未找到对应的 Bot 实例。")]))
            return
        nickname = self.plugin.bot_map.get(bot_name, {}).get("nickname", bot_name)
        session_id = event.get_group_id() or event.get_sender_id()
        record = await self.plugin.data_cache.get_user_profile_record(
            bot_name, session_id, user_id
        )
        if record is None:
            yield await event.send(
                MessageChain([Plain(f"当前会话中暂无用户 {user_id} 的画像记录。")])
            )
            return

        report = build_user_profile_report(
            bot_name, nickname, session_id, user_id, record
        )
        mode = self.plugin.conf.get("report_config", {}).get("render_mode", "文本响应")
        if mode == "图片响应":
            try:
                path = await self.plugin.reports.render_image("user_profile", report)
            except Exception:
                logger.exception(
                    "[Giftia Reports] User profile image failed; falling back to text"
                )
            else:
                try:
                    yield await event.send(MessageChain([Image.fromFileSystem(path)]))
                finally:
                    Path(path).unlink(missing_ok=True)
                return

        profile_lines = "\n".join(
            f"• {label}：{value}" for label, value in report["profile_fields"].items()
        )
        msg = (
            f"【{report['title']}】\n"
            f"🤖 机器人：{report['nickname']} ({report['bot_name']})\n"
            f"👤 用户 ID：{report['user_id']}\n"
            f"🤝 好感度：{report['relation']}\n"
            f"🏷️ 关系称谓：{report['relation_title']}\n\n"
            f"{profile_lines}"
        )
        yield await event.send(MessageChain([Plain(msg)]))

    @staticmethod
    def _profile_target(
        event: AstrMessageEvent, target: str, default_to_sender: bool = True
    ) -> str:
        """Resolve a single mention, explicit user ID, or optionally the sender.

        Args:
            event: Event containing structured mentions; bot wake mentions are ignored.
            target: Raw arguments; empty text uses the sender when enabled.
            default_to_sender: Whether an empty target should use the sender ID.

        Returns:
            The platform user ID without numeric conversion.

        Raises:
            ValueError: If the resolved ID is empty, invalid, ambiguous, or everyone.
        """
        mentions = set()
        command_started = False
        for component in event.get_messages():
            if isinstance(component, Plain) and component.text.strip():
                command_started = True
            elif isinstance(component, At):
                user_id = str(component.qq)
                if user_id == str(event.get_self_id()) and not command_started:
                    continue
                mentions.add(user_id)
        if "all" in mentions or len(mentions) > 1:
            raise ValueError("请只 @ 一位用户，或使用 /画像 用户ID 查询。")
        if mentions:
            return mentions.pop()
        user_id = target.strip()
        if not user_id:
            if default_to_sender:
                user_id = str(event.get_sender_id() or "").strip()
            else:
                return ""
        if len(user_id.split()) != 1 or user_id.startswith("@") or user_id == "all":
            raise ValueError("用法：/画像 @一位用户 或 /画像 用户ID")
        return user_id

    async def set_session_access(
        self,
        event: AstrMessageEvent,
        session_id: str,
        enabled: bool,
        *,
        is_private: bool | None = None,
    ):
        """Persist session access in the list configured by the dashboard.

        Args:
            event: Administrator's command event, including the target bot adapter.
            session_id: Group or private-chat user ID; empty selects this session.
            enabled: Whether the session should be allowed to process messages.
            is_private: Force a chat type, or infer it from a mention or this event.
        """
        event._giftia_bypass_logging = True
        bot_name = self.plugin.adapter_id_map.get(event.platform_meta.id)
        if not bot_name:
            yield await event.send(MessageChain([Plain("未找到对应的 Bot 实例。")]))
            return
        try:
            mentioned_session_id = self._profile_target(event, "", False)
        except ValueError:
            yield await event.send(
                MessageChain([Plain("请只 @ 一位用户，或填写单个会话 ID。")])
            )
            return
        group_id = event.get_group_id()
        if is_private is None:
            is_private = bool(mentioned_session_id) or not bool(group_id)
        session_id = str(mentioned_session_id or session_id or "").strip()
        if not session_id:
            if is_private and group_id:
                command = "开启私聊" if enabled else "关闭私聊"
                yield await event.send(
                    MessageChain(
                        [Plain(f"用法：/{command} 用户ID 或 /{command} @用户")]
                    )
                )
                return
            session_id = str(group_id or event.get_sender_id() or "").strip()
        if (
            not session_id
            or len(session_id.split()) != 1
            or ":" in session_id
            or session_id.startswith("@")
        ):
            yield await event.send(
                MessageChain(
                    [
                        Plain(
                            "请填写单个会话 ID（群号或私聊用户 ID），留空表示当前会话。"
                        )
                    ]
                )
            )
            return

        manager = self.plugin.bot_config_manager
        bots = deepcopy(manager.load_bots())
        bot = next((b for b in bots if b["name"] == bot_name), None)
        if bot is None:
            yield await event.send(MessageChain([Plain("未找到对应的 Bot 配置。")]))
            return
        decision_conf = bot["decision_conf"]
        kind = "private" if is_private else "group"
        session_type = "私聊" if is_private else "群聊"
        list_key = f"{kind}_whitelist"
        session_list = decision_conf[list_key]
        mode = decision_conf[f"{kind}_whitelist_mode"]
        if isinstance(mode, bool):
            mode = "whitelist" if mode else "blacklist"
        else:
            mode = str(mode or "blacklist").strip().lower()
        is_whitelist_mode = mode in ("whitelist", "白名单", "白名单模式")
        should_be_in_list = enabled == is_whitelist_mode
        if should_be_in_list:
            if session_id not in session_list:
                session_list.append(session_id)
        else:
            decision_conf[list_key] = [
                item for item in session_list if item != session_id
            ]
            session_list = decision_conf[list_key]
        list_name = "白名单" if is_whitelist_mode else "黑名单"
        if not manager.save_bots(bots):
            yield await event.send(
                MessageChain(
                    [Plain(f"保存配置失败，{session_type}{list_name}未更新。")]
                )
            )
            return
        self.plugin.sync_bot_maps()
        session_allowed = (
            session_id in session_list
            if is_whitelist_mode
            else session_id not in session_list
        )
        if not session_allowed:
            self.plugin.active_reply_counters.pop(f"{bot_name}:{session_id}", None)
        action = "加入" if should_be_in_list else "移出"
        yield await event.send(
            MessageChain(
                [
                    Plain(
                        f"已将{session_type} {session_id} {action}【{bot['nickname']}】的{session_type}{list_name}。"
                    )
                ]
            )
        )

    async def block_user(self, event: AstrMessageEvent, user_id: str):
        """Exclude a user's future messages from this bot and conversation.

        Args:
            event: Administrator's command event, identifying the bot and session.
            user_id: ID of the user whose messages must not be stored or processed.
        """
        event._giftia_bypass_logging = True
        try:
            user_id = self._profile_target(event, str(user_id or ""), False)
        except ValueError:
            yield await event.send(MessageChain([Plain("用法：/屏蔽 用户ID")]))
            return
        if not user_id:
            yield await event.send(MessageChain([Plain("用法：/屏蔽 用户ID")]))
            return
        bot_name = self.plugin.adapter_id_map.get(event.platform_meta.id)
        if not bot_name:
            yield await event.send(MessageChain([Plain("未找到对应的 Bot 实例。")]))
            return
        manager = self.plugin.bot_config_manager
        bots = deepcopy(manager.load_bots())
        bot = next((b for b in bots if b["name"] == bot_name), None)
        if bot is None:
            yield await event.send(MessageChain([Plain("未找到对应的 Bot 配置。")]))
            return
        users = bot["blocked_users"].setdefault(event.unified_msg_origin, [])
        if user_id not in users:
            users.append(user_id)
        if not manager.save_bots(bots):
            yield await event.send(MessageChain([Plain("保存配置失败，屏蔽未生效。")]))
            return
        self.plugin.sync_bot_maps()
        yield await event.send(
            MessageChain(
                [Plain(f"已在当前会话屏蔽用户 {user_id}，后续发言不入库、不触发回复。")]
            )
        )

    async def unblock_user(self, event: AstrMessageEvent, user_id: str):
        """Remove a user from the current bot and conversation block list.

        Args:
            event: Administrator's command event, identifying the bot and conversation.
            user_id: User ID or structured mention to unblock.
        """
        event._giftia_bypass_logging = True
        try:
            user_id = self._profile_target(event, str(user_id or ""), False)
        except ValueError:
            yield await event.send(
                MessageChain([Plain("用法：/取消屏蔽 @用户 或 用户ID")])
            )
            return
        if not user_id:
            yield await event.send(
                MessageChain([Plain("用法：/取消屏蔽 @用户 或 用户ID")])
            )
            return
        bot_name = self.plugin.adapter_id_map.get(event.platform_meta.id)
        if not bot_name:
            yield await event.send(MessageChain([Plain("未找到对应的 Bot 实例。")]))
            return
        manager = self.plugin.bot_config_manager
        bots = deepcopy(manager.load_bots())
        bot = next((b for b in bots if b["name"] == bot_name), None)
        if bot is None:
            yield await event.send(MessageChain([Plain("未找到对应的 Bot 配置。")]))
            return
        blocked_users = bot["blocked_users"].get(event.unified_msg_origin, [])
        if user_id in blocked_users:
            blocked_users.remove(user_id)
        if blocked_users:
            bot["blocked_users"][event.unified_msg_origin] = blocked_users
        else:
            bot["blocked_users"].pop(event.unified_msg_origin, None)
        if not manager.save_bots(bots):
            yield await event.send(
                MessageChain([Plain("保存配置失败，取消屏蔽未生效。")])
            )
            return
        self.plugin.sync_bot_maps()
        yield await event.send(
            MessageChain([Plain(f"已在当前会话取消屏蔽用户 {user_id}。")])
        )

    async def silence_session(self, event: AstrMessageEvent):
        """将当前会话的状态设置为不活跃"""
        bot_name = self.plugin.adapter_id_map.get(event.platform_meta.id)
        if not bot_name:
            yield await event.send(MessageChain([Plain("未找到对应的 Bot 实例。")]))
            return

        bot_conf = self.plugin.bot_map.get(bot_name, {})
        nickname = bot_conf.get("nickname", bot_name)
        group_or_user_id = event.get_group_id() or event.get_sender_id()

        # 1. 重置当前群/会话的接话活跃分析窗口为 0
        fmt_key = f"{bot_name}:{group_or_user_id}"
        self.plugin.active_reply_counters[fmt_key] = 0

        # 2. 将当前会话 Bot 的临时状态更新为「不活跃」并持久化
        await self.plugin.data_cache.set_bot_status(
            bot_name=bot_name,
            group_id=group_or_user_id,
            status=Status(state="不活跃"),
        )

        yield await event.send(
            MessageChain(
                [
                    Plain(
                        f"已将【{nickname}】在当前会话的状态设置为「不活跃」，接话分析窗口已重置。"
                    )
                ]
            )
        )

    async def force_summarize(
        self, event: AstrMessageEvent, bot_name: str, group_or_user_id: str
    ):
        """手动强制总结当前会话的未处理消息记录"""
        yield await event.send(
            MessageChain([Plain("开始分析并提炼当前会话记忆，请稍候...（同步执行中）")])
        )

        result = await self.plugin.passive_memory_manager.force_trigger_passive_memory(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            self_id=event.get_self_id(),
        )

        yield await event.send(MessageChain([Plain(result)]))

    async def set_stronghold(self, event: AstrMessageEvent):
        """将当前会话设置为通知据点（唯一，覆盖旧据点）"""
        unified_origin = event.unified_msg_origin
        group_id = event.get_group_id()
        sender_id = event.get_sender_id()
        is_group = bool(group_id)
        session_id = group_id if is_group else sender_id
        platform_name = event.get_platform_name()
        adapter_id = getattr(getattr(event, "platform_meta", None), "id", "")
        self_id = event.get_self_id()

        # 获取显示名称
        display_name = f"群聊({group_id})" if is_group else f"私聊({sender_id})"
        if is_group:
            try:
                group_obj = await event.get_group(group_id)
                if group_obj and getattr(group_obj, "group_name", None):
                    display_name = f"群聊【{group_obj.group_name}】({group_id})"
            except Exception:
                pass
        else:
            sender_name = event.get_sender_name()
            if sender_name:
                display_name = f"私聊【{sender_name}】({sender_id})"

        stronghold_data = {
            "unified_msg_origin": unified_origin,
            "platform_name": platform_name,
            "adapter_id": adapter_id,
            "group_id": group_id or "",
            "user_id": sender_id if not is_group else "",
            "is_group": is_group,
            "session_id": session_id,
            "self_id": self_id,
            "display_name": display_name,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        await self.plugin.data_cache.set_stronghold(stronghold_data)
        yield await event.send(
            MessageChain(
                [
                    Plain(
                        f"已成功将当前会话 {display_name} 设置为通知据点！\n当机器人被禁言时，将自动在此处告状并同步现场线索。"
                    )
                ]
            )
        )

    async def leave_group(self, event: AstrMessageEvent, group_id: str):
        """退出指定群聊：/退群 <群号>"""
        group_id_str = str(group_id or "").strip()
        if not group_id_str:
            yield await event.send(
                MessageChain([Plain("请输入要退出的群号，例如：/退群 123456789")])
            )
            return

        is_qq_official = hasattr(
            self.plugin, "qq_official"
        ) and self.plugin.qq_official.is_qq_official(event)
        if is_qq_official:
            err_msg = await self.plugin.qq_official.group_leave(event, group_id_str)
        else:
            try:
                g_id_int = int(group_id_str)
                err_msg = await self.plugin.aiocqhttp.group_leave(event, g_id_int)
            except ValueError:
                yield await event.send(
                    MessageChain(
                        [Plain(f"群号格式错误: {group_id_str}，群号必须为纯数字")]
                    )
                )
                return

        if err_msg:
            yield await event.send(
                MessageChain([Plain(f"退出群聊 {group_id_str} 失败: {err_msg}")])
            )
        else:
            yield await event.send(
                MessageChain([Plain(f"已成功退出群聊 {group_id_str}！")])
            )
