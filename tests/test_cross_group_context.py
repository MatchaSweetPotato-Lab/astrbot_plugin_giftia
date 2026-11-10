"""Recent cross-group context preserves observed source conversations."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from xml.etree.ElementTree import fromstring

import aiosqlite
import pytest
import pytest_asyncio
from core.database.database import Database
from core.database.repositories import chat_history
from core.database.schema import initialize_database
from core.llm.prompt import build_reply_prompt
from core.utils.schemas import MediaCaption, MessageData


@pytest_asyncio.fixture
async def runtime(monkeypatch):
    now = datetime(2026, 11, 10, 12)
    monkeypatch.setattr(
        chat_history,
        "datetime",
        SimpleNamespace(now=lambda: now, fromisoformat=datetime.fromisoformat),
    )
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await initialize_database(conn)
        yield SimpleNamespace(db=Database(conn), now=now)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({}, True),
        ({"group_or_user_id": "current"}, False),
        ({"group_or_user_id": "alice"}, False),
        ({"group_or_user_id": ""}, False),
        ({"group_or_user_id": None}, False),
        ({"bot_name": "other-bot"}, False),
        ({"user_id": "bob"}, False),
        ({"memory_status": "unseen"}, False),
        ({"memory_status": "summarized"}, False),
        ({"role": "operation_log"}, False),
        ({"is_recalled": 1}, False),
        ({"age_seconds": 1200}, True),
        ({"age_seconds": 1200.001}, False),
        ({"age_seconds": -1}, False),
        ({"created_at": "invalid"}, False),
        ({"created_at": None}, False),
    ],
)
async def test_anchor_requires_recent_pending_speech_in_another_group(
    runtime, overrides, expected
):
    overrides = dict(overrides)
    age = overrides.pop("age_seconds", 30)
    fields = {
        "bot_name": "bot",
        "group_or_user_id": "source",
        "user_id": "alice",
        "message_id": "anchor",
        "memory_status": "pending",
        "role": "message",
        "is_recalled": 0,
        "created_at": (runtime.now - timedelta(seconds=age)).isoformat(),
        **overrides,
    }
    await runtime.db.conn.execute(
        f"INSERT INTO chat_history ({','.join(fields)}) "
        f"VALUES ({','.join('?' for _ in fields)})",
        tuple(fields.values()),
    )
    await runtime.db.conn.commit()
    result = await runtime.db.get_cross_group_pending_context(
        "bot", "current", ["alice"]
    )
    assert bool(result) is expected
    assert await runtime.db.get_cross_group_pending_context("bot", "current", []) == {}


@pytest.mark.asyncio
async def test_latest_anchor_uses_actual_time_across_offsets_and_insertion_order(
    runtime,
):
    recent = runtime.now - timedelta(seconds=10)
    older = runtime.now - timedelta(minutes=2)
    for group, moment in (
        ("latest", recent.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")),
        ("older", older.astimezone(timezone(timedelta(hours=8))).isoformat()),
        ("oldest", (runtime.now - timedelta(minutes=10)).isoformat()),
    ):
        await runtime.db.conn.execute(
            "INSERT INTO chat_history "
            "(bot_name, group_or_user_id, user_id, message_id, memory_status, created_at) "
            "VALUES ('bot', ?, 'alice', 'shared-id', 'pending', ?)",
            (group, moment),
        )
    await runtime.db.conn.commit()
    result = await runtime.db.get_cross_group_pending_context(
        "bot", "current", ["alice", "alice"]
    )
    assert list(result) == ["latest"]
    assert [msg.message_id for msg in result["latest"]] == ["shared-id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("alice,bob", [(20, 25), (15, 45)])
async def test_windows_include_ten_neighbors_and_merge_without_filling_gaps(
    runtime, alice, bob
):
    # Equal timestamps and interleaved foreign rows must not lose any neighbors.
    for index in range(61):
        for bot in ("bot", "other-bot"):
            user = {alice: "alice", bob: "bob", alice + 1: "self"}.get(
                index, "bystander"
            )
            await runtime.db.conn.execute(
                "INSERT INTO chat_history "
                "(bot_name, group_or_user_id, user_id, message_id, content, "
                "memory_status, created_at) VALUES (?, 'source', ?, ?, ?, ?, ?)",
                (
                    bot,
                    user,
                    str(index),
                    f"{bot}/{index}",
                    "pending" if index in (alice, bob) else "summarized",
                    runtime.now.isoformat(),
                ),
            )
    await runtime.db.conn.commit()

    single = await runtime.db.get_cross_group_pending_context(
        "bot", "current", ["alice"]
    )
    assert [int(msg.message_id) for msg in single["source"]] == list(
        range(alice - 10, alice + 11)
    )
    assert next(msg for msg in single["source"] if msg.user_id == "self")

    merged = await runtime.db.get_cross_group_pending_context(
        "bot", "current", ["alice", "bob", "alice"]
    )
    assert list(merged) == ["source"]
    expected = sorted(
        set(range(alice - 10, alice + 11)) | set(range(bob - 10, bob + 11))
    )
    assert [int(msg.message_id) for msg in merged["source"]] == expected
    assert all(msg.group_or_user_id == "source" for msg in merged["source"])
    assert all(msg.content.startswith("bot/") for msg in merged["source"])


@pytest.mark.asyncio
async def test_windows_exclude_unseen_recalled_and_logs_without_changing_states(
    runtime,
):
    await runtime.db.conn.executemany(
        "INSERT INTO chat_history "
        "(bot_name, group_or_user_id, user_id, message_id, memory_status, "
        "role, is_recalled, created_at) VALUES ('bot', 'source', ?, ?, ?, ?, ?, ?)",
        [
            (user, mid, status, role, recalled, runtime.now.isoformat())
            for user, mid, status, role, recalled in (
                ("other", "unseen", "unseen", "message", 0),
                ("other", "before", "summarized", "message", 0),
                ("alice", "anchor", "pending", "message", 0),
                ("self", "bot-reply", "pending", "message", 0),
                ("other", "recalled", "pending", "message", 1),
                ("other", "log", "pending", "operation_log", 0),
            )
        ],
    )
    await runtime.db.conn.commit()
    async with runtime.db.conn.execute("SELECT * FROM chat_history") as cursor:
        before = await cursor.fetchall()
    result = await runtime.db.get_cross_group_pending_context(
        "bot", "current", ["alice"]
    )
    assert [msg.message_id for msg in result["source"]] == [
        "before",
        "anchor",
        "bot-reply",
    ]
    async with runtime.db.conn.execute("SELECT * FROM chat_history") as cursor:
        assert await cursor.fetchall() == before


def test_cross_group_prompt_preserves_sources_media_and_current_reply_targets():
    foreign = MessageData(
        user_id="alice",
        message_id="shared-id",
        content="[图片:foreign]",
        media_id_list=["foreign"],
        forward_messages=[{"id": "forward", "source": "group", "source_id": "A"}],
    )
    local = MessageData(user_id="alice", message_id="shared-id", content="hello")
    prompt = build_reply_prompt(
        recent_messages=[],
        reply_messages=[local],
        bot_status=None,
        media_captions=[MediaCaption(hash_val="foreign", caption="A remembered scene")],
        cross_group_context={"A & B": [foreign], "C": [foreign]},
    )
    root = fromstring(f"<root>{prompt}</root>")
    assert [msg.text for msg in root.find("reply_messages")] == ["hello"]
    groups = root.findall("cross_group_context/group")
    assert [group.get("group_id") for group in groups] == ["A & B", "C"]
    assert all(group.get("source").startswith("来源群聊：") for group in groups)
    assert all(
        group.find("message").get("message_id") == "shared-id" for group in groups
    )
    assert all(group.find("forwarded_messages") is not None for group in groups)
    assert root.find("forwarded_messages") is None
    assert "A remembered scene" in prompt
    assert prompt.index("<cross_group_context>") < prompt.index("<reply_messages>")
    assert foreign.content == "[图片:foreign]"
    assert local.content == "hello"
