"""Known group context follows each member's own recorded speech."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from xml.etree.ElementTree import fromstring

import aiosqlite
import pytest
import pytest_asyncio
from core.database import profile_store
from core.database.database import Database
from core.database.schema import initialize_database
from core.llm.prompt import build_user_profile_block
from core.utils.schemas import MessageData


@pytest_asyncio.fixture
async def runtime(monkeypatch):
    clock = SimpleNamespace(now=datetime(2026, 10, 29, 12))
    monkeypatch.setattr(
        profile_store,
        "datetime",
        SimpleNamespace(now=lambda: clock.now, fromisoformat=datetime.fromisoformat),
    )
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await initialize_database(conn)
        yield SimpleNamespace(db=Database(conn), clock=clock)


@pytest.mark.asyncio
async def test_groups_are_deduplicated_scoped_and_ordered_by_member_speech(runtime):
    now = runtime.clock.now
    rows = [
        ("bot", "current", "alice", now, "message", 0),
        ("bot", "alice", "alice", now, "message", 0),
        ("bot", "", "alice", now, "message", 0),
        ("bot", None, "alice", now, "message", 0),
        ("other-bot", "foreign-bot", "alice", now, "message", 0),
        ("bot", "foreign-member", "bob", now, "message", 0),
        ("bot", "operation-only", "alice", now, "operation_log", 0),
        ("bot", "recalled-only", "alice", now, "message", 1),
        ("bot", "recent", "alice", now - timedelta(hours=1), "message", 0),
        ("bot", "recent", "alice", now - timedelta(days=10), "message", 0),
        ("bot", "b-tied", "alice", now - timedelta(hours=2), "message", 0),
        ("bot", "a-tied", "alice", now - timedelta(hours=2), "message", 0),
        ("bot", "old", "alice", now - timedelta(days=3), "message", 0),
        ("bot", "old", "alice", now, "operation_log", 0),
        ("bot", "old", "alice", now, "message", 1),
        ("bot", "old", "bob", now, "message", 0),
        ("bot", "older", "alice", now - timedelta(days=5), "message", 0),
        ("bot", "legacy", "alice", now - timedelta(days=6), None, None),
    ]
    await runtime.db.conn.executemany(
        "INSERT INTO chat_history "
        "(bot_name, group_or_user_id, user_id, created_at, role, is_recalled) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (bot, group, user, time.isoformat(), role, recalled)
            for bot, group, user, time, role, recalled in rows
        ],
    )
    await runtime.db.conn.commit()
    groups = await runtime.db.get_user_common_groups(
        "bot", "current", ["alice", "bob", "unknown", "alice"]
    )
    assert [(g["group_id"], g["recently_active"]) for g in groups["alice"]] == [
        ("recent", True),
        ("a-tied", True),
        ("b-tied", True),
        ("old", False),
        ("older", False),
        ("legacy", False),
    ]
    assert {g["group_id"] for g in groups["bob"]} == {"foreign-member", "old"}
    assert groups["unknown"] == []
    assert await runtime.db.get_user_common_groups("bot", "current", []) == {}
    assert [
        g["group_id"]
        for g in (await runtime.db.get_user_common_groups("bot", "alice", ["alice"]))[
            "alice"
        ]
    ][0] == "current"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "time,active",
    [
        ("2026-10-29T12:00:00", True),
        ("2026-10-28T12:00:00", True),
        ("2026-10-28T11:59:59", False),
        ("2026-10-29T12:00:01", False),
        ("invalid-time", False),
        ("", False),
        (None, False),
    ],
)
async def test_recent_activity_requires_a_message_in_the_past_24_hours(
    runtime, time, active
):
    await runtime.db.insert_message(
        "bot",
        MessageData(
            user_id="alice", group_or_user_id="other", time=time, message_id="A"
        ),
    )
    groups = await runtime.db.get_user_common_groups("bot", "current", ["alice"])
    assert len(groups["alice"]) == 1
    assert groups["alice"][0]["recently_active"] is active


@pytest.mark.asyncio
async def test_new_speech_and_elapsed_time_refresh_activity_without_profile_updates(
    runtime,
):
    await runtime.db.insert_message(
        "bot",
        MessageData(
            user_id="alice",
            group_or_user_id="other",
            time=runtime.clock.now.isoformat(),
            message_id="A",
        ),
    )
    first = await runtime.db.get_user_common_groups("bot", "current", ["alice"])
    assert first["alice"][0]["recently_active"]
    runtime.clock.now += timedelta(days=1, seconds=1)
    expired = await runtime.db.get_user_common_groups("bot", "current", ["alice"])
    assert not expired["alice"][0]["recently_active"]
    await runtime.db.insert_message(
        "bot",
        MessageData(
            user_id="alice",
            group_or_user_id="other",
            time=runtime.clock.now.isoformat(),
            message_id="B",
        ),
    )
    refreshed = await runtime.db.get_user_common_groups("bot", "current", ["alice"])
    assert refreshed["alice"][0]["recently_active"]


def test_profile_formats_group_ids_safely_and_omits_empty_groups():
    block = build_user_profile_block(
        "alice",
        None,
        None,
        common_groups=[
            {"group_id": "recent<&>", "recently_active": True},
            {"group_id": "old", "recently_active": False},
        ],
    )
    root = fromstring(block)
    assert root.get("user_id") == "alice"
    assert root.text.strip() == "已知共同群聊：recent<&>（近期活跃）、old"
    assert build_user_profile_block("alice", None, None, common_groups=[]) == ""
    assert "已知共同群聊" not in build_user_profile_block("alice", {}, (50, ""))
