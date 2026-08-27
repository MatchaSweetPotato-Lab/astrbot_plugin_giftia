import asyncio
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from astrbot.api import FunctionTool, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, StarTools
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext

from ..conversation.media_captioner import MediaCaptioner
from ..utils.event_utils import resolve_bot_name
from ..utils.schemas import (
    FORWARD_MEDIA_PATTERN,
    FORWARD_NESTED_PATTERN,
    MediaCaption,
    MessageData,
)
from .prompt import USER_PROFILE_FIELDS, normalize_profile_value

if TYPE_CHECKING:
    from ...main import Giftia

TOOLS_NAMESPACE = [
    "search_chat_history",
    "get_message_context",
    "search_user_profile",
    "inspect_forward_message",
    "inspect_media",
    "set_user_avatar_description",
]


@dataclass
class SearchChatHistoryTool(FunctionTool):
    plugin: Any = None
    name: str = "search_chat_history"
    description: str = "搜索历史记录，所有参数均可选填"
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "关键字，如果你要查找两人说过的第一句话，这里可填用户ID",
                },
                "user_id": {
                    "type": "string",
                    "description": "筛选特定用户ID（QQ号）发送的消息。",
                },
                "start_time": {
                    "type": "string",
                    "description": "iso8601格式的开始时间",
                },
                "end_time": {
                    "type": "string",
                    "description": "iso8601格式的结束时间",
                },
                "sort_order": {
                    "type": "string",
                    "description": "asc|desc",
                },
                "limit": {
                    "type": "integer",
                    "description": "最大50，默认30",
                },
            },
            "required": [],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        keyword: str = "",
        user_id: str = "",
        start_time: str = "",
        end_time: str = "",
        sort_order: str = "desc",
        limit: int = 30,
    ):
        if self.plugin is None:
            logger.warning("SearchChatHistoryTool 未绑定插件实例")
            return "检索失败：未找到插件实例"
        plugin: Giftia = self.plugin
        event: AstrMessageEvent = context.context.event
        bot_name = resolve_bot_name(plugin, event)
        if not bot_name:
            logger.warning("SearchChatHistoryTool 未找到对应的 bot_name")
            return "检索失败：未找到对应的 bot_name"

        group_or_user_id = event.get_group_id() or event.get_sender_id()
        limit = min(limit, 50)
        msgs = await plugin.db.search_messages(
            group_or_user_id=group_or_user_id,
            bot_name=bot_name,
            user_id=user_id or None,
            keyword=keyword or None,
            start_time=start_time or None,
            end_time=end_time or None,
            sort_order=sort_order or "desc",
            limit=limit,
        )
        if not msgs:
            logger.info("SearchChatHistoryTool 未找到相关历史记录")
            return "未找到相关历史记录"

        lines = [f"[{m.time}] {m.nickname}({m.user_id}): {m.content}" for m in msgs]
        return "查询到的历史记录:\n" + "\n".join(lines)


@dataclass
class GetMessageContextTool(FunctionTool):
    plugin: Any = None
    name: str = "get_message_context"
    description: str = "获取特定消息前后的上下文记录。如果你在搜索记录时得到了一条消息，可以用它的message_id来这里获取上下文。"
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "目标消息的ID。",
                },
                "limit": {
                    "type": "integer",
                    "description": "目标消息前后获取的数量，默认30。最大50。",
                },
            },
            "required": ["message_id"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        message_id: str,
        limit: int = 30,
    ):
        if self.plugin is None:
            logger.warning("GetMessageContextTool 未绑定插件实例")
            return "检索失败：未找到插件实例"
        plugin: Giftia = self.plugin
        event: AstrMessageEvent = context.context.event
        bot_name = resolve_bot_name(plugin, event)
        if not bot_name:
            logger.warning("GetMessageContextTool 未找到对应的 bot_name")
            return "检索失败：未找到对应的 bot_name"

        group_or_user_id = event.get_group_id() or event.get_sender_id()
        limit = min(limit, 50)
        msgs = await plugin.db.get_message_context(
            message_id=message_id,
            group_or_user_id=group_or_user_id,
            bot_name=bot_name,
            limit=limit,
        )
        if not msgs:
            logger.warning("GetMessageContextTool 未找到上下文记录")
            return "未找到上下文记录"

        lines = []
        for m in msgs:
            prefix = "=> " if str(m.message_id) == str(message_id) else "   "
            lines.append(f"{prefix}[{m.time}] {m.nickname}({m.user_id}): {m.content}")
        return f"消息 {message_id} 的上下文:\n" + "\n".join(lines)


@dataclass
class SearchUserProfileTool(FunctionTool):
    plugin: Any = None
    name: str = "search_user_profile"
    description: str = "在当前会话内模糊搜索成员画像。可用 user_id、昵称、你的称呼、其他外号等关键词定位成员。"
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，可填写用户ID、昵称、你的称呼、其他外号或画像关键词。",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回数量，默认5，最大20。",
                },
            },
            "required": ["query"],
        }
    )

    def _format_profile_result(self, item: dict) -> str:
        header = f"用户 {item.get('user_id', '')}"
        nickname = normalize_profile_value(item.get("nickname"))
        if nickname:
            header += f" ({nickname})"

        lines = [header]
        relation = item.get("relation")
        if relation not in (None, "", 0):
            lines.append(f"- 好感度：{relation}")
        title = normalize_profile_value(item.get("title"))
        if title:
            lines.append(f"- 关系头衔：{title}")

        for field, label in USER_PROFILE_FIELDS:
            value = normalize_profile_value(item.get(field))
            if value:
                lines.append(f"- {label}：{value}")

        return "\n".join(lines)

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        query: str,
        limit: int = 5,
    ):
        if self.plugin is None:
            logger.warning("SearchUserProfileTool 未绑定插件实例")
            return "检索失败：未找到插件实例"
        plugin: Giftia = self.plugin
        event: AstrMessageEvent = context.context.event
        bot_name = resolve_bot_name(plugin, event)
        if not bot_name:
            logger.warning("SearchUserProfileTool 未找到对应的 bot_name")
            return "检索失败：未找到对应的 bot_name"

        group_or_user_id = event.get_group_id() or event.get_sender_id()
        try:
            clean_limit = min(max(1, int(limit or 5)), 20)
        except (TypeError, ValueError):
            clean_limit = 5
        results = await plugin.db.search_user_profiles(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            query=query,
            limit=clean_limit,
        )
        bot_conf = plugin.bot_map.get(bot_name, {})
        self_refs = {
            str(event.get_self_id() or "").strip(),
            str(bot_conf.get("nickname") or "").strip(),
            str(bot_name or "").strip(),
            "bot",
        }
        self_refs.discard("")
        results = [
            item
            for item in results
            if str(item.get("user_id") or "").strip() not in self_refs
        ]
        if not results:
            return f"未在当前会话找到与「{query}」相关的成员画像"

        return "查询到的成员画像:\n" + "\n\n".join(
            self._format_profile_result(item) for item in results
        )


@dataclass
class InspectForwardMessageTool(FunctionTool):
    plugin: Any = None
    name: str = "inspect_forward_message"
    description: str = (
        "按forward_id查看当前会话中的合并转发消息。小转发直接返回原文和媒体转述；"
        "大转发会结合原文和必要的媒体转述生成总结。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "forward_id": {
                    "type": "string",
                    "description": '提示词中 [合并转发:fwd_xxx] 或 <forward id="fwd_xxx"/> 的 id。',
                },
            },
            "required": ["forward_id"],
        }
    )

    _default_raw_threshold = 20
    _summary_node_limit = 80
    _tool_result_max_chars = 6000
    _media_pattern = FORWARD_MEDIA_PATTERN
    _nested_pattern = FORWARD_NESTED_PATTERN
    _quote_message_id_pattern = re.compile(
        r"""(<quote\b[^>]*?)\s+message_id=("[^"]*"|'[^']*'|[^\s>]+)"""
    )

    def _find_forward_in_messages(
        self, messages: list[MessageData], forward_id: str
    ) -> tuple[MessageData | None, dict | None]:
        for msg in reversed(messages or []):
            for forward in getattr(msg, "forward_messages", []) or []:
                if isinstance(forward, dict) and str(forward.get("id") or "") == str(
                    forward_id
                ):
                    return msg, forward
        return None, None

    async def _find_forward(
        self,
        plugin,
        bot_name: str,
        group_or_user_id: str,
        forward_id: str,
    ) -> tuple[MessageData | None, dict | None]:
        fmt_key = f"{bot_name}:{group_or_user_id}"
        cached = list(plugin.data_cache.recent_messages.get(fmt_key, []))
        msg, forward = self._find_forward_in_messages(cached, forward_id)
        if forward:
            return msg, forward

        try:
            recent = await plugin.data_cache.get_recent_message(
                bot_name, group_or_user_id, plugin.msg_number
            )
            msg, forward = self._find_forward_in_messages(recent, forward_id)
            if forward:
                return msg, forward
        except Exception as e:
            logger.debug(f"InspectForwardMessageTool 读取近期消息失败: {e}")

        return await plugin.db.find_forward_message_by_id(
            group_or_user_id=group_or_user_id,
            bot_name=bot_name,
            forward_id=forward_id,
        )

    @staticmethod
    def _clean_int(value, default: int, min_value: int, max_value: int) -> int:
        try:
            clean = int(value)
        except (TypeError, ValueError):
            clean = default
        return min(max(clean, min_value), max_value)

    @staticmethod
    def _shorten(text: str, limit: int) -> str:
        text = str(text or "").strip()
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 12)].rstrip() + "...<已截断>"

    def _strip_message_ids_from_text(self, text: str) -> str:
        return self._quote_message_id_pattern.sub(r"\1", str(text or ""))

    def _node_text(self, node: dict) -> str:
        sender = str(node.get("sender_name") or node.get("sender_id") or "未知用户")
        sender_id = str(node.get("sender_id") or "").strip()
        if sender_id and sender_id != sender:
            sender = f"{sender}({sender_id})"
        node_index = str(node.get("index") or "?")
        prefix = f"[{node_index}] {sender}"
        content = self._strip_message_ids_from_text(str(node.get("content") or ""))
        return f"{prefix}: {content}"

    def _append_limited(self, lines: list[str], text: str, max_chars: int) -> bool:
        current_len = sum(len(line) + 1 for line in lines)
        if current_len >= max_chars:
            return False
        remaining = max_chars - current_len
        lines.append(self._shorten(text, remaining))
        return len(text) <= remaining

    def _media_ids_from_nodes(self, nodes: list[dict]) -> list[str]:
        media_ids = []
        seen = set()
        for node in nodes:
            if not isinstance(node, dict):
                continue
            for media_id in self._media_pattern.findall(str(node.get("content") or "")):
                if media_id not in seen:
                    seen.add(media_id)
                    media_ids.append(media_id)
        return media_ids

    def _nested_ids_from_nodes(self, nodes: list[dict]) -> list[str]:
        nested_ids = []
        seen = set()
        for node in nodes:
            if not isinstance(node, dict):
                continue
            for forward_id in self._nested_pattern.findall(
                str(node.get("content") or "")
            ):
                if forward_id not in seen:
                    seen.add(forward_id)
                    nested_ids.append(forward_id)
        return nested_ids

    def _format_nodes(
        self, title: str, nodes: list[dict], max_chars: int, per_node_limit: int = 700
    ) -> str:
        lines = [title]
        if not nodes:
            lines.append("未找到匹配节点")
            return "\n".join(lines)
        for node in nodes:
            text = self._shorten(self._node_text(node), per_node_limit)
            if not self._append_limited(lines, text, max_chars):
                lines.append("...<结果已截断>")
                break
        return "\n".join(lines)

    def _reply_provider_ids(self, plugin, bot_name: str) -> list[str]:
        bot_conf = getattr(plugin, "bot_map", {}).get(bot_name, {})
        llm_reply_conf = bot_conf.get("llm_reply_conf", {})
        provider_ids = llm_reply_conf.get("provider_ids")
        if not provider_ids:
            old_provider_id = llm_reply_conf.get("provider_id")
            if old_provider_id:
                provider_ids = [old_provider_id] + llm_reply_conf.get(
                    "fallback_provider_ids", []
                )
            else:
                provider_ids = []
        return [provider_id for provider_id in provider_ids if provider_id]

    async def _generate_forward_summary(
        self,
        plugin,
        bot_name: str,
        forward_id: str,
        nodes: list[dict],
        media_captions: list | None,
        max_chars: int,
    ) -> str:
        provider_ids = self._reply_provider_ids(plugin, bot_name)
        if not provider_ids:
            return ""

        selected = nodes[: self._summary_node_limit]
        if not selected:
            return "未找到可转述的转发节点"

        material_limit = max(2000, min(12000, max_chars * 2))
        material = self._format_nodes(
            "合并转发原文节点:",
            selected,
            max_chars=material_limit,
            per_node_limit=600,
        )
        media_material = ""
        if media_captions:
            media_material = self._format_media_captions(
                media_captions, max_chars=max(1200, material_limit // 3)
            )
        omitted = max(0, len(nodes) - len(selected))
        omitted_line = f"\n另有 {omitted} 条节点未放入本次转述素材。" if omitted else ""
        media_section = f"\n\n{media_material}" if media_material else ""
        prompt = (
            "请把下面的合并转发消息转述成简洁中文总结。"
            "要求：只依据给出的媒体转述和原文节点；保留关键人物、时间顺序、结论和待办；"
            "媒体转述可用于理解对应媒体脚注，未转述的媒体脚注只说明存在媒体，不要编造。"
            f"\nforward_id: {forward_id}{omitted_line}{media_section}\n\n{material}"
        )
        system_prompt = "你是聊天合并转发内容的转述器，输出直接可供另一个助手理解。"
        for provider_id in provider_ids:
            try:
                llm_resp = await plugin.context.llm_generate(
                    chat_provider_id=provider_id,
                    system_prompt=system_prompt,
                    prompt=prompt,
                )
                completion = getattr(llm_resp, "completion_text", "") or ""
                if completion.strip():
                    return self._shorten(completion.strip(), max_chars)
            except Exception as e:
                logger.warning(
                    f"InspectForwardMessageTool 转发转述失败: provider_id={provider_id}, error={e}"
                )
        return ""

    async def _load_media_captions(
        self, plugin, bot_name: str, media_ids: list[str]
    ) -> list:
        if not media_ids:
            return []
        max_media = self._max_media_captions(plugin)
        if max_media <= 0:
            return []

        from ..conversation.media_captioner import MediaCaptioner

        captioner = MediaCaptioner(plugin)

        async def _resolve_media_caption(media_id: str) -> MediaCaption | None:
            try:
                cap = await plugin.data_cache.get_caption_by_hash(media_id)
                if cap and getattr(cap, "is_captioned", False):
                    return cap
                cap_obj, _ = await captioner.inspect_media(
                    hash_val=media_id,
                    bot_name=bot_name,
                )
                if cap_obj and getattr(cap_obj, "is_captioned", False):
                    return cap_obj
            except Exception as e:
                logger.warning(f"[Giftia] 转发媒体 [{media_id}] 解析转述失败: {e}")
            return None

        tasks = [_resolve_media_caption(media_id) for media_id in media_ids[:max_media]]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        valid_captions = []
        for r in results:
            if isinstance(r, Exception):
                logger.warning(f"[Giftia] 转发媒体并发解析异常: {r}")
            elif r is not None:
                valid_captions.append(r)
        return valid_captions

    def _raw_threshold(self, plugin) -> int:
        return self._clean_int(
            getattr(plugin, "tools_config", {}).get(
                "inspect_forward_raw_threshold", self._default_raw_threshold
            ),
            self._default_raw_threshold,
            1,
            200,
        )

    def _max_media_captions(self, plugin) -> int:
        return self._clean_int(
            getattr(plugin, "tools_config", {}).get(
                "inspect_forward_max_media_captions", 5
            ),
            5,
            0,
            20,
        )

    def _format_media_captions(self, captions: list, max_chars: int) -> str:
        lines = ["媒体转述:"]
        if not captions:
            lines.append("未找到可用媒体转述")
            return "\n".join(lines)
        for caption in captions:
            parts = []
            if getattr(caption, "caption", ""):
                parts.append(f"描述: {caption.caption}")
            if getattr(caption, "text", ""):
                parts.append(f"文字: {caption.text}")
            if getattr(caption, "genre", ""):
                parts.append(f"类型: {caption.genre}")
            media_line = f"- {caption.hash_val} ({caption.media_type or 'media'}): "
            media_line += "；".join(parts) if parts else "暂无转述内容"
            if not self._append_limited(lines, media_line, max_chars):
                lines.append("- ...<媒体转述已截断>")
                break
        return "\n".join(lines)

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        forward_id: str,
    ):
        if self.plugin is None:
            logger.warning("InspectForwardMessageTool 未绑定插件实例")
            return "查看失败：未找到插件实例"
        plugin: Giftia = self.plugin
        event: AstrMessageEvent = context.context.event
        bot_name = resolve_bot_name(plugin, event)
        if not bot_name:
            logger.warning("InspectForwardMessageTool 未找到对应的 bot_name")
            return "查看失败：未找到对应的 bot_name"

        forward_id = str(forward_id or "").strip()
        if not forward_id:
            return "查看失败：缺少 forward_id"

        group_or_user_id = event.get_group_id() or event.get_sender_id()
        _owner_msg, forward = await self._find_forward(
            plugin, bot_name, group_or_user_id, forward_id
        )
        if not forward:
            return f"未找到合并转发消息: {forward_id}"

        nodes = forward.get("nodes") if isinstance(forward.get("nodes"), list) else []
        raw_threshold = self._raw_threshold(plugin)
        max_chars = self._tool_result_max_chars
        media_ids = self._media_ids_from_nodes(nodes)
        nested_ids = self._nested_ids_from_nodes(nodes)
        max_media_captions = self._max_media_captions(plugin)
        captions: list | None = None

        async def load_captions_if_needed() -> list:
            nonlocal captions
            if captions is None:
                if media_ids and max_media_captions > 0:
                    captions = await self._load_media_captions(
                        plugin, bot_name, media_ids
                    )
                else:
                    captions = []
            return captions

        header = [
            f"合并转发 {forward_id}",
            f"- 节点数: {len(nodes)}",
            f"- 媒体数: {len(media_ids)}",
            f"- 嵌套转发数: {len(nested_ids)}",
        ]
        if forward.get("truncated"):
            header.append("- 注意: 入库时已截断过长转发")
        if forward.get("unresolved"):
            header.append("- 注意: 原始转发内容未能完整拉取")
        if nested_ids:
            header.append("- 嵌套转发ID: " + ", ".join(nested_ids[:10]))

        if len(nodes) <= raw_threshold:
            body = self._format_nodes(
                "原文:",
                nodes,
                max_chars=max_chars,
                per_node_limit=800,
            )
        else:
            body = ""
            cached_summary = await plugin.db.get_forward_summary(
                bot_name, group_or_user_id, forward_id
            )
            if cached_summary:
                body = "缓存转述:\n" + self._shorten(cached_summary, max_chars)
            if not body:
                captions_for_summary = await load_captions_if_needed()
                generated_summary = await self._generate_forward_summary(
                    plugin=plugin,
                    bot_name=bot_name,
                    forward_id=forward_id,
                    nodes=nodes,
                    media_captions=captions_for_summary,
                    max_chars=max_chars,
                )
                if generated_summary:
                    body = "转述:\n" + generated_summary
                    await plugin.db.update_forward_summary(
                        bot_name, group_or_user_id, forward_id, generated_summary
                    )
                else:
                    body = self._format_nodes(
                        f"转述生成失败，提供前 {raw_threshold} 条原文摘录:",
                        nodes[:raw_threshold],
                        max_chars=max_chars,
                        per_node_limit=500,
                    )

        lines = ["\n".join(header), body]
        if len(nodes) <= raw_threshold and media_ids and max_media_captions > 0:
            captions_for_output = await load_captions_if_needed()
            lines.append(
                self._format_media_captions(captions_for_output, max_chars=max_chars)
            )
            omitted_media = max(0, len(media_ids) - max_media_captions)
            if omitted_media:
                lines.append(f"另有 {omitted_media} 个媒体未转述。")
        elif len(nodes) <= raw_threshold and media_ids:
            lines.append("媒体转述未启用，原文中只保留媒体脚注。")

        return "\n\n".join(line for line in lines if line).strip()


@dataclass
class InspectMediaTool(FunctionTool):
    plugin: Any = None
    name: str = "inspect_media"
    description: str = (
        "按 media_id 查看当前会话中未转述的历史媒体（图片/语音/视频），或对已有媒体提出具体问题重新深入查看。"
        "如果目标为较长视频，可通过 start_time 指定切片起始秒数（默认为 0 秒）。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "media_id": {
                    "type": "string",
                    "description": "媒体占位符 [图片:media_id]、[语音:media_id] 或 [视频:media_id] 中的 16 位哈希 ID。",
                },
                "question": {
                    "type": "string",
                    "description": "希望能特别关注的观察指令或具体问题。可选填。",
                },
                "start_time": {
                    "type": "integer",
                    "description": "若为视频，可指定截取起始时间(秒)，默认 0。",
                },
            },
            "required": ["media_id"],
        }
    )

    async def call(self, context, **kwargs) -> str:
        if self.plugin is None:
            return "分析失败：未绑定插件实例"

        # 兼容 ContextWrapper 或 AstrMessageEvent
        if hasattr(context, "context") and hasattr(context.context, "event"):
            event = context.context.event
        else:
            event = context

        platform_id = getattr(getattr(event, "platform_meta", None), "id", None)
        bot_name = (
            self.plugin.adapter_id_map.get(platform_id) or "" if platform_id else ""
        )
        group_or_user_id = ""
        if hasattr(event, "get_group_id") and hasattr(event, "get_sender_id"):
            group_or_user_id = event.get_group_id() or event.get_sender_id() or ""

        media_id = str(kwargs.get("media_id") or "").strip()
        start_time = int(kwargs.get("start_time") or 0)
        question = str(kwargs.get("question") or "").strip()

        if not media_id:
            return "请求参数错误：未提供 media_id"

        caption_obj, result_str = await MediaCaptioner(self.plugin).inspect_media(
            hash_val=media_id,
            question=question,
            start_time=start_time,
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
        )
        media_type = (
            getattr(caption_obj, "media_type", "media") if caption_obj else "media"
        )
        return f"媒体 [{media_id}] ({media_type}) 分析转述结果:\n{result_str}"


@dataclass
class SetUserAvatarDescriptionTool(FunctionTool):
    plugin: Any = None
    name: str = "set_user_avatar_description"
    description: str = (
        "为指定用户设置或更新头像视觉特征描述，并持久化保存至画像库。"
        "通常在通过头像转述工具或感知获取到该用户的头像特征后调用。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "目标用户的数字ID/用户标识。",
                },
                "avatar_description": {
                    "type": "string",
                    "description": "头像的特征描述内容（如主体角色、外貌、风格、色调等一句话概括）。",
                },
            },
            "required": ["user_id", "avatar_description"],
        }
    )

    async def call(self, context, **kwargs) -> str:
        if self.plugin is None:
            return "设置失败：未绑定插件实例"

        if hasattr(context, "context") and hasattr(context.context, "event"):
            event = context.context.event
        else:
            event = context

        platform_id = getattr(getattr(event, "platform_meta", None), "id", None)
        bot_name = (
            self.plugin.adapter_id_map.get(platform_id) or "" if platform_id else ""
        )
        group_or_user_id = ""
        if hasattr(event, "get_group_id") and hasattr(event, "get_sender_id"):
            group_or_user_id = event.get_group_id() or event.get_sender_id() or ""

        user_id = str(kwargs.get("user_id") or "").strip()
        avatar_desc = str(kwargs.get("avatar_description") or "").strip()

        if not user_id:
            return "请求参数错误：未提供目标用户的 user_id"
        if not avatar_desc:
            return "请求参数错误：未提供头像描述 avatar_description"

        try:
            await self.plugin.data_cache.set_user_profile(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                user_id=user_id,
                profile_fields={"avatar_description": avatar_desc},
                alias_increment_count=False,
            )
            logger.info(
                f"[Giftia] 工具 set_user_avatar_description 已更新用户 {user_id} 的头像描述: {avatar_desc}"
            )
            return f"已成功将用户 {user_id} 的头像描述记录至画像库:\n{avatar_desc}"
        except Exception as e:
            logger.error(
                f"[Giftia] set_user_avatar_description 执行失败: {e}", exc_info=True
            )
            return f"保存头像描述失败: {e}"


def remove_tools(context: Context):
    func_tool = context.get_llm_tool_manager()
    for name in TOOLS_NAMESPACE:
        tool = func_tool.get_func(name)
        if tool:
            StarTools.unregister_llm_tool(name)
            logger.info(f"[Giftia] 已移除 {name} 工具注册")
