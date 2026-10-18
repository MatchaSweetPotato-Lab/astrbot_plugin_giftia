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
        "relation_before, relation_after, reason, occurred_at, created_at) "
        "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)",
        [
            (
                *scope,
                index,
                index + 1,
                f"互动理由 {index + 1}",
                "2026-10-18T12:00:00+08:00",
                "2026-10-18T12:01:00+08:00",
            )
            for index in range(count)
        ],
    )
    await plugin.db.conn.commit()


@pytest.mark.asyncio
async def test_profile_cards_receive_latest_three_changes_in_each_exact_scope(
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
        "互动理由 5",
        "互动理由 4",
        "互动理由 3",
    ]
    assert items[0]["recent_relation_changes"][0]["relation_after"] == 5


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
        seen.extend(item["relation_after"] for item in data["items"])
    assert seen == list(range(63, 0, -1))
    data = (
        await client.get(
            "/profiles/user/relation_history", params={**scope, "page": 999}
        )
    ).json()["data"]
    assert data["page"] == 7
    assert len(data["items"]) == 3


@pytest.mark.asyncio
async def test_history_isolated_by_bot_conversation_and_member(profile_api):
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
    assert data["total"] == 2
    assert [item["id"] for item in data["items"]] == [2, 1]


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
@pytest.mark.parametrize("missing", ["bot_name", "group_or_user_id", "user_id"])
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
    assert history == {"items": [], "total": 0, "page": 1, "limit": 10}
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
