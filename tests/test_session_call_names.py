from types import SimpleNamespace

import aiosqlite
import pytest
import pytest_asyncio
from core.database import migrations, schema_v1
from core.database.data_cache import DataCache
from core.database.database import Database
from core.database.schema import initialize_database


@pytest_asyncio.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "astrbot.api.star.StarTools.get_data_dir", lambda *args: tmp_path
    )
    db = await Database.connect()
    try:
        yield SimpleNamespace(db=db, cache=DataCache(db, None, None))
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_local_names_win_and_only_name_writes_change_the_fallback(runtime):
    rt = runtime
    for bot, session, user, name in (
        ("bot", "group-a", "user", "骑士"),
        ("bot", "group-b", "user", "学者"),
        ("other-bot", "group-b", "user", "其他机器人称呼"),
        ("bot", "group-b", "other-user", "其他成员"),
    ):
        await rt.cache.set_user_profile(
            bot, session, user, profile_fields={"call_name": name}
        )
    for session, expected, local in (
        ("group-a", "骑士", "骑士"),
        ("group-b", "学者", "学者"),
        ("group-c", "学者", None),
    ):
        record = await rt.cache.get_user_profile_record("bot", session, "user")
        assert record["call_name"] == expected
        assert record["local_call_name"] == local
        assert expected in await rt.cache.get_user_profile("bot", session, "user")
    await rt.cache.set_user_profile(
        "bot", "group-a", "user", profile_fields={"avatar_description": "新头像"}
    )
    await rt.cache.set_user_profile(
        "bot",
        "group-a",
        "user",
        profile_fields={"call_name": "骑士", "attitude": "新基调"},
    )
    assert (await rt.cache.get_user_profile_record("bot", "group-c", "user"))[
        "call_name"
    ] == "学者"
    await rt.cache.set_user_profile(
        "bot", "group-a", "user", profile_fields={"call_name": "新骑士"}
    )
    assert "新骑士" in await rt.cache.get_user_profile("bot", "group-c", "user")
    assert (await rt.cache.get_user_profile_record("bot", "group-b", "user"))[
        "call_name"
    ] == "学者"
    assert "call_name" not in rt.cache.user_profile_records["bot:user"]


@pytest.mark.asyncio
async def test_clearing_local_names_reveals_the_latest_remaining_name(runtime):
    rt = runtime
    for session, name in (("group-a", "骑士"), ("private-user", "朋友")):
        await rt.cache.set_user_profile(
            "bot", session, "user", profile_fields={"call_name": name}
        )
    await rt.cache.set_user_profile(
        "bot", "private-user", "user", profile_fields={"call_name": " \t "}
    )
    record = await rt.cache.get_user_profile_record("bot", "private-user", "user")
    assert record["call_name"] == "骑士" and record["local_call_name"] is None
    await rt.cache.set_user_profile(
        "bot", "group-a", "user", profile_fields={"call_name": None}
    )
    assert (await rt.cache.get_user_profile_record("bot", "private-user", "user"))[
        "call_name"
    ] is None


@pytest.mark.asyncio
async def test_search_and_alias_filtering_use_the_requesting_groups_name(runtime):
    rt = runtime
    await rt.cache.set_user_profile(
        "bot", "group-b", "user", profile_fields={"call_name": "学者"}
    )
    for session in ("group-a", "group-b"):
        await rt.db.upsert_user_aliases(
            "bot", session, "user", "骑士", increment_count=False
        )
    await rt.cache.set_user_profile(
        "bot", "group-a", "user", profile_fields={"call_name": "骑士"}
    )
    assert await rt.db.search_user_profiles("bot", "group-a", "学者") == []
    assert (await rt.db.search_user_profiles("bot", "group-b", "学者"))[0][
        "call_name"
    ] == "学者"
    assert (await rt.db.search_user_profiles("bot", "group-c", "骑士"))[0][
        "call_name"
    ] == "骑士"
    assert (
        await rt.db.get_user_aliases("bot", "group-a", "user", ignore_count_filter=True)
        == []
    )
    assert (
        await rt.db.get_user_aliases("bot", "group-b", "user", ignore_count_filter=True)
    )[0]["alias"] == "骑士"


@pytest.mark.asyncio
async def test_session_and_user_deletion_remove_only_the_appropriate_names(runtime):
    rt = runtime
    for session, name in (("group-a", "骑士"), ("group-b", "学者")):
        await rt.db.upsert_user_profile(
            "bot", session, "user", profile_fields={"call_name": name}
        )
    await rt.db.delete_group_user_profiles("bot", "group-a")
    assert await rt.db.get_user_call_names("bot", "group-a", "user") == {
        "call_name": "学者",
        "local_call_name": None,
    }
    await rt.db.delete_user_profile("bot", "group-b", "user")
    async with rt.db.conn.execute("SELECT COUNT(*) FROM user_call_names") as cursor:
        assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("named_rows", [False, True])
async def test_v1_names_migrate_once_as_unscoped_fallbacks(
    tmp_path, monkeypatch, named_rows
):
    monkeypatch.setattr(
        "astrbot.api.star.StarTools.get_data_dir", lambda *args: tmp_path
    )
    async with aiosqlite.connect(":memory:") as conn:
        if named_rows:
            conn.row_factory = aiosqlite.Row
        for statement in (*schema_v1.TABLE_DEFINITIONS, *schema_v1.INDEX_DEFINITIONS):
            await conn.execute(statement)
        await conn.execute("PRAGMA user_version=1")
        await conn.execute(
            "INSERT INTO user_profiles (bot_name, user_id, call_name, avatar_description, updated_at) "
            "VALUES ('bot', 'user', '  旧称呼  ', '保留头像', '2026-01-01 12:00:00')"
        )
        await conn.commit()
        await initialize_database(conn)
        conn.row_factory = aiosqlite.Row
        db = Database(conn)
        for group in ("group-a", "group-b"):
            record = await db.get_user_profile_record("bot", group, "user")
            assert record["call_name"] == "旧称呼" and record["local_call_name"] is None
            assert record["avatar_description"] == "保留头像"
        await db.upsert_user_profile(
            "bot", "group-a", "user", profile_fields={"call_name": "新称呼"}
        )
        await initialize_database(conn)
        assert (await db.get_user_call_names("bot", "group-b", "user"))[
            "call_name"
        ] == "新称呼"
        async with conn.execute("SELECT COUNT(*) FROM user_call_names") as cursor:
            assert (await cursor.fetchone())[0] == 2
        async with conn.execute("PRAGMA user_version") as cursor:
            assert (await cursor.fetchone())[0] == migrations.CURRENT_SCHEMA_VERSION
