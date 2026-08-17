import asyncio
from collections import defaultdict
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import (
    At,
    Face,
    File,
    Forward,
    Image,
    Json,
    Node,
    Nodes,
    Plain,
    Poke,
    Record,
    Reply,
    Video,
)
from astrbot.core.message.components import BaseMessageComponent

from ..database.data_cache import DataCache
from ..llm.call_llm import CallLLM
from .http_manager import HttpManager
from .message_forward import (
    MAX_FORWARD_FETCH as MAX_FORWARD_FETCH,
    MAX_FORWARD_NODE_COUNT as MAX_FORWARD_NODE_COUNT,
    MAX_FORWARD_NODE_DEPTH as MAX_FORWARD_NODE_DEPTH,
    MessageForwardParser,
)
from .message_media import (
    MessageMediaFormatter,
    LockManager,
    SUPPORTED_FILE_FORMATS_WITH_DOT as SUPPORTED_FILE_FORMATS_WITH_DOT,
)
from .message_parse_types import ChainParseResult
from .schemas import MediaCaption, MessageData
from .emoji_constants import EMOJI_MAP
from .notice_parse import NoticeParser, NoticeParseResult, REACTION_NOTICE_TYPES


class MessageParser:
    def __init__(
        self,
        data_cache: DataCache,
        http_manager: HttpManager,
        image_caption_enabled: bool,
        audio_caption_enabled: bool,
        call_llm: CallLLM,
    ):
        self.data_cache = data_cache
        self.http_manager = http_manager
        self.image_caption_enabled = image_caption_enabled
        self.audio_caption_enabled = audio_caption_enabled
        self.call_llm = call_llm
        # 异步锁，防止多机器人场景重复解析媒体信息
        self.url_locks = LockManager()
        self.hash_locks = LockManager()
        self.media_formatter = MessageMediaFormatter(
            data_cache=data_cache,
            http_manager=http_manager,
            image_caption_enabled=image_caption_enabled,
            audio_caption_enabled=audio_caption_enabled,
            call_llm=call_llm,
            url_locks=self.url_locks,
            hash_locks=self.hash_locks,
        )
        self.forward_parser = MessageForwardParser(
            chain_to_result=self.chain_to_result,
            format_image_ref=self._format_image_ref,
            format_audio_ref=self._format_audio_ref,
            format_video_ref=self._format_video_ref,
        )
        def _get_bot_config(bot_name: str) -> dict:
            plugin = getattr(self.data_cache, "plugin", None)
            if plugin and hasattr(plugin, "get_bot_config"):
                return plugin.get_bot_config(bot_name)
            return {}

        self.notice_parser = NoticeParser(
            resolve_user_name_fn=self._resolve_user_name,
            get_bot_config_fn=_get_bot_config,
        )

    async def _resolve_user_name(
        self,
        event: AstrMessageEvent,
        user_id: str,
        current_name: str | None,
        force_lookup: bool = False,
    ) -> str:
        """Resolve a readable nickname for notice events that only carry user_id."""
        user_id = str(user_id or "").strip()
        current_name = str(current_name or "").strip()
        if current_name and current_name != user_id and not force_lookup:
            return current_name
        if not user_id:
            return current_name

        group_id = str(event.get_group_id() or "").strip()
        bot = getattr(event, "bot", None)
        routing_params = {}
        self_id = str(event.get_self_id() or "").strip()
        if self_id:
            routing_params["self_id"] = self_id

        if bot and hasattr(bot, "call_action"):
            try:
                query_user_id = int(user_id) if user_id.isdigit() else user_id
                if group_id:
                    query_group_id = int(group_id) if group_id.isdigit() else group_id
                    info = await bot.call_action(
                        action="get_group_member_info",
                        group_id=query_group_id,
                        user_id=query_user_id,
                        no_cache=False,
                        **routing_params,
                    )
                    nickname = (
                        info.get("card")
                        or info.get("nickname")
                        or info.get("nick")
                    )
                    if nickname:
                        return str(nickname)
                info = await bot.call_action(
                    action="get_stranger_info",
                    user_id=query_user_id,
                    no_cache=False,
                    **routing_params,
                )
                nickname = info.get("nick") or info.get("nickname")
                if nickname:
                    return str(nickname)
            except Exception as e:
                logger.debug(f"[Giftia] 解析用户昵称失败: {e}")

        if group_id:
            try:
                group = await event.get_group(group_id)
                for member in getattr(group, "members", []) or []:
                    if str(getattr(member, "user_id", "")) == user_id:
                        nickname = getattr(member, "nickname", "")
                        if nickname:
                            return str(nickname)
            except Exception as e:
                logger.debug(f"[Giftia] 从群成员列表解析昵称失败: {e}")

        return current_name or user_id

    @staticmethod
    def _get_event_field(obj, field_name: str, default=None):
        """从字典或对象中安全地读取属性/字段"""
        return NoticeParser.get_event_field(obj, field_name, default)

    @staticmethod
    def _get_poke_target_id(comp: Poke) -> str:
        target_id = (
            comp.target_id()
            if hasattr(comp, "target_id")
            else getattr(comp, "id", None) or getattr(comp, "qq", None)
        )
        return str(target_id or "").strip()

    @staticmethod
    def _format_duration(duration: int) -> str:
        """格式化禁言秒数为人类可读字符串"""
        return NoticeParser.format_duration(duration)

    @staticmethod
    def _format_user_ref(name: str, user_id: str) -> str:
        return NoticeParser.format_user_ref(name, user_id)

    async def _format_poke_message(
        self,
        event: AstrMessageEvent,
        chain: list[BaseMessageComponent],
    ) -> str:
        parts = []
        for comp in chain:
            if not isinstance(comp, Poke):
                continue
            target_id = self._get_poke_target_id(comp)
            target_name = await self._resolve_user_name(
                event=event,
                user_id=target_id,
                current_name=target_id,
                force_lookup=True,
            )
            target_ref = self._format_user_ref(target_name, target_id)
            parts.append(f"[戳一戳:{target_ref}]")
        return " ".join(parts)

    async def parse_user_message(
        self, event: AstrMessageEvent, bot_name: str, defer_caption: bool = False
    ) -> tuple[MessageData, list[str], list[str]]:
        """解析用户发送的消息"""
        # 获取时间
        iso_string = datetime.fromtimestamp(event.message_obj.timestamp).isoformat()
        # 获取消息内容
        parsed = await self.chain_to_result(
            event.get_messages(), defer_caption, event=event
        )
        msg = parsed.content
        media_id_list = parsed.media_id_list
        group_or_user_id = event.get_group_id() or event.get_sender_id()
        sender_id = event.get_sender_id()
        has_poke = any(isinstance(comp, Poke) for comp in event.get_messages())

        raw_message = getattr(event.message_obj, "raw_message", None)
        is_system_notice = False
        notice_result: NoticeParseResult | None = None

        if raw_message:
            notice_result = await self.notice_parser.parse_notice(
                event=event,
                raw_message=raw_message,
                bot_name=bot_name,
            )
            # 将解析结果附加至 event 便于下游消费
            setattr(event, "_notice_result", notice_result)

            if notice_result.is_notice:
                msg = notice_result.content
                if notice_result.role == "system":
                    is_system_notice = True

        if is_system_notice and notice_result:
            sender_id = notice_result.sender_id or sender_id
            sender_name = notice_result.sender_name or sender_id
        else:
            sender_name = await self._resolve_user_name(
                event=event,
                user_id=sender_id,
                current_name=event.get_sender_name(),
                force_lookup=has_poke or bool(notice_result and notice_result.is_notice),
            )

        if has_poke:
            poke_msg = await self._format_poke_message(
                event=event,
                chain=event.get_messages(),
            )
            if poke_msg:
                msg = poke_msg

        msg_data = MessageData(
            nickname=sender_name,
            user_id=sender_id,
            group_or_user_id=group_or_user_id,
            time=iso_string,
            message_id=event.message_obj.message_id,
            content=msg,
            is_recalled=0,
            media_id_list=media_id_list,
            forward_messages=parsed.forward_messages,
            role="system" if is_system_notice else "message",
        )
        # 将消息写入缓存
        await self.data_cache.add_message(bot_name, group_or_user_id, msg_data)
        return msg_data, parsed.image_urls, parsed.audio_urls

    async def chain_to_str(
        self, chain: list[BaseMessageComponent], defer_caption: bool = False
    ) -> tuple[str, list[str]]:
        """将消息链转换为字符串，用于接收用户消息时转换使用"""
        parsed = await self.chain_to_result(chain, defer_caption)
        return parsed.content, parsed.media_id_list

    async def chain_to_result(
        self,
        chain: list[BaseMessageComponent],
        defer_caption: bool = False,
        event: AstrMessageEvent | None = None,
        _forward_ctx: dict | None = None,
        _depth: int = 0,
    ) -> ChainParseResult:
        """将消息链转换为可入库的正文、媒体脚注和转发结构。"""
        if _forward_ctx is None:
            _forward_ctx = {
                "remote_refs": {},
                "fetch_count": 0,
                "fetching": set(),
            }

        msg_parts = []
        result = ChainParseResult()
        index = 0
        while index < len(chain):
            comp = chain[index]
            index += 1
            if isinstance(comp, Plain):
                msg_parts.append(comp.text)
            elif isinstance(comp, Reply):
                # 引用消息文本
                quote_text = ""
                if comp.chain:
                    quote_result = await self.chain_to_result(
                        comp.chain,
                        defer_caption=defer_caption,
                        event=event,
                        _forward_ctx=_forward_ctx,
                        _depth=_depth + 1,
                    )
                    result.merge(quote_result)
                    quote_text = quote_result.content
                msg_parts.append(
                    f"<quote message_id={comp.id} sender_id={comp.sender_id} sender_name={comp.sender_nickname}>{quote_text}</quote>"
                )
            elif isinstance(comp, At):
                msg_parts.append(f"<@{comp.name}({comp.qq})>")
            elif isinstance(comp, Image):
                custom_desc = getattr(comp, "meme_desc", None)
                part, media_result = await self._format_image_ref(
                    comp.url or "",
                    comp.file,
                    defer_caption,
                    custom_desc=custom_desc,
                    event=event,
                )
                result.merge(media_result)
                msg_parts.append(part)
            # 语音消息
            elif isinstance(comp, Record):
                part, media_result = await self._format_audio_ref(
                    comp.url or "", comp.file, defer_caption, event=event
                )
                result.merge(media_result)
                msg_parts.append(part)
            elif isinstance(comp, Video):
                part, media_result = await self._format_video_ref(
                    comp.url or "",
                    getattr(comp, "file", None),
                    defer_caption,
                    event=event,
                    path=getattr(comp, "path", None),
                )
                result.merge(media_result)
                msg_parts.append(part)
            elif isinstance(comp, Json):
                forward_result = await self._json_to_forward_result(
                    comp.data,
                    defer_caption=defer_caption,
                    event=event,
                    forward_ctx=_forward_ctx,
                    depth=_depth,
                )
                if forward_result:
                    result.merge(forward_result)
                    msg_parts.append(forward_result.content)
                else:
                    msg_parts.append("[合并转发消息]")
            elif isinstance(comp, File):
                # 暂不支持文件转述
                msg_parts.append(f"[文件:{comp.name}]")
            elif isinstance(comp, Face):
                emoji_id = str(comp.id)
                emoji_desc = EMOJI_MAP.get(emoji_id, emoji_id)
                msg_parts.append(f"[表情:{emoji_desc}]")
            elif isinstance(comp, Poke):
                target_id = self._get_poke_target_id(comp)
                msg_parts.append(
                    f"[戳一戳:{target_id}]" if target_id else "[戳一戳]"
                )
            elif isinstance(comp, Node):
                nodes = [comp]
                while index < len(chain) and isinstance(chain[index], Node):
                    nodes.append(chain[index])
                    index += 1
                forward_result = await self._component_nodes_to_forward_result(
                    nodes,
                    defer_caption=defer_caption,
                    event=event,
                    forward_ctx=_forward_ctx,
                    depth=_depth,
                )
                result.merge(forward_result)
                msg_parts.append(forward_result.content)
            elif isinstance(comp, Nodes):
                forward_result = await self._component_nodes_to_forward_result(
                    comp.nodes or [],
                    defer_caption=defer_caption,
                    event=event,
                    forward_ctx=_forward_ctx,
                    depth=_depth,
                )
                result.merge(forward_result)
                msg_parts.append(forward_result.content)
            elif isinstance(comp, Forward):
                forward_result = await self._forward_id_to_result(
                    getattr(comp, "id", ""),
                    defer_caption=defer_caption,
                    event=event,
                    forward_ctx=_forward_ctx,
                    depth=_depth,
                )
                result.merge(forward_result)
                msg_parts.append(forward_result.content)
        result.content = " ".join(part for part in msg_parts if part).strip()
        return result

    @staticmethod
    def _make_forward_id(payload: dict) -> str:
        return MessageForwardParser.make_forward_id(payload)

    @staticmethod
    def _unwrap_action_response(payload) -> dict:
        return MessageForwardParser.unwrap_action_response(payload)

    @staticmethod
    def _first_media_url(url: str | None, file_name: str | None) -> str:
        return MessageMediaFormatter.first_media_url(url, file_name)

    @staticmethod
    def _filename_stable_hash(file_name: str | None) -> str | None:
        return MessageMediaFormatter.filename_stable_hash(file_name)

    @staticmethod
    def _is_filename_stable_hash(hash_val: str | None) -> bool:
        return MessageMediaFormatter.is_filename_stable_hash(hash_val)

    async def _format_image_ref(
        self,
        url: str,
        file_name: str | None,
        defer_caption: bool,
        custom_desc: str | None = None,
        event: AstrMessageEvent | None = None,
    ) -> tuple[str, ChainParseResult]:
        return await self.media_formatter.format_image_ref(
            url,
            file_name,
            defer_caption,
            custom_desc=custom_desc,
            event=event,
        )

    async def _format_audio_ref(
        self, url: str, file_name: str | None, defer_caption: bool, event: AstrMessageEvent | None = None
    ) -> tuple[str, ChainParseResult]:
        return await self.media_formatter.format_audio_ref(
            url,
            file_name,
            defer_caption,
            event=event,
        )

    async def _format_video_ref(
        self,
        url: str,
        file_name: str | None,
        defer_caption: bool,
        event: AstrMessageEvent | None = None,
        path: str | None = None,
    ) -> tuple[str, ChainParseResult]:
        return await self.media_formatter.format_video_ref(
            url,
            file_name,
            defer_caption,
            event=event,
            path=path,
        )

    async def _component_nodes_to_forward_result(
        self,
        nodes: list[Node],
        defer_caption: bool,
        event: AstrMessageEvent | None,
        forward_ctx: dict,
        depth: int,
    ) -> ChainParseResult:
        return await self.forward_parser.component_nodes_to_forward_result(
            nodes,
            defer_caption=defer_caption,
            event=event,
            forward_ctx=forward_ctx,
            depth=depth,
        )

    async def _call_forward_msg(
        self, event: AstrMessageEvent | None, forward_id: str
    ) -> dict | None:
        return await self.forward_parser.call_forward_msg(event, forward_id)

    async def _forward_id_to_result(
        self,
        forward_id,
        defer_caption: bool,
        event: AstrMessageEvent | None,
        forward_ctx: dict,
        depth: int,
    ) -> ChainParseResult:
        return await self.forward_parser.forward_id_to_result(
            forward_id,
            defer_caption=defer_caption,
            event=event,
            forward_ctx=forward_ctx,
            depth=depth,
        )

    async def _onebot_forward_payload_to_result(
        self,
        payload: dict,
        source_id: str,
        defer_caption: bool,
        event: AstrMessageEvent | None,
        forward_ctx: dict,
        depth: int,
    ) -> ChainParseResult:
        return await self.forward_parser.onebot_forward_payload_to_result(
            payload,
            source_id=source_id,
            defer_caption=defer_caption,
            event=event,
            forward_ctx=forward_ctx,
            depth=depth,
        )

    async def _onebot_nodes_to_forward_result(
        self,
        nodes: list,
        source_id: str,
        defer_caption: bool,
        event: AstrMessageEvent | None,
        forward_ctx: dict,
        depth: int,
    ) -> ChainParseResult:
        return await self.forward_parser.onebot_nodes_to_forward_result(
            nodes,
            source_id=source_id,
            defer_caption=defer_caption,
            event=event,
            forward_ctx=forward_ctx,
            depth=depth,
        )

    async def _onebot_content_to_result(
        self,
        raw_content,
        defer_caption: bool,
        event: AstrMessageEvent | None,
        forward_ctx: dict,
        depth: int,
    ) -> ChainParseResult:
        return await self.forward_parser.onebot_content_to_result(
            raw_content,
            defer_caption=defer_caption,
            event=event,
            forward_ctx=forward_ctx,
            depth=depth,
        )

    async def _onebot_segments_to_result(
        self,
        segments: list,
        defer_caption: bool,
        event: AstrMessageEvent | None,
        forward_ctx: dict,
        depth: int,
    ) -> ChainParseResult:
        return await self.forward_parser.onebot_segments_to_result(
            segments,
            defer_caption=defer_caption,
            event=event,
            forward_ctx=forward_ctx,
            depth=depth,
        )

    def _extract_json_forward_source_id(self, data: dict) -> str:
        return self.forward_parser.extract_json_forward_source_id(data)

    def _extract_json_forward_preview_nodes(self, data: dict) -> list[dict]:
        return self.forward_parser.extract_json_forward_preview_nodes(data)

    async def _json_to_forward_result(
        self,
        data,
        defer_caption: bool,
        event: AstrMessageEvent | None,
        forward_ctx: dict,
        depth: int,
    ) -> ChainParseResult | None:
        return await self.forward_parser.json_to_forward_result(
            data,
            defer_caption=defer_caption,
            event=event,
            forward_ctx=forward_ctx,
            depth=depth,
        )

    async def _get_image_caption(
        self,
        url: str,
        file_name: str | None = None,
        defer_caption: bool = False,
        custom_desc: str | None = None,
    ) -> tuple[str | None, MediaCaption | None]:
        return await self.media_formatter.get_image_caption(
            url,
            file_name=file_name,
            defer_caption=defer_caption,
            custom_desc=custom_desc,
        )

    async def _get_audio_caption(
        self, url: str, file_name: str | None = None, defer_caption: bool = False
    ) -> tuple[str | None, MediaCaption | None]:
        return await self.media_formatter.get_audio_caption(
            url,
            file_name=file_name,
            defer_caption=defer_caption,
        )
