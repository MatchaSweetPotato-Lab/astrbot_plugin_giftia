import asyncio
import logging
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite

try:
    import astrbot
except ImportError:
    import types
    astrbot_module = types.ModuleType("astrbot")
    astrbot_api_module = types.ModuleType("astrbot.api")
    astrbot_api_module.logger = logging.getLogger("astrbot")
    astrbot_module.api = astrbot_api_module
    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = astrbot_api_module

from core.database.schema import initialize_database
from core.database.profile_store import ProfileStoreMixin
from core.llm.prompt import build_user_profile_block, build_active_user_briefs, USER_PROFILE_FIELDS
from core.llm.llm_tools import SetUserAvatarDescriptionTool


class DummyDB(ProfileStoreMixin):
    def __init__(self, conn):
        self.conn = conn


class AvatarDescriptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = await aiosqlite.connect(":memory:")
        self.conn.row_factory = aiosqlite.Row
        await initialize_database(self.conn)
        self.db = DummyDB(self.conn)

    async def asyncTearDown(self):
        await self.conn.close()

    async def test_schema_has_avatar_description(self):
        async with self.conn.execute("PRAGMA table_info(user_profiles)") as cursor:
            columns = await cursor.fetchall()
        column_names = {col["name"] for col in columns}
        self.assertIn("avatar_description", column_names)

    async def test_upsert_and_get_avatar_description(self):
        await self.db.upsert_user_profile(
            bot_name="bot1",
            group_or_user_id="group1",
            user_id="user1",
            profile_fields={
                "call_name": "小明",
                "personality": "活泼开朗",
                "avatar_description": "二次元银发蓝瞳少女，表情微笑，背景为淡蓝色星空",
            },
        )

        record = await self.db.get_user_profile_record("bot1", "group1", "user1")
        self.assertIsNotNone(record)
        self.assertEqual(record["call_name"], "小明")
        self.assertEqual(record["personality"], "活泼开朗")
        self.assertEqual(
            record["avatar_description"],
            "二次元银发蓝瞳少女，表情微笑，背景为淡蓝色星空",
        )

        # Update other fields without touching avatar_description
        await self.db.upsert_user_profile(
            bot_name="bot1",
            group_or_user_id="group1",
            user_id="user1",
            profile_fields={
                "interests": "编程与画画",
            },
        )
        updated_record = await self.db.get_user_profile_record("bot1", "group1", "user1")
        self.assertEqual(updated_record["interests"], "编程与画画")
        self.assertEqual(
            updated_record["avatar_description"],
            "二次元银发蓝瞳少女，表情微笑，背景为淡蓝色星空",
        )

    async def test_search_user_profile_by_avatar_description(self):
        await self.db.upsert_user_profile(
            bot_name="bot1",
            group_or_user_id="group1",
            user_id="user2",
            profile_fields={
                "call_name": "猫猫",
                "avatar_description": "戴着红色毛线帽的橘猫特写",
            },
        )

        results = await self.db.search_user_profiles("bot1", "group1", query="毛线帽")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["user_id"], "user2")
        self.assertEqual(results[0]["avatar_description"], "戴着红色毛线帽的橘猫特写")

    def test_user_profile_fields_constant(self):
        field_keys = [field for field, _ in USER_PROFILE_FIELDS]
        self.assertIn("avatar_description", field_keys)

    def test_build_user_profile_block_with_avatar(self):
        profile = {
            "call_name": "测试用户",
            "personality": "幽默",
            "avatar_description": "风景照，落日余晖下的大海与沙滩",
        }
        block = build_user_profile_block(
            user_id="12345",
            user_profile=profile,
            user_relation=(5, "好友"),
            nickname="TestUser",
        )
        self.assertIn('user_id="12345"', block)
        self.assertIn("你的称呼：测试用户", block)
        self.assertIn("性格风格：幽默", block)
        self.assertIn("头像描述：风景照，落日余晖下的大海与沙滩", block)

    def test_build_active_user_briefs_with_avatar(self):
        briefs = [
            {
                "user_id": "67890",
                "nickname": "Alice",
                "call_name": "小爱",
                "aliases": "爱酱",
                "relation": 10,
                "title": "挚友",
                "avatar_description": "黑白线稿简笔画小狗",
            }
        ]
        brief_block = build_active_user_briefs(briefs)
        self.assertIn("<active_user_briefs>", brief_block)
        self.assertIn('user_id="67890"', brief_block)
        self.assertIn("你的称呼：小爱", brief_block)
        self.assertIn("头像描述：黑白线稿简笔画小狗", brief_block)

    async def test_set_user_avatar_description_tool(self):
        mock_plugin = MagicMock()
        mock_plugin.adapter_id_map = {"platform-1": "bot1"}
        mock_plugin.data_cache = AsyncMock()

        tool = SetUserAvatarDescriptionTool(plugin=mock_plugin)
        self.assertEqual(tool.name, "set_user_avatar_description")
        self.assertIn("user_id", tool.parameters["required"])
        self.assertIn("avatar_description", tool.parameters["required"])

        # Test call with missing user_id
        res = await tool.call(context=MagicMock(), user_id="", avatar_description="描述")
        self.assertIn("未提供目标用户的 user_id", res)

        # Test call with missing avatar_description
        res = await tool.call(context=MagicMock(), user_id="123", avatar_description="")
        self.assertIn("未提供头像描述 avatar_description", res)

        # Test successful call
        mock_event = MagicMock(spec=["platform_meta", "get_group_id", "get_sender_id"])
        mock_event.platform_meta = MagicMock(id="platform-1")
        mock_event.get_group_id.return_value = "group1"
        mock_event.get_sender_id.return_value = "sender1"

        res = await tool.call(
            context=mock_event,
            user_id="user_target",
            avatar_description="戴着金丝眼镜的短发青年",
        )
        self.assertIn("已成功将用户 user_target 的头像描述记录至画像库", res)
        mock_plugin.data_cache.set_user_profile.assert_called_once_with(
            bot_name="bot1",
            group_or_user_id="group1",
            user_id="user_target",
            profile_fields={"avatar_description": "戴着金丝眼镜的短发青年"},
            alias_increment_count=False,
        )


if __name__ == "__main__":
    unittest.main()
