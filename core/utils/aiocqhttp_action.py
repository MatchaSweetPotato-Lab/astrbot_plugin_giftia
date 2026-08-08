import copy
import random

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, File, Image, Plain, Record, Video
from astrbot.core.message.components import BaseMessageComponent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)


class AIoCQHTTPAction:
    """
    这个类用于处理AIOCQHTTP的互动
    """

    def __init__(self, sticker_summaries: list[str] | None = None):
        self.sticker_summaries = sticker_summaries or ["图片"]

    async def send_message(
        self,
        event: AstrMessageEvent,
        message_chain: list[BaseMessageComponent],
    ) -> tuple[bool, int | None]:
        """发送消息
        Args:
            event: 消息事件
            message_chain: 消息链
        """

        if event.get_platform_name() == "aiocqhttp" and isinstance(
            event, AiocqhttpMessageEvent
        ):
            try:
                message_data = await self._msg_chain_to_data(message_chain)
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
                logger.error(f"[Giftia] 发送消息失败: {e}")
                return False, None
        else:
            logger.warning("[Giftia] 发送消息失败: 当前仅支持aiocqhttp平台")
            return False, None

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
            logger.warning(f"[Giftia] 调用 OneBot 动作 {action_name} 失败: event.bot 不存在")
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

        logger.warning(f"[Giftia] OneBot 动作 {action_name} 所有调用路径均失败: params={params}")
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
                logger.warning(f"撤回消息失败: {str(e)}")
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
                    logger.warning(f"点赞失败: {str(e)}")
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
                logger.warning(f"踢出群成员失败: {str(e)}")
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
                logger.warning(f"提出群成员失败: {str(e)}")
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
                logger.warning(f"退群失败: {str(e)}")
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
                logger.warning(f"贴表情失败: {str(e)}")
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
                logger.warning(f"戳一戳失败: {str(e)}")
                return str(e)
        else:
            logger.warning("[Giftia] 当前仅支持aiocqhttp平台")
            return "当前仅支持aiocqhttp平台"

    async def _msg_chain_to_data(
        self,
        message_chain: list[BaseMessageComponent],
    ) -> list:
        """
        将消息链转换为aiocqhttp的数据结构
        """
        message_data: list = []
        for component in message_chain:
            if isinstance(component, Plain):
                if not component.text.strip():
                    continue
                # 检查前面是不是@，如果是@，添加\u200b字符
                if message_data and message_data[-1].get("type") == "at":
                    component.text = "\u200b " + component.text
                message_data.append(await component.to_dict())
            # 如果是@，也需要检查前面是不是@
            elif isinstance(component, At):
                if message_data and message_data[-1].get("type") == "at":
                    message_data.append(
                        {
                            "type": "text",
                            "data": {"text": "\u200b \u200b"},
                        }
                    )
                message_data.append(await component.to_dict())
            elif isinstance(component, Image | Record):
                # For Image and Record segments, we convert them to base64
                bs64 = await component.convert_to_base64()
                data_dict: dict = {
                    "file": f"base64://{bs64}",
                }
                if isinstance(component, Image):
                    # 同时兼容NapCat/Lagrange/go-cqhttp的命名规范
                    sub_type = getattr(component, "sub_type", None)
                    if sub_type is None:
                        sub_type = getattr(component, "subType", None)

                    if sub_type == 1:
                        # 显式标记为表情包/小图
                        data_dict["subType"] = 1
                        data_dict["sub_type"] = 1
                        data_dict["subtype"] = 1
                        data_dict["summary"] = random.choice(self.sticker_summaries)
                    else:
                        # 普通大图模式
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
