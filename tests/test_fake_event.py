"""主动消息伪造事件的回归测试。

覆盖历史故障：定时任务 / 主动消息走伪造事件时，`AstrMessageEvent.__init__`
赋的实例属性不在类 spec 内，取值直接抛
`AttributeError: Mock object has no attribute 'platform_meta'`，
把整条回复链路（含备用模型兜底）一并击穿。
"""

import ast
import asyncio
import inspect
import pathlib
import re
import textwrap
import unittest
from unittest.mock import MagicMock

from core.utils.event_utils import (
    REAL_EVENT_INSTANCE_ATTRS,
    bind_fake_event_extras,
    build_fake_event,
    get_adapter_id,
    resolve_bot_name,
)


class StubPlatformMeta:
    """模拟 PlatformMetadata"""

    def __init__(self, meta_id: str) -> None:
        self.id = meta_id
        self.name = "aiocqhttp"


class StubMessageEvent:
    """模拟 AstrMessageEvent：关键状态全部是 `__init__` 里赋的实例属性"""

    def __init__(self, platform_meta: StubPlatformMeta) -> None:
        self.message_str = ""
        self.message_obj = None
        self.platform_meta = platform_meta
        self.platform = platform_meta
        self.session = None
        self.role = "member"
        self.is_wake = False
        self.is_at_or_wake_command = False
        self.call_llm = False
        self.created_at = 0.0
        self.plugins_name = None
        self._extras = {}
        self._result = None
        self._force_stopped = False
        self._has_send_oper = False
        self._temporary_local_files = []

    def get_group_id(self) -> str:
        return ""

    def get_extra(self, key=None, default=None):
        if key is None:
            return self._extras
        return self._extras.get(key, default)

    def set_extra(self, key, value) -> None:
        self._extras[key] = value

    def clear_extra(self) -> None:
        self._extras.clear()

    def get_result(self):
        return self._result

    async def send(self, message) -> None:
        return None


class StubAiocqhttpEvent(StubMessageEvent):
    """模拟 AiocqhttpMessageEvent：额外多一个 `bot` 实例属性"""

    def __init__(self, platform_meta: StubPlatformMeta, bot) -> None:
        super().__init__(platform_meta)
        self.bot = bot


def make_fake_event(**overrides):
    meta = StubPlatformMeta("aiocqhttp:bot1")
    attrs = {
        "bot": None,
        "platform_meta": meta,
        "platform": meta,
        "message_obj": object(),
        "message_str": "",
        "session": object(),
        "role": "member",
        "is_wake": True,
        "is_at_or_wake_command": True,
        "call_llm": False,
        "created_at": 1.0,
        "plugins_name": None,
        "_result": None,
        "_force_stopped": False,
        "_has_send_oper": False,
        "_temporary_local_files": [],
        "unified_msg_origin": "aiocqhttp:bot1:GroupMessage:123",
    }
    attrs.update(overrides)
    event = build_fake_event(StubAiocqhttpEvent, attrs)
    bind_fake_event_extras(event)
    return event


class FakeEventTests(unittest.TestCase):
    def test_bare_spec_mock_reproduces_original_crash(self):
        """裸 spec Mock 会复现历史报错，证明补齐实例属性是必需的"""
        bare = MagicMock(spec=StubAiocqhttpEvent)
        with self.assertRaises(AttributeError) as ctx:
            _ = bare.platform_meta
        self.assertIn("platform_meta", str(ctx.exception))

    def test_real_instance_attrs_all_readable(self):
        """真实事件的实例属性在伪造事件上必须全部可取且保留真值"""
        event = make_fake_event()
        real = StubAiocqhttpEvent(StubPlatformMeta("aiocqhttp:bot1"), None)
        for name in set(vars(real)) | set(REAL_EVENT_INSTANCE_ATTRS):
            with self.subTest(attr=name):
                self.assertIn(name, event.__dict__, f"{name} 未被显式补齐")
        self.assertEqual(event.platform_meta.id, "aiocqhttp:bot1")
        self.assertEqual(get_adapter_id(event), "aiocqhttp:bot1")
        self.assertIsNone(event.plugins_name)
        self.assertEqual(event.role, "member")

    def test_unknown_attr_degrades_instead_of_raising(self):
        """AstrBot 后续新增的未知实例属性降级为空壳 Mock，不再中断回复"""
        event = make_fake_event()
        value = event.some_future_attr
        self.assertIsInstance(value, MagicMock)
        self.assertIs(value, event.some_future_attr)

    def test_isinstance_and_async_method_preserved(self):
        """spec 必须保留：平台动作分支依赖 isinstance；异步方法要可 await"""
        event = make_fake_event()
        self.assertIsInstance(event, StubAiocqhttpEvent)
        asyncio.run(event.send("x"))
        event.send.assert_awaited_once()

    def test_extras_and_result_have_real_semantics(self):
        """extras 与 get_result 需给出真实语义，避免上游拿到真值 Mock"""
        event = make_fake_event()
        self.assertIsNone(event.get_extra("background_note"))
        self.assertEqual(event.get_extra("activated_handlers", []), [])
        event.set_extra("activated_handlers", ["a"])
        self.assertEqual(event.get_extra("activated_handlers"), ["a"])
        self.assertEqual(event.get_extra(), {"activated_handlers": ["a"]})
        event.clear_extra()
        self.assertEqual(event.get_extra(), {})
        self.assertIsNone(event.get_result())

    def test_adapter_id_helpers_tolerate_missing_meta(self):
        """取值收口：连 platform_meta 都缺失时也只退化为空串"""
        bare = MagicMock(spec=StubAiocqhttpEvent)
        self.assertEqual(get_adapter_id(bare), "")
        self.assertEqual(resolve_bot_name(MagicMock(adapter_id_map={}), bare), "")
        self.assertEqual(get_adapter_id(None), "")

        event = make_fake_event()
        plugin = MagicMock(adapter_id_map={"aiocqhttp:bot1": "小机"})
        self.assertEqual(resolve_bot_name(plugin, event), "小机")


class FakeEventImplSyncTests(unittest.TestCase):
    """名单与实现必须同步：漏补一个实例属性就会重演历史崩溃"""

    def test_chat_manager_populates_every_known_attr(self):
        source = (
            pathlib.Path(__file__).resolve().parents[1]
            / "core"
            / "conversation"
            / "chat_manager.py"
        ).read_text(encoding="utf-8")
        body = source.split("def fake_event(", 1)[1].split("return mock_event", 1)[0]
        populated = set(re.findall(r'^\s*"(\w+)":', body, re.MULTILINE))
        # _extras 由 bind_fake_event_extras 连同读写替身一起挂上
        missing = set(REAL_EVENT_INSTANCE_ATTRS) - populated - {"_extras"}
        self.assertEqual(missing, set(), "ChatManager.fake_event 漏补了实例属性")


class RealAstrBotEventDriftTests(unittest.TestCase):
    """装了 AstrBot 时校验实例属性名单未随版本漂移"""

    def setUp(self):
        try:
            from astrbot.core.platform.astr_message_event import AstrMessageEvent
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )
        except ImportError as e:  # 本地无 AstrBot 环境时跳过
            self.skipTest(f"AstrBot 不可用: {e}")
        self.event_classes = (AstrMessageEvent, AiocqhttpMessageEvent)

    @staticmethod
    def _init_assigned_attrs(event_cls) -> set[str]:
        """取出 `__init__` 里所有 `self.x = ...` / `self.x: T = ...` 的属性名"""
        tree = ast.parse(textwrap.dedent(inspect.getsource(event_cls.__init__)))
        found: set[str] = set()
        for node in ast.walk(tree):
            targets: list = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    found.add(target.attr)
        return found

    def test_init_assigned_attrs_are_covered(self):
        assigned: set[str] = set()
        for cls in self.event_classes:
            assigned |= self._init_assigned_attrs(cls)
        # trace / span 是链路追踪对象，故意交给 Mock 兜底
        assigned -= {"trace", "span"}
        missing = assigned - set(REAL_EVENT_INSTANCE_ATTRS)
        self.assertEqual(
            missing,
            set(),
            "AstrBot 新增了实例属性，请同步补进 REAL_EVENT_INSTANCE_ATTRS "
            "与 ChatManager.fake_event",
        )


if __name__ == "__main__":
    unittest.main()
