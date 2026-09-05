import asyncio
import logging
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

logging.basicConfig(level=logging.DEBUG)

# 1. Mock aiocqhttp
if "aiocqhttp" not in sys.modules:
    aiocqhttp_module = types.ModuleType("aiocqhttp")
    class CQHttp: pass
    aiocqhttp_module.CQHttp = CQHttp
    sys.modules["aiocqhttp"] = aiocqhttp_module

if "xxhash" not in sys.modules:
    import types
    xxhash_module = types.ModuleType("xxhash")
    xxhash_module.xxh3_64_hexdigest = lambda *args, **kwargs: "dummy_hash"
    sys.modules["xxhash"] = xxhash_module

if "PIL" not in sys.modules:
    pil_module = types.ModuleType("PIL")
    class Image: pass
    pil_module.Image = Image
    sys.modules["PIL"] = pil_module

if "aiohttp" not in sys.modules:
    aiohttp_module = types.ModuleType("aiohttp")
    class ClientSession: pass
    class ClientTimeout: pass
    class TCPConnector: pass
    class ClientError(Exception): pass
    class ClientConnectorCertificateError(Exception): pass
    class ClientConnectorSSLError(Exception): pass
    aiohttp_module.ClientSession = ClientSession
    aiohttp_module.ClientTimeout = ClientTimeout
    aiohttp_module.TCPConnector = TCPConnector
    aiohttp_module.ClientError = ClientError
    aiohttp_module.ClientConnectorCertificateError = ClientConnectorCertificateError
    aiohttp_module.ClientConnectorSSLError = ClientConnectorSSLError
    sys.modules["aiohttp"] = aiohttp_module

if "aiosqlite" not in sys.modules:
    aiosqlite_module = types.ModuleType("aiosqlite")
    class OperationalError(Exception): pass
    class Connection: pass
    class Row: pass
    aiosqlite_module.OperationalError = OperationalError
    aiosqlite_module.Connection = Connection
    aiosqlite_module.Row = Row
    sys.modules["aiosqlite"] = aiosqlite_module

if "cachetools" not in sys.modules:
    cachetools_module = types.ModuleType("cachetools")
    class LRUCache(dict):
        def __init__(self, maxsize=1000):
            super().__init__()
            self.maxsize = maxsize
    cachetools_module.LRUCache = LRUCache
    sys.modules["cachetools"] = cachetools_module

if "bs4" not in sys.modules:
    bs4_m = types.ModuleType("bs4")
    class BeautifulSoup:
        def __init__(self, *args, **kwargs):
            pass
    bs4_m.BeautifulSoup = BeautifulSoup
    sys.modules["bs4"] = bs4_m

    bs4_elem_m = types.ModuleType("bs4.element")
    class NavigableString: pass
    class Tag: pass
    bs4_elem_m.NavigableString = NavigableString
    bs4_elem_m.Tag = Tag
    bs4_m.element = bs4_elem_m
    sys.modules["bs4.element"] = bs4_elem_m

if "mcp" not in sys.modules:
    mcp_m = types.ModuleType("mcp")
    sys.modules["mcp"] = mcp_m
    mcp_types_m = types.ModuleType("mcp.types")
    mcp_m.types = mcp_types_m
    sys.modules["mcp.types"] = mcp_types_m

# 2. Mock astrbot structure
def _ensure_module(name):
    if name not in sys.modules:
        m = types.ModuleType(name)
        sys.modules[name] = m
    return sys.modules[name]

astrbot_m = _ensure_module("astrbot")
api_m = _ensure_module("astrbot.api")
astrbot_m.api = api_m
api_m.logger = logging.getLogger("astrbot")

event_m = _ensure_module("astrbot.api.event")
class AstrMessageEvent: pass
class MessageChain:
    def __init__(self, chain=None):
        self.chain = chain or []
class filter:
    class PermissionType:
        ADMIN = "ADMIN"
    @staticmethod
    def permission_type(*args, **kwargs):
        def dec(f): return f
        return dec
    @staticmethod
    def command(*args, **kwargs):
        def dec(f): return f
        return dec
    @staticmethod
    def event_message_type(*args, **kwargs):
        def dec(f): return f
        return dec

event_m.AstrMessageEvent = AstrMessageEvent
event_m.MessageChain = MessageChain
event_m.filter = filter
api_m.event = event_m

star_api_m = _ensure_module("astrbot.api.star")
api_m.star = star_api_m
class Context: pass
star_api_m.Context = Context
class StarTools:
    @staticmethod
    def get_data_dir(*args, **kwargs):
        return "/tmp"
star_api_m.StarTools = StarTools

comp_m = _ensure_module("astrbot.api.message_components")
class At: pass
class Node: pass
class Nodes: pass
class Plain:
    def __init__(self, text=""):
        self.text = text
class Image: pass
class Record: pass
class File: pass
class Reply: pass
class Poke: pass
class Video: pass
comp_m.At = At
comp_m.Node = Node
comp_m.Nodes = Nodes
comp_m.Plain = Plain
comp_m.Image = Image
comp_m.Record = Record
comp_m.File = File
comp_m.Reply = Reply
comp_m.Poke = Poke
comp_m.Video = Video
api_m.message_components = comp_m

core_m = _ensure_module("astrbot.core")
astrbot_m.core = core_m
class AstrBotConfig(dict): pass
core_m.AstrBotConfig = AstrBotConfig

msg_m = _ensure_module("astrbot.core.message")
core_m.message = msg_m
msg_comp_m = _ensure_module("astrbot.core.message.components")
msg_m.components = msg_comp_m
class BaseMessageComponent: pass
msg_comp_m.BaseMessageComponent = BaseMessageComponent

exc_m = _ensure_module("astrbot.core.exceptions")
core_m.exceptions = exc_m
class EmptyModelOutputError(Exception): pass
exc_m.EmptyModelOutputError = EmptyModelOutputError

star_m = _ensure_module("astrbot.core.star")
core_m.star = star_m
star_tools_m = _ensure_module("astrbot.core.star.star_tools")
star_m.star_tools = star_tools_m
class StarTools:
    @staticmethod
    def get_data_dir(*args, **kwargs):
        return "/tmp"
star_tools_m.StarTools = StarTools

plat_m = _ensure_module("astrbot.core.platform")
core_m.platform = plat_m
ab_msg_m = _ensure_module("astrbot.core.platform.astrbot_message")
plat_m.astrbot_message = ab_msg_m
class AstrBotMessage: pass
class MessageMember: pass
ab_msg_m.AstrBotMessage = AstrBotMessage
ab_msg_m.MessageMember = MessageMember

ms_m = _ensure_module("astrbot.core.platform.message_session")
plat_m.message_session = ms_m
class MessageSession: pass
ms_m.MessageSession = MessageSession

mt_m = _ensure_module("astrbot.core.platform.message_type")
plat_m.message_type = mt_m
class MessageType:
    GROUP_MESSAGE = "GROUP_MESSAGE"
mt_m.MessageType = MessageType

pm_m = _ensure_module("astrbot.core.platform.platform_metadata")
plat_m.platform_metadata = pm_m
class PlatformMetadata: pass
pm_m.PlatformMetadata = PlatformMetadata

src_m = _ensure_module("astrbot.core.platform.sources")
plat_m.sources = src_m
aiocq_src = _ensure_module("astrbot.core.platform.sources.aiocqhttp")
src_m.aiocqhttp = aiocq_src
aiocq_evt = _ensure_module("astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event")
aiocq_src.aiocqhttp_message_event = aiocq_evt
class AiocqhttpMessageEvent: pass
aiocq_evt.AiocqhttpMessageEvent = AiocqhttpMessageEvent

aiocq_adp = _ensure_module("astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter")
aiocq_src.aiocqhttp_platform_adapter = aiocq_adp
class AiocqhttpAdapter: pass
aiocq_adp.AiocqhttpAdapter = AiocqhttpAdapter

utils_m = _ensure_module("astrbot.core.utils")
core_m.utils = utils_m
media_u_m = _ensure_module("astrbot.core.utils.media_utils")
media_u_m.detect_image_mime_type = lambda *args, **kwargs: "image/jpeg"
utils_m.media_utils = media_u_m
path_m = _ensure_module("astrbot.core.utils.astrbot_path")
utils_m.astrbot_path = path_m
path_m.get_astrbot_data_path = lambda *args, **kwargs: "/tmp"
path_m.get_astrbot_plugin_data_path = lambda *args, **kwargs: "/tmp"

agent_ctx_m = _ensure_module("astrbot.core.astr_agent_context")
core_m.astr_agent_context = agent_ctx_m
class AgentContextWrapper: pass
class AstrAgentContext: pass
agent_ctx_m.AgentContextWrapper = AgentContextWrapper
agent_ctx_m.AstrAgentContext = AstrAgentContext

tool_exec_m = _ensure_module("astrbot.core.astr_agent_tool_exec")
core_m.astr_agent_tool_exec = tool_exec_m
class FunctionToolExecutor: pass
tool_exec_m.FunctionToolExecutor = FunctionToolExecutor

lock_m = _ensure_module("astrbot.core.utils.session_lock")
utils_m.session_lock = lock_m
class DummySessionLockManager:
    def acquire_lock(self, *args, **kwargs):
        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def _lock():
            yield
        return _lock()
lock_m.session_lock_manager = DummySessionLockManager()

# 3. Now import project modules
from core.conversation.chat_manager import ChatManager
from core.handlers.commands import CommandHandler
from core.utils.schemas import Status


class MockFilter:
    def __init__(self, class_name: str, command_name: str = ""):
        self.__class__.__name__ = class_name
        self.command_name = command_name

    def get_complete_command_names(self):
        return [self.command_name]


class MockHandler:
    def __init__(self, handler_name: str, event_filters: list):
        self.handler_name = handler_name
        self.event_filters = event_filters


class CommandBlockingAndStatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = MagicMock()
        self.plugin._terminated = False
        self.plugin.adapter_id_map = {"adapter_1": "Giftia"}
        self.plugin.bot_map = {"Giftia": {"nickname": "小吉", "name": "Giftia"}}
        self.plugin.active_reply_counters = {}
        self.plugin.replying_status = {}
        self.plugin.parse_locks = {"Giftia:10001": asyncio.Lock()}
        self.plugin.data_cache.add_message = AsyncMock()
        self.plugin.data_cache.get_bot_status = AsyncMock()
        self.plugin.data_cache.update_bot_custom_status = AsyncMock()
        self.plugin.message_parser.parse_user_message = AsyncMock(
            return_value=(MagicMock(), [], [])
        )
        self.plugin.message_parser.chain_to_result = AsyncMock(
            return_value=MagicMock(content="bot reply", media_id_list=[], forward_messages=[])
        )

        self.plugin.passive_memory_enabled = False
        self.plugin.data_cache.get_stronghold = AsyncMock(return_value=None)

        self.chat_manager = ChatManager(self.plugin)
        self.chat_manager.decision_engine.check_whitelists = MagicMock(return_value=True)

    def _create_mock_event(self, activated_handlers: list | None = None, message_str: str = ""):
        event = MagicMock()
        event._giftia_bypass_logging = False
        event.get_self_id = MagicMock(return_value="3970706156")
        event.get_sender_id = MagicMock(return_value="12345678")
        event.get_group_id = MagicMock(return_value="10001")
        event.get_messages = MagicMock(return_value=[])
        event.get_message_str = MagicMock(return_value=message_str)
        event.platform_meta = MagicMock()
        event.platform_meta.id = "adapter_1"
        event.send = AsyncMock(side_effect=lambda msg: msg)
        event.message_obj = MagicMock()
        event.message_obj.raw_message = {"post_type": "message"}

        extras = {}
        if activated_handlers is not None:
            extras["activated_handlers"] = activated_handlers

        def get_extra(key, default=None):
            return extras.get(key, default)

        def set_extra(key, val):
            extras[key] = val

        event.get_extra = MagicMock(side_effect=get_extra)
        event.set_extra = MagicMock(side_effect=set_extra)
        return event

    async def test_normal_command_blocked_when_switch_enabled(self):
        """当 block_command_messages 为 True 时，普通指令及其响应不入库"""
        self.plugin.block_command_messages = True

        cmd_handler = MockHandler("help_cmd", [MockFilter("CommandFilter", "help")])
        event = self._create_mock_event(
            activated_handlers=[cmd_handler, MockHandler("on_message", [])],
            message_str="/help",
        )

        await self.chat_manager.handle_message(event)

        # 检查 bypass 标记是否设置
        self.assertTrue(getattr(event, "_giftia_bypass_logging", False))

        # 检查用户指令是否未被解析入库
        self.plugin.message_parser.parse_user_message.assert_not_called()
        self.plugin.data_cache.add_message.assert_not_called()

        # 测试 Bot 响应通过 event.send 时的表现
        await event.send(MagicMock())
        # data_cache.add_message 仍不应被调用（被 intercepted_send 拦截屏蔽）
        self.plugin.data_cache.add_message.assert_not_called()

    async def test_normal_command_allowed_when_switch_disabled(self):
        """当 block_command_messages 为 False 时，普通指令及其响应正常入库"""
        self.plugin.block_command_messages = False

        cmd_handler = MockHandler("help_cmd", [MockFilter("CommandFilter", "help")])
        event = self._create_mock_event(
            activated_handlers=[cmd_handler, MockHandler("on_message", [])],
            message_str="/help",
        )

        await self.chat_manager.handle_message(event)

        # 检查 bypass 标记未设置
        self.assertFalse(getattr(event, "_giftia_bypass_logging", False))

        # 用户指令应被正常解析
        self.plugin.message_parser.parse_user_message.assert_called_once()

        # 测试 Bot 响应通过 event.send
        msg = MessageChain([Plain("bot reply")])
        await event.send(msg)
        self.plugin.data_cache.add_message.assert_called_once()

    async def test_delete_message_command_always_blocked_even_when_switch_disabled(self):
        """无论 block_command_messages 是否开启，/删除消息 指令及其响应均强制屏蔽入库"""
        self.plugin.block_command_messages = False

        del_handler = MockHandler("delete_message", [MockFilter("CommandFilter", "删除消息")])
        event = self._create_mock_event(
            activated_handlers=[del_handler, MockHandler("on_message", [])],
            message_str="/删除消息",
        )

        await self.chat_manager.handle_message(event)

        # 检查 bypass 标记为 True
        self.assertTrue(getattr(event, "_giftia_bypass_logging", False))

        # 用户指令未入库
        self.plugin.message_parser.parse_user_message.assert_not_called()

        # Bot 响应不入库
        await event.send(MagicMock())
        self.plugin.data_cache.add_message.assert_not_called()

    async def test_pseudo_command_is_treated_as_normal_message(self):
        """未注册的伪指令（如 /起飞）不命中 CommandFilter，应正常存入数据库"""
        self.plugin.block_command_messages = True

        # 伪指令没有激活任何 CommandFilter，仅激活 on_message
        event = self._create_mock_event(
            activated_handlers=[MockHandler("on_message", [])],
            message_str="/起飞",
        )

        await self.chat_manager.handle_message(event)

        # bypass 标记应为 False
        self.assertFalse(getattr(event, "_giftia_bypass_logging", False))

        # 用户消息被正常解析入库
        self.plugin.message_parser.parse_user_message.assert_called_once()

    async def test_set_persistent_status_command(self):
        """测试 /设置常驻状态 指令添加、修改与删除"""
        cmd_handler = CommandHandler(self.plugin)
        event = self._create_mock_event()

        # 1. 正常设置状态
        chunks = [c async for c in cmd_handler.set_persistent_status(event, "服装", "女仆装")]
        self.plugin.data_cache.update_bot_custom_status.assert_called_with(
            bot_name="Giftia", group_id="10001", custom_status_updates={"服装": "女仆装"}
        )
        self.assertEqual(len(chunks), 1)

        # 2. 状态值为空（删除）
        chunks = [c async for c in cmd_handler.set_persistent_status(event, "服装", "")]
        self.plugin.data_cache.update_bot_custom_status.assert_called_with(
            bot_name="Giftia", group_id="10001", custom_status_updates={"服装": ""}
        )

        # 3. 状态值为 "删除" 关键字
        chunks = [c async for c in cmd_handler.set_persistent_status(event, "服装", "删除")]
        self.plugin.data_cache.update_bot_custom_status.assert_called_with(
            bot_name="Giftia", group_id="10001", custom_status_updates={"服装": ""}
        )

        # 4. 状态名为空
        chunks = [c async for c in cmd_handler.set_persistent_status(event, "", "女仆装")]
        self.assertIn("状态名不能为空", chunks[0].chain[0].text)

    async def test_get_bot_status_command(self):
        """测试 /查看状态 指令返回结构化看板"""
        cmd_handler = CommandHandler(self.plugin)
        event = self._create_mock_event()

        mock_status = Status(
            mood="开心",
            state="空闲",
            action="品茶",
            energy="95.5",
            custom_status={"服装": "水手服", "场景": "教室"},
        )
        self.plugin.data_cache.get_bot_status.return_value = mock_status

        chunks = [c async for c in cmd_handler.get_bot_status(event)]
        self.assertEqual(len(chunks), 1)
        output_text = chunks[0].chain[0].text

        self.assertIn("【Bot 状态看板】", output_text)
        self.assertIn("小吉 (Giftia)", output_text)
        self.assertIn("心情：开心", output_text)
        self.assertIn("状态：空闲", output_text)
        self.assertIn("动作：品茶", output_text)
        self.assertIn("能量：95.5%", output_text)
        self.assertIn("服装：水手服", output_text)
        self.assertIn("场景：教室", output_text)

    def test_delete_table_removed(self):
        """验证 delete_table 指令已从 CommandHandler 彻底移除"""
        cmd_handler = CommandHandler(self.plugin)
        self.assertFalse(hasattr(cmd_handler, "delete_table"))

    async def test_delete_message_command_and_logging(self):
        """测试 /删除消息 指令的执行过程与各层日志记录"""
        cmd_handler = CommandHandler(self.plugin)

        # 1. 无 Reply 组件，提示未找到引用消息
        event_no_reply = self._create_mock_event()
        chunks = [c async for c in cmd_handler.delete_message(event_no_reply)]
        self.assertIn("未找到引用消息的消息ID", chunks[0].chain[0].text)

        # 2. 有 Reply 组件，删除成功返回成功提示
        self.plugin.data_cache.delete_message = AsyncMock(return_value=True)
        event_with_reply = self._create_mock_event()
        reply = Reply()
        reply.id = "msg_88888"
        reply.sender_id = "user_123"
        reply.message_str = "这是一条指令回复"
        event_with_reply.get_messages.return_value = [reply]

        chunks2 = [c async for c in cmd_handler.delete_message(event_with_reply)]
        self.assertIn("删除消息成功", chunks2[0].chain[0].text)
        self.plugin.data_cache.delete_message.assert_called_once_with(
            bot_name="Giftia",
            group_or_user_id="10001",
            message_id="msg_88888",
        )

        # 3. 有 Reply 组件，但库中未匹配到（返回 False），提示前往 WebUI 删除
        self.plugin.data_cache.delete_message = AsyncMock(return_value=False)
        chunks3 = [c async for c in cmd_handler.delete_message(event_with_reply)]
        self.assertEqual(
            chunks3[0].chain[0].text,
            "删除消息失败：指令响应消息请前往 WebUI 决策审计页面手动删除。",
        )

    async def test_intercepted_send_captures_platform_message_id(self):
        """测试 intercepted_send 通过包装 bot 方法成功捕获平台返回的真实 message_id"""
        self.plugin.block_command_messages = False

        event = self._create_mock_event(
            activated_handlers=[MockHandler("help_cmd", [MockFilter("CommandFilter", "help")])],
            message_str="/help",
        )
        mock_bot = MagicMock()
        mock_bot.send_group_msg = AsyncMock(return_value={"message_id": 1710535200})
        mock_bot.call_action = AsyncMock(return_value={"message_id": 1710535200})
        mock_bot.send_private_msg = AsyncMock(return_value={"message_id": 1710535200})
        event.bot = mock_bot

        # 模拟 AstrBot 的底层实现：适配器的 event.send 调用协议端 bot.send_group_msg
        async def adapter_send(chain):
            return await event.bot.send_group_msg(group_id=10001, message=[])

        event.send = adapter_send

        # handle_message 执行，用 intercepted_send 替换 event.send
        await self.chat_manager.handle_message(event)

        # 触发拦截发送
        await event.send(MessageChain([Plain("bot reply")]))

        # 验证存入数据库的 MessageData 带有真实的 message_id
        self.plugin.data_cache.add_message.assert_called_once()
        saved_msg_data = self.plugin.data_cache.add_message.call_args[0][2]
        self.assertEqual(saved_msg_data.message_id, "1710535200")

    async def test_intercepted_send_concurrency_isolation(self):
        """测试同一个 bot 实例在多个并发事件中发送时，通过 ContextVar 隔离捕获正确的 message_id"""
        self.plugin.block_command_messages = False
        shared_bot = MagicMock()

        async def mock_call_action(action, **params):
            if params.get("msg") == "msg1":
                await asyncio.sleep(0.02)
                return {"message_id": 1001}
            elif params.get("msg") == "msg2":
                await asyncio.sleep(0.01)
                return {"message_id": 1002}
            return {"message_id": 9999}

        shared_bot.call_action = mock_call_action
        shared_bot.send_group_msg = AsyncMock()
        shared_bot.send_private_msg = AsyncMock()

        event1 = self._create_mock_event(
            activated_handlers=[MockHandler("cmd1", [MockFilter("CommandFilter", "cmd1")])],
            message_str="/cmd1",
        )
        event1.bot = shared_bot
        async def send1(chain):
            return await shared_bot.call_action("send_group_msg", group_id=10001, msg="msg1")
        event1.send = send1

        event2 = self._create_mock_event(
            activated_handlers=[MockHandler("cmd2", [MockFilter("CommandFilter", "cmd2")])],
            message_str="/cmd2",
        )
        event2.bot = shared_bot
        async def send2(chain):
            return await shared_bot.call_action("send_group_msg", group_id=10001, msg="msg2")
        event2.send = send2

        await self.chat_manager.handle_message(event1)
        await self.chat_manager.handle_message(event2)

        self.plugin.message_parser.chain_to_result = AsyncMock(
            side_effect=lambda *args, **kwargs: MagicMock(
                content=args[0][0].text if args and args[0] and hasattr(args[0][0], "text") else "bot reply",
                media_id_list=[],
                forward_messages=[],
            )
        )

        saved_msgs = []
        self.plugin.data_cache.add_message = AsyncMock(side_effect=lambda b, g, msg: saved_msgs.append(msg))

        # 并发执行两个 event.send
        await asyncio.gather(
            event1.send(MessageChain([Plain("reply 1")])),
            event2.send(MessageChain([Plain("reply 2")])),
        )

        msg1_record = next((m for m in saved_msgs if m.content == "reply 1"), None)
        msg2_record = next((m for m in saved_msgs if m.content == "reply 2"), None)
        self.assertIsNotNone(msg1_record)
        self.assertIsNotNone(msg2_record)
        self.assertEqual(msg1_record.message_id, "1001")
        self.assertEqual(msg2_record.message_id, "1002")

    async def test_data_cache_delete_message_exact_match(self):
        """测试 DataCache.delete_message 依据 message_id 精确删除并返回结果"""
        from core.database.data_cache import DataCache
        from core.utils.schemas import MessageData

        db_mock = MagicMock()
        db_mock.delete_message = AsyncMock(return_value=True)

        cache = DataCache.__new__(DataCache)
        cache.recent_messages = {"Giftia:10001": []}
        cache.db = db_mock

        target_msg = MessageData(
            nickname="小吉",
            user_id="3970706156",
            group_or_user_id="10001",
            time="2026-09-05T18:18:07",
            message_id="1710535200",
            content="这是一条正常回复",
        )
        cache.recent_messages["Giftia:10001"].append(target_msg)

        # 匹配成功
        success = await cache.delete_message(
            bot_name="Giftia",
            group_or_user_id="10001",
            message_id="1710535200",
        )
        self.assertTrue(success)
        self.assertEqual(len(cache.recent_messages["Giftia:10001"]), 0)
        db_mock.delete_message.assert_called_once_with(
            bot_name="Giftia",
            group_or_user_id="10001",
            message_id="1710535200",
        )

        # 匹配失败
        db_mock.delete_message.return_value = False
        fail_res = await cache.delete_message(
            bot_name="Giftia",
            group_or_user_id="10001",
            message_id="non_existent_id",
        )
        self.assertFalse(fail_res)

    async def test_get_bot_status_with_zero_energy(self):
        """测试 get_bot_status 在能量为 0 时能正确展示 0% 而不会被误 fallback 为 100.0%"""
        from core.handlers.commands import CommandHandler
        from core.utils.schemas import Status

        cmd_handler = CommandHandler(self.plugin)
        mock_status = Status(
            mood="平稳",
            state="空闲",
            action="待机",
            energy=0,
            custom_status={},
        )
        self.plugin.data_cache.get_bot_status = AsyncMock(return_value=mock_status)

        event = self._create_mock_event()
        chunks = [c async for c in cmd_handler.get_bot_status(event)]
        reply_text = chunks[0].chain[0].text
        self.assertIn("• 能量：0%", reply_text)
        self.assertNotIn("• 能量：100.0%", reply_text)

    async def test_silence_session_command(self):
        """测试 /静默 指令将接话活跃窗口重置为 0 并将状态更新为「不活跃」"""
        from core.handlers.commands import CommandHandler

        cmd_handler = CommandHandler(self.plugin)
        self.plugin.active_reply_counters = {"Giftia:10001": 10}
        self.plugin.data_cache.set_bot_status = AsyncMock()

        event = self._create_mock_event()
        chunks = [c async for c in cmd_handler.silence_session(event)]

        # 1. 活跃窗口计数重置为 0
        self.assertEqual(self.plugin.active_reply_counters["Giftia:10001"], 0)

        # 2. data_cache.set_bot_status 被调用且传入 state="不活跃"
        self.plugin.data_cache.set_bot_status.assert_called_once()
        call_kwargs = self.plugin.data_cache.set_bot_status.call_args[1]
        self.assertEqual(call_kwargs["bot_name"], "Giftia")
        self.assertEqual(call_kwargs["group_id"], "10001")
        self.assertEqual(call_kwargs["status"].state, "不活跃")

        # 3. 回复文案包含提示
        reply_text = chunks[0].chain[0].text
        self.assertIn("设置为「不活跃」", reply_text)
        self.assertIn("接话分析窗口已重置", reply_text)


if __name__ == "__main__":
    unittest.main()
