import asyncio
import logging
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# 1. Mock dependencies if not already loaded
if "aiocqhttp" not in sys.modules:
    aiocqhttp_module = types.ModuleType("aiocqhttp")
    class CQHttp: pass
    aiocqhttp_module.CQHttp = CQHttp
    sys.modules["aiocqhttp"] = aiocqhttp_module

if "xxhash" not in sys.modules:
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
event_m.AstrMessageEvent = AstrMessageEvent
event_m.MessageChain = MessageChain
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
    def __init__(self, text=""): self.text = text
class Image: pass
class Record: pass
class File: pass
class Reply:
    def __init__(self, id="", sender_id="", message_str=""):
        self.id = id
        self.sender_id = sender_id
        self.message_str = message_str
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

web_m = _ensure_module("astrbot.api.web")
api_m.web = web_m
def json_response(data=None, status=200):
    return {"status_code": status, "body": data}

def error_response(message="error", status=400):
    return {"status_code": status, "body": {"status": "error", "message": message}}

class MockRequest:
    def __init__(self):
        self.query = {}
        self._json = {}
    async def json(self):
        return self._json

mock_request = MockRequest()
web_m.json_response = json_response
web_m.error_response = error_response
web_m.request = mock_request


from core.database.repositories.chat_history import ChatHistoryRepository
from core.database.data_cache import DataCache
from core.database.database import Database
from core.web.chat_history_api import ChatHistoryApi
from core.web.webui_manager import WebUIManager
from core.utils.schemas import MessageData


class SingleMessageDeleteTests(unittest.IsolatedAsyncioTestCase):

    async def test_chat_history_repo_delete_by_db_id_success(self):
        """测试 ChatHistoryRepository.delete_message_by_db_id 成功删除单条记录并级联清理 forwarded_message"""
        class MockExecuteResult:
            def __init__(self, cursor):
                self.cursor = cursor
            async def __aenter__(self):
                return self.cursor
            async def __aexit__(self, *args):
                pass
            def __await__(self):
                async def _coro():
                    return self.cursor
                return _coro().__await__()

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone = AsyncMock(return_value={
            "id": 42,
            "bot_name": "Giftia",
            "group_or_user_id": "10001",
            "message_id": "msg_999",
            "content": "这是一条测试消息",
        })
        mock_conn.execute = MagicMock(return_value=MockExecuteResult(mock_cursor))
        mock_conn.commit = AsyncMock()

        repo = ChatHistoryRepository(mock_conn)
        res = await repo.delete_message_by_db_id(42)

        self.assertIsNotNone(res)
        self.assertEqual(res["id"], 42)
        self.assertEqual(res["bot_name"], "Giftia")
        self.assertEqual(res["message_id"], "msg_999")
        mock_conn.commit.assert_called_once()

    async def test_chat_history_repo_delete_by_db_id_not_found(self):
        """测试 ChatHistoryRepository.delete_message_by_db_id 不存在时返回 None"""
        class MockExecuteResult:
            def __init__(self, cursor):
                self.cursor = cursor
            async def __aenter__(self):
                return self.cursor
            async def __aexit__(self, *args):
                pass
            def __await__(self):
                async def _coro():
                    return self.cursor
                return _coro().__await__()

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone = AsyncMock(return_value=None)
        mock_conn.execute = MagicMock(return_value=MockExecuteResult(mock_cursor))
        mock_conn.commit = AsyncMock()

        repo = ChatHistoryRepository(mock_conn)
        res = await repo.delete_message_by_db_id(9999)

        self.assertIsNone(res)
        mock_conn.commit.assert_not_called()

    async def test_data_cache_delete_by_db_id(self):
        """测试 DataCache.delete_message_by_db_id 同步从内存缓存中剔除目标消息"""
        mock_db = MagicMock()
        mock_db.delete_message_by_db_id = AsyncMock(return_value={
            "id": 100,
            "bot_name": "Giftia",
            "group_or_user_id": "group_123",
            "message_id": "msg_local_100",
        })

        cache = DataCache.__new__(DataCache)
        cache.db = mock_db
        msg1 = MessageData(db_id=99, message_id="msg_99", content="msg 1")
        msg2 = MessageData(db_id=100, message_id="msg_local_100", content="msg 2")
        msg3 = MessageData(db_id=101, message_id="msg_101", content="msg 3")
        cache.recent_messages = {
            "Giftia:group_123": [msg1, msg2, msg3]
        }

        # 执行删除 id=100
        success = await cache.delete_message_by_db_id(100)
        self.assertTrue(success)
        remaining = cache.recent_messages["Giftia:group_123"]
        self.assertEqual(len(remaining), 2)
        self.assertNotIn(msg2, remaining)
        self.assertIn(msg1, remaining)
        self.assertIn(msg3, remaining)

    async def test_data_cache_delete_by_db_id_returns_false_when_not_found(self):
        """测试 DataCache.delete_message_by_db_id 在底层返回 None 时返回 False"""
        mock_db = MagicMock()
        mock_db.delete_message_by_db_id = AsyncMock(return_value=None)

        cache = DataCache.__new__(DataCache)
        cache.db = mock_db
        cache.recent_messages = {"Giftia:group_123": []}

        success = await cache.delete_message_by_db_id(99999)
        self.assertFalse(success)

    async def test_chat_history_api_delete_single_message_success(self):
        """测试 ChatHistoryApi.delete_single_message 接口成功删除"""
        api = ChatHistoryApi()
        mock_giftia = MagicMock()
        mock_giftia.data_cache.delete_message_by_db_id = AsyncMock(return_value=True)
        api.giftia = mock_giftia

        from astrbot.api.web import request
        request._json = {"id": 123}

        res = await api.delete_single_message()
        self.assertEqual(res["status_code"], 200)
        self.assertEqual(res["body"]["status"], "success")
        mock_giftia.data_cache.delete_message_by_db_id.assert_called_once_with(123)

    async def test_chat_history_api_delete_single_message_invalid_or_missing_id(self):
        """测试 ChatHistoryApi.delete_single_message 缺少或无效参数时返回错误响应"""
        api = ChatHistoryApi()
        api.giftia = MagicMock()
        from astrbot.api.web import request

        # 缺少 id
        request._json = {}
        res = await api.delete_single_message()
        self.assertEqual(res["status_code"], 400)
        self.assertIn("缺少 id 参数", res["body"]["message"])

        # 无效 id
        request._json = {"id": "invalid_number"}
        res2 = await api.delete_single_message()
        self.assertEqual(res2["status_code"], 400)
        self.assertIn("无效的消息 id", res2["body"]["message"])

    async def test_chat_history_api_delete_single_message_not_found(self):
        """测试 ChatHistoryApi.delete_single_message 找不到记录时返回错误响应"""
        api = ChatHistoryApi()
        mock_giftia = MagicMock()
        mock_giftia.data_cache.delete_message_by_db_id = AsyncMock(return_value=False)
        api.giftia = mock_giftia

        from astrbot.api.web import request
        request._json = {"id": 99999}

        res = await api.delete_single_message()
        self.assertEqual(res["status_code"], 400)
        self.assertIn("未找到该消息或已被删除", res["body"]["message"])

    async def test_webui_manager_registers_delete_single_message_route(self):
        """测试 WebUIManager 成功注册 /astrbot_plugin_giftia/chat_history/message/delete 路由"""
        mock_plugin = MagicMock()
        mock_ctx = MagicMock()
        mock_plugin.context = mock_ctx

        manager = WebUIManager(mock_plugin)
        manager.register_routes()

        registered_routes = [call.kwargs.get("route") for call in mock_ctx.register_web_api.call_args_list]
        self.assertIn("/astrbot_plugin_giftia/chat_history/message/delete", registered_routes)

    async def test_chat_history_repo_delete_message_exact_success(self):
        """测试 ChatHistoryRepository.delete_message 按 message_id 精确删除成功并级联清理 forwarded_message"""
        class MockExecuteResult:
            def __init__(self, rowcount=1):
                self.rowcount = rowcount
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def __await__(self):
                async def _coro():
                    return self
                return _coro().__await__()

        executed_sqls = []
        async def mock_execute(sql, params=None):
            executed_sqls.append((sql.strip(), params))
            return MockExecuteResult(rowcount=1)

        mock_conn = MagicMock()
        mock_conn.execute = MagicMock(side_effect=mock_execute)
        mock_conn.commit = AsyncMock()

        repo = ChatHistoryRepository(mock_conn)
        success = await repo.delete_message(
            bot_name="Giftia",
            group_or_user_id="10001",
            message_id="msg_real_123",
        )

        self.assertTrue(success)
        self.assertEqual(len(executed_sqls), 2)
        self.assertIn("DELETE FROM chat_history", executed_sqls[0][0])
        self.assertEqual(executed_sqls[0][1], ("msg_real_123", "10001", "Giftia"))
        self.assertIn("DELETE FROM forwarded_message", executed_sqls[1][0])
        self.assertEqual(executed_sqls[1][1], ("msg_real_123", "10001", "Giftia"))
        mock_conn.commit.assert_called_once()

    async def test_chat_history_repo_delete_message_not_found(self):
        """测试 ChatHistoryRepository.delete_message 未匹配到记录时返回 False 且不级联清理 forwarded_message"""
        class MockExecuteResult:
            def __init__(self, rowcount=0):
                self.rowcount = rowcount
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def __await__(self):
                async def _coro():
                    return self
                return _coro().__await__()

        executed_sqls = []
        async def mock_execute(sql, params=None):
            executed_sqls.append((sql.strip(), params))
            return MockExecuteResult(rowcount=0)

        mock_conn = MagicMock()
        mock_conn.execute = MagicMock(side_effect=mock_execute)
        mock_conn.commit = AsyncMock()

        repo = ChatHistoryRepository(mock_conn)
        success = await repo.delete_message(
            bot_name="Giftia",
            group_or_user_id="10001",
            message_id="msg_non_existent",
        )

        self.assertFalse(success)
        self.assertEqual(len(executed_sqls), 1)
        self.assertIn("DELETE FROM chat_history", executed_sqls[0][0])
        mock_conn.commit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
