import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request

from astrbot.api.web import PluginRequest, bind_request_context
from core.database.data_cache import DataCache
from core.database.database import Database
from core.database.schema import initialize_database
from core.web.memory_api import MemoryApi


@pytest_asyncio.fixture
async def memory_api(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path / "runtime"))
    conn = await aiosqlite.connect(tmp_path / "memory.db")
    conn.row_factory = aiosqlite.Row
    await initialize_database(conn)
    plugin = SimpleNamespace(
        db=Database(conn),
        passive_memory_manager=SimpleNamespace(_sync_memory_vectors=AsyncMock()),
    )
    plugin.data_cache = DataCache(plugin.db, None, None, plugin=plugin)
    api = MemoryApi()
    api.giftia = plugin
    app = FastAPI()
    handlers = {
        "memories": api.get_memories,
        "memories/filter_options": api.get_memory_filter_options,
        "memories/add": api.add_memory,
        "memories/update": api.update_memory,
        "memories/delete": api.delete_memory,
        "memories/clean/candidates": api.get_memory_clean_candidates,
    }

    @app.api_route("/{endpoint:path}", methods=["GET", "POST"])
    async def dispatch(endpoint: str, request: Request):
        with bind_request_context(PluginRequest(request)):
            return await handlers[endpoint]()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        yield client, plugin
    await conn.close()


async def seed_memory(plugin, memory_id, **fields):
    """Seed a synthetic atom for endpoint tests.

    Args:
        plugin: Isolated plugin runtime.
        memory_id: Stable test identifier.
        **fields: Stored fields overriding defaults.
    """
    item = {
        "bot_name": "bot",
        "group_or_user_id": "group",
        "memory_id": memory_id,
        "kind": "event",
        "status": "active",
        "text": memory_id,
        "importance": 3,
        "created_at": "2020-01-01T00:00:00+00:00",
        "updated_at": "2020-01-01T00:00:00+00:00",
        "metadata": json.dumps(
            {
                "schema_version": 2,
                "subject_id": "1",
                "associated_user_ids": ["1"],
                "origin": "observed",
                "topic": "game",
                "evidence": [
                    {
                        "source_id": 12,
                        "quote": "original",
                        "user_id": "1",
                        "time": "2020-01-01",
                    }
                ],
            }
        ),
        **fields,
    }
    await plugin.db.conn.execute(
        f"INSERT INTO memories ({','.join(item)}) VALUES ({','.join('?' for _ in item)})",
        list(item.values()),
    )
    await plugin.db.conn.commit()


@pytest.mark.asyncio
async def test_atomic_filters_counts_and_effective_expiration(memory_api):
    client, plugin = memory_api
    await seed_memory(plugin, "event")
    await seed_memory(
        plugin, "open", kind="agreement", expires_at="2999-01-01T00:00:00+00:00"
    )
    await seed_memory(
        plugin, "expired", kind="agreement", expires_at="2020-01-02T00:00:00+00:00"
    )
    await seed_memory(plugin, "closed", kind="agreement", status="completed")
    await seed_memory(
        plugin,
        "habit",
        kind="habit",
        metadata=json.dumps({"origin": "derived", "subject_id": "1"}),
    )
    await seed_memory(plugin, "legacy", metadata="{malformed")
    await seed_memory(
        plugin,
        "other",
        metadata=json.dumps(
            {"subject_id": "11", "associated_user_ids": ["11"], "text": "1"}
        ),
    )
    params = {
        "bot_name": "bot",
        "group_or_user_id": "group",
        "kind": "agreement",
        "status": "active",
    }
    data = (await client.get("/memories", params=params)).json()["data"]
    assert [item["memory_id"] for item in data["items"]] == ["open"]
    counts = (await client.get("/memories/filter_options", params=params)).json()[
        "data"
    ]
    assert counts["sessions"] == [{"group_or_user_id": "group", "total": 1}]
    params["status"] = "expired"
    expired = (await client.get("/memories", params=params)).json()["data"]["items"]
    assert [(item["memory_id"], item["status"]) for item in expired] == [
        ("expired", "expired")
    ]
    exact = (
        await client.get(
            "/memories",
            params={"bot_name": "bot", "associated_user_id": "1", "limit": 2},
        )
    ).json()["data"]
    assert exact["total"] == 5
    assert len(exact["items"]) == 2
    assert (
        await client.get("/memories", params={"kind": "habit' OR 1=1"})
    ).status_code == 400
    derived = (await client.get("/memories", params={"origin": "derived"})).json()[
        "data"
    ]
    assert [item["memory_id"] for item in derived["items"]] == ["habit"]
    clean = (
        await client.post(
            "/memories/clean/candidates",
            json={
                **params,
                "min_age_days": 0,
                "last_hit_before_days": 0,
            },
        )
    ).json()["data"]
    assert [item["memory_id"] for item in clean["items"]] == ["expired"]


@pytest.mark.asyncio
async def test_memory_names_are_scoped_to_selected_session(memory_api):
    client, plugin = memory_api
    await seed_memory(plugin, "event")
    await plugin.db.conn.executemany(
        "INSERT INTO user_profiles (bot_name, group_or_user_id, user_id, call_name) VALUES (?, ?, ?, ?)",
        [("bot", "group", "1", "local name"), ("bot", "other", "1", "private name")],
    )
    await plugin.db.conn.commit()
    data = (
        await client.get(
            "/memories", params={"bot_name": "bot", "group_or_user_id": "group"}
        )
    ).json()["data"]
    assert data["user_id_to_name"] == {"1": "local name"}
    data = (await client.get("/memories", params={"bot_name": "bot"})).json()["data"]
    assert data["user_id_to_name"] == {}


@pytest.mark.asyncio
async def test_manual_atom_keeps_explicit_origin_without_forged_evidence(memory_api):
    client, plugin = memory_api
    body = {
        "bot_name": "bot",
        "group_or_user_id": "group",
        "kind": "habit",
        "subject_id": "A",
        "object_id": "B",
        "topic": "emoji",
        "text": "A 最近常用某个表情与 B 互动",
        "associated_user_ids": "C",
        "evidence": [{"source_id": 9999, "quote": "forged"}],
        "origin": "derived",
    }
    result = (await client.post("/memories/add", json=body)).json()
    assert result["status"] == "success"
    item = (
        await client.get("/memories", params={"origin": "manual", "kind": "habit"})
    ).json()["data"]["items"][0]
    assert item["metadata"]["evidence"] == []
    assert item["metadata"]["manual_entry"] is True
    assert item["metadata"]["origin"] == "observed"
    assert item["metadata"]["associated_user_ids"] == ["A", "B", "C"]
    assert item["metadata"]["subject_id"] == "A"
    assert item["metadata"]["object_id"] == "B"
    plugin.passive_memory_manager._sync_memory_vectors.assert_awaited_once_with(
        "bot", "group"
    )
    derived = (await client.get("/memories", params={"origin": "derived"})).json()[
        "data"
    ]
    assert derived["total"] == 0
    assert (
        await client.post(
            "/memories/add", json={**body, "kind": "relationship", "object_id": ""}
        )
    ).status_code == 400
    assert (
        await client.post("/memories/add", json={**body, "expires_at": "2999-01-01"})
    ).status_code == 400


@pytest.mark.asyncio
async def test_agreement_edit_preserves_provenance_and_checks_revision(memory_api):
    client, plugin = memory_api
    await seed_memory(plugin, "promise", kind="agreement")
    item = (await client.get("/memories")).json()["data"]["items"][0]
    body = {
        "memory_id": "promise",
        "bot_name": "bot",
        "group_or_user_id": "group",
        "text": "约定已完成",
        "status": "completed",
        "revision": item["revision"],
        "associated_user_ids": "B",
        "user_id": "admin",
    }
    await plugin.data_cache.get_memories("bot", "group")
    assert (await client.post("/memories/update", json=body)).json()[
        "status"
    ] == "success"
    after = (await client.get("/memories")).json()["data"]["items"][0]
    assert after["memory_id"] == item["memory_id"]
    assert after["kind"] == "agreement"
    assert after["status"] == "completed"
    assert after["created_at"] == item["created_at"]
    assert after["metadata"]["evidence"] == item["metadata"]["evidence"]
    assert after["metadata"]["subject_id"] == "1"
    assert after["metadata"]["associated_user_ids"] == ["1", "B"]
    assert after["metadata"]["manually_edited"] is True
    assert after["revision"] == item["revision"] + 1
    assert await plugin.data_cache.get_memories("bot", "group") == []
    for changes in [
        {},
        {"revision": after["revision"], "status": "active"},
        {"revision": after["revision"], "kind": "event"},
        {"group_or_user_id": "other"},
    ]:
        assert (
            await client.post("/memories/update", json={**body, **changes})
        ).status_code == 400
    assert (await client.get("/memories")).json()["data"]["items"][0][
        "revision"
    ] == after["revision"]


@pytest.mark.asyncio
async def test_expiry_edits_are_validated_and_historical_rows_stay_closed(memory_api):
    client, plugin = memory_api
    await seed_memory(plugin, "event")
    await seed_memory(
        plugin, "expired", kind="agreement", expires_at="2020-01-01T00:00:00+00:00"
    )
    base = {"bot_name": "bot", "group_or_user_id": "group", "text": "text"}
    for fields in [
        {"memory_id": "event", "status": "completed"},
        {"memory_id": "event", "expires_at": "2999-01-01"},
        {"memory_id": "expired", "expires_at": ""},
        {"memory_id": "expired", "expires_at": "2999-01-01"},
    ]:
        assert (
            await client.post("/memories/update", json={**base, **fields})
        ).status_code == 400
    result = await client.post(
        "/memories/update", json={**base, "memory_id": "expired"}
    )
    assert result.json()["status"] == "success"
    expired = (await client.get("/memories", params={"status": "expired"})).json()[
        "data"
    ]["items"][0]
    assert expired["status"] == "expired"


@pytest.mark.asyncio
async def test_scoped_delete_rejects_another_conversation(memory_api):
    client, plugin = memory_api
    await seed_memory(plugin, 'event')
    result = await client.post('/memories/delete', json={
        'memory_id': 'event', 'bot_name': 'bot', 'group_or_user_id': 'other',
    })
    assert result.status_code == 400
    assert (await client.get('/memories')).json()['data']['total'] == 1
    result = await client.post('/memories/delete', json={
        'memory_id': 'event', 'bot_name': 'bot', 'group_or_user_id': 'group',
    })
    assert result.json()['status'] == 'success'
    assert (await client.get('/memories')).json()['data']['total'] == 0
