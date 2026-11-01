"""Account categories are confirmed once per shared sender profile."""

import asyncio
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import pytest
import pytest_asyncio
from core.conversation.chat_manager import ChatManager
from core.database import data_cache as cache_module
from core.database.data_cache import DataCache
from core.database.database import Database
from core.database.schema import initialize_database
from core.utils.message_parse import MessageParser

from astrbot.api.message_components import Plain


@pytest_asyncio.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(
        cache_module, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    async with aiosqlite.connect(tmp_path / "profiles.db") as conn:
        conn.row_factory = aiosqlite.Row
        await initialize_database(conn)
        db = Database(conn)
        yield SimpleNamespace(db=db, cache=DataCache(db, None, None), clock=clock)


@pytest.fixture
def incoming():
    def make_event(group_id="123", user_id="1001", client=None):
        """Build an incoming sender with a controllable group API.

        Args:
            group_id: Current group, empty for a private message.
            user_id: Actual sender's account.
            client: Optional API client shared by multiple events.

        Returns:
            Event exposing the routing information needed for initialization.
        """
        return SimpleNamespace(
            get_sender_id=lambda: user_id,
            get_group_id=lambda: group_id,
            get_self_id=lambda: "999",
            bot=client
            or SimpleNamespace(call_action=AsyncMock(return_value={"is_robot": True})),
        )

    return make_event


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "is_robot,expected", [(True, "official_bot"), (False, "qq_account")]
)
@pytest.mark.parametrize("envelope", [False, True])
async def test_success_is_shared_across_groups_private_chats_and_restarts(
    runtime, incoming, is_robot, expected, envelope
):
    event = incoming()
    info = {"user_id": 1001, "group_id": 123, "is_robot": is_robot}
    event.bot.call_action.return_value = (
        {"status": "ok", "retcode": 0, "data": info} if envelope else info
    )
    for group in ("123", "456", ""):
        current = incoming(group, client=event.bot)
        await runtime.cache.initialize_user_profile("bot", group or "1001", current)
        profile = await runtime.cache.get_user_profile_record(
            "bot", group or "1001", "1001"
        )
        assert profile["account_type"] == expected
        assert profile["base_relation"] == 0 and profile["relation"] == 50
        assert profile["total_count"] == 0
    event.bot.call_action.assert_awaited_once_with(
        action="get_group_member_info",
        group_id=123,
        user_id=1001,
        no_cache=False,
        self_id="999",
    )
    restarted = DataCache(runtime.db, None, None)
    await restarted.initialize_user_profile(
        "bot", "789", incoming("789", client=event.bot)
    )
    assert event.bot.call_action.await_count == 1
    async with runtime.db.query(
        "SELECT group_or_user_id FROM user_profile_sessions WHERE bot_name='bot' ORDER BY group_or_user_id"
    ) as cursor:
        assert [row[0] for row in await cursor.fetchall()] == [
            "1001",
            "123",
            "456",
            "789",
        ]


@pytest.mark.asyncio
async def test_private_or_unsupported_initialization_is_completed_by_a_later_group(
    runtime, incoming
):
    private = incoming("")
    await runtime.cache.initialize_user_profile("bot", "1001", private)
    private.bot.call_action.assert_not_awaited()
    profile = await runtime.cache.get_user_profile_record("bot", "1001", "1001")
    assert profile["account_type"] == "unknown"
    unsupported = incoming("123")
    unsupported.bot = SimpleNamespace()
    await runtime.cache.initialize_user_profile("bot", "123", unsupported)
    await runtime.cache.initialize_user_profile("bot", "456", incoming("456"))
    profile = await runtime.cache.get_user_profile_record("bot", "1001", "1001")
    assert profile["account_type"] == "official_bot"


@pytest.mark.asyncio
async def test_existing_unknown_profile_keeps_background_and_ratings(runtime, incoming):
    await runtime.cache.set_user_profile(
        "bot",
        "123",
        "1001",
        base_relation=17,
        profile_fields={
            "call_name": "小祥",
            "attitude": "熟络",
            "avatar_description": "猫",
        },
    )
    await runtime.cache.update_relation("bot", "123", "1001", 2, "他认真道谢")
    before = await runtime.cache.get_user_profile_record("bot", "123", "1001")
    history = await runtime.db.get_relation_changes("bot", "123", "1001")
    await runtime.cache.initialize_user_profile("bot", "123", incoming())
    after = await runtime.cache.get_user_profile_record("bot", "123", "1001")
    assert after == {**before, "account_type": "official_bot"}
    assert await runtime.db.get_relation_changes("bot", "123", "1001") == history


@pytest.mark.asyncio
@pytest.mark.parametrize("known_type", ["official_bot", "qq_account"])
async def test_confirmed_profiles_are_not_queried_or_rewritten(
    runtime, incoming, known_type
):
    await runtime.cache.set_user_profile(
        "bot", "123", "1001", profile_fields={"account_type": known_type}
    )
    await runtime.db.conn.execute(
        "UPDATE user_profiles SET updated_at='2000-01-01' WHERE bot_name='bot'"
    )
    await runtime.db.conn.commit()
    event = incoming("456")
    await runtime.cache.initialize_user_profile("bot", "456", event)
    event.bot.call_action.assert_not_awaited()
    async with runtime.db.query(
        "SELECT account_type, updated_at FROM user_profiles"
    ) as cursor:
        assert tuple(await cursor.fetchone()) == (known_type, "2000-01-01")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"is_robot": None},
        {"is_robot": "false"},
        {"is_robot": 0},
        {"is_robot": 1},
        {"status": "failed", "retcode": 1, "data": {"is_robot": True}},
        {"status": "ok", "retcode": 1, "data": {"is_robot": True}},
        {"status": "ok", "retcode": 0, "data": []},
        {"is_robot": True, "user_id": 2002},
        {"is_robot": True, "group_id": 456},
    ],
)
async def test_invalid_responses_stay_unknown_and_retry_after_shared_cooldown(
    runtime, incoming, response
):
    event = incoming()
    event.bot.call_action.return_value = response
    await runtime.cache.initialize_user_profile("bot", "123", event)
    profile = await runtime.cache.get_user_profile_record("bot", "123", "1001")
    assert profile["account_type"] == "unknown"
    other_group = incoming("456", client=event.bot)
    await runtime.cache.initialize_user_profile("bot", "456", other_group)
    assert event.bot.call_action.await_count == 1
    runtime.clock.now += 61
    event.bot.call_action.return_value = {"is_robot": False}
    await runtime.cache.initialize_user_profile("bot", "456", other_group)
    assert event.bot.call_action.await_count == 2
    profile = await runtime.cache.get_user_profile_record("bot", "123", "1001")
    assert profile["account_type"] == "qq_account"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "timeout"])
async def test_lookup_failures_do_not_abort_initialization(
    runtime, incoming, monkeypatch, failure
):
    event = incoming()
    if failure == "exception":
        event.bot.call_action.side_effect = RuntimeError("API unavailable")
    else:
        monkeypatch.setattr(cache_module, "ACCOUNT_TYPE_QUERY_TIMEOUT", 0.01)

        async def stalled(**kwargs):
            await asyncio.Event().wait()

        event.bot.call_action.side_effect = stalled
    await asyncio.wait_for(
        runtime.cache.initialize_user_profile("bot", "123", event), 0.5
    )
    assert (await runtime.cache.get_user_profile_record("bot", "123", "1001"))[
        "account_type"
    ] == "unknown"


@pytest.mark.asyncio
async def test_concurrent_groups_share_one_lookup_without_holding_the_database(
    runtime, incoming
):
    started, release = asyncio.Event(), asyncio.Event()

    async def lookup(**kwargs):
        started.set()
        await release.wait()
        return {"is_robot": True}

    client = SimpleNamespace(call_action=AsyncMock(side_effect=lookup))
    tasks = [
        asyncio.create_task(
            runtime.cache.initialize_user_profile(
                "bot", group, incoming(group, client=client)
            )
        )
        for group in ("123", "456")
    ]
    try:
        await asyncio.wait_for(started.wait(), 0.5)
        await asyncio.wait_for(
            runtime.cache.initialize_user_profile(
                "bot", "789", incoming("789", "2002")
            ),
            0.5,
        )
        assert client.call_action.await_count == 1
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert client.call_action.await_count == 1
    for group in ("123", "456"):
        profile = await runtime.cache.get_user_profile_record("bot", group, "1001")
        assert profile["account_type"] == "official_bot"


@pytest.mark.asyncio
async def test_manual_edit_during_lookup_is_preserved(runtime, incoming):
    started, release = asyncio.Event(), asyncio.Event()

    async def lookup(**kwargs):
        started.set()
        await release.wait()
        return {"is_robot": True}

    event = incoming(client=SimpleNamespace(call_action=AsyncMock(side_effect=lookup)))
    task = asyncio.create_task(
        runtime.cache.initialize_user_profile("bot", "123", event)
    )
    try:
        await asyncio.wait_for(started.wait(), 0.5)
        await runtime.cache.set_user_profile(
            "bot", "123", "1001", profile_fields={"account_type": "qq_account"}
        )
    finally:
        release.set()
        await task
    assert (await runtime.cache.get_user_profile_record("bot", "123", "1001"))[
        "account_type"
    ] == "qq_account"


@pytest.mark.asyncio
async def test_profiles_and_lookup_completion_are_isolated_by_bot(runtime, incoming):
    event = incoming()
    await runtime.cache.initialize_user_profile("first", "123", event)
    event.bot.call_action.return_value = {"is_robot": False}
    await runtime.cache.initialize_user_profile("second", "123", event)
    assert event.bot.call_action.await_count == 2
    assert (await runtime.cache.get_user_profile_record("first", "123", "1001"))[
        "account_type"
    ] == "official_bot"
    assert (await runtime.cache.get_user_profile_record("second", "123", "1001"))[
        "account_type"
    ] == "qq_account"


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", ["", "999"])
async def test_missing_and_self_senders_never_create_profiles(
    runtime, incoming, user_id
):
    event = incoming(user_id=user_id)
    await runtime.cache.initialize_user_profile("bot", "123", event)
    assert await runtime.db.get_user_profile_record("bot", "123", user_id) is None
    event.bot.call_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [False, True])
async def test_real_incoming_message_initializes_before_admission_without_passive_memory(
    runtime, incoming, allowed
):
    plugin = SimpleNamespace(
        db=runtime.db,
        data_cache=runtime.cache,
        adapter_id_map={"adapter": "bot"},
        bot_map={"bot": {}},
        active_reply_counters={},
        parse_locks=defaultdict(asyncio.Lock),
        passive_memory_enabled=False,
        message_parser=MessageParser(runtime.cache, None, False, False, None),
    )
    event = incoming()
    event.platform_meta = SimpleNamespace(id="adapter")
    event.unified_msg_origin = "adapter:GroupMessage:123"
    event.message_obj = SimpleNamespace(
        timestamp=1793443198, message_id="first", raw_message={"post_type": "message"}
    )
    event.get_messages = lambda: [Plain("hello")]
    event.get_sender_name = lambda: "Member"
    event.get_platform_name = lambda: "aiocqhttp"
    manager = ChatManager(plugin)
    manager._check_command_info = Mock(return_value=(False, False))
    manager.decision_engine.check_whitelists = Mock(return_value=allowed)

    async def admit(*args):
        profile = await runtime.cache.get_user_profile_record("bot", "123", "1001")
        assert profile["account_type"] == "official_bot"
        assert args[-1].user_id == "1001"

    manager._enqueue_message = AsyncMock(side_effect=admit)
    await manager.job(event)
    if allowed:
        manager._enqueue_message.assert_awaited_once()
        event.bot.call_action.assert_awaited_once()
        assert (await runtime.db.get_messages("123", "bot"))[0].content == "hello"
    else:
        manager._enqueue_message.assert_not_awaited()
        event.bot.call_action.assert_not_awaited()
        assert await runtime.db.get_user_profile_record("bot", "123", "1001") is None
