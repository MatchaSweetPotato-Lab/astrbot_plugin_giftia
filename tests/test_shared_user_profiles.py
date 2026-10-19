import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio

from core.database.data_cache import DataCache
from core.database.database import Database
from core.database.schema import initialize_database
from core.llm.prompt import build_user_profile_block
from core.memory.passive_memory import PassiveMemoryManager


@pytest_asyncio.fixture
async def shared_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "astrbot.api.star.StarTools.get_data_dir", lambda *args: tmp_path
    )
    db = await Database.connect()
    plugin = SimpleNamespace(db=db)
    plugin.data_cache = DataCache(db, None, None)
    manager = PassiveMemoryManager(plugin)
    manager._call_summary_llm = AsyncMock(
        return_value='{"attitude": "熟络，常互相接梗"}'
    )
    try:
        yield SimpleNamespace(db=db, cache=plugin.data_cache, manager=manager)
    finally:
        await manager.close()
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("bases", [(10, 20), (-7, -3)])
@pytest.mark.parametrize("named_rows", [True, False])
async def test_legacy_migration_merges_profiles_and_preserves_every_source_record(
    tmp_path, monkeypatch, bases, named_rows
):
    monkeypatch.setattr(
        "astrbot.api.star.StarTools.get_data_dir", lambda *args: tmp_path
    )
    async with aiosqlite.connect(":memory:") as conn:
        if named_rows:
            conn.row_factory = aiosqlite.Row
        await conn.execute("""
            CREATE TABLE user_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_name TEXT NOT NULL, group_or_user_id TEXT NOT NULL, user_id TEXT NOT NULL,
                call_name TEXT, aliases TEXT, avatar_description TEXT, attitude TEXT,
                base_relation INTEGER NOT NULL DEFAULT 0,
                attitude_evaluated_change_id INTEGER NOT NULL DEFAULT 0,
                relation_history_revision INTEGER NOT NULL DEFAULT 0,
                created_at TEXT, updated_at TEXT
            )
        """)
        await conn.executemany(
            "INSERT INTO user_profiles (bot_name, group_or_user_id, user_id, call_name, "
            "aliases, avatar_description, attitude, base_relation, "
            "attitude_evaluated_change_id, created_at, updated_at) "
            "VALUES (?, ?, 'user', ?, ?, ?, '旧态度', ?, 41, ?, ?)",
            [
                (
                    "bot",
                    "group-a",
                    "旧称呼",
                    "阿明",
                    "银发头像",
                    bases[0],
                    "2026-10-01",
                    "2026-10-01",
                ),
                (
                    "bot",
                    "group-b",
                    "新称呼",
                    "明仔",
                    "",
                    bases[1],
                    "2026-10-02",
                    "2026-10-02",
                ),
                (
                    "other-bot",
                    "group-a",
                    "另一机器人称呼",
                    "",
                    "",
                    30,
                    "2026-10-01",
                    "2026-10-01",
                ),
            ],
        )
        await conn.execute("""
            CREATE TABLE relation_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_name TEXT NOT NULL, group_or_user_id TEXT NOT NULL, user_id TEXT NOT NULL,
                delta INTEGER NOT NULL, relation_before REAL NOT NULL, relation_after REAL NOT NULL,
                reason TEXT NOT NULL, source_ids TEXT, occurred_at TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(bot_name, group_or_user_id, user_id, source_ids)
            )
        """)
        await conn.executemany(
            "INSERT INTO relation_changes VALUES (?, 'bot', ?, 'user', ?, 50, 51, "
            "'独立互动', '[7]', '2026-10-01', '2026-10-01')",
            [(40, "group-a", 2), (41, "group-b", -1)],
        )
        await conn.execute(
            "UPDATE sqlite_sequence SET seq=100 WHERE name='relation_changes'"
        )
        await conn.commit()

        await initialize_database(conn)
        conn.row_factory = aiosqlite.Row
        db = Database(conn)
        a = await db.get_user_profile_record("bot", "group-a", "user")
        b = await db.get_user_profile_record("bot", "group-b", "user")
        assert a["call_name"] == b["call_name"] == "新称呼"
        assert a["avatar_description"] == b["avatar_description"] == "银发头像"
        assert a["base_relation"] == b["base_relation"] == max(bases)
        assert a["attitude"] is None
        assert a["total_count"] == 2
        assert a["relation"] == pytest.approx(max(bases) + 700 / 13)
        assert (
            await db.get_user_aliases(
                "bot", "group-a", "user", ignore_count_filter=True
            )
        )[0]["alias"] == "阿明"
        assert (
            await db.get_user_aliases(
                "bot", "group-b", "user", ignore_count_filter=True
            )
        )[0]["alias"] == "明仔"
        history = await db.get_relation_changes("bot", "group-a", "user")
        assert [row["id"] for row in history] == [40, 41]
        assert [row["source_ids"] for row in history] == ["[7]", "[7]"]
        assert [row["source_label"] for row in history] == ["", "来源群聊：group-b"]
        assert all(
            "relation_before" not in row and "relation_after" not in row
            for row in history
        )
        async with conn.execute(
            "SELECT COUNT(*) FROM user_profile_sessions WHERE bot_name='bot'"
        ) as cursor:
            assert (await cursor.fetchone())[0] == 2
        await db.upsert_user_profile(
            "bot", "group-a", "user", profile_fields={"attitude": "新态度"}
        )
        assert await db.save_attitude_evaluation("bot", "group-b", "user", 0, 41)
        await initialize_database(conn)
        assert (await db.get_user_profile_record("bot", "group-b", "user"))[
            "attitude"
        ] == "新态度"
        async with conn.execute(
            "SELECT attitude_evaluated_change_id FROM user_profiles WHERE bot_name='bot'"
        ) as cursor:
            assert (await cursor.fetchone())[0] == 41
        async with conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='relation_changes'"
        ) as cursor:
            assert (await cursor.fetchone())[0] == 100
        assert (await db.get_user_profile_record("other-bot", "group-a", "user"))[
            "base_relation"
        ] == 30


@pytest.mark.asyncio
async def test_shared_cache_keeps_aliases_local_and_invalidates_scores_across_groups(
    shared_runtime,
):
    rt = shared_runtime
    await rt.cache.set_user_profile(
        "bot", "group-a", "user", base_relation=10, profile_fields={"call_name": "小明"}
    )
    for _ in range(3):
        await rt.db.upsert_user_aliases("bot", "group-a", "user", "阿明")
        await rt.db.upsert_user_aliases("bot", "group-b", "user", "明仔")
    assert (await rt.cache.get_user_profile_record("bot", "group-a", "user"))[
        "aliases"
    ] == "阿明"
    assert (await rt.cache.get_user_profile_record("bot", "group-b", "user"))[
        "aliases"
    ] == "明仔"
    await rt.cache.get_user_relation("bot", "group-a", "user")
    await rt.cache.set_user_profile(
        "bot", "group-b", "user", profile_fields={"call_name": "明同学"}
    )
    assert (await rt.cache.get_user_profile_record("bot", "group-a", "user"))[
        "call_name"
    ] == "明同学"
    results = await asyncio.gather(
        rt.cache.update_relation("bot", "group-a", "user", 2, "认真道谢", [7]),
        rt.cache.update_relation("bot", "group-b", "user", -1, "打断交流", [7]),
    )
    assert results == [True, True]
    assert not await rt.cache.update_relation("bot", "group-b", "user", -1, "重试", [7])
    a = await rt.cache.get_user_profile_record("bot", "group-a", "user")
    b = await rt.cache.get_user_profile_record("bot", "group-b", "user")
    assert a["relation"] == b["relation"] == pytest.approx(10 + 700 / 13)
    assert a["aliases"] == "阿明" and b["aliases"] == "明仔"
    assert "aliases" not in rt.cache.user_profile_records["bot:user"]
    history = await rt.db.get_relation_changes("bot", "group-a", "user")
    prompt = build_user_profile_block("user", a, None, relation_changes=history)
    assert "[来源群聊：group-b]" in prompt
    assert "来源群聊：group-a" not in prompt
    assert "禁止在对外回复中披露记录内容或来源" in prompt
    assert (await rt.cache.get_user_relation("bot", "group-a", "user")) == (
        await rt.cache.get_user_relation("bot", "group-b", "user")
    )


@pytest.mark.asyncio
async def test_attitude_review_uses_one_shared_cursor_and_cross_group_reasons(
    shared_runtime,
):
    rt = shared_runtime
    for index in range(5):
        await rt.cache.update_relation(
            "bot", "group-a" if index < 2 else "group-b", "user", 1, f"互动{index}"
        )
    await asyncio.gather(
        rt.manager._run_attitude_summary_task("bot", "group-a", "Giftia", "self"),
        rt.manager._run_attitude_summary_task("bot", "group-b", "Giftia", "self"),
    )
    rt.manager._call_summary_llm.assert_awaited_once()
    payload = json.loads(rt.manager._call_summary_llm.call_args.args[2])
    assert len(payload["relation_changes"]) == 5
    assert [row["reason"] for row in payload["relation_changes"]] == [
        f"互动{index}" for index in range(5)
    ]
    assert all(
        "group_or_user_id" not in row and "source_label" not in row
        for row in payload["relation_changes"]
    )
    system_prompt = rt.manager._call_summary_llm.call_args.args[1]
    assert "跨群" not in system_prompt
    assert "group_or_user_id" not in system_prompt
    assert "source_label" not in system_prompt
    assert (await rt.cache.get_user_profile_record("bot", "group-b", "user"))[
        "attitude"
    ] == "熟络，常互相接梗"
    assert await rt.db.get_pending_attitude_evaluations("bot", "group-a") == []
    assert await rt.db.get_pending_attitude_evaluations("bot", "group-b") == []


@pytest.mark.asyncio
async def test_public_history_filters_source_before_pagination_without_changing_internal_history(
    shared_runtime,
):
    rt = shared_runtime
    for index in range(4):
        await rt.cache.update_relation("bot", "group-a", "user", 1, f"local {index}")
    for index in range(5):
        await rt.cache.update_relation("bot", "group-b", "user", 1, f"foreign {index}")
    local = await rt.db.get_relation_changes(
        "bot", "group-a", "user", limit=3, current_session_only=True
    )
    assert [row["reason"] for row in local] == ["local 1", "local 2", "local 3"]
    assert all(row["group_or_user_id"] == "group-a" for row in local)
    shared = await rt.db.get_relation_changes("bot", "group-a", "user", limit=3)
    assert [row["reason"] for row in shared] == ["foreign 2", "foreign 3", "foreign 4"]


@pytest.mark.asyncio
async def test_session_cleanup_preserves_shared_profile_and_other_group_data(
    shared_runtime,
):
    rt = shared_runtime
    await rt.cache.set_user_profile(
        "bot", "group-a", "user", base_relation=12, profile_fields={"call_name": "小明"}
    )
    for group in ("group-a", "group-b"):
        await rt.cache.update_relation("bot", group, "user", 2, "互动")
        await rt.db.upsert_user_aliases("bot", group, "user", "群内外号")
    await rt.cache.get_user_profile_record("bot", "group-b", "user")
    await rt.cache.get_user_relation("bot", "group-b", "user")
    rt.cache.ltm = SimpleNamespace(delete_all_memories=AsyncMock())
    await rt.cache.delete_all_memories("bot", "group-a")
    record = await rt.cache.get_user_profile_record("bot", "group-b", "user")
    assert record["call_name"] == "小明" and record["base_relation"] == 12
    assert record["total_count"] == 1
    assert (
        await rt.db.get_user_aliases("bot", "group-a", "user", ignore_count_filter=True)
        == []
    )
    assert (
        len(
            await rt.db.get_user_aliases(
                "bot", "group-b", "user", ignore_count_filter=True
            )
        )
        == 1
    )
    assert (await rt.db.get_relation_changes("bot", "group-a", "user"))[0][
        "group_or_user_id"
    ] == "group-b"
