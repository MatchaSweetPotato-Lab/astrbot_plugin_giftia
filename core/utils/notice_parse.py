from dataclasses import dataclass
from typing import Callable, Coroutine, Any
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain
from .emoji_constants import EMOJI_MAP

# 交互/反应类通知类型常量列表
REACTION_NOTICE_TYPES = [
    "group_react",
    "friend_react",
    "reaction",
    "group_reaction",
    "friend_reaction",
    "group_msg_emoji_like",
    "friend_msg_emoji_like",
]


@dataclass
class NoticeParseResult:
    """系统通知事件解析结果结构化数据"""

    is_notice: bool = False
    content: str = ""
    sender_id: str | None = None
    sender_name: str | None = None
    role: str = "message"  # "system" | "message"

    # 是否为禁言相关通知（由上层业务层如 ChatManager 执行 data_cache 状态更新）
    is_ban_event: bool = False
    is_lift_ban_event: bool = False
    is_all_member_ban: bool = False
    is_target_self: bool = False
    duration: int = 0
    group_id: str = ""
    target_id: str = ""
    operator_id: str = ""
    target_ref: str = ""
    operator_ref: str = ""
    sub_type: str = ""
    notice_type: str = ""


class NoticeParser:
    """专门负责各类 OneBot / AstrBot 系统通知事件的结构化解析与文案格式化"""

    def __init__(
        self,
        resolve_user_name_fn: Callable[..., Coroutine[Any, Any, str]],
        get_bot_config_fn: Callable[[str], dict] | None = None,
    ):
        self._resolve_user_name = resolve_user_name_fn
        self._get_bot_config = get_bot_config_fn

    @staticmethod
    def get_event_field(obj, field_name: str, default=None):
        """从字典或对象中安全地读取属性/字段"""
        if obj is None:
            return default
        if hasattr(obj, "get"):
            return obj.get(field_name, default)
        return getattr(obj, field_name, default)

    @staticmethod
    def format_duration(duration: int) -> str:
        """格式化禁言秒数为人类可读字符串"""
        if duration <= 0:
            return ""
        if duration < 60:
            return f"{duration}秒"
        if duration % 86400 == 0:
            return f"{duration // 86400}天"
        if duration % 3600 == 0:
            return f"{duration // 3600}小时"
        if duration % 60 == 0:
            return f"{duration // 60}分钟"

        days = duration // 86400
        rem = duration % 86400
        hours = rem // 3600
        rem = rem % 3600
        minutes = rem // 60
        seconds = rem % 60
        parts = []
        if days > 0:
            parts.append(f"{days}天")
        if hours > 0:
            parts.append(f"{hours}小时")
        if minutes > 0:
            parts.append(f"{minutes}分钟")
        if seconds > 0:
            parts.append(f"{seconds}秒")
        return "".join(parts)

    @staticmethod
    def format_user_ref(name: str, user_id: str) -> str:
        """格式化用户引用名称：昵称(QQ) 或 纯昵称/QQ"""
        name = str(name or "").strip()
        user_id = str(user_id or "").strip()
        if name and user_id and name != user_id:
            return f"{name}({user_id})"
        return user_id or name or "未知用户"

    async def resolve_target_ref(
        self,
        event: AstrMessageEvent,
        bot_name: str,
        target_id: str,
        self_id: str = "",
    ) -> tuple[str, bool]:
        """统一解析目标用户昵称，若目标为 Bot 自身则自动尝试回退到 Bot 预设昵称。

        Returns:
            tuple[str, bool]: (target_ref, is_target_self)
        """
        target_id = str(target_id or "").strip()
        self_id = str(self_id or "").strip()
        is_target_self = bool(self_id and target_id == self_id)

        target_name = await self._resolve_user_name(
            event=event,
            user_id=target_id,
            current_name="",
            force_lookup=True,
        )
        if is_target_self and (not target_name or target_name == target_id):
            if self._get_bot_config:
                bot_conf = self._get_bot_config(bot_name)
                if bot_conf and bot_conf.get("nickname"):
                    target_name = bot_conf["nickname"]

        target_ref = self.format_user_ref(target_name or target_id, target_id)
        return target_ref, is_target_self

    async def resolve_operator_ref(
        self,
        event: AstrMessageEvent,
        operator_id: str,
        target_id: str = "",
    ) -> str:
        """统一解析操作者（管理员等）的显示名称"""
        operator_id = str(operator_id or "").strip()
        target_id = str(target_id or "").strip()
        if not operator_id or operator_id == "0" or (target_id and operator_id == target_id):
            return ""

        operator_name = await self._resolve_user_name(
            event=event,
            user_id=operator_id,
            current_name=operator_id,
            force_lookup=True,
        )
        return self.format_user_ref(operator_name or operator_id, operator_id)

    async def parse_notice(
        self,
        event: AstrMessageEvent,
        raw_message: dict | Any,
        bot_name: str,
    ) -> NoticeParseResult:
        """解析各类通知事件并返回结构化解析结果"""
        result = NoticeParseResult()
        if not raw_message:
            return result

        post_type = self.get_event_field(raw_message, "post_type", "")
        notice_type = self.get_event_field(raw_message, "notice_type", "")
        message_name = str(self.get_event_field(raw_message, "name", "") or "")
        sub_type = str(self.get_event_field(raw_message, "sub_type", "") or "").strip()
        self_id = str(event.get_self_id() or "").strip()
        group_id = str(event.get_group_id() or "").strip()

        result.sub_type = sub_type
        result.notice_type = notice_type
        result.group_id = group_id

        reaction_names = [f"notice.{t}" for t in REACTION_NOTICE_TYPES]

        # 1. 贴表情回应（Reaction）
        if (post_type == "notice" and notice_type in REACTION_NOTICE_TYPES) or message_name in reaction_names:
            result.is_notice = True
            result.role = "message"

            emoji_id = str(self.get_event_field(raw_message, "emoji_id", "") or "")
            if not emoji_id:
                likes = self.get_event_field(raw_message, "likes", [])
                if likes and isinstance(likes, list) and len(likes) > 0:
                    first_like = likes[0]
                    emoji_id = str(self.get_event_field(first_like, "emoji_id", "") or "")

            target_msg_id = str(self.get_event_field(raw_message, "message_id", "") or "")
            emoji_desc = EMOJI_MAP.get(emoji_id, f"表情:{emoji_id}")
            result.content = (
                f'[贴表情回应: {emoji_desc}] <emoji_like message_id="{target_msg_id}" emoji_id="{emoji_id}" />'
            )
            if hasattr(event, "message_obj") and event.message_obj:
                event.message_obj.message = [Plain(text=result.content)]
            return result

        # 2. 群管理员变动 (group_admin)
        if (post_type == "notice" and notice_type == "group_admin") or (
            message_name and message_name.startswith("notice.group_admin")
        ):
            result.is_notice = True
            result.role = "system"
            result.sender_id = "system"
            result.sender_name = "系统"

            target_id = str(self.get_event_field(raw_message, "user_id", "") or "").strip()
            target_ref, _ = await self.resolve_target_ref(event, bot_name, target_id, self_id)

            if sub_type == "set":
                msg = f"【系统消息】{target_ref} 被设置为群管理员"
            elif sub_type == "unset":
                msg = f"【系统消息】{target_ref} 被取消了群管理员"
            else:
                msg = f"【系统消息】{target_ref} 群管理员状态变更"

            result.content = msg
            if hasattr(event, "message_obj") and event.message_obj:
                event.message_obj.message = [Plain(text=msg)]
            return result

        # 3. 成员加入群聊 (group_increase)
        if (post_type == "notice" and notice_type == "group_increase") or (
            message_name and message_name.startswith("notice.group_increase")
        ):
            result.is_notice = True
            result.role = "system"
            result.sender_id = "system"
            result.sender_name = "系统"

            target_id = str(self.get_event_field(raw_message, "user_id", "") or "").strip()
            operator_id = str(self.get_event_field(raw_message, "operator_id", "") or "").strip()

            target_ref, _ = await self.resolve_target_ref(event, bot_name, target_id, self_id)
            operator_ref = await self.resolve_operator_ref(event, operator_id, target_id)

            if sub_type == "approve":
                msg = (
                    f"【系统消息】{target_ref} 通过 {operator_ref} 的审核加入群聊"
                    if operator_ref
                    else f"【系统消息】{target_ref} 加入了群聊"
                )
            elif sub_type == "invite":
                msg = (
                    f"【系统消息】{target_ref} 由 {operator_ref} 邀请加入群聊"
                    if operator_ref
                    else f"【系统消息】{target_ref} 加入了群聊"
                )
            else:
                msg = f"【系统消息】{target_ref} 加入了群聊"

            result.content = msg
            if hasattr(event, "message_obj") and event.message_obj:
                event.message_obj.message = [Plain(text=msg)]
            return result

        # 4. 成员退出/被移出群聊 (group_decrease)
        if (post_type == "notice" and notice_type == "group_decrease") or (
            message_name and message_name.startswith("notice.group_decrease")
        ):
            result.is_notice = True
            result.role = "system"
            result.sender_id = "system"
            result.sender_name = "系统"

            target_id = str(self.get_event_field(raw_message, "user_id", "") or "").strip()
            operator_id = str(self.get_event_field(raw_message, "operator_id", "") or "").strip()

            target_ref, _ = await self.resolve_target_ref(event, bot_name, target_id, self_id)
            operator_ref = await self.resolve_operator_ref(event, operator_id, target_id)

            if sub_type == "leave":
                msg = f"【系统消息】{target_ref} 退出了群聊"
            elif sub_type in ("kick", "kick_me"):
                msg = (
                    f"【系统消息】{target_ref} 被 {operator_ref} 移出了群聊"
                    if operator_ref
                    else f"【系统消息】{target_ref} 被移出了群聊"
                )
            else:
                msg = f"【系统消息】{target_ref} 离开了群聊"

            result.content = msg
            if hasattr(event, "message_obj") and event.message_obj:
                event.message_obj.message = [Plain(text=msg)]
            return result

        # 5. 群名片修改 (group_card)
        if (post_type == "notice" and notice_type == "group_card") or (
            message_name and message_name.startswith("notice.group_card")
        ):
            result.is_notice = True
            result.role = "system"
            result.sender_id = "system"
            result.sender_name = "系统"

            target_id = str(self.get_event_field(raw_message, "user_id", "") or "").strip()
            card_new = str(self.get_event_field(raw_message, "card_new", "") or "")
            card_old = str(self.get_event_field(raw_message, "card_old", "") or "")
            target_name = card_new or card_old or target_id
            target_ref = self.format_user_ref(target_name, target_id)
            msg = f'【系统消息】{target_ref} 的群名片从 "{card_old}" 改为 "{card_new}"'

            result.content = msg
            if hasattr(event, "message_obj") and event.message_obj:
                event.message_obj.message = [Plain(text=msg)]
            return result

        # 6. 群文件上传 (group_upload)
        if (post_type == "notice" and notice_type == "group_upload") or (
            message_name and message_name.startswith("notice.group_upload")
        ):
            result.is_notice = True
            result.role = "system"
            result.sender_id = "system"
            result.sender_name = "系统"

            target_id = str(self.get_event_field(raw_message, "user_id", "") or "").strip()
            file_info = self.get_event_field(raw_message, "file", {}) or {}
            file_name = file_info.get("name") if isinstance(file_info, dict) else "未知文件"

            target_name = await self._resolve_user_name(
                event=event,
                user_id=target_id,
                current_name="",
                force_lookup=True,
            )
            target_ref = self.format_user_ref(target_name or target_id, target_id)
            msg = f'【系统消息】{target_ref} 上传了群文件 "{file_name}"'

            result.content = msg
            if hasattr(event, "message_obj") and event.message_obj:
                event.message_obj.message = [Plain(text=msg)]
            return result

        # 7. 荣誉与头衔等提示 (notify)
        if (post_type == "notice" and notice_type == "notify") or (
            message_name and message_name.startswith("notice.notify")
        ):
            if sub_type == "honor":
                result.is_notice = True
                result.role = "system"
                result.sender_id = "system"
                result.sender_name = "系统"

                target_id = str(self.get_event_field(raw_message, "user_id", "") or "").strip()
                honor_type = str(self.get_event_field(raw_message, "honor_type", "") or "").strip()
                honor_map = {
                    "talkative": "龙王",
                    "performer": "群聊之火",
                    "legend": "群聊炽焰",
                    "strong_newbie": "冒尖小春笋",
                    "emotion": "快乐源泉",
                }
                honor_name = honor_map.get(honor_type, honor_type or "荣誉")
                target_name = await self._resolve_user_name(
                    event=event,
                    user_id=target_id,
                    current_name="",
                    force_lookup=True,
                )
                target_ref = self.format_user_ref(target_name or target_id, target_id)
                msg = f'【系统消息】{target_ref} 获得了"{honor_name}"头衔'

                result.content = msg
                if hasattr(event, "message_obj") and event.message_obj:
                    event.message_obj.message = [Plain(text=msg)]
                return result

            elif sub_type == "lucky_king":
                result.is_notice = True
                result.role = "system"
                result.sender_id = "system"
                result.sender_name = "系统"

                target_id = str(self.get_event_field(raw_message, "target_id", "") or "").strip()
                operator_id = str(self.get_event_field(raw_message, "user_id", "") or "").strip()

                target_name = await self._resolve_user_name(
                    event=event,
                    user_id=target_id,
                    current_name="",
                    force_lookup=True,
                )
                target_ref = self.format_user_ref(target_name or target_id, target_id)

                operator_name = await self._resolve_user_name(
                    event=event,
                    user_id=operator_id,
                    current_name="",
                    force_lookup=True,
                )
                operator_ref = self.format_user_ref(operator_name or operator_id, operator_id)
                msg = f"【系统消息】{target_ref} 成为了 {operator_ref} 发送红包的运气王"

                result.content = msg
                if hasattr(event, "message_obj") and event.message_obj:
                    event.message_obj.message = [Plain(text=msg)]
                return result

            elif sub_type == "title":
                result.is_notice = True
                result.role = "system"
                result.sender_id = "system"
                result.sender_name = "系统"

                target_id = str(self.get_event_field(raw_message, "user_id", "") or "").strip()
                title = str(self.get_event_field(raw_message, "title", "") or "").strip()

                target_name = await self._resolve_user_name(
                    event=event,
                    user_id=target_id,
                    current_name="",
                    force_lookup=True,
                )
                target_ref = self.format_user_ref(target_name or target_id, target_id)
                msg = f'【系统消息】{target_ref} 获得了新头衔 "{title}"'

                result.content = msg
                if hasattr(event, "message_obj") and event.message_obj:
                    event.message_obj.message = [Plain(text=msg)]
                return result

        # 8. 群禁言通知 (group_ban)
        if (post_type == "notice" and notice_type == "group_ban") or (
            message_name and message_name.startswith("notice.group_ban")
        ):
            result.is_notice = True
            result.role = "system"
            result.sender_id = "system"
            result.sender_name = "系统"

            operator_id = str(self.get_event_field(raw_message, "operator_id", "") or "").strip()
            target_id = str(self.get_event_field(raw_message, "user_id", "") or "").strip()
            duration = int(self.get_event_field(raw_message, "duration", 0) or 0)

            if not sub_type:
                sub_type = "ban" if duration > 0 else "lift_ban"

            result.sub_type = sub_type
            result.duration = duration
            result.target_id = target_id
            result.operator_id = operator_id

            operator_ref = await self.resolve_operator_ref(event, operator_id) or (
                operator_id or "管理员"
            )
            result.operator_ref = operator_ref

            is_all_member = target_id in ("0", "")
            result.is_all_member_ban = is_all_member

            if is_all_member:
                if sub_type == "ban":
                    msg = f"【系统消息】{operator_ref} 开启了全员禁言"
                    result.is_ban_event = True
                else:
                    msg = f"【系统消息】{operator_ref} 关闭了全员禁言"
                    result.is_lift_ban_event = True
            else:
                target_ref, is_target_self = await self.resolve_target_ref(
                    event, bot_name, target_id, self_id
                )
                result.target_ref = target_ref
                result.is_target_self = is_target_self

                if sub_type == "ban":
                    dur_desc = self.format_duration(duration) if duration > 0 else ""
                    dur_text = f" {dur_desc}" if dur_desc else ""
                    msg = f"【系统消息】{operator_ref} 禁言了 {target_ref}{dur_text}"
                    result.is_ban_event = True
                else:
                    msg = f"【系统消息】{operator_ref} 解除了 {target_ref} 的禁言"
                    result.is_lift_ban_event = True

            result.content = msg
            if hasattr(event, "message_obj") and event.message_obj:
                event.message_obj.message = [Plain(text=msg)]
            return result

        return result
