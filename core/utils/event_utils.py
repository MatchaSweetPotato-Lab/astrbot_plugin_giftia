"""事件取值收口工具：兼容真实事件与主动消息伪造事件。

`ChatManager.fake_event()` 产出的是 `MagicMock(spec=AiocqhttpMessageEvent)`，
而 `platform_meta` / `bot` 都是 `AstrMessageEvent.__init__` 里赋的**实例属性**，
不在类 spec 之内，Mock 不会自动补齐。因此一旦伪造事件没能显式赋上
`platform_meta`（例如平台适配器已停用、被改名，或该 Bot 不在 aiocqhttp 平台上），
任何 `event.platform_meta.id` 都会抛出：

    AttributeError: Mock object has no attribute 'platform_meta'

这类异常一旦落在 `except` 分支里的取值语句上，还会二次抛出并击穿整个
重试 / 备用模型兜底循环。这里统一收口：取不到就退化为空串，绝不抛异常。

`platform_meta` 只是最先撞上的一个。`AstrMessageEvent.__init__` 还会赋
`message_obj` / `session` / `role` / `plugins_name` 等十余个实例属性，
AstrBot 每次升级都可能再加。因此本模块同时提供伪造事件的构造收口
（`build_fake_event` / `bind_fake_event_extras`），把「补齐已知实例属性」与
「未知属性降级为空壳 Mock」固定下来，避免同一类崩溃换个属性名再来一次。
"""

from typing import Any
from unittest.mock import MagicMock

REAL_EVENT_INSTANCE_ATTRS: tuple[str, ...] = (
    "message_str",
    "message_obj",
    "platform_meta",
    "platform",
    "session",
    "role",
    "is_wake",
    "is_at_or_wake_command",
    "call_llm",
    "created_at",
    "plugins_name",
    "_extras",
    "_result",
    "_force_stopped",
    "_has_send_oper",
    "_temporary_local_files",
    "bot",
)
"""真实事件在 `__init__` 里赋值、但不在类 spec 内的实例属性名单。

伪造事件必须逐个补齐（`bot` 为 aiocqhttp / 官方 QQ 事件子类所加）。
"""


class FakeEventMock(MagicMock):
    """伪造事件专用 Mock：保留 spec 以便 `isinstance` 判定，同时兜底未知属性。

    `MagicMock(spec=cls)` 只放行类上存在的名字，实例属性一律抛 AttributeError；
    而平台动作分支（如 `isinstance(event, AiocqhttpMessageEvent)`）又依赖 spec，
    不能简单改用无 spec 的 Mock。这里把 spec 之外的未知属性降级为普通 Mock：
    宁可取到一个空壳对象，也不要让一次取值中断整条主动消息回复链路。
    """

    def __getattr__(self, name: str) -> Any:
        try:
            return super().__getattr__(name)
        except AttributeError:
            if name.startswith(("_mock", "assert")) or (
                name.startswith("__") and name.endswith("__")
            ):
                raise
            child = MagicMock()
            setattr(self, name, child)
            return child


def build_fake_event(spec_cls: Any, attrs: dict[str, Any]) -> Any:
    """按 spec 造一个行为贴近真实事件的伪造事件。

    - `spec_cls` 决定 `isinstance` 判定（平台侧动作分支依赖它）；
    - `attrs` 里的实例属性与方法替身逐个显式赋上；
    - 其余未知属性由 `FakeEventMock` 兜底为空壳 Mock。
    """
    event = FakeEventMock(spec=spec_cls)
    for name, value in attrs.items():
        setattr(event, name, value)
    return event


def bind_fake_event_extras(
    event: Any, extras: dict[str, Any] | None = None
) -> dict[str, Any]:
    """给伪造事件挂上真实语义的 extras 读写与空结果，返回底层 extras 字典。

    纯 Mock 上 `event.get_extra("k")` 永远返回真值 Mock，
    `event.get_extra(key) or 默认值` 这类写法会因此取到空壳对象；
    `event.get_result()` 同理会让上游误判「工具已经直接发过消息」。
    """
    store: dict[str, Any] = dict(extras or {})

    def _get_extra(key: str | None = None, default: Any = None) -> Any:
        if key is None:
            return store
        return store.get(key, default)

    def _set_extra(key: Any, value: Any) -> None:
        store[key] = value

    def _clear_extra() -> None:
        store.clear()

    event._extras = store
    event.get_extra = MagicMock(side_effect=_get_extra)
    event.set_extra = MagicMock(side_effect=_set_extra)
    event.clear_extra = MagicMock(side_effect=_clear_extra)
    event.get_result = MagicMock(return_value=None)
    return store


def get_adapter_id(event: Any) -> str:
    """取事件所属平台适配器 ID（即 `platform_meta.id`），取不到返回空串。"""
    if not event:
        return ""
    meta = getattr(event, "platform_meta", None)
    return str(getattr(meta, "id", "") or "")


def resolve_bot_name(plugin: Any, event: Any) -> str:
    """按适配器 ID 反查本插件管理的 Bot 名称，取不到返回空串。"""
    if not event or plugin is None:
        return ""
    adapter_id = get_adapter_id(event)
    if not adapter_id:
        return ""
    return (getattr(plugin, "adapter_id_map", None) or {}).get(adapter_id) or ""
