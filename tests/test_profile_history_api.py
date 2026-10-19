import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import aiosqlite
import httpx
import pytest
import pytest_asyncio
from core.database.data_cache import DataCache
from core.database.database import Database
from core.database.schema import initialize_database
from core.web.profile_api import ProfileApi
from core.web.webui_manager import WebUIManager
from fastapi import FastAPI, Request

from astrbot.api.web import PluginRequest, bind_request_context


@pytest_asyncio.fixture
async def profile_api(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path / "runtime"))
    async with aiosqlite.connect(tmp_path / "profiles.db") as conn:
        conn.row_factory = aiosqlite.Row
        await initialize_database(conn)
        plugin = SimpleNamespace(db=Database(conn))
        plugin.data_cache = DataCache(plugin.db, None, None)
        api = ProfileApi()
        api.giftia = plugin
        app = FastAPI()
        handlers = {
            "profiles/user": api.get_user_profiles,
            "profiles/user/filter_options": api.get_user_profile_filter_options,
            "profiles/user/relation_history": api.get_user_relation_history,
        }

        @app.get("/{endpoint:path}")
        async def dispatch(endpoint: str, request: Request):
            with bind_request_context(PluginRequest(request)):
                return await handlers[endpoint]()

        @app.post("/profiles/user/update")
        async def update_profile(request: Request):
            with bind_request_context(PluginRequest(request)):
                return await api.update_user_profile()

        @app.post("/profiles/user/relation_history/save")
        async def save_history(request: Request):
            with bind_request_context(PluginRequest(request)):
                return await api.save_user_relation_history()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            yield client, plugin


async def seed_changes(plugin, count, scope=("bot", "group", "user")):
    """Seed ordered profile changes for paginated API checks.

    Args:
        plugin: Isolated database owner.
        count: Number of history entries to create.
        scope: Bot, conversation and member owning every entry.
    """
    await plugin.db.upsert_user_profile(*scope, base_relation=count)
    await plugin.db.conn.executemany(
        "INSERT INTO relation_changes (bot_name, group_or_user_id, user_id, delta, "
        "reason, occurred_at, created_at) "
        "VALUES (?, ?, ?, 1, ?, ?, ?)",
        [
            (
                *scope,
                f"互动理由 {index + 1}",
                "2026-10-18T12:00:00+08:00",
                "2026-10-18T12:01:00+08:00",
            )
            for index in range(count)
        ],
    )
    await plugin.db.conn.commit()


@pytest.mark.asyncio
async def test_profile_cards_receive_latest_three_changes_across_groups(
    profile_api,
):
    client, plugin = profile_api
    await seed_changes(plugin, 5)
    await seed_changes(plugin, 1, ("bot", "other", "user"))
    await seed_changes(plugin, 1, ("other", "group", "user"))
    response = await client.get(
        "/profiles/user", params={"bot_name": "bot", "group_or_user_id": "group"}
    )
    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert len(items) == 1
    assert [row["reason"] for row in items[0]["recent_relation_changes"]] == [
        "互动理由 1",
        "互动理由 5",
        "互动理由 4",
    ]
    assert items[0]["recent_relation_changes"][0]["source_label"] == "来源群聊：other"
    assert items[0]["total_count"] == 6
    assert "relation_after" not in items[0]["recent_relation_changes"][0]


@pytest.mark.asyncio
async def test_history_paginates_all_entries_beyond_model_window_without_duplicates(
    profile_api,
):
    client, plugin = profile_api
    await seed_changes(plugin, 63)
    scope = {"bot_name": "bot", "group_or_user_id": "group", "user_id": "user"}
    seen = []
    for page in range(1, 8):
        response = await client.get(
            "/profiles/user/relation_history",
            params={**scope, "page": page, "limit": 100},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert (data["total"], data["page"], data["limit"]) == (63, page, 10)
        assert len(data["items"]) == (10 if page < 7 else 3)
        seen.extend(item["id"] for item in data["items"])
    assert seen == list(range(63, 0, -1))
    data = (
        await client.get(
            "/profiles/user/relation_history", params={**scope, "page": 999}
        )
    ).json()["data"]
    assert data["page"] == 7
    assert len(data["items"]) == 3


@pytest.mark.asyncio
async def test_history_shared_across_groups_and_isolated_by_bot_and_member(profile_api):
    client, plugin = profile_api
    for scope in [
        ("bot", "group", "user"),
        ("other", "group", "user"),
        ("bot", "other", "user"),
        ("bot", "group", "other"),
    ]:
        await seed_changes(plugin, 2, scope)
    data = (
        await client.get(
            "/profiles/user/relation_history",
            params={"bot_name": "bot", "group_or_user_id": "group", "user_id": "user"},
        )
    ).json()["data"]
    assert data["total"] == 4
    assert [item["id"] for item in data["items"]] == [6, 5, 2, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("page", ["0", "-1", "bad", "1.5", ""])
async def test_history_rejects_invalid_pages(profile_api, page):
    client, _ = profile_api
    response = await client.get(
        "/profiles/user/relation_history",
        params={
            "bot_name": "bot",
            "group_or_user_id": "group",
            "user_id": "user",
            "page": page,
        },
    )
    assert response.status_code == 400
    assert response.json()["status"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["bot_name", "user_id"])
async def test_history_requires_complete_scope(profile_api, missing):
    client, _ = profile_api
    scope = {"bot_name": "bot", "group_or_user_id": "group", "user_id": "user"}
    scope.pop(missing)
    response = await client.get("/profiles/user/relation_history", params=scope)
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_history_and_profile_without_changes_return_empty_state(profile_api):
    client, plugin = profile_api
    await plugin.db.upsert_user_profile("bot", "group", "user", base_relation=0)
    scope = {"bot_name": "bot", "group_or_user_id": "group", "user_id": "user"}
    history = (
        await client.get("/profiles/user/relation_history", params={**scope, "page": 3})
    ).json()["data"]
    assert {
        key: history[key] for key in ("items", "total", "page", "limit", "revision")
    } == {"items": [], "total": 0, "page": 1, "limit": 10, "revision": 0}
    assert history["score"]["relation"] == 50
    profile = (await client.get("/profiles/user", params=scope)).json()["data"][
        "items"
    ][0]
    assert profile["recent_relation_changes"] == []


def test_history_route_is_registered_as_read_only():
    plugin = SimpleNamespace(context=SimpleNamespace(register_web_api=Mock()))
    manager = WebUIManager(plugin)
    manager.register_routes()
    routes = [call.kwargs for call in plugin.context.register_web_api.call_args_list]
    route = next(
        item
        for item in routes
        if item["route"].endswith("/profiles/user/relation_history")
    )
    assert route["methods"] == ["GET"]
    assert route["view_handler"] == manager.web_api.get_user_relation_history
    mutation = next(
        item for item in routes if item["route"].endswith("/relation_history/save")
    )
    assert mutation["methods"] == ["POST"]
    assert mutation["view_handler"] == manager.web_api.save_user_relation_history


@pytest.mark.asyncio
async def test_manual_base_updates_derived_scores_and_keeps_interaction_history(
    profile_api,
):
    client, plugin = profile_api
    scope = {"bot_name": "bot", "group_or_user_id": "group", "user_id": "user"}
    await plugin.data_cache.update_relation("bot", "group", "user", 1, "友好交流")
    await plugin.data_cache.get_user_profile_record("bot", "group", "user")
    await plugin.data_cache.get_user_relation("bot", "group", "user")
    history = await plugin.db.get_relation_changes("bot", "group", "user")
    response = await client.post(
        "/profiles/user/update", json={**scope, "base_relation": -70}
    )
    assert response.status_code == 200
    profile = (await client.get("/profiles/user", params=scope)).json()["data"][
        "items"
    ][0]
    assert profile["base_relation"] == -70
    assert profile["interaction_score"] == pytest.approx(600 / 11)
    assert profile["relation"] == pytest.approx(-70 + 600 / 11)
    cached = await plugin.data_cache.get_user_profile_record("bot", "group", "user")
    assert cached["relation"] == profile["relation"]
    assert (await plugin.data_cache.get_user_relation("bot", "group", "user"))[
        0
    ] == profile["relation"]
    assert await plugin.db.get_relation_changes("bot", "group", "user") == history
    await client.post("/profiles/user/update", json={**scope, "call_name": "新称呼"})
    assert (await plugin.db.get_relation_score("bot", "group", "user"))[
        "base_relation"
    ] == -70


@pytest.mark.asyncio
@pytest.mark.parametrize("base", [1.5, 2.0, True, "1.5", "bad", "", 2**53])
async def test_base_update_rejects_values_that_are_not_safe_integers(profile_api, base):
    client, plugin = profile_api
    response = await client.post(
        "/profiles/user/update",
        json={
            "bot_name": "bot",
            "group_or_user_id": "group",
            "user_id": "user",
            "base_relation": base,
        },
    )
    assert response.status_code == 400
    assert await plugin.db.get_user_profile_record("bot", "group", "user") is None


@pytest.mark.asyncio
async def test_history_create_edit_delete_recalculates_scores_and_invalidates_caches(
    profile_api,
):
    client, plugin = profile_api
    await seed_changes(plugin, 3)
    scope = {"bot_name": "bot", "group_or_user_id": "group", "user_id": "user"}
    original = (
        await client.get("/profiles/user/relation_history", params=scope)
    ).json()["data"]
    target = original["items"][1]
    operations = [
        ({"action": "add", "delta": 5, "reason": "手动记录的帮助"}, 3 + 1300 / 18, 4),
        (
            {
                "action": "update",
                "id": target["id"],
                "delta": -4,
                "reason": "修正为明显冒犯",
            },
            3 + 1200 / 21,
            4,
        ),
    ]
    created_id = None
    for revision, (operation, expected, count) in enumerate(operations):
        await plugin.data_cache.get_user_profile("bot", "group", "user")
        await plugin.data_cache.get_user_profile_record("bot", "group", "user")
        await plugin.data_cache.get_user_relation("bot", "group", "user")
        response = await client.post(
            "/profiles/user/relation_history/save",
            json={**scope, **operation, "revision": revision},
        )
        assert response.status_code == 200
        result = response.json()["data"]
        if revision == 0:
            created_id = result["id"]
        assert result["revision"] == revision + 1
        assert result["score"]["relation"] == pytest.approx(expected)
        assert result["score"]["total_count"] == count
        for cache in (
            plugin.data_cache.user_profiles,
            plugin.data_cache.user_profile_records,
            plugin.data_cache.relations,
        ):
            assert "bot:user" not in cache
        assert (await plugin.data_cache.get_user_relation("bot", "group", "user"))[
            0
        ] == pytest.approx(expected)
    history = (
        await client.get("/profiles/user/relation_history", params=scope)
    ).json()["data"]
    edited = next(item for item in history["items"] if item["id"] == target["id"])
    assert edited["reason"] == "修正为明显冒犯"
    assert edited["delta"] == -4
    for field in ("occurred_at", "created_at", "group_or_user_id"):
        assert edited[field] == target[field]
    response = await client.post(
        "/profiles/user/relation_history/save",
        json={**scope, "action": "delete", "id": created_id, "revision": 2},
    )
    assert response.json()["data"]["score"]["relation"] == pytest.approx(46.75)
    revision = 3
    for item in original["items"]:
        response = await client.post(
            "/profiles/user/relation_history/save",
            json={**scope, "action": "delete", "id": item["id"], "revision": revision},
        )
        assert response.status_code == 200
        revision += 1
    score = response.json()["data"]["score"]
    assert score["base_relation"] == 3
    assert score["interaction_score"] == 50
    assert score["relation"] == 53
    assert score["total_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("delta", [0, -6, 6, 1.5, True, "1", None])
async def test_history_mutations_reject_invalid_ratings(profile_api, delta):
    client, plugin = profile_api
    await seed_changes(plugin, 1)
    response = await client.post(
        "/profiles/user/relation_history/save",
        json={
            "bot_name": "bot",
            "group_or_user_id": "group",
            "user_id": "user",
            "revision": 0,
            "action": "add",
            "delta": delta,
            "reason": "评价内容",
        },
    )
    assert response.status_code == 400
    assert len(await plugin.db.get_relation_changes("bot", "group", "user")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["", "  ", None, {}, "字" * 121])
async def test_history_mutations_require_concrete_text(profile_api, reason):
    client, plugin = profile_api
    await seed_changes(plugin, 1)
    response = await client.post(
        "/profiles/user/relation_history/save",
        json={
            "bot_name": "bot",
            "group_or_user_id": "group",
            "user_id": "user",
            "revision": 0,
            "action": "add",
            "delta": 1,
            "reason": reason,
        },
    )
    assert response.status_code == 400
    assert len(await plugin.db.get_relation_changes("bot", "group", "user")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["update", "delete"])
@pytest.mark.parametrize(
    "other",
    [("other", "group", "user"), ("bot", "group", "other")],
)
async def test_history_mutations_cannot_modify_another_scope(
    profile_api, action, other
):
    client, plugin = profile_api
    await seed_changes(plugin, 1)
    await seed_changes(plugin, 1, other)
    foreign = (await plugin.db.get_relation_changes(*other))[0]
    response = await client.post(
        "/profiles/user/relation_history/save",
        json={
            "bot_name": "bot",
            "group_or_user_id": "group",
            "user_id": "user",
            "revision": 0,
            "action": action,
            "id": foreign["id"],
            "delta": -5,
            "reason": "不应保存",
        },
    )
    assert response.status_code == 404
    assert (await plugin.db.get_relation_changes(*other))[0] == foreign


@pytest.mark.asyncio
async def test_history_rejects_stale_and_concurrent_edits(profile_api):
    client, plugin = profile_api
    await seed_changes(plugin, 1)
    item = (await plugin.db.get_relation_changes("bot", "group", "user"))[0]
    body = {
        "bot_name": "bot",
        "group_or_user_id": "group",
        "user_id": "user",
        "revision": 0,
        "action": "update",
        "id": item["id"],
        "delta": 2,
        "reason": "首次修订",
    }
    responses = await asyncio.gather(
        *(
            client.post("/profiles/user/relation_history/save", json=body)
            for _ in range(2)
        )
    )
    assert sorted(response.status_code for response in responses) == [200, 409]
    response = await client.post(
        "/profiles/user/relation_history/save", json={**body, "reason": "过期修订"}
    )
    assert response.status_code == 409
    assert (await plugin.db.get_relation_changes("bot", "group", "user"))[0][
        "reason"
    ] == "首次修订"


@pytest.mark.asyncio
async def test_failed_history_mutation_rolls_back_rating_and_revision(profile_api):
    client, plugin = profile_api
    await seed_changes(plugin, 1)
    await plugin.db.conn.execute(
        "CREATE TRIGGER reject_history BEFORE UPDATE OF relation_history_revision ON user_profiles BEGIN SELECT RAISE(ABORT, 'reject'); END"
    )
    response = await client.post(
        "/profiles/user/relation_history/save",
        json={
            "bot_name": "bot",
            "group_or_user_id": "group",
            "user_id": "user",
            "revision": 0,
            "action": "add",
            "delta": 1,
            "reason": "不应保存",
        },
    )
    assert response.status_code == 500
    assert len(await plugin.db.get_relation_changes("bot", "group", "user")) == 1
    async with plugin.db.conn.execute(
        "SELECT relation_history_revision FROM user_profiles"
    ) as cursor:
        assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_manual_edit_preserves_source_batch_replay_protection(profile_api):
    client, plugin = profile_api
    await plugin.db.apply_relation_change("bot", "group", "user", 1, "原评价", [7])
    original = (await plugin.db.get_relation_changes("bot", "group", "user"))[0]
    response = await client.post(
        "/profiles/user/relation_history/save",
        json={
            "bot_name": "bot",
            "group_or_user_id": "group",
            "user_id": "user",
            "revision": 0,
            "action": "update",
            "id": original["id"],
            "delta": -1,
            "reason": "更正评价",
        },
    )
    assert response.status_code == 200
    edited = (await plugin.db.get_relation_changes("bot", "group", "user"))[0]
    assert edited["source_ids"] == original["source_ids"]
    assert edited["occurred_at"] == original["occurred_at"]
    _, _, applied = await plugin.db.apply_relation_change(
        "bot", "group", "user", 1, "重复批次", [7]
    )
    assert not applied
    assert len(await plugin.db.get_relation_changes("bot", "group", "user")) == 1


@pytest.mark.asyncio
async def test_history_read_does_not_pair_stale_rows_with_a_new_revision(
    profile_api, monkeypatch
):
    client, plugin = profile_api
    await seed_changes(plugin, 1)
    item = (await plugin.db.get_relation_changes("bot", "group", "user"))[0]
    get_score = plugin.db.get_relation_score
    revised = False

    async def edit_during_read(*args, **kwargs):
        nonlocal revised
        score = await get_score(*args, **kwargs)
        if not revised:
            revised = True
            await plugin.db.mutate_relation_history(
                "bot",
                "group",
                "user",
                action="update",
                expected_revision=0,
                change_id=item["id"],
                delta=-1,
                reason="读取时修订",
            )
        return score

    monkeypatch.setattr(plugin.db, "get_relation_score", edit_during_read)
    response = await client.get(
        "/profiles/user/relation_history",
        params={"bot_name": "bot", "group_or_user_id": "group", "user_id": "user"},
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_profile_filters_keep_session_membership_and_shared_fields(profile_api):
    client, plugin = profile_api
    await plugin.data_cache.set_user_profile(
        "bot",
        "group-a",
        "user",
        base_relation=10,
        profile_fields={"call_name": "小明"},
    )
    await plugin.data_cache.set_user_profile(
        "bot",
        "group-b",
        "user",
        profile_fields={"avatar_description": "银发头像"},
    )
    for _ in range(3):
        await plugin.db.upsert_user_aliases("bot", "group-a", "user", "阿明")
        await plugin.db.upsert_user_aliases("bot", "group-b", "user", "明仔")
    all_profiles = (
        await client.get("/profiles/user", params={"bot_name": "bot"})
    ).json()["data"]
    assert all_profiles["total"] == 1
    for group, alias in (("group-a", "阿明"), ("group-b", "明仔")):
        data = (
            await client.get(
                "/profiles/user", params={"bot_name": "bot", "group_or_user_id": group}
            )
        ).json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["call_name"] == "小明"
        assert data["items"][0]["avatar_description"] == "银发头像"
        assert data["items"][0]["aliases"] == alias
    filters = (
        await client.get(
            "/profiles/user/filter_options",
            params={"bot_name": "bot", "search": "银发"},
        )
    ).json()["data"]
    assert {row["group_or_user_id"] for row in filters["sessions"]} == {
        "group-a",
        "group-b",
    }
    response = await client.post(
        "/profiles/user/update",
        json={"bot_name": "bot", "user_id": "user", "call_name": "明同学"},
    )
    assert response.status_code == 200
    assert (await plugin.data_cache.get_user_profile_record("bot", "group-b", "user"))[
        "call_name"
    ] == "明同学"


@pytest.mark.asyncio
async def test_history_edits_from_another_group_keep_source_and_share_revision(
    profile_api,
):
    client, plugin = profile_api
    await plugin.db.apply_relation_change("bot", "group-a", "user", 2, "原评价", [7])
    target = (await plugin.db.get_relation_changes("bot", "group-b", "user"))[0]
    scope = {"bot_name": "bot", "group_or_user_id": "group-b", "user_id": "user"}
    response = await client.post(
        "/profiles/user/relation_history/save",
        json={
            **scope,
            "action": "update",
            "id": target["id"],
            "delta": -2,
            "reason": "修正评价",
            "revision": 0,
        },
    )
    assert response.status_code == 200
    a = (
        await client.get(
            "/profiles/user/relation_history",
            params={**scope, "group_or_user_id": "group-a"},
        )
    ).json()["data"]
    b = (await client.get("/profiles/user/relation_history", params=scope)).json()[
        "data"
    ]
    assert a["revision"] == b["revision"] == 1
    assert a["score"] == b["score"]
    assert (
        a["items"][0]["group_or_user_id"]
        == b["items"][0]["group_or_user_id"]
        == "group-a"
    )
    assert a["items"][0]["source_label"] == ""
    assert b["items"][0]["source_label"] == "来源群聊：group-a"
    stale = await client.post(
        "/profiles/user/relation_history/save",
        json={
            **scope,
            "action": "delete",
            "id": target["id"],
            "revision": 0,
        },
    )
    assert stale.status_code == 409
