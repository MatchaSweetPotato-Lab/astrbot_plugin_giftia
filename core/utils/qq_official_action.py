import aiohttp
import asyncio
import copy
import datetime
import random
import re
from typing import Any, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, File, Image, Plain, Record, Reply, Video
from astrbot.core.message.components import BaseMessageComponent

QQ_OFFICIAL_PLATFORMS = {
    "qqofficial",
    "qqofficial_webhook",
    "qq_official",
    "qqchannel",
}


def is_qq_official(target: Any) -> bool:
    """判断传入的 event 对象或 platform_name 字符串是否对应官方 QQ 平台"""
    if not target:
        return False
    if isinstance(target, str):
        return target.strip().lower() in QQ_OFFICIAL_PLATFORMS
    try:
        if hasattr(target, "get_platform_name"):
            pname = str(target.get_platform_name() or "").strip().lower()
            if pname in QQ_OFFICIAL_PLATFORMS:
                return True
        cls_name = target.__class__.__name__.lower()
        return "qqofficial" in cls_name or "qqchannel" in cls_name
    except Exception:
        return False


class QQOfficialAction:
    """
    该类用于处理官方 QQ Open API (qqofficial / qqchannel / qq_official) 的互动操作，
    包括：
    1. 引用回复 (POST /v2/groups/{group_openid}/messages 携带 message_reference)
    2. AT 群友 (格式化为 <qqbot-at-user id="{user_openid}" />)
    3. 禁言用户 (POST /v2/groups/{group_openid}/restrict_chat_setting)
    4. 撤回消息 (DELETE /v2/groups/{group_openid}/messages/{message_id})
    """

    def __init__(self, sticker_summaries: list[str] | None = None, max_cache_size: int = 2000):
        self.sticker_summaries = sticker_summaries or ["图片"]
        self._msg_id_to_idx: dict[str, str] = {}
        self._max_cache_size = max_cache_size

    def record_msg_mapping(self, msg_id: str | None, msg_idx: str | None) -> None:
        """记录消息 msg_id (ROBOT1.0_...) 到 引用索引 msg_idx (REFIDX_...) 的映射"""
        if not msg_id or not msg_idx:
            return
        m_id = str(msg_id).strip()
        m_idx = str(msg_idx).strip()
        if m_id and m_idx:
            if len(self._msg_id_to_idx) >= self._max_cache_size:
                keys_to_remove = list(self._msg_id_to_idx.keys())[: self._max_cache_size // 2]
                for k in keys_to_remove:
                    self._msg_id_to_idx.pop(k, None)
            self._msg_id_to_idx[m_id] = m_idx
            logger.debug(f"[QQOfficial] 记录引用映射: {m_id[:25]}... -> {m_idx}")

    def resolve_quote_id(self, ref_id: str | None) -> str | None:
        """解析引用 ID，若映射表中存在对应的 msg_idx 则优先替换为 msg_idx，否则返回原 ref_id"""
        if not ref_id:
            return None
        r_id = str(ref_id).strip()
        if r_id in self._msg_id_to_idx:
            resolved = self._msg_id_to_idx[r_id]
            logger.debug(f"[QQOfficial] 引用 ID 成功映射转换: {r_id[:25]}... -> {resolved}")
            return resolved
        return r_id

    @staticmethod
    def is_qq_official(event: AstrMessageEvent) -> bool:
        """判断当前事件是否来自官方 QQ 平台"""
        return is_qq_official(event)

    @staticmethod
    def extract_msg_id_and_idx(event: AstrMessageEvent) -> tuple[str | None, str | None]:
        """
        从官方 QQ 事件中提取消息 ID (id) 与 消息索引 (msg_idx，从 message_scene.ext 数组中解析)
        """
        if not event:
            return None, None

        msg_obj = getattr(event, "message_obj", None)
        msg_id = getattr(msg_obj, "message_id", None) if msg_obj else None

        raw_data = None
        if msg_obj:
            raw_data = getattr(msg_obj, "raw_data", None)
            if not raw_data:
                raw_msg = getattr(msg_obj, "raw_message", None)
                if raw_msg:
                    raw_data = getattr(raw_msg, "raw_data", None) or raw_msg
        if not raw_data:
            raw_data = getattr(event, "raw_data", None) or getattr(event, "raw_message", None)

        if isinstance(raw_data, dict) and not msg_id:
            msg_id = raw_data.get("id")

        msg_idx = None
        message_scene = None
        if isinstance(raw_data, dict):
            message_scene = raw_data.get("message_scene")
        elif hasattr(raw_data, "message_scene"):
            message_scene = getattr(raw_data, "message_scene")

        if message_scene:
            ext_list = None
            if isinstance(message_scene, dict):
                ext_list = message_scene.get("ext")
            elif hasattr(message_scene, "ext"):
                ext_list = getattr(message_scene, "ext")

            if isinstance(ext_list, list):
                for item in ext_list:
                    item_str = str(item)
                    if item_str.startswith("msg_idx="):
                        msg_idx = item_str.split("msg_idx=", 1)[1]
                        break

        return str(msg_id) if msg_id else None, msg_idx

    @staticmethod
    def _extract_bot_api(event: AstrMessageEvent) -> Any:
        """从 event 中提取 bot 的 OpenAPI 实例或 client 实例"""
        bot = getattr(event, "bot", None)
        if not bot:
            return None
        api = getattr(bot, "api", None)
        if api:
            return api
        client = getattr(bot, "client", None)
        if client and hasattr(client, "api"):
            return client.api
        return bot

    async def _request_via_botpy_route(
        self, http_obj: Any, method: str, path: str, json_data: dict | None
    ) -> tuple[bool, dict | None]:
        """尝试通过 botpy.http.Route + http_obj.request 发起请求"""
        request_func = getattr(http_obj, "request", None)
        if not callable(request_func):
            return False, None
        try:
            from botpy.http import Route
            route = Route(method, path)
            res = await request_func(route, json=json_data)
            return True, res if isinstance(res, dict) else {}
        except (ImportError, ModuleNotFoundError):
            logger.debug(
                f"[QQOfficial] 未安装 botpy 模块，降级走 aiohttp REST 请求 [{method} {path}]"
            )
        except Exception as err_route:
            logger.debug(
                f"[QQOfficial] botpy Route 请求尝试失败 [{method} {path} | payload={json_data}]: {err_route}"
            )
        return False, None

    async def _request_via_aiohttp_session(
        self, http_obj: Any, api: Any, method: str, path: str, json_data: dict | None
    ) -> tuple[bool, dict | None]:
        """通过 aiohttp ClientSession 发起 REST 请求兜底"""
        session = getattr(http_obj, "_session", None) or getattr(http_obj, "session", None)
        headers = (
            getattr(http_obj, "_headers", None)
            or getattr(http_obj, "headers", None)
            or getattr(api, "headers", None)
            or {}
        )
        if hasattr(http_obj, "headers_get") and callable(http_obj.headers_get):
            get_h = http_obj.headers_get
            raw_headers = await get_h() if asyncio.iscoroutinefunction(get_h) else get_h()
            headers = raw_headers if isinstance(raw_headers, dict) else {}

        if not (session and hasattr(session, "request")):
            return False, None

        base_url = (
            getattr(http_obj, "base_url", None)
            or getattr(api, "base_url", None)
            or "https://api.sgroup.qq.com"
        )
        url = path if path.startswith("http") else f"{str(base_url).rstrip('/')}/{path.lstrip('/')}"
        try:
            async with session.request(method, url, headers=headers, json=json_data) as resp:
                if resp.status == 204:
                    return True, {}
                if resp.status in (200, 201, 202):
                    text = await resp.text()
                    if not text or not text.strip():
                        return True, {}
                    try:
                        import json
                        return True, json.loads(text)
                    except Exception as err_json:
                        logger.warning(
                            f"[QQOfficial] REST 请求成功 [{method} {url} | HTTP {resp.status}] 但 JSON 解析响应失败: {err_json}"
                        )
                        return True, {}
                else:
                    text = await resp.text()
                    logger.warning(
                        f"[QQOfficial] REST 请求失败 [{method} {url} | HTTP {resp.status}]: {text} | Payload: {json_data}"
                    )
                    return True, None
        except Exception as e:
            if isinstance(e, aiohttp.ClientError):
                logger.warning(f"[QQOfficial] HTTP REST 网络传输故障 [{method} {path}]: {e}")
            else:
                logger.error(
                    f"[QQOfficial] HTTP REST 请求遭遇未预期异常 [{method} {path} | payload={json_data}]: {e}",
                    exc_info=True,
                )
            return True, None

    async def _post_rest_api(
        self,
        event: AstrMessageEvent,
        method: str,
        path: str,
        json_data: dict | None = None,
    ) -> dict | None:
        """
        向腾讯官方 Open API 发起 HTTP REST 请求
        使用 botpy 的 _http.request（包含 Authorization 鉴权头自动刷新与注入）
        """
        api = self._extract_bot_api(event)
        if not api:
            logger.warning(
                f"[QQOfficial] 发起 REST 请求 [{method} {path}] 失败: 未能识别 bot API 实例"
            )
            return None

        http_obj = getattr(api, "_http", None) or getattr(api, "http", None)
        if not http_obj and hasattr(event, "bot"):
            http_obj = getattr(event.bot, "_http", None) or getattr(event.bot, "http", None)
        if not http_obj:
            http_obj = api

        # 刷新 session 与 动态 OAuth 鉴权 Token
        if hasattr(http_obj, "check_session") and callable(http_obj.check_session):
            try:
                check = http_obj.check_session
                if asyncio.iscoroutinefunction(check):
                    await check()
                else:
                    check()
            except Exception as e:
                logger.debug(f"[QQOfficial] check_session 刷鉴权头异常 [{method} {path}]: {e}")

        # 1. 优先尝试使用 botpy.http.Route + http_obj.request
        ok, res = await self._request_via_botpy_route(http_obj, method, path, json_data)
        if ok:
            return res

        # 2. 降级：从 http_obj 或 api 中获取 aiohttp ClientSession
        ok, res = await self._request_via_aiohttp_session(http_obj, api, method, path, json_data)
        if ok:
            return res

        logger.warning(
            f"[QQOfficial] 当前 bot.api 未找到可用的 HTTP 请求通道 [{method} {path}]"
        )
        return None

    def _msg_chain_to_content_and_components(
        self,
        message_chain: list[BaseMessageComponent],
    ) -> tuple[str, list[BaseMessageComponent], bool, str | None]:
        """
        解析消息链：
        - Plain: 组合为文本
        - At: 转换为官方格式 `<qqbot-at-user id="{user_openid}" />`
        - 其他组件（图片/语音/视频/文件等）保留在组件列表中
        """
        text_parts = []
        other_components = []
        has_at = False
        chain_quote_id = None
        for component in message_chain:
            if isinstance(component, Plain):
                t = component.text
                if t:
                    if re.search(r'<(?:qqbot-at-user|at)\b', t):
                        has_at = True
                    t = re.sub(
                        r'<at\s+(?:user_id|qq|id)=["\']?([^"\'\s/>]+)["\']?\s*/?>',
                        r'<qqbot-at-user id="\1" />',
                        t,
                    )
                    text_parts.append(t)
            elif isinstance(component, At):
                has_at = True
                target_id = (
                    getattr(component, "qq", None)
                    or getattr(component, "user_id", None)
                    or getattr(component, "target", None)
                    or ""
                )
                target_str = str(target_id).strip()
                if target_str:
                    text_parts.append(f'<qqbot-at-user id="{target_str}" />')
            elif isinstance(component, Reply):
                reply_id = getattr(component, "id", None) or getattr(component, "message_id", None)
                if reply_id:
                    chain_quote_id = str(reply_id).strip()
                    logger.debug(f"[QQOfficial] 从消息链中成功提取 Reply 引用 ID: {chain_quote_id}")
            else:
                other_components.append(component)

        plain_text = "".join(text_parts)
        return plain_text, other_components, has_at, chain_quote_id

    async def send_message(
        self,
        event: AstrMessageEvent,
        message_chain: list[BaseMessageComponent],
        quote_message_id: str | None = None,
        use_markdown: bool = False,
    ) -> tuple[bool, str | None]:
        """
        发送官方 QQ 消息
        支持：引用回复 (message_reference) 与 AT 群友 (<qqbot-at-user id="..." />，要求 msg_type=2 Markdown 格式)
        """
        if not self.is_qq_official(event):
            logger.warning("[QQOfficial] 发送消息失败: 当前事件非官方 QQ 平台")
            return False, None

        api = self._extract_bot_api(event)
        if not api:
            logger.warning("[QQOfficial] 发送消息失败: 未获取到 bot.api 实例")
            return False, None

        try:
            logger.debug(
                f"[QQOfficial] 准备发送消息 | 原始组件数: {len(message_chain)} | quote_message_id: {quote_message_id}"
            )
            (
                plain_text,
                other_comps,
                has_at,
                chain_quote_id,
            ) = self._msg_chain_to_content_and_components(message_chain)

            if other_comps:
                logger.debug(
                    f"[QQOfficial] 消息链包含 {len(other_comps)} 个富媒体组件，QQOfficialAction 返回 (False, None) 以触发上层原生降级发送"
                )
                return False, None

            group_id = event.get_group_id()

            # 引用回复 Payload 拼装 (将 msg_id 转换映射为官方引用要求的 msg_idx)
            message_reference = None
            ref_id = quote_message_id or chain_quote_id
            if ref_id:
                resolved_ref_id = self.resolve_quote_id(ref_id)
                message_reference = {
                    "message_id": str(resolved_ref_id),
                    "ignore_get_message_error": True,
                }

            base_payload: dict[str, Any] = {
                "msg_seq": random.randint(1, 10000),
            }
            if message_reference:
                base_payload["message_reference"] = message_reference

            # 被动回复 msg_id
            passive_msg_id = getattr(event, "_giftia_reply_msg_id", None)
            if not passive_msg_id and hasattr(event, "message_obj") and event.message_obj:
                passive_msg_id = getattr(event.message_obj, "message_id", None)
            if passive_msg_id:
                base_payload["msg_id"] = str(passive_msg_id)

            # 官方 QQ 要求包含 <qqbot-at-user ... /> 时使用 msg_type=2 (Markdown 格式) 才能成功渲染 AT 组件
            should_use_md = has_at or use_markdown
            payload_to_send = dict(base_payload)
            if should_use_md:
                payload_to_send["msg_type"] = 2
                payload_to_send["markdown"] = {"content": plain_text}
                payload_to_send["content"] = plain_text
            else:
                payload_to_send["msg_type"] = 0
                payload_to_send["content"] = plain_text

            logger.debug(
                f"[QQOfficial] 最终发送 Payload (group_id={group_id}, ref_id={ref_id}, has_at={has_at}): {payload_to_send}"
            )

            async def _do_send(p: dict) -> Any:
                if group_id:
                    post_group_msg = getattr(api, "post_group_message", None)
                    if callable(post_group_msg):
                        return await post_group_msg(
                            group_openid=str(group_id),
                            **p,
                        )
                    path = f"/v2/groups/{group_id}/messages"
                    return await self._post_rest_api(event, "POST", path, json_data=p)
                else:
                    sender_id = event.get_sender_id()
                    post_c2c_msg = getattr(api, "post_c2c_message", None)
                    if callable(post_c2c_msg):
                        return await post_c2c_msg(
                            openid=str(sender_id),
                            **p,
                        )
                    path = f"/v2/users/{sender_id}/messages"
                    return await self._post_rest_api(event, "POST", path, json_data=p)

            resp = None
            try:
                resp = await _do_send(payload_to_send)
                logger.debug(f"[QQOfficial] 消息发送成功，响应结果: {resp}")
            except Exception as err_send:
                if should_use_md:
                    logger.warning(
                        f"[QQOfficial] Markdown(msg_type=2) 发送失败，尝试降级为 plain text (msg_type=0): {err_send}"
                    )
                    fallback_payload = dict(base_payload)
                    fallback_payload["msg_type"] = 0
                    fallback_payload["content"] = plain_text
                    resp = await _do_send(fallback_payload)
                else:
                    raise err_send

            msg_id = None
            if resp:
                if isinstance(resp, dict):
                    msg_id = resp.get("id") or resp.get("message_id")
                elif hasattr(resp, "id"):
                    msg_id = getattr(resp, "id", None)
                elif hasattr(resp, "message_id"):
                    msg_id = getattr(resp, "message_id", None)

            return True, str(msg_id) if msg_id else None

        except Exception as e:
            logger.error(f"[QQOfficial] 发送消息失败: {e}", exc_info=True)
            return False, None

    async def delete_messages(
        self, event: AstrMessageEvent, message_ids: list[str | int]
    ) -> str | None:
        """
        撤回自身消息 (DELETE /v2/groups/{group_openid}/messages/{message_id})
        """
        if not self.is_qq_official(event):
            return "当前非官方QQ平台"

        api = self._extract_bot_api(event)
        group_id = event.get_group_id()
        if not group_id:
            return "仅支持在群聊中撤回消息"

        err_list = []
        for msg_id in message_ids:
            try:
                delete_group_msg = getattr(api, "delete_group_message", None) or getattr(
                    api, "delete_message", None
                )
                if callable(delete_group_msg):
                    await delete_group_msg(
                        group_openid=str(group_id), message_id=str(msg_id)
                    )
                else:
                    path = f"/v2/groups/{group_id}/messages/{msg_id}"
                    await self._post_rest_api(event, "DELETE", path)
            except Exception as e:
                logger.warning(f"[QQOfficial] 撤回消息 {msg_id} 失败: {e}")
                err_list.append(str(e))

        return "; ".join(err_list) if err_list else None

    async def group_ban(
        self,
        event: AstrMessageEvent,
        group_id: str | int,
        user_id: str | int,
        duration: int = 30 * 60,
    ) -> str | None:
        """
        禁言用户 (POST /v2/groups/{group_openid}/restrict_chat_setting)
        body: {"mute_user": {"user_openid": "xxx", "mute_seconds": "300"}}
        """
        if not self.is_qq_official(event):
            return "当前非官方QQ平台"

        api = self._extract_bot_api(event)
        target_group = str(group_id or event.get_group_id() or "")
        target_user = str(user_id or "")

        if not target_group or not target_user:
            return "缺少 group_id 或 user_id"

        try:
            duration_sec = int(duration)
        except (ValueError, TypeError):
            duration_sec = 1800

        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
        if duration_sec > 0:
            op = "add"
            expire_dt = now + datetime.timedelta(seconds=duration_sec)
            expire_str = expire_dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        else:
            op = "del"
            expire_str = "0"

        payload = {
            "members": [
                {
                    "op": op,
                    "member_openid": target_user,
                    "mute_expire_at": expire_str,
                }
            ],
            "mute_user": {
                "user_openid": target_user,
                "mute_seconds": duration_sec,
            },
        }

        try:
            ban_func = (
                getattr(api, "post_group_restrict_chat_setting", None)
                or getattr(api, "post_group_restrict_chat", None)
                or getattr(api, "mute_group_member", None)
                or getattr(api, "restrict_chat_setting", None)
            )
            if callable(ban_func):
                await ban_func(group_openid=target_group, **payload)
            else:
                path = f"/v2/groups/{target_group}/restrict_chat_setting"
                await self._post_rest_api(event, "POST", path, json_data=payload)
            return None
        except Exception as e:
            logger.warning(f"[QQOfficial] 禁言失败: {e}")
            return str(e)

    async def group_kick(
        self,
        event: AstrMessageEvent,
        group_id: str | int,
        user_id: str | int,
        reject_add_request: bool = False,
    ) -> str | None:
        """踢出群成员（官方 QQ API 不支持此操作）"""
        logger.warning("[QQOfficial] 官方 QQ 平台暂不支持踢人 (group_kick) 操作")
        return "官方 QQ 平台暂不支持踢人操作"

    async def repeat_message(
        self,
        event: AstrMessageEvent,
        message_id: int | str,
    ) -> tuple[bool, str | None, str | None]:
        """复读消息（官方 QQ 不支持原样拉取其他消息进行复读）"""
        return False, None, "官方 QQ 平台暂不支持复读原消息"

    async def like(
        self, event: AstrMessageEvent, user_id: str | int, count: int
    ) -> str | None:
        """点赞（官方 QQ 个人名片点赞暂不支持）"""
        return "官方 QQ 平台暂不支持点赞操作"

    async def msg_emoji_like(
        self,
        event: AstrMessageEvent,
        message_id: str | int,
        emoji_id: int,
        set: bool = True,
    ) -> str | None:
        """消息贴表情"""
        return "官方 QQ 平台暂不支持消息贴表情操作"

    async def group_poke(
        self,
        event: AstrMessageEvent,
        group_id: str | int,
        user_id: str | int,
    ) -> str | None:
        """戳一戳（官方 QQ 暂不支持此操作）"""
        return "官方 QQ 平台暂不支持戳一戳操作"

    async def group_leave(self, event: AstrMessageEvent, group_id: str | int) -> str | None:
        """退群（官方 QQ 暂不支持此操作）"""
        return "官方 QQ 平台暂不支持退群操作"
