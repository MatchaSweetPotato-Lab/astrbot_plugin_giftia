import json
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import pytest
import pytest_asyncio
from core.bot.bot_config_manager import INTERACTIVE_FEATURES_METADATA, BotConfigManager
from core.conversation.action_dispatcher import ActionDispatcher
from core.conversation.reply_pipeline import ReplyPipeline
from core.database.data_cache import DataCache
from core.database.profile_store import ProfileStoreMixin
from core.database.schema import initialize_database
from core.llm.preset_prompts import build_xml_instructions
from core.llm.prompt import (
    USER_PROFILE_FIELDS,
    build_active_user_briefs,
    build_user_profile_block,
)
from core.llm.xml_parse import XmlParse
from core.utils.schemas import FeatureKey


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
        updated_record = await self.db.get_user_profile_record(
            "bot1", "group1", "user1"
        )
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


@pytest_asyncio.fixture
async def avatar_runtime():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await initialize_database(conn)
        db = DummyDB(conn)
        cache = DataCache.__new__(DataCache)
        cache.db = db
        cache.user_profile_records = {}
        cache.user_profiles = {}
        cache.is_bot_muted = Mock(return_value=False)
        cache.add_message = AsyncMock()
        plugin = SimpleNamespace(
            data_cache=cache,
            get_bot_config=Mock(
                return_value={"enabled_interactive_features": [FeatureKey.SET_AVATAR]}
            ),
            message_parser=SimpleNamespace(
                chain_to_result=AsyncMock(
                    return_value=SimpleNamespace(
                        content="reply", media_id_list=[], forward_messages=[]
                    )
                )
            ),
        )
        event = SimpleNamespace(
            get_group_id=Mock(return_value="group1"),
            get_platform_name=lambda: "generic",
            get_self_id=lambda: "self",
            send=AsyncMock(),
        )
        yield SimpleNamespace(
            db=db,
            cache=cache,
            plugin=plugin,
            event=event,
            parser=XmlParse(None, None),
            dispatcher=ActionDispatcher(plugin),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("group_id,session_id", [("group1", "group1"), ("", "sender1")])
async def test_avatar_action_saves_current_session_and_sends_reply(
    avatar_runtime, group_id, session_id
):
    rt = avatar_runtime
    rt.event.get_group_id.return_value = group_id
    await rt.db.upsert_user_profile(
        "bot1",
        session_id,
        "target",
        profile_fields={"call_name": "小明", "avatar_description": "旧头像"},
    )
    await rt.db.upsert_user_profile(
        "bot1",
        "other-session",
        "target",
        profile_fields={"avatar_description": "其他会话"},
    )
    await rt.cache.get_user_profile_record("bot1", session_id, "target")
    result = await rt.parser.decode_llm_xml(
        '<set_avatar user_id=" target " group_or_user_id="other-session" bot_name="other-bot">'
        " 黑白线稿，猫 &amp; 狗\n衣服上写着 &quot;Hi&quot; "
        "</set_avatar><message>真可爱！</message>",
        session_id,
    )
    assert result.tools_to_call == []
    assert ReplyPipeline._has_non_message_work(result)
    await rt.dispatcher.dispatch_actions(rt.event, "bot1", "Bot", session_id, result)
    record = await rt.cache.get_user_profile_record("bot1", session_id, "target")
    assert record["avatar_description"] == '黑白线稿，猫 & 狗\n衣服上写着 "Hi"'
    assert record["call_name"] == "小明"
    assert (await rt.db.get_user_profile_record("bot1", "other-session", "target"))[
        "avatar_description"
    ] == "其他会话"
    assert (
        await rt.db.get_user_profile_record("other-bot", session_id, "target") is None
    )
    rt.event.send.assert_awaited_once()
    assert rt.event.send.call_args.args[0].chain[0].text == "真可爱！"
    log = next(
        call.args[2].content
        for call in rt.cache.add_message.await_args_list
        if call.args[2].role == "operation_log"
    )
    node = ET.fromstring(log)
    assert node.attrib["result"] == "success"
    assert node.attrib["avatar_description"] == record["avatar_description"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["disabled", "missing_user", "empty_description", "database_error"]
)
async def test_failed_avatar_action_preserves_profile_and_reply(avatar_runtime, mode):
    rt = avatar_runtime
    await rt.db.upsert_user_profile(
        "bot1", "group1", "target", profile_fields={"avatar_description": "原描述"}
    )
    if mode == "disabled":
        rt.plugin.get_bot_config.return_value = {"enabled_interactive_features": []}
    if mode == "database_error":
        rt.cache.set_user_profile = AsyncMock(
            side_effect=RuntimeError('cannot save "avatar" & data')
        )
    user_id = "" if mode == "missing_user" else "target"
    description = " " if mode == "empty_description" else "新描述"
    result = await rt.parser.decode_llm_xml(
        f'<set_avatar user_id="{user_id}">{description}</set_avatar><message>回复</message>',
        "group1",
    )
    await rt.dispatcher.dispatch_actions(rt.event, "bot1", "Bot", "group1", result)
    assert (await rt.db.get_user_profile_record("bot1", "group1", "target"))[
        "avatar_description"
    ] == "原描述"
    rt.event.send.assert_awaited_once()
    log = next(
        call.args[2].content
        for call in rt.cache.add_message.await_args_list
        if call.args[2].role == "operation_log"
    )
    assert ET.fromstring(log).attrib["result"] == "failed"


def test_avatar_instructions_and_bot_metadata_use_feature_switch():
    feature = FeatureKey.SET_AVATAR
    assert feature not in build_xml_instructions([])
    assert feature in build_xml_instructions([feature])
    assert feature in build_xml_instructions([feature], is_qq_official=True)
    assert feature in {item["key"] for item in INTERACTIVE_FEATURES_METADATA}


def test_avatar_configuration_has_no_legacy_switch():
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text()
    )
    assert "set_user_avatar_description_enabled" not in schema["tools_config"]["items"]
    manager = BotConfigManager.__new__(BotConfigManager)
    feature = FeatureKey.SET_AVATAR
    assert feature in manager.normalize_bot_config({})["enabled_interactive_features"]
    assert (
        manager.normalize_bot_config({"enabled_interactive_features": []})[
            "enabled_interactive_features"
        ]
        == []
    )
    assert manager.normalize_bot_config({"enabled_interactive_features": ["poke"]})[
        "enabled_interactive_features"
    ] == ["poke"]
