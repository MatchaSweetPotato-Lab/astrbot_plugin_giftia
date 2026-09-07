import json

from xxhash import xxh3_64_hexdigest

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Node
from astrbot.core.message.components import BaseMessageComponent

from .message_parse_types import ChainParseResult

MAX_FORWARD_FETCH = 5
MAX_FORWARD_NODE_DEPTH = 4
MAX_FORWARD_NODE_COUNT = 80


class MessageForwardParser:
    _forward_node_list_keys = ("messages", "message", "nodes", "nodeList")
    _forward_node_hint_keys = (
        "sender",
        "message",
        "content",
        "nickname",
        "name",
        "user_id",
        "uin",
    )
    _plain_segment_types = {
        "text",
        "plain",
        "at",
        "image",
        "record",
        "voice",
        "audio",
        "video",
        "file",
        "json",
        "xml",
        "face",
        "reply",
        "forward",
        "forward_msg",
        "nodes",
    }

    def __init__(self, chain_to_result, format_image_ref, format_audio_ref, format_video_ref=None):
        self.chain_to_result = chain_to_result
        self.format_image_ref = format_image_ref
        self.format_audio_ref = format_audio_ref
        self.format_video_ref = format_video_ref

    @staticmethod
    def make_forward_id(payload: dict) -> str:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return f"fwd_{xxh3_64_hexdigest(raw.encode())[:12]}"

    @staticmethod
    def unwrap_action_response(payload):
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return {}
        data = payload.get("data")
        if isinstance(data, (dict, list)):
            return data
        return payload

    @staticmethod
    def payload_brief(payload) -> str:
        if not isinstance(payload, dict):
            if isinstance(payload, list):
                return f"type=list, len={len(payload)}"
            return f"type={type(payload).__name__}"

        data = payload.get("data")
        brief = {
            "keys": list(payload.keys())[:12],
            "status": payload.get("status"),
            "retcode": payload.get("retcode"),
            "message": payload.get("message") or payload.get("wording"),
            "data_type": type(data).__name__,
        }
        if isinstance(data, dict):
            brief["data_keys"] = list(data.keys())[:12]
        elif isinstance(data, list):
            brief["data_len"] = len(data)
        return json.dumps(brief, ensure_ascii=False, default=str)

    @staticmethod
    def extract_forward_nodes_from_payload(payload):
        data = MessageForwardParser.unwrap_action_response(payload)
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            return None
        for key in MessageForwardParser._forward_node_list_keys:
            if key not in data:
                continue
            return MessageForwardParser.parse_json_text(data.get(key))
        return None

    @staticmethod
    def parse_json_text(value):
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value.strip().replace("&#44;", ","))
        except Exception:
            return value

    @staticmethod
    def looks_like_forward_node_list(value) -> bool:
        if not isinstance(value, list) or not value:
            return False
        for item in value:
            if not isinstance(item, dict):
                return False
            item_type = str(item.get("type") or "").strip().lower()
            if item_type in MessageForwardParser._plain_segment_types:
                return False
            node_data = item.get("data") if isinstance(item.get("data"), dict) else {}
            if item_type and item_type != "node" and not any(
                key in item or key in node_data
                for key in ("message", "content", "messages", "nodes")
            ):
                return False
            if not any(
                key in item or key in node_data
                for key in MessageForwardParser._forward_node_hint_keys
            ):
                return False
        return True

    async def component_nodes_to_forward_result(
        self,
        nodes: list[Node],
        defer_caption: bool,
        event: AstrMessageEvent | None,
        forward_ctx: dict,
        depth: int,
    ) -> ChainParseResult:
        result = ChainParseResult()
        if depth > MAX_FORWARD_NODE_DEPTH:
            return result

        node_items = []
        for index, node in enumerate((nodes or [])[:MAX_FORWARD_NODE_COUNT], start=1):
            node_result = await self.chain_to_result(
                node.content or [],
                defer_caption=True,
                event=event,
                _forward_ctx=forward_ctx,
                _depth=depth + 1,
            )
            result.forward_messages.extend(node_result.forward_messages)
            node_items.append(
                {
                    "index": index,
                    "sender_name": node.name or "bot",
                    "sender_id": str(node.uin or ""),
                    "time": str(node.time or ""),
                    "content": node_result.content,
                }
            )

        block = {
            "source": "component",
            "nodes": node_items,
            "truncated": len(nodes or []) > MAX_FORWARD_NODE_COUNT,
        }
        block["id"] = self.make_forward_id(block)
        result.forward_messages.insert(0, block)
        result.content = f"[合并转发:{block['id']}]"
        return result

    async def call_forward_msg(
        self, event: AstrMessageEvent | None, forward_id: str
    ):
        if not event or not forward_id:
            return None
        bot = getattr(event, "bot", None)
        callers = []
        api = getattr(bot, "api", None)
        if callable(getattr(api, "call_action", None)):
            callers.append(api.call_action)
        if callable(getattr(bot, "call_action", None)):
            callers.append(bot.call_action)
        if not callers:
            return None

        forward_id = str(forward_id).strip()
        params_list = [{"message_id": forward_id}, {"id": forward_id}]
        if forward_id.isdigit():
            int_id = int(forward_id)
            params_list.extend([{"message_id": int_id}, {"id": int_id}])

        routing_params = {}
        try:
            self_id = str(event.get_self_id() or "").strip()
        except Exception:
            self_id = ""
        if self_id:
            routing_params["self_id"] = self_id

        last_error = None
        for caller in callers:
            for params in params_list:
                call_params = dict(params)
                call_params.update(routing_params)
                for keyword_action in (True, False):
                    try:
                        if keyword_action:
                            payload = await caller(
                                action="get_forward_msg", **call_params
                            )
                        else:
                            payload = await caller("get_forward_msg", **call_params)
                        if isinstance(payload, (dict, list)):
                            if isinstance(payload, list):
                                return payload
                            status = payload.get("status")
                            retcode = payload.get("retcode")
                            if status == "failed" or (
                                retcode not in (None, 0, "0")
                                and not payload.get("data")
                            ):
                                last_error = self.payload_brief(payload)
                                continue
                            if self.extract_forward_nodes_from_payload(payload) is None:
                                last_error = self.payload_brief(payload)
                                continue
                            return payload
                    except TypeError as e:
                        last_error = e
                        continue
                    except Exception as e:
                        last_error = e
                        continue
        if last_error:
            logger.debug(f"[Giftia] 获取合并转发消息失败: {last_error}")
        return None

    async def forward_id_to_result(
        self,
        forward_id,
        defer_caption: bool,
        event: AstrMessageEvent | None,
        forward_ctx: dict,
        depth: int,
    ) -> ChainParseResult:
        forward_id = str(forward_id or "").strip()
        if not forward_id:
            result = ChainParseResult()
            result.content = "[合并转发消息]"
            return result

        if forward_id in forward_ctx["remote_refs"]:
            block_id = forward_ctx["remote_refs"][forward_id]
            result = ChainParseResult()
            result.content = f"[合并转发:{block_id}]"
            return result

        if (
            depth > MAX_FORWARD_NODE_DEPTH
            or forward_ctx["fetch_count"] >= MAX_FORWARD_FETCH
            or forward_id in forward_ctx["fetching"]
        ):
            block = {
                "source": "remote",
                "source_id": forward_id,
                "nodes": [],
                "truncated": True,
            }
            block["id"] = self.make_forward_id(block)
            forward_ctx["remote_refs"][forward_id] = block["id"]
            result = ChainParseResult(
                content=f"[合并转发:{block['id']}]",
                forward_messages=[block],
            )
            return result

        forward_ctx["fetching"].add(forward_id)
        forward_ctx["fetch_count"] += 1
        payload = await self.call_forward_msg(event, forward_id)
        forward_ctx["fetching"].discard(forward_id)
        if not payload:
            block = {
                "source": "remote",
                "source_id": forward_id,
                "nodes": [],
                "unresolved": True,
            }
            block["id"] = self.make_forward_id(block)
            forward_ctx["remote_refs"][forward_id] = block["id"]
            return ChainParseResult(
                content=f"[合并转发:{block['id']}]",
                forward_messages=[block],
            )

        parsed = await self.onebot_forward_payload_to_result(
            payload,
            source_id=forward_id,
            defer_caption=defer_caption,
            event=event,
            forward_ctx=forward_ctx,
            depth=depth,
        )
        if parsed.forward_messages:
            forward_ctx["remote_refs"][forward_id] = parsed.forward_messages[0]["id"]
        return parsed

    async def onebot_forward_payload_to_result(
        self,
        payload,
        source_id: str,
        defer_caption: bool,
        event: AstrMessageEvent | None,
        forward_ctx: dict,
        depth: int,
    ) -> ChainParseResult:
        nodes = self.extract_forward_nodes_from_payload(payload)
        if not isinstance(nodes, list):
            block = {
                "source": "remote",
                "source_id": source_id,
                "nodes": [],
                "unresolved": True,
            }
            block["id"] = self.make_forward_id(block)
            return ChainParseResult(
                content=f"[合并转发:{block['id']}]",
                forward_messages=[block],
            )
        return await self.onebot_nodes_to_forward_result(
            nodes,
            source_id=source_id,
            defer_caption=defer_caption,
            event=event,
            forward_ctx=forward_ctx,
            depth=depth,
        )

    async def onebot_nodes_to_forward_result(
        self,
        nodes: list,
        source_id: str,
        defer_caption: bool,
        event: AstrMessageEvent | None,
        forward_ctx: dict,
        depth: int,
    ) -> ChainParseResult:
        result = ChainParseResult()
        if depth > MAX_FORWARD_NODE_DEPTH:
            return result

        node_items = []
        for index, node in enumerate(nodes[:MAX_FORWARD_NODE_COUNT], start=1):
            if not isinstance(node, dict):
                continue
            node_data = node.get("data") if isinstance(node.get("data"), dict) else {}
            sender = node.get("sender")
            if not isinstance(sender, dict):
                sender = (
                    node_data.get("sender")
                    if isinstance(node_data.get("sender"), dict)
                    else {}
                )
            sender_id = (
                sender.get("user_id")
                or sender.get("id")
                or node_data.get("user_id")
                or node_data.get("uin")
                or node.get("user_id")
                or ""
            )
            sender_name = (
                sender.get("nickname")
                or sender.get("card")
                or node_data.get("nickname")
                or node_data.get("name")
                or node.get("nickname")
                or node.get("name")
                or sender_id
                or "未知用户"
            )
            raw_content = (
                node.get("message")
                or node.get("content")
                or node_data.get("content")
                or node_data.get("message")
                or []
            )
            node_result = await self.onebot_content_to_result(
                raw_content,
                defer_caption=True,
                event=event,
                forward_ctx=forward_ctx,
                depth=depth + 1,
            )
            result.forward_messages.extend(node_result.forward_messages)
            node_items.append(
                {
                    "index": index,
                    "sender_name": str(sender_name),
                    "sender_id": str(sender_id or ""),
                    "time": str(node.get("time") or node_data.get("time") or ""),
                    "content": node_result.content,
                }
            )

        block = {
            "source": "remote" if source_id else "onebot",
            "source_id": source_id,
            "nodes": node_items,
            "truncated": len(nodes) > MAX_FORWARD_NODE_COUNT,
        }
        block["id"] = self.make_forward_id(block)
        result.forward_messages.insert(0, block)
        result.content = f"[合并转发:{block['id']}]"
        return result

    async def onebot_content_to_result(
        self,
        raw_content,
        defer_caption: bool,
        event: AstrMessageEvent | None,
        forward_ctx: dict,
        depth: int,
    ) -> ChainParseResult:
        if isinstance(raw_content, list):
            if raw_content and all(
                isinstance(item, BaseMessageComponent) for item in raw_content
            ):
                return await self.chain_to_result(
                    raw_content,
                    defer_caption=defer_caption,
                    event=event,
                    _forward_ctx=forward_ctx,
                    _depth=depth,
                )
            if self.looks_like_forward_node_list(raw_content):
                return await self.onebot_nodes_to_forward_result(
                    raw_content,
                    source_id="",
                    defer_caption=defer_caption,
                    event=event,
                    forward_ctx=forward_ctx,
                    depth=depth,
                )
            return await self.onebot_segments_to_result(
                raw_content,
                defer_caption=defer_caption,
                event=event,
                forward_ctx=forward_ctx,
                depth=depth,
            )
        if isinstance(raw_content, dict):
            seg_type = str(raw_content.get("type") or "").lower()
            seg_data = (
                raw_content.get("data")
                if isinstance(raw_content.get("data"), dict)
                else {}
            )
            if not seg_type:
                for source in (raw_content, seg_data):
                    if not isinstance(source, dict):
                        continue
                    for key in ("nodes", "messages", "message", "content"):
                        if key not in source:
                            continue
                        nested = self.parse_json_text(source.get(key))
                        if nested is raw_content:
                            continue
                        return await self.onebot_content_to_result(
                            nested,
                            defer_caption=defer_caption,
                            event=event,
                            forward_ctx=forward_ctx,
                            depth=depth,
                        )
            return await self.onebot_segments_to_result(
                [raw_content],
                defer_caption=defer_caption,
                event=event,
                forward_ctx=forward_ctx,
                depth=depth,
            )
        if isinstance(raw_content, str):
            text = raw_content.strip()
            if not text:
                return ChainParseResult()
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                return await self.onebot_segments_to_result(
                    parsed,
                    defer_caption=defer_caption,
                    event=event,
                    forward_ctx=forward_ctx,
                    depth=depth,
                )
            if isinstance(parsed, dict):
                return await self.onebot_segments_to_result(
                    [parsed],
                    defer_caption=defer_caption,
                    event=event,
                    forward_ctx=forward_ctx,
                    depth=depth,
                )
            return ChainParseResult(content=text)
        return ChainParseResult()

    async def onebot_segments_to_result(
        self,
        segments: list,
        defer_caption: bool,
        event: AstrMessageEvent | None,
        forward_ctx: dict,
        depth: int,
    ) -> ChainParseResult:
        result = ChainParseResult()
        parts = []
        index = 0
        while index < len(segments):
            seg = segments[index]
            index += 1
            if not isinstance(seg, dict):
                continue
            seg_type = str(seg.get("type") or "").lower()
            seg_data = seg.get("data") if isinstance(seg.get("data"), dict) else {}

            if seg_type in ("text", "plain"):
                text = seg_data.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
            elif seg_type == "at":
                qq = seg_data.get("qq") or seg_data.get("user_id") or ""
                name = seg_data.get("name") or ""
                parts.append(f"@{name}({qq})" if name else f"@{qq}")
            elif seg_type == "image":
                url = seg_data.get("url") or ""
                file_name = seg_data.get("file") or seg_data.get("path") or ""
                part, media_result = await self.format_image_ref(
                    str(url or ""), str(file_name or ""), defer_caption, event=event
                )
                result.merge(media_result)
                parts.append(part)
            elif seg_type in ("record", "voice", "audio"):
                url = seg_data.get("url") or ""
                file_name = seg_data.get("file") or seg_data.get("path") or ""
                part, media_result = await self.format_audio_ref(
                    str(url or ""), str(file_name or ""), defer_caption, event=event
                )
                result.merge(media_result)
                parts.append(part)
            elif seg_type == "video":
                url = seg_data.get("url") or ""
                file_name = seg_data.get("file") or seg_data.get("path") or ""
                if self.format_video_ref:
                    part, media_result = await self.format_video_ref(
                        str(url or ""), str(file_name or ""), defer_caption, event=event
                    )
                    result.merge(media_result)
                    parts.append(part)
                else:
                    parts.append("[视频]")
            elif seg_type == "file":
                name = (
                    seg_data.get("name")
                    or seg_data.get("file_name")
                    or seg_data.get("file")
                    or "file"
                )
                parts.append(f"[文件:{name}]")
            elif seg_type in ("forward", "forward_msg"):
                fid = (
                    seg_data.get("id")
                    or seg_data.get("message_id")
                    or seg_data.get("resid")
                    or seg_data.get("m_resid")
                    or seg_data.get("forward_id")
                    or seg_data.get("msg_resid")
                )
                nested = (
                    self.parse_json_text(seg_data.get("content"))
                    or seg_data.get("nodes")
                    or seg_data.get("messages")
                    or seg_data.get("message")
                )
                if fid:
                    forward_result = await self.forward_id_to_result(
                        fid,
                        defer_caption=defer_caption,
                        event=event,
                        forward_ctx=forward_ctx,
                        depth=depth,
                    )
                    first_block = (
                        forward_result.forward_messages[0]
                        if forward_result.forward_messages
                        else {}
                    )
                    if first_block.get("unresolved") and nested:
                        nested_result = await self.onebot_content_to_result(
                            nested,
                            defer_caption=defer_caption,
                            event=event,
                            forward_ctx=forward_ctx,
                            depth=depth + 1,
                        )
                        if nested_result.forward_messages or nested_result.content:
                            if nested_result.forward_messages:
                                block_id = str(
                                    nested_result.forward_messages[0].get("id") or ""
                                )
                                if block_id:
                                    forward_ctx["remote_refs"][str(fid)] = block_id
                            result.merge(nested_result)
                            parts.append(
                                nested_result.content
                                if nested_result.content
                                else "[合并转发消息]"
                            )
                        else:
                            result.merge(forward_result)
                            parts.append(forward_result.content)
                    else:
                        result.merge(forward_result)
                        parts.append(forward_result.content)
                else:
                    nested_result = await self.onebot_content_to_result(
                        nested,
                        defer_caption=defer_caption,
                        event=event,
                        forward_ctx=forward_ctx,
                        depth=depth + 1,
                    )
                    result.merge(nested_result)
                    if nested_result.content:
                        parts.append(nested_result.content)
                    else:
                        parts.append("[合并转发消息]")
            elif seg_type == "node":
                nodes = [seg]
                while index < len(segments):
                    next_seg = segments[index]
                    if not isinstance(next_seg, dict):
                        break
                    if str(next_seg.get("type") or "").lower() != "node":
                        break
                    nodes.append(next_seg)
                    index += 1
                forward_result = await self.onebot_nodes_to_forward_result(
                    nodes,
                    source_id="",
                    defer_caption=defer_caption,
                    event=event,
                    forward_ctx=forward_ctx,
                    depth=depth,
                )
                result.merge(forward_result)
                parts.append(forward_result.content)
            elif seg_type == "nodes":
                nodes = (
                    seg_data.get("nodes")
                    or seg_data.get("messages")
                    or seg_data.get("message")
                    or []
                )
                if isinstance(nodes, list):
                    forward_result = await self.onebot_nodes_to_forward_result(
                        nodes,
                        source_id="",
                        defer_caption=defer_caption,
                        event=event,
                        forward_ctx=forward_ctx,
                        depth=depth,
                    )
                    result.merge(forward_result)
                    parts.append(forward_result.content)
            elif seg_type == "json":
                raw_json = seg_data.get("data") or seg_data
                forward_result = await self.json_to_forward_result(
                    raw_json,
                    defer_caption=defer_caption,
                    event=event,
                    forward_ctx=forward_ctx,
                    depth=depth,
                )
                if forward_result:
                    result.merge(forward_result)
                    parts.append(forward_result.content)
                else:
                    parts.append("[合并转发消息]")
        result.content = " ".join(part for part in parts if part).strip()
        return result

    def extract_json_forward_source_id(self, data: dict) -> str:
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        detail = meta.get("detail") if isinstance(meta.get("detail"), dict) else {}
        candidates = [detail, meta, data]
        for item in candidates:
            for key in (
                "resid",
                "m_resid",
                "forward_id",
                "message_id",
                "id",
                "msg_resid",
                "uniseq",
            ):
                value = item.get(key) if isinstance(item, dict) else None
                if isinstance(value, (str, int)) and str(value).strip():
                    return str(value).strip()
        return ""

    def extract_json_forward_preview_nodes(self, data: dict) -> list[dict]:
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        detail = meta.get("detail") if isinstance(meta.get("detail"), dict) else {}
        news_items = detail.get("news")
        if not isinstance(news_items, list):
            return []

        nodes = []
        for item in news_items:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            sender_name = item.get("name") or item.get("title") or ""
            nodes.append(
                {
                    "sender": {"nickname": sender_name},
                    "message": [{"type": "text", "data": {"text": text.strip()}}],
                }
            )
        return nodes

    def extract_json_forward_embedded_nodes(self, data: dict) -> list:
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        detail = meta.get("detail") if isinstance(meta.get("detail"), dict) else {}
        for item in (detail, meta, data):
            if not isinstance(item, dict):
                continue
            for key in ("nodes", "messages", "message", "content"):
                value = item.get(key)
                if isinstance(value, str):
                    try:
                        value = json.loads(value.strip().replace("&#44;", ","))
                    except Exception:
                        continue
                if isinstance(value, list):
                    return value
        return []

    async def json_to_forward_result(
        self,
        data,
        defer_caption: bool,
        event: AstrMessageEvent | None,
        forward_ctx: dict,
        depth: int,
    ) -> ChainParseResult | None:
        if isinstance(data, str):
            raw = data.strip().replace("&#44;", ",")
            try:
                data = json.loads(raw)
            except Exception:
                return None
        elif isinstance(data, dict) and "data" in data:
            inner = data.get("data")
            if isinstance(inner, str):
                try:
                    parsed_inner = json.loads(inner.strip().replace("&#44;", ","))
                    if isinstance(parsed_inner, dict):
                        data = parsed_inner
                except Exception:
                    pass
            elif isinstance(inner, dict):
                data = inner
        if not isinstance(data, dict):
            return None

        is_multimsg = data.get("app") == "com.tencent.multimsg" or data.get("view") in (
            "contact",
            "Forward",
        )
        if not is_multimsg:
            return None

        source_id = self.extract_json_forward_source_id(data)
        embedded_nodes = self.extract_json_forward_embedded_nodes(data)
        preview_nodes = self.extract_json_forward_preview_nodes(data)
        if source_id:
            fetched = await self.forward_id_to_result(
                source_id,
                defer_caption=defer_caption,
                event=event,
                forward_ctx=forward_ctx,
                depth=depth,
            )
            first_block = fetched.forward_messages[0] if fetched.forward_messages else {}
            if fetched.forward_messages and not first_block.get("unresolved"):
                return fetched
            if embedded_nodes:
                embedded_result = await self.onebot_nodes_to_forward_result(
                    embedded_nodes,
                    source_id=source_id,
                    defer_caption=defer_caption,
                    event=event,
                    forward_ctx=forward_ctx,
                    depth=depth,
                )
                if embedded_result.forward_messages:
                    forward_ctx["remote_refs"][source_id] = embedded_result.forward_messages[
                        0
                    ]["id"]
                return embedded_result
            if preview_nodes:
                preview_result = await self.onebot_nodes_to_forward_result(
                    preview_nodes,
                    source_id=source_id,
                    defer_caption=defer_caption,
                    event=event,
                    forward_ctx=forward_ctx,
                    depth=depth,
                )
                if preview_result.forward_messages:
                    forward_ctx["remote_refs"][source_id] = preview_result.forward_messages[
                        0
                    ]["id"]
                return preview_result
            return fetched
        if embedded_nodes:
            return await self.onebot_nodes_to_forward_result(
                embedded_nodes,
                source_id="",
                defer_caption=defer_caption,
                event=event,
                forward_ctx=forward_ctx,
                depth=depth,
            )
        if preview_nodes:
            return await self.onebot_nodes_to_forward_result(
                preview_nodes,
                source_id="",
                defer_caption=defer_caption,
                event=event,
                forward_ctx=forward_ctx,
                depth=depth,
            )
        return None
