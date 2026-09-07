import json
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request

from astrbot.api.web import PluginRequest, bind_request_context
from core.database.database import Database
from core.web.dashboard_api import DashboardApi


@pytest_asyncio.fixture
async def dashboard_api_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path / "runtime"))
    import aiosqlite
    from core.database.schema import initialize_database

    db_file = tmp_path / "test_chat_history.db"
    conn = await aiosqlite.connect(db_file)
    conn.row_factory = aiosqlite.Row
    await initialize_database(conn)
    db = Database(conn)

    plugin = SimpleNamespace(db=db)
    api = DashboardApi(plugin)

    app = FastAPI()

    @app.api_route("/{endpoint:path}", methods=["GET", "POST"])
    async def dispatch(endpoint: str, request: Request):
        handlers = {
            "settings/nav_config": {
                "GET": api.get_nav_config,
                "POST": api.save_nav_config,
            }
        }
        handler = handlers.get(endpoint, {}).get(request.method)
        if not handler:
            return {"status": "not_found"}
        with bind_request_context(PluginRequest(request)):
            return await handler()

    yield app, plugin
    await conn.close()


@pytest.mark.asyncio
async def test_dashboard_nav_config_flow(dashboard_api_fixture):
    app, plugin = dashboard_api_fixture
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        # 1. Initial GET should return None
        res = await client.get("/settings/nav_config")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert data["data"] is None

        # 2. POST invalid payload
        invalid_res = await client.post("/settings/nav_config", json={"wrong": 123})
        assert invalid_res.status_code == 400
        assert invalid_res.json()["status"] == "error"

        # 3. POST valid config
        new_config = [
            {"id": "chat-history", "pinned": True},
            {"id": "slang", "pinned": True},
            {"id": "bots", "pinned": False},
        ]
        save_res = await client.post("/settings/nav_config", json={"config": new_config})
        assert save_res.status_code == 200
        save_data = save_res.json()
        assert save_data["status"] == "success"
        assert len(save_data["data"]) == 3
        assert save_data["data"][0]["id"] == "chat-history"
        assert save_data["data"][0]["pinned"] is True
        assert save_data["data"][2]["pinned"] is False

        # 4. GET should now return the saved config
        get_res = await client.get("/settings/nav_config")
        assert get_res.status_code == 200
        get_data = get_res.json()
        assert get_data["status"] == "success"
        assert get_data["data"] == save_data["data"]
