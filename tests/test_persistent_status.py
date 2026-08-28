import sys
import types
import unittest

if "astrbot" not in sys.modules:
    import logging
    astrbot_module = types.ModuleType("astrbot")
    astrbot_api_module = types.ModuleType("astrbot.api")
    astrbot_api_module.logger = logging.getLogger("astrbot")
    astrbot_module.api = astrbot_api_module

    astrbot_star_module = types.ModuleType("astrbot.api.star")
    class Context: pass
    class StarTools: pass
    astrbot_star_module.Context = Context
    astrbot_star_module.StarTools = StarTools
    astrbot_api_module.star = astrbot_star_module

    astrbot_core_module = types.ModuleType("astrbot.core")
    astrbot_core_msg_module = types.ModuleType("astrbot.core.message")
    astrbot_core_msg_comp_module = types.ModuleType("astrbot.core.message.components")

    class BaseMessageComponent: pass
    class AstrBotConfig: pass
    astrbot_core_msg_comp_module.BaseMessageComponent = BaseMessageComponent
    astrbot_core_msg_module.components = astrbot_core_msg_comp_module
    astrbot_core_module.message = astrbot_core_msg_module
    astrbot_core_module.AstrBotConfig = AstrBotConfig
    astrbot_module.core = astrbot_core_module

    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = astrbot_api_module
    sys.modules["astrbot.api.star"] = astrbot_star_module
    sys.modules["astrbot.core"] = astrbot_core_module
    sys.modules["astrbot.core.message"] = astrbot_core_msg_module
    sys.modules["astrbot.core.message.components"] = astrbot_core_msg_comp_module

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

from core.utils.schemas import FeatureKey, Status, XmlLlmResult, FLAT_CLOSABLE_TAGS
from core.llm.preset_prompts import build_xml_instructions
from core.llm.prompt import build_persistent_status_block


class PersistentStatusTests(unittest.TestCase):
    def test_schemas(self):
        self.assertEqual(FeatureKey.PERSISTENT_STATUS, "persistent_status")
        self.assertIn("set_status", FLAT_CLOSABLE_TAGS)

        status = Status(mood="开心", custom_status={"服装": "制服"})
        self.assertEqual(status.custom_status, {"服装": "制服"})

        result = XmlLlmResult()
        self.assertEqual(result.set_custom_status, {})
        result.set_custom_status["服装"] = "女仆装"
        self.assertEqual(result.set_custom_status, {"服装": "女仆装"})

    def test_build_persistent_status_block(self):
        # Empty
        self.assertEqual(build_persistent_status_block(None), "")
        self.assertEqual(build_persistent_status_block(Status()), "")
        self.assertEqual(build_persistent_status_block(Status(custom_status={})), "")
        self.assertEqual(build_persistent_status_block(Status(custom_status={" ": ""})), "")

        # Filled
        status = Status(custom_status={"当前服装": "粉色毛衣", "所在场景": "图书馆"})
        block = build_persistent_status_block(status)
        self.assertEqual(
            block,
            "<bot_persistent_status>\n当前服装：粉色毛衣\n所在场景：图书馆\n</bot_persistent_status>"
        )

    def test_preset_prompts_instruction_toggle(self):
        # Enabled
        inst_enabled = build_xml_instructions([FeatureKey.PERSISTENT_STATUS])
        self.assertIn("set_status", inst_enabled)
        self.assertIn("常驻/低频状态管理", inst_enabled)

        # Disabled
        inst_disabled = build_xml_instructions([FeatureKey.POKE])
        self.assertNotIn("set_status", inst_disabled)
        self.assertNotIn("常驻/低频状态管理", inst_disabled)

    def test_xml_parser_set_status(self):
        try:
            from core.llm.xml_parse import XmlParse
        except ImportError:
            return

        class DummyCache:
            pass
        class DummyEmoji:
            pass

        parser = XmlParse(DummyCache(), DummyEmoji())

        # Test key/value format
        xml1 = '<status>心情: 开心\n状态: 发呆</status><set_status key="当前服装" value="水手服"/><message>你好</message>'
        import asyncio
        res1 = asyncio.run(parser.decode_llm_xml(xml1, "group_1"))
        self.assertIsNotNone(res1)
        self.assertEqual(res1.set_custom_status, {"当前服装": "水手服"})

        # Test multi-attribute format with Chinese attribute keys (including anti-drool cleaning)
        xml2 = '<status>心情: 开心\n状态: 发呆</status><set_status 当前服装="睡衣" 所在场景="卧室"/><message>晚安</message>'
        res2 = asyncio.run(parser.decode_llm_xml(xml2, "group_1"))
        self.assertIsNotNone(res2)
        self.assertEqual(res2.set_custom_status, {"当前服装": "睡衣", "所在场景": "卧室"})

        # Test child text format
        xml3 = '<set_status key="随身物品">吉他</set_status>'
        res3 = asyncio.run(parser.decode_llm_xml(xml3, "group_1"))
        self.assertIsNotNone(res3)
        self.assertEqual(res3.set_custom_status, {"随身物品": "吉他"})

    def test_legacy_bot_config_no_auto_migration(self):
        from core.bot.bot_config_manager import BotConfigManager
        mgr = BotConfigManager(None)
        legacy_bot = {
            "name": "TestBot",
            "enabled_interactive_features": ["poke", "sticker", "task_board"]
        }
        normalized = mgr.normalize_bot_config(legacy_bot)
        # Verify that persistent_status is NOT force-injected into existing configurations
        self.assertNotIn(FeatureKey.PERSISTENT_STATUS, normalized["enabled_interactive_features"])

    def test_datacache_custom_status_merging(self):
        import asyncio
        from core.database.data_cache import DataCache

        class FakeDB:
            def __init__(self):
                self.statuses = {}
            async def get_bot_status(self, group_or_user_id, bot_name):
                return self.statuses.get(f"{bot_name}:{group_or_user_id}", Status(custom_status={}))
            async def upsert_bot_status(self, group_or_user_id, bot_name, status):
                self.statuses[f"{bot_name}:{group_or_user_id}"] = status

        class FakePlugin:
            def __init__(self, db):
                self.db = db

        async def run_test():
            db = FakeDB()
            plugin = FakePlugin(db)
            cache = DataCache(db=db, http_manager=None, ltm=None, plugin=plugin)

            # Update custom status
            await cache.update_bot_custom_status("Giftia", "g1", {"服装": "女仆装", "地点": "咖啡厅"})
            s1 = await cache.get_bot_status("Giftia", "g1")
            self.assertEqual(s1.custom_status, {"服装": "女仆装", "地点": "咖啡厅"})

            # Normal high-frequency status update should not overwrite custom_status
            hf = Status(mood="高兴", state="忙碌", custom_status={})
            await cache.set_bot_status("Giftia", "g1", hf)
            s2 = await cache.get_bot_status("Giftia", "g1")
            self.assertEqual(s2.mood, "高兴")
            self.assertEqual(s2.custom_status, {"服装": "女仆装", "地点": "咖啡厅"})

            # Partial key update and deletion
            await cache.update_bot_custom_status("Giftia", "g1", {"服装": "日常校服", "地点": ""})
            s3 = await cache.get_bot_status("Giftia", "g1")
            self.assertEqual(s3.custom_status, {"服装": "日常校服"})

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
