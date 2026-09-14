import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import httpx
import pytest
import pytest_asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from core.database.database import Database
from core.database.schema import initialize_database
from core.utils.scheduler import Scheduler
from core.utils.schemas import ShortTask
from core.utils.task_board import TaskBoardManager
from core.web.webui_manager import WebUIManager
from fastapi import FastAPI, Request

from astrbot.api.web import PluginRequest, bind_request_context


@pytest_asyncio.fixture
async def task_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path / "runtime"))
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await initialize_database(conn)
        scheduler = Scheduler.__new__(Scheduler)
        scheduler.registered_funcs = {"remind": AsyncMock()}
        scheduler.scheduler = AsyncIOScheduler()
        scheduler.scheduler.start(paused=True)
        plugin = SimpleNamespace(
            db=Database(conn),
            tools_config={"task_board_max_active": 3},
            task_manager=scheduler,
            get_bot_config=lambda name=None: {"nickname": "Giftia"},
            context=MagicMock(),
        )
        plugin.task_board = TaskBoardManager(plugin)
        manager = WebUIManager(plugin)
        manager.register_routes()
        routes = {
            call.kwargs["route"]: call.kwargs
            for call in plugin.context.register_web_api.call_args_list
        }
        app = FastAPI()

        @app.api_route("/{endpoint:path}", methods=["GET", "POST"])
        async def dispatch(endpoint: str, request: Request):
            route = routes[f"/astrbot_plugin_giftia/{endpoint}"]
            assert request.method in route["methods"]
            with bind_request_context(PluginRequest(request)):
                return await route["view_handler"]()

        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test"
            ) as client:
                yield plugin, client
        finally:
            scheduler.scheduler.shutdown(wait=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("category", "deleted"),
    [
        ("active", {"active"}),
        ("completed", {"completed"}),
        ("archived", {"canceled", "expired"}),
        ("all", {"active", "completed", "canceled", "expired"}),
    ],
)
async def test_clear_short_tasks_preserves_other_sessions(
    task_runtime, category, deleted
):
    plugin, client = task_runtime
    statuses = {"active", "completed", "canceled", "expired"}
    for bot, session in [("Giftia", "g1"), ("OtherBot", "g1"), ("Giftia", "g2")]:
        for status in statuses:
            await plugin.db.insert_short_task(
                ShortTask(
                    task_id=f"{bot}_{session}_{status}",
                    bot_name=bot,
                    group_or_user_id=session,
                    creator_user_id="tester",
                    creator_nickname="Tester",
                    content=status,
                    status=status,
                )
            )
    response = await client.post(
        "/task_board/clear",
        json={
            "bot_name": "Giftia",
            "group_or_user_id": "g1",
            "status": category,
        },
    )
    assert response.status_code == 200
    assert response.json()["cleared_count"] == len(deleted)
    remaining = await plugin.db.get_short_tasks("Giftia", "g1")
    assert {task.status for task in remaining} == statuses - deleted
    assert len(await plugin.db.get_short_tasks("OtherBot", "g1")) == 4
    assert len(await plugin.db.get_short_tasks("Giftia", "g2")) == 4
    repeat = await client.post(
        "/task_board/clear",
        json={
            "bot_name": "Giftia",
            "group_or_user_id": "g1",
            "status": category,
        },
    )
    assert repeat.json()["cleared_count"] == 0


@pytest.mark.asyncio
async def test_short_task_create_defaults_expiry_and_limit(task_runtime):
    plugin, client = task_runtime
    payload = {
        "bot_name": "Giftia",
        "group_or_user_id": "g1",
        "content": "  Plan a trip  ",
    }
    response = await client.post("/task_board/create", json=payload)
    assert response.status_code == 200
    task = response.json()["data"]
    assert task["content"] == "Plan a trip"
    assert task["status"] == "active"
    assert task["creator_user_id"] == "dashboard"
    assert datetime.fromisoformat(task["expires_at"]) > datetime.now()
    expiry = (datetime.now() + timedelta(days=2)).isoformat(timespec="seconds")
    response = await client.post(
        "/task_board/create", json={**payload, "expires_at": expiry}
    )
    assert response.json()["data"]["expires_at"] == expiry.replace("T", " ")
    await client.post("/task_board/create", json=payload)
    response = await client.post("/task_board/create", json=payload)
    assert response.status_code == 400
    assert "上限" in response.json()["message"]
    board = await client.get(
        "/task_board", params={"bot_name": "Giftia", "group_or_user_id": "g1"}
    )
    assert board.json()["data"]["stats"]["active"] == 3
    assert len(await plugin.db.get_short_tasks("Giftia", "g1")) == 3


@pytest.mark.asyncio
async def test_short_task_create_limit_is_atomic_under_concurrency(task_runtime):
    plugin, client = task_runtime
    payload = {
        "bot_name": "Giftia",
        "group_or_user_id": "g1",
        "content": "concurrent task",
    }

    responses = await asyncio.gather(
        *[
            client.post(
                "/task_board/create",
                json={**payload, "content": f"concurrent task {index}"},
            )
            for index in range(20)
        ]
    )

    successes = [response for response in responses if response.status_code == 200]
    failures = [response for response in responses if response.status_code == 400]
    assert len(successes) == plugin.tools_config["task_board_max_active"]
    assert len(failures) == 20 - len(successes)
    assert all("上限" in response.json()["message"] for response in failures)
    assert (
        await plugin.db.count_active_short_tasks("Giftia", "g1")
        == plugin.tools_config["task_board_max_active"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"bot_name": ""},
        {"group_or_user_id": ""},
        {"content": "  "},
        {"expires_at": "not-a-date"},
        {"expires_at": "2000-01-01T00:00"},
    ],
)
async def test_short_task_rejects_invalid_creation(task_runtime, change):
    plugin, client = task_runtime
    response = await client.post(
        "/task_board/create",
        json={
            "bot_name": "Giftia",
            "group_or_user_id": "g1",
            "content": "Task",
            **change,
        },
    )
    assert response.status_code == 400
    assert await plugin.db.get_short_tasks("Giftia", "g1") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("group_id", ["g1", ""])
@pytest.mark.parametrize("use_existing_job", [False, True])
async def test_create_scheduled_task_preserves_real_routing(
    task_runtime, group_id, use_existing_job
):
    plugin, client = task_runtime
    context = {
        "unified_msg_origin": "adapter:GroupMessage:g1"
        if group_id
        else "adapter:FriendMessage:g1",
        "adapter_id": "adapter",
        "self_id": "bot-account",
        "platform_name": "aiocqhttp",
        "group_id": group_id,
    }
    if use_existing_job:
        plugin.task_manager.add_job(
            "old",
            "remind",
            "0 8 * * *",
            kwargs={
                **context,
                "bot_name": "Giftia",
                "group_or_user_id": "g1",
                "user_id": "original-user",
                "user_name": "Original user",
            },
        )
    else:
        await plugin.db.upsert_kv_data("task_session:Giftia:g1", json.dumps(context))
    for time_expr in ["0 8 * * *", (datetime.now() + timedelta(days=1)).isoformat()]:
        response = await client.post(
            "/scheduled_tasks/create",
            json={
                "bot_name": "Giftia",
                "group_or_user_id": "g1",
                "time_expr": time_expr,
                "remind_message": "  Drink water  ",
            },
        )
        assert response.status_code == 200, response.text
        job = plugin.task_manager.scheduler.get_job(response.json()["data"]["task_id"])
        assert job.args == ("remind",)
        assert all(job.kwargs[key] == value for key, value in context.items())
        assert job.kwargs["remind_message"] == "Drink water"
        assert job.kwargs["user_id"] == ("dashboard" if group_id else "g1")
        assert job.kwargs["user_name"] == "任务看板"
        assert job.kwargs["nickname"] == "Giftia"
    response = await client.get(
        "/scheduled_tasks", params={"bot_name": "Giftia", "group_or_user_id": "g1"}
    )
    assert response.json()["data"]["total"] == 2 + int(use_existing_job)


@pytest.mark.asyncio
async def test_scheduled_creation_errors_do_not_create_jobs(task_runtime):
    plugin, client = task_runtime
    payload = {
        "bot_name": "Giftia",
        "group_or_user_id": "g1",
        "time_expr": "0 8 * * *",
        "remind_message": "Task",
    }
    response = await client.post("/scheduled_tasks/create", json=payload)
    assert response.status_code == 400
    assert "会话路由信息" in response.json()["message"]
    await plugin.db.upsert_kv_data(
        "task_session:Giftia:g1",
        json.dumps(
            {
                "unified_msg_origin": "adapter:GroupMessage:g1",
                "adapter_id": "adapter",
                "self_id": "bot-account",
                "platform_name": "aiocqhttp",
                "group_id": "g1",
            }
        ),
    )
    for change in [
        {"bot_name": ""},
        {"group_or_user_id": ""},
        {"remind_message": "  "},
        {"time_expr": ""},
        {"time_expr": "invalid"},
        {"time_expr": "2000-01-01 08:00:00"},
    ]:
        response = await client.post(
            "/scheduled_tasks/create", json={**payload, **change}
        )
        assert response.status_code == 400, response.text
    plugin.task_manager.registered_funcs = {}
    response = await client.post("/scheduled_tasks/create", json=payload)
    assert response.status_code == 400
    assert plugin.task_manager.scheduler.get_jobs() == []
