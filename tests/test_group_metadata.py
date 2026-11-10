"""Group names are observed independently from conversation and memory state."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest
import pytest_asyncio
from core.database import data_cache as cache_module
from core.database.data_cache import DataCache
from core.database.database import Database
from core.database.schema import initialize_database
from core.llm.prompt import build_reply_prompt
from core.utils.schemas import MessageData
from core.web.web_api import GiftiaWebApi
from fastapi import FastAPI, Request

from astrbot.api.web import PluginRequest, bind_request_context


@pytest_asyncio.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
    clock = SimpleNamespace(now=1794268800.0, monotonic=100.0)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(clock.now, tz)

    monkeypatch.setattr(cache_module, "datetime", Clock)
    monkeypatch.setattr(
        cache_module, "time", SimpleNamespace(monotonic=lambda: clock.monotonic)
    )
    async with aiosqlite.connect(tmp_path / "metadata.db") as conn:
        conn.row_factory = aiosqlite.Row
        await initialize_database(conn)
        db = Database(conn)
        plugin = SimpleNamespace(
            db=db,
            running_tasks={},
            bot_map={"bot": {}},
            bot_config_manager=SimpleNamespace(load_bots=lambda: [{"name": "bot"}]),
        )
        cache = DataCache(db, None, None, plugin=plugin)
        plugin.data_cache = cache
        event = SimpleNamespace(
            get_group_id=lambda: "123",
            get_self_id=lambda: "999",
            message_obj=SimpleNamespace(
                group=SimpleNamespace(group_name=None), raw_message={}
            ),
            bot=SimpleNamespace(
                call_action=AsyncMock(
                    return_value={"group_id": 123, "group_name": "群 A"}
                )
            ),
        )

        async def settle():
            while plugin.running_tasks:
                await asyncio.gather(*list(plugin.running_tasks.values()))
                await asyncio.sleep(0)

        try:
            yield SimpleNamespace(
                db=db,
                cache=cache,
                plugin=plugin,
                clock=clock,
                event=event,
                settle=settle,
            )
        finally:
            for task in list(plugin.running_tasks.values()):
                task.cancel()
            await asyncio.gather(
                *list(plugin.running_tasks.values()), return_exceptions=True
            )


@pytest.mark.asyncio
async def test_inbound_name_is_persistent_scoped_and_not_written_for_every_message(
    runtime,
):
    runtime.event.message_obj.group.group_name = "群 A"
    await runtime.cache.observe_group_metadata("bot", runtime.event)
    changes = runtime.db.conn.total_changes
    for _ in range(3):
        await runtime.cache.observe_group_metadata("bot", runtime.event)
    assert runtime.db.conn.total_changes == changes
    assert await runtime.db.get_group_names("bot", ["123", "unknown", "123"]) == {
        "123": "群 A"
    }
    assert await runtime.db.get_group_names("other-bot", ["123"]) == {}
    restarted = DataCache(runtime.db, None, None)
    await restarted.observe_group_metadata("bot", runtime.event)
    runtime.event.bot.call_action.assert_not_awaited()
    async with runtime.db.query("SELECT COUNT(*) FROM group_profiles") as cursor:
        assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_missing_name_refresh_is_nonblocking_deduplicated_and_uses_only_group_info(
    runtime,
):
    started, release = asyncio.Event(), asyncio.Event()

    async def lookup(**kwargs):
        started.set()
        await release.wait()
        return {"group_id": 123, "group_name": "查询到的群名"}

    runtime.event.bot.call_action.side_effect = lookup
    await runtime.cache.observe_group_metadata("bot", runtime.event)
    await started.wait()
    await asyncio.gather(
        *(runtime.cache.observe_group_metadata("bot", runtime.event) for _ in range(5))
    )
    runtime.event.bot.call_action.assert_awaited_once_with(
        action="get_group_info", group_id=123, no_cache=True, self_id="999"
    )
    assert len(runtime.plugin.running_tasks) == 1
    release.set()
    await runtime.settle()
    assert await runtime.db.get_group_names("bot", ["123"]) == {"123": "查询到的群名"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        None,
        {"group_name": ""},
        {"group_id": 456, "group_name": "错误群"},
        {"status": "failed", "retcode": 1, "data": {"group_name": "失败结果"}},
    ],
)
async def test_failed_refresh_preserves_old_name_and_throttles_retry(runtime, response):
    await runtime.cache.remember_group_name("bot", "123", "旧名称")
    original = await runtime.db.get_group_metadata("bot", "123")
    runtime.clock.now += 24 * 60 * 60
    runtime.event.bot.call_action.return_value = response
    await runtime.cache.observe_group_metadata("bot", runtime.event)
    await runtime.settle()
    assert await runtime.db.get_group_metadata("bot", "123") == original
    await runtime.cache.observe_group_metadata("bot", runtime.event)
    runtime.event.bot.call_action.assert_awaited_once()
    runtime.clock.monotonic += 61
    runtime.event.bot.call_action.return_value = {"group_name": "新名称"}
    await runtime.cache.observe_group_metadata("bot", runtime.event)
    await runtime.settle()
    assert runtime.event.bot.call_action.await_count == 2
    assert (await runtime.db.get_group_metadata("bot", "123"))["group_name"] == "新名称"


@pytest.mark.asyncio
async def test_rename_notice_wins_over_a_lookup_already_in_flight(runtime):
    await runtime.cache.remember_group_name("bot", "123", "旧名称")
    runtime.clock.now += 24 * 60 * 60
    started, release = asyncio.Event(), asyncio.Event()

    async def lookup(**kwargs):
        started.set()
        await release.wait()
        return {"group_name": "查询开始时的旧名称"}

    runtime.event.bot.call_action.side_effect = lookup
    await runtime.cache.observe_group_metadata("bot", runtime.event)
    await started.wait()
    runtime.event.message_obj.group.group_name = "旧名称"
    runtime.event.message_obj.raw_message = {
        "post_type": "notice",
        "notice_type": "notify",
        "sub_type": "group_name",
        "name_new": "改名后的群 & <名称>",
    }
    await runtime.cache.observe_group_metadata("bot", runtime.event)
    release.set()
    await runtime.settle()
    assert await runtime.db.get_group_names("bot", ["123"]) == {
        "123": "改名后的群 & <名称>"
    }


@pytest.mark.asyncio
async def test_empty_names_and_private_messages_do_not_clear_or_create_group_metadata(
    runtime,
):
    await runtime.cache.remember_group_name("bot", "123", "已知名称")
    for name in (None, "", "   "):
        await runtime.cache.remember_group_name("bot", "123", name, refreshed=True)
    runtime.event.get_group_id = lambda: ""
    await runtime.cache.observe_group_metadata("bot", runtime.event)
    runtime.event.bot.call_action.assert_not_awaited()
    assert await runtime.db.get_group_names("bot", ["123"]) == {"123": "已知名称"}


@pytest.mark.asyncio
async def test_dashboard_group_names_keep_bot_scope_and_filter_ids(runtime):
    await runtime.db.upsert_group_metadata(
        "bot", "123", "兴趣群 & <讨论>", runtime.clock.now
    )
    await runtime.db.upsert_group_metadata(
        "other-bot", "123", "另一个 Bot 的名称", runtime.clock.now
    )
    await runtime.db.insert_message(
        "bot",
        MessageData(
            group_or_user_id="123", user_id="alice", message_id="1", content="hello"
        ),
    )
    await runtime.db.upsert_group_profile(
        bot_name="bot", group_or_user_id="123", profile="群规内容"
    )
    await runtime.db.conn.execute(
        "INSERT INTO slang (bot_name, group_or_user_id, term, description, created_at, updated_at) "
        "VALUES ('bot', '123', '词语', '解释', '2026-11-10', '2026-11-10')"
    )
    await runtime.db.conn.commit()
    api = GiftiaWebApi(runtime.plugin)
    handlers = {
        "chat_history/filter_options": api.get_chat_history_filter_options,
        "profiles/group/filter_options": api.get_group_profile_filter_options,
        "profiles/group": api.get_group_profiles,
        "slang/filter_options": api.get_slang_filter_options,
    }
    app = FastAPI()

    @app.get("/{endpoint:path}")
    async def dispatch(endpoint: str, request: Request):
        with bind_request_context(PluginRequest(request)):
            return await handlers[endpoint]()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        for endpoint in handlers:
            result = (
                await client.get(
                    f"/{endpoint}",
                    params={"bot_name": "bot", "group_or_user_id": "123"},
                )
            ).json()
            assert result["status"] == "success", result
            records = result["data"].get("sessions", result["data"].get("items"))
            assert len(records) == 1, (endpoint, result)
            assert records[0]["group_or_user_id"] == "123"
            assert records[0]["group_name"] == "兴趣群 & <讨论>"
    runtime.event.bot.call_action.assert_not_awaited()


def test_cross_group_names_are_escaped_and_group_ids_remain_available():
    from xml.etree.ElementTree import fromstring

    prompt = build_reply_prompt(
        recent_messages=[],
        media_captions=[],
        bot_status=None,
        cross_group_context={"123": [MessageData(user_id="alice", content="earlier")]},
        cross_group_names={"123": '兴趣群 & <讨论> "A"'},
    )
    group = fromstring(f"<root>{prompt}</root>").find("cross_group_context/group")
    assert group.get("group_id") == "123"
    assert group.get("group_name") == '兴趣群 & <讨论> "A"'
    assert group.get("source") == '来源群聊：兴趣群 & <讨论> "A"（123）'
