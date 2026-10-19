from types import SimpleNamespace
from unittest.mock import Mock

import aiosqlite
import pytest
import pytest_asyncio
from core.database.database import Database
from core.database.schema import initialize_database
from core.llm.llm_tools import SearchUserProfileTool


@pytest_asyncio.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "astrbot.api.star.StarTools.get_data_dir", lambda *args: tmp_path
    )
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await initialize_database(conn)
        db = Database(conn)
        plugin = SimpleNamespace(
            db=db,
            adapter_id_map={"adapter": "bot"},
            bot_map={"bot": {"nickname": "Giftia"}},
        )
        event = Mock(
            platform_meta=SimpleNamespace(id="adapter"),
            get_group_id=Mock(return_value="group"),
            get_sender_id=Mock(return_value="sender"),
            get_self_id=Mock(return_value="self"),
        )
        yield SimpleNamespace(
            db=db,
            event=event,
            tool=SearchUserProfileTool(plugin=plugin),
            context=SimpleNamespace(context=SimpleNamespace(event=event)),
        )


async def seed_history(runtime, count, scope=("bot", "group", "123")):
    """Create scoped profiles and ordered ratings for tool integration checks.

    Args:
        runtime: Isolated database and tool runtime.
        count: Number of ratings to create.
        scope: Bot, conversation and member owning the records.
    """
    await runtime.db.upsert_user_profile(*scope, profile_fields={"call_name": "小明"})
    await runtime.db.conn.executemany(
        "INSERT INTO relation_changes (bot_name, group_or_user_id, user_id, delta, "
        "relation_before, relation_after, reason, occurred_at, created_at) "
        "VALUES (?, ?, ?, 1, 50, 51, ?, ?, ?)",
        [
            (
                *scope,
                f"{'/'.join(scope)}#{index:03d}",
                "2026-10-18T12:00:00+08:00",
                "2026-10-18T12:01:00+08:00",
            )
            for index in range(1, count + 1)
        ],
    )
    await runtime.db.conn.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["123", "小明", "明明", "平台昵称", "银发头像"])
async def test_search_matches_identifiers_names_aliases_and_avatar(runtime, query):
    await seed_history(runtime, 0)
    await runtime.db.upsert_user_profile(
        "bot", "group", "123", profile_fields={"avatar_description": "银发头像"}
    )
    await runtime.db.upsert_user_aliases("bot", "group", "123", "明明")
    await runtime.db.conn.execute(
        "INSERT INTO chat_history (bot_name, group_or_user_id, user_id, nickname) "
        "VALUES ('bot', 'group', '123', '平台昵称')"
    )
    results = await runtime.db.search_user_profiles("bot", "group", query)
    assert [item["user_id"] for item in results] == ["123"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    [
        "personality",
        "interests",
        "agreements",
        "extra",
        "title",
        "attitude",
    ],
)
async def test_search_does_not_match_profile_descriptions_or_retired_fields(
    runtime, field
):
    kwargs = (
        {"title": "限定词"}
        if field == "title"
        else {"profile_fields": {field: "限定词"}}
    )
    await runtime.db.upsert_user_profile("bot", "group", "123", **kwargs)
    assert await runtime.db.search_user_profiles("bot", "group", "限定词") == []
    assert len(await runtime.db.search_user_profiles("bot", "group", "123")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [("other", "group", "123"), ("bot", "other", "123")])
async def test_nickname_and_alias_matches_stay_in_the_current_scope(runtime, scope):
    await seed_history(runtime, 0)
    await seed_history(runtime, 0, scope)
    await runtime.db.upsert_user_aliases(*scope, "其他会话外号")
    await runtime.db.conn.execute(
        "INSERT INTO chat_history (bot_name, group_or_user_id, user_id, nickname) "
        "VALUES (?, ?, ?, '其他会话昵称')",
        scope,
    )
    for query in ("其他会话外号", "其他会话昵称"):
        assert await runtime.db.search_user_profiles("bot", "group", query) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "page", "indices"),
    [
        ({}, 1, range(55, 45, -1)),
        ({"page": 2}, 2, range(45, 35, -1)),
        ({"page": 6}, 6, range(5, 0, -1)),
        ({"page": 999}, 6, range(5, 0, -1)),
    ],
)
async def test_tool_pages_complete_history_newest_first_without_changing_scores(
    runtime, arguments, page, indices
):
    await seed_history(runtime, 55)
    score = await runtime.db.get_relation_score("bot", "group", "123")
    writes = runtime.db.conn.total_changes
    result = await runtime.tool.call(runtime.context, "123", limit=1, **arguments)
    assert "用户 123" in result
    assert "你的称呼：小明" in result
    assert f"第 {page}/6 页；每页 10 条，共 55 条" in result
    assert "评价值不等于最终好感度增减" in result
    assert "2026-10-18" in result
    reasons = [
        line.split("：", 1)[1]
        for line in result.splitlines()
        if "互动评价 +1：" in line
    ]
    assert reasons == [f"bot/group/123#{index:03d}" for index in indices]
    assert runtime.db.conn.total_changes == writes
    assert await runtime.db.get_relation_score("bot", "group", "123") == score


@pytest.mark.asyncio
async def test_page_applies_to_each_matched_member_and_limit_counts_members(runtime):
    await seed_history(runtime, 15, ("bot", "group", "A"))
    await seed_history(runtime, 3, ("bot", "group", "B"))
    result = await runtime.tool.call(runtime.context, "小明", limit=2, page=2)
    profiles = {
        block.splitlines()[0]: block
        for block in result.removeprefix("查询到的成员画像:\n").split("\n\n")
    }
    first = next(block for header, block in profiles.items() if "用户 A" in header)
    second = next(block for header, block in profiles.items() if "用户 B" in header)
    assert "第 2/2 页；每页 10 条，共 15 条" in first
    assert first.count("互动评价 +1：") == 5
    assert "bot/group/B#" not in first
    assert "第 1/1 页；每页 10 条，共 3 条" in second
    assert second.count("互动评价 +1：") == 3
    assert "bot/group/A#" not in second
    limited = await runtime.tool.call(runtime.context, "小明", limit=1)
    assert limited.count("你的称呼：小明") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("group_id,session", [("group", "group"), ("", "sender")])
async def test_tool_history_isolated_by_bot_conversation_and_member(
    runtime, group_id, session
):
    runtime.event.get_group_id.return_value = group_id
    scope = ("bot", session, "123")
    for item in [
        scope,
        ("other", session, "123"),
        ("bot", "other", "123"),
        ("bot", session, "456"),
    ]:
        await seed_history(runtime, 2, item)
    result = await runtime.tool.call(runtime.context, "123")
    assert "共 2 条" in result
    assert f"bot/{session}/123#002" in result
    for omitted in (f"other/{session}/123#", "bot/other/123#", f"bot/{session}/456#"):
        assert omitted not in result


@pytest.mark.asyncio
async def test_tool_without_history_keeps_profile_and_shows_empty_state(runtime):
    await seed_history(runtime, 0)
    result = await runtime.tool.call(runtime.context, "123", page=3)
    assert "你的称呼：小明" in result
    assert "好感度：50.0" in result
    assert "第 1/1 页；每页 10 条，共 0 条" in result
    assert "暂无互动评价记录" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("page", [0, -1, "bad", "1.5", 1.5, True, None])
async def test_tool_rejects_invalid_history_pages(runtime, page):
    result = await runtime.tool.call(runtime.context, "123", page=page)
    assert result == "查询失败：互动评价历史页码必须是正整数"
