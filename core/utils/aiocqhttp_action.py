import asyncio
import copy
import inspect
import random
import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, File, Image, Plain, Record, Video
from astrbot.core.message.components import BaseMessageComponent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .event_utils import resolve_bot_name
from .schemas import ImageSendType


class AIoCQHTTPAction:
    """
    这个类用于处理AIOCQHTTP的互动
    """

    def __init__(
        self,
        sticker_summaries: list[str] | None = None,
        plugin=None,
    ):
        self.sticker_summaries = sticker_summaries or ["图片"]
        self.plugin = plugin

    async def send_message(
        self,
        event: AstrMessageEvent,
        message_chain: list[BaseMessageComponent],
        image_type: ImageSendType = ImageSendType.NORMAL,
    ) -> tuple[bool, int | None]:
        """发送消息
        Args:
            event: 消息事件
            message_chain: 消息链
            image_type: 图片发送模式 (默认为表情包小图)
        """

        if event.get_platform_name() == "aiocqhttp" and isinstance(
            event, AiocqhttpMessageEvent
        ):
            try:
                message_data = await self._msg_chain_to_data(
                    message_chain, image_type=image_type
                )
                group_id = event.get_group_id()
                if group_id:
                    resp = await event.bot.send_group_msg(
                        group_id=int(group_id), message=message_data
                    )
                else:
                    resp = await event.bot.send_private_msg(
                        user_id=int(event.get_sender_id()), message=message_data
                    )
                if resp and isinstance(resp, dict) and resp.get("message_id"):
                    message_id = resp["message_id"]
                    return True, message_id
                else:
                    logger.error(f"[Giftia] 发送消息失败: {resp}")
                    return True, None
            except Exception as e:
                # 兜底自愈机制：若捕获到被禁言异常（如离线/重启期间被禁言未收到 notice 事件），
                # 主动查询群成员禁言剩余时间并更新静默状态，实现故障自愈并阻断后续重复报错。
                if self._is_mute_or_ban_error(e):
                    group_id = event.get_group_id()
                    if (
                        group_id
                        and hasattr(self, "plugin")
                        and self.plugin
                        and hasattr(self.plugin, "data_cache")
                    ):
                        bot_name = resolve_bot_name(self.plugin, event)
                        if bot_name:
                            duration = 600
                            try:
                                info = await event.bot.get_group_member_info(
                                    group_id=int(group_id),
                                    user_id=int(event.get_self_id()),
                                    no_cache=True,
                                )
                                shut_up = int(info.get("shut_up_timestamp", 0) or 0)
                                now = int(time.time())
                                if shut_up > now:
                                    duration = shut_up - now
                            except Exception:
                                pass
                            self.plugin.data_cache.set_bot_muted(
                                bot_name, str(group_id), duration
                            )
                            logger.warning(
                                f"[Giftia] 消息发送被拒绝（账号已被禁言），Bot {bot_name} 在群 {group_id} 进入静默状态（约 {int(duration)} 秒）"
                            )
                logger.error(f"[Giftia] 发送消息失败: {e}")
                return False, None
        else:
            logger.warning("[Giftia] 发送消息失败: 当前仅支持aiocqhttp平台")
            return False, None

    def _is_mute_or_ban_error(self, exc: Exception) -> bool:
        """检查异常是否为 OneBot 禁言错误（ActionFailed.retcode == 1200）。

        作为【冷启动与自愈兜底】：当 Bot 离线重启期间被禁言未收到通知时，
        在首次发消息被拒时感知 retcode == 1200 并自动进入静默，防止后续持续报错。
        """
        return getattr(exc, "retcode", None) == 1200

    @staticmethod
    def _unwrap_action_response(payload) -> dict:
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            return payload["data"]
        return payload if isinstance(payload, dict) else {}

    async def _call_onebot_action(
        self,
        event: AstrMessageEvent,
        action_name: str,
        params: dict,
    ) -> dict | None:
        bot = getattr(event, "bot", None)
        if not bot:
            logger.warning(
                f"[Giftia] 调用 OneBot 动作 {action_name} 失败: event.bot 不存在"
            )
            return None

        routing_params = {}
        try:
            self_id = str(event.get_self_id() or "").strip()
        except Exception:
            self_id = ""
        if self_id:
            routing_params["self_id"] = self_id

        direct = getattr(bot, action_name, None)
        if callable(direct):
            try:
                payload = await direct(**params)
                if isinstance(payload, dict):
                    return payload
                logger.warning(
                    f"[Giftia] OneBot 动作 {action_name} direct 返回非 dict: {payload!r}"
                )
            except Exception as e:
                logger.warning(
                    f"[Giftia] OneBot 动作 {action_name} direct 调用失败: params={params}, error={e}"
                )

        api = getattr(bot, "api", None)
        callers = []
        if callable(getattr(api, "call_action", None)):
            callers.append(("bot.api.call_action", api.call_action))
        if callable(getattr(bot, "call_action", None)):
            callers.append(("bot.call_action", bot.call_action))

        call_params = dict(params)
        call_params.update(routing_params)
        for caller_name, caller in callers:
            try:
                payload = await caller(action=action_name, **call_params)
            except TypeError as e:
                logger.debug(
                    f"[Giftia] OneBot 动作 {action_name} 通过 {caller_name} 使用 action 关键字失败，尝试位置参数: {e}"
                )
                try:
                    payload = await caller(action_name, **call_params)
                except Exception as positional_error:
                    logger.warning(
                        f"[Giftia] OneBot 动作 {action_name} 通过 {caller_name} 调用失败: params={call_params}, error={positional_error}"
                    )
                    continue
            except Exception as e:
                logger.warning(
                    f"[Giftia] OneBot 动作 {action_name} 通过 {caller_name} 调用失败: params={call_params}, error={e}"
                )
                continue

            if isinstance(payload, dict):
                return payload
            logger.warning(
                f"[Giftia] OneBot 动作 {action_name} 通过 {caller_name} 返回非 dict: {payload!r}"
            )

        logger.warning(
            f"[Giftia] OneBot 动作 {action_name} 所有调用路径均失败: params={params}"
        )
        return None

    @staticmethod
    def _extract_repeat_message_data(payload: dict):
        data = AIoCQHTTPAction._unwrap_action_response(payload)
        message_data = (
            data.get("message")
            if data.get("message") is not None
            else data.get("messages")
        )
        if message_data is None:
            message_data = data.get("raw_message")
        if isinstance(message_data, list):
            clean_message = [
                seg for seg in copy.deepcopy(message_data) if isinstance(seg, dict)
            ]
            return clean_message or None
        if isinstance(message_data, str):
            return message_data if message_data.strip() else None
        return None

    async def repeat_message(
        self,
        event: AstrMessageEvent,
        message_id: int,
    ) -> tuple[bool, int | None, str | None]:
        """原样复读一条 OneBot 消息。调用方负责校验消息是否在上下文窗口内。"""
        if not (
            event.get_platform_name() == "aiocqhttp"
            and isinstance(event, AiocqhttpMessageEvent)
        ):
            logger.warning("[Giftia] 复读消息失败: 当前仅支持aiocqhttp平台")
            return False, None, "当前仅支持aiocqhttp平台"

        try:
            payload = await self._call_onebot_action(
                event, "get_msg", {"message_id": message_id}
            )
            if not payload:
                return False, None, "获取原消息失败"

            message_data = self._extract_repeat_message_data(payload)
            if message_data is None:
                return False, None, "原消息为空或暂不支持复读"

            group_id = event.get_group_id()
            if group_id:
                resp = await event.bot.send_group_msg(
                    group_id=int(group_id), message=message_data
                )
            else:
                resp = await event.bot.send_private_msg(
                    user_id=int(event.get_sender_id()), message=message_data
                )

            resp_data = self._unwrap_action_response(resp)
            if resp_data and resp_data.get("message_id"):
                return True, resp_data["message_id"], None
            if isinstance(resp, dict) and resp.get("message_id"):
                return True, resp["message_id"], None
            logger.warning(f"[Giftia] 复读消息已发送但未返回 message_id: {resp}")
            return True, None, "平台未返回message_id，无法写入复读消息记录"
        except Exception as e:
            logger.error(f"[Giftia] 复读消息失败: {e}", exc_info=True)
            return False, None, str(e)

    async def delete_messages(
        self, event: AstrMessageEvent, message_ids: list[int]
    ) -> str | None:
        """撤回消息"""
        if event.get_platform_name() == "aiocqhttp" and isinstance(
            event, AiocqhttpMessageEvent
        ):
            try:
                for message_id in message_ids:
                    await event.bot.delete_msg(message_id=message_id)
                return None
            except Exception as e:
                logger.warning(f"撤回消息失败: {e!s}")
                return str(e)
        else:
            logger.warning("[Giftia] 撤回消息失败: 当前仅支持aiocqhttp平台")
            return "当前仅支持aiocqhttp平台"

    async def like(
        self, event: AstrMessageEvent, user_id: int, count: int
    ) -> str | None:
        """点赞"""
        if event.get_platform_name() == "aiocqhttp" and isinstance(
            event, AiocqhttpMessageEvent
        ):
            # 超过50个赞截断成50个
            total_likes = min(count, 50)
            # 计算分组
            full_groups = total_likes // 10
            remainder = total_likes % 10
            batches = [10] * full_groups
            if remainder > 0:
                batches.append(remainder)
            for index, count in enumerate(batches):
                try:
                    await event.bot.send_like(user_id=user_id, times=count)
                except Exception as e:
                    logger.warning(f"点赞失败: {e!s}")
                    # 如果是第一次点赞失败，返回错误信息
                    if index == 0:
                        return str(e)
                    return None
            return None
        else:
            logger.warning("[Giftia] 点赞失败: 仅支持aiocqhttp平台")
            return "当前仅支持aiocqhttp平台"

    async def group_kick(
        self,
        event: AstrMessageEvent,
        group_id: int,
        user_id: int,
        reject_add_request=False,
    ) -> str | None:
        """踢出群成员"""
        if event.get_platform_name() == "aiocqhttp" and isinstance(
            event, AiocqhttpMessageEvent
        ):
            try:
                await event.bot.set_group_kick(
                    group_id=group_id,
                    user_id=user_id,
                    reject_add_request=reject_add_request,
                )
                return None
            except Exception as e:
                logger.warning(f"踢出群成员失败: {e!s}")
                return str(e)
        else:
            logger.warning("[Giftia] 当前仅支持aiocqhttp平台")
            return "当前仅支持aiocqhttp平台"

    async def group_ban(
        self,
        event: AstrMessageEvent,
        group_id: int,
        user_id: int,
        duration: int = 30 * 60,
    ) -> str | None:
        """禁言"""
        if event.get_platform_name() == "aiocqhttp" and isinstance(
            event, AiocqhttpMessageEvent
        ):
            try:
                await event.bot.set_group_ban(
                    group_id=group_id,
                    user_id=user_id,
                    duration=duration,
                )
                return None
            except Exception as e:
                logger.warning(f"提出群成员失败: {e!s}")
                return str(e)
        else:
            logger.warning("[Giftia] 当前仅支持aiocqhttp平台")
            return "当前仅支持aiocqhttp平台"

    async def group_leave(self, event: AstrMessageEvent, group_id: int) -> str | None:
        """退群"""
        if event.get_platform_name() == "aiocqhttp" and isinstance(
            event, AiocqhttpMessageEvent
        ):
            try:
                await event.bot.set_group_leave(group_id=group_id)
                return None
            except Exception as e:
                logger.warning(f"退群失败: {e!s}")
                return str(e)
        else:
            logger.warning("[Giftia] 当前仅支持aiocqhttp平台")
            return "当前仅支持aiocqhttp平台"

    async def msg_emoji_like(
        self,
        event: AstrMessageEvent,
        message_id: int,
        emoji_id: int,
        set=True,
    ):
        """贴表情"""
        if event.get_platform_name() == "aiocqhttp" and isinstance(
            event, AiocqhttpMessageEvent
        ):
            try:
                await event.bot.set_msg_emoji_like(
                    message_id=message_id,
                    emoji_id=emoji_id,
                    set=set,
                )
                return None
            except Exception as e:
                logger.warning(f"贴表情失败: {e!s}")
                return str(e)
        else:
            logger.warning("[Giftia] 当前仅支持aiocqhttp平台")
            return "当前仅支持aiocqhttp平台"

    async def group_poke(
        self,
        event: AstrMessageEvent,
        group_id: int,
        user_id: int,
    ) -> str | None:
        """戳一戳"""
        if event.get_platform_name() == "aiocqhttp" and isinstance(
            event, AiocqhttpMessageEvent
        ):
            try:
                # logger.info(f"尝试戳一戳: group_id={group_id}, user_id={user_id}")
                await event.bot.group_poke(
                    group_id=group_id,
                    user_id=user_id,
                )
                return None
            except Exception as e:
                logger.warning(f"戳一戳失败: {e!s}")
                return str(e)
        else:
            logger.warning("[Giftia] 当前仅支持aiocqhttp平台")
            return "当前仅支持aiocqhttp平台"

    @staticmethod
    async def _make_text_segment(component: Plain, text: str) -> dict:
        try:
            res = component.to_dict()
            d = (
                await res
                if (asyncio.iscoroutine(res) or inspect.isawaitable(res))
                else res
            )
            if isinstance(d, dict):
                d = copy.deepcopy(d)
                if "data" in d and isinstance(d["data"], dict):
                    d["data"]["text"] = text
                else:
                    d["data"] = {"text": text}
                return d
        except Exception:
            pass
        return {"type": "text", "data": {"text": text}}

    async def _parse_plain_inline_ats(
        self,
        component: Plain,
        last_was_at: bool,
    ) -> tuple[list[dict], bool]:
        """
        解析 Plain 文本中内嵌的 `<at id/user_id/qq="..." />` 语法。

        支持的语法结构:
        - `<at user_id="12345" />` / `<at qq="12345" />` / `<at id="12345" />`

        处理规则:
        1. 当紧邻前一个组件为 at 时，自动在文本前缝合 '\u200b ' 零宽空格，防止客户端合并 @。
        2. 返回解析后的字典片段列表，以及更新后的 last_was_at 标记（末尾是否为 at）。
        """
        segments: list[dict] = []
        text = component.text
        if not text:
            return segments, last_was_at

        at_pattern = r'<at\s+(?:id|user_id|qq)=["\']?([^"\'\s/>]+)["\']?\s*/?>'
        matches = list(re.finditer(at_pattern, text))
        if matches:
            last_end = 0
            for match in matches:
                start, end = match.span()
                plain_part = text[last_end:start]
                if plain_part:
                    if last_was_at:
                        plain_part = "\u200b " + plain_part
                    segments.append(
                        await self._make_text_segment(component, plain_part)
                    )
                    last_was_at = False

                target_qq = match.group(1)
                if last_was_at:
                    segments.append(
                        await self._make_text_segment(
                            Plain("\u200b \u200b"), "\u200b \u200b"
                        )
                    )
                segments.append({"type": "at", "data": {"qq": str(target_qq)}})
                last_was_at = True
                last_end = end

            remaining = text[last_end:]
            if remaining:
                if last_was_at:
                    remaining = "\u200b " + remaining
                segments.append(await self._make_text_segment(component, remaining))
                last_was_at = False
        else:
            if last_was_at:
                text = "\u200b " + text
            segments.append(await self._make_text_segment(component, text))
            last_was_at = False

        return segments, last_was_at

    async def _msg_chain_to_data(
        self,
        message_chain: list[BaseMessageComponent],
        image_type: ImageSendType = ImageSendType.NORMAL,
    ) -> list:
        """
        将消息链转换为aiocqhttp的数据结构
        """
        message_data: list = []
        last_was_at = False
        for component in message_chain:
            if isinstance(component, Plain):
                parsed_segs, last_was_at = await self._parse_plain_inline_ats(
                    component, last_was_at
                )
                message_data.extend(parsed_segs)
            # 如果是@，也需要检查前面是不是@
            elif isinstance(component, At):
                if last_was_at:
                    message_data.append(
                        {
                            "type": "text",
                            "data": {"text": "\u200b \u200b"},
                        }
                    )
                message_data.append(await component.to_dict())
                last_was_at = True
            elif isinstance(component, Image | Record):
                last_was_at = False
                # For Image and Record segments, we convert them to base64
                bs64 = await component.convert_to_base64()
                data_dict: dict = {
                    "file": f"base64://{bs64}",
                }
                if isinstance(component, Image):
                    if image_type == ImageSendType.STICKER:
                        # 显式标记为表情包/小图 (同时兼容NapCat/Lagrange/go-cqhttp的命名规范)
                        data_dict["subType"] = 1
                        data_dict["sub_type"] = 1
                        data_dict["subtype"] = 1
                        data_dict["summary"] = random.choice(self.sticker_summaries)
                    else:
                        # 普通大图/原图模式
                        data_dict["subType"] = 0
                        data_dict["sub_type"] = 0
                        data_dict["subtype"] = 0
                message_data.append(
                    {
                        "type": component.type.lower(),
                        "data": data_dict,
                    }
                )
            elif isinstance(component, File):
                # For File segments, we need to handle the file differently
                d = await component.to_dict()
                message_data.append(d)
            elif isinstance(component, Video):
                d = await component.to_dict()
                message_data.append(d)
            else:
                message_data.append(await component.to_dict())
        return message_data
