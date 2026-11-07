"""Verify model inventory and successful manual recovery through real routes."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from core.llm.provider_health import get_provider_health
from core.web.model_health_api import ModelHealthApi
from fastapi import FastAPI, Request

from astrbot.api.web import PluginRequest, bind_request_context


class ServiceError(Exception):
    status_code = 503


@pytest.fixture
def runtime():
    provider_ids = [
        "shared",
        "reply",
        "backup",
        "review",
        "media",
        "inspect",
        "summary",
        "unused",
    ]
    context = SimpleNamespace(
        llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text="OK")),
        provider_manager=SimpleNamespace(
            inst_map={
                provider_id: SimpleNamespace(
                    provider_config={
                        "name": f"Name {provider_id}",
                        "api_key": "sk-secret-never-publish",
                    },
                    model_name=f"model-{provider_id}",
                )
                for provider_id in provider_ids
            }
        ),
    )
    plugin = SimpleNamespace(
        context=context,
        bot_map={
            "bot": {
                "decision_conf": {"provider_ids": ["shared"]},
                "llm_reply_conf": {
                    "provider_ids": ["reply", "shared", "missing"],
                    "invalid_reply_fallback_provider_ids": ["backup"],
                    "review_provider_id": "review",
                },
            }
        },
        caption_config={
            "media_caption_provider_ids": ["media", "shared"],
            "inspect_media_provider_ids": ["inspect"],
        },
        passive_memory_provider_ids=["summary", "shared"],
        db=SimpleNamespace(log_token_usage=AsyncMock()),
    )
    api = ModelHealthApi(plugin)
    app = FastAPI()

    @app.api_route("/models/{action}", methods=["GET", "POST"])
    async def dispatch(action: str, request: Request):
        with bind_request_context(PluginRequest(request)):
            return await (
                api.get_model_health()
                if action == "health"
                else api.test_model_connection()
            )

    return SimpleNamespace(plugin=plugin, app=app, health=get_provider_health(plugin))


@pytest.mark.asyncio
async def test_inventory_deduplicates_ids_covers_all_roles_and_omits_credentials(
    runtime,
):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(runtime.app), base_url="http://test"
    ) as client:
        response = await client.get("/models/health")
    rows = {row["provider_id"]: row for row in response.json()["data"]}
    assert set(rows) == {
        "shared",
        "reply",
        "backup",
        "review",
        "media",
        "inspect",
        "summary",
        "missing",
    }
    assert set(rows["shared"]["uses"]) == {
        "bot · 前置决策",
        "bot · 正式回复/转发转述",
        "媒体转述",
        "记忆/好感度归纳",
    }
    assert rows["missing"]["registered"] is False
    assert rows["reply"]["model"] == "model-reply"
    assert "sk-secret" not in response.text
    runtime.plugin.context.llm_generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_connectivity_test_bypasses_cooldown_and_restores_shared_state(
    runtime,
):
    for _ in range(2):
        with pytest.raises(ServiceError):
            await runtime.health.call(
                AsyncMock(side_effect=ServiceError()), chat_provider_id="shared"
            )
    assert runtime.health.snapshot("shared")["status"] == "cooldown"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(runtime.app), base_url="http://test"
    ) as client:
        result = (
            await client.post("/models/test", json={"provider_id": "shared"})
        ).json()
        rows = (await client.get("/models/health")).json()["data"]
    assert result["status"] == "success"
    assert result["data"]["status"] == "healthy"
    assert result["data"]["failures"] == 0
    assert (
        next(row for row in rows if row["provider_id"] == "shared")["status"]
        == "healthy"
    )
    runtime.plugin.context.llm_generate.assert_awaited_once()
    usage = runtime.plugin.db.log_token_usage.await_args.kwargs
    assert usage["type"] == "provider_test"
    assert usage["extra_info"]["status"] == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
async def test_failed_manual_test_never_restores_health(runtime, empty):
    for _ in range(2):
        with pytest.raises(ServiceError):
            await runtime.health.call(
                AsyncMock(side_effect=ServiceError()), chat_provider_id="shared"
            )
    if empty:
        runtime.plugin.context.llm_generate.return_value = SimpleNamespace(
            completion_text=""
        )
    else:
        runtime.plugin.context.llm_generate.side_effect = ServiceError(
            "sk-secret-never-publish"
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(runtime.app), base_url="http://test"
    ) as client:
        response = await client.post("/models/test", json={"provider_id": "shared"})
    assert response.json()["status"] == "error"
    assert response.json()["data"]["status"] == "cooldown"
    assert "sk-secret" not in response.text
    assert (
        runtime.plugin.db.log_token_usage.await_args.kwargs["extra_info"]["status"]
        == "api_failed"
    )


@pytest.mark.asyncio
async def test_concurrent_tests_allow_only_one_provider_request(runtime):
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(**kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(completion_text="OK")

    runtime.plugin.context.llm_generate.side_effect = generate
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(runtime.app), base_url="http://test"
    ) as client:
        first = asyncio.create_task(
            client.post("/models/test", json={"provider_id": "shared"})
        )
        await entered.wait()
        second = await client.post("/models/test", json={"provider_id": "shared"})
        assert second.status_code == 400
        runtime.plugin.context.llm_generate.assert_awaited_once()
        release.set()
        assert (await first).json()["data"]["status"] == "healthy"
    runtime.plugin.db.log_token_usage.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload", [{}, {"provider_id": "unused"}, {"provider_id": ["shared"]}, []]
)
async def test_unknown_or_malformed_test_targets_are_rejected_without_requests(
    runtime, payload
):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(runtime.app), base_url="http://test"
    ) as client:
        response = await client.post("/models/test", json=payload)
    assert response.status_code == 400
    runtime.plugin.context.llm_generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_inventory_includes_models_configured_for_disabled_bots(runtime):
    runtime.plugin.bot_config_manager = SimpleNamespace(
        bots=[
            {
                "name": "disabled",
                "enabled": False,
                "llm_reply_conf": {"provider_ids": ["reply"]},
            }
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(runtime.app), base_url="http://test"
    ) as client:
        rows = (await client.get("/models/health")).json()["data"]
    reply = next(row for row in rows if row["provider_id"] == "reply")
    assert reply["uses"] == ["disabled（已停用） · 正式回复/转发转述"]


@pytest.mark.asyncio
async def test_background_probe_and_manual_test_share_one_request_slot(runtime):
    health = runtime.health
    ticks = [100.0]
    health.clock = lambda: ticks[0]
    health.CHECK_INTERVAL = 0.01
    for _ in range(2):
        with pytest.raises(ServiceError):
            await health.call(
                AsyncMock(side_effect=ServiceError()), chat_provider_id="shared"
            )
    entered, release, logged = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def generate(**kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(completion_text="OK")

    runtime.plugin.context.llm_generate.side_effect = generate
    runtime.plugin.db.log_token_usage.side_effect = lambda **_: logged.set()
    ticks[0] += 60
    health.start(runtime.plugin.db.log_token_usage)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(runtime.app), base_url="http://test"
        ) as client:
            response = await client.post("/models/test", json={"provider_id": "shared"})
        assert response.status_code == 400
        assert "正在进行" in response.json()["message"]
        runtime.plugin.context.llm_generate.assert_awaited_once()
        runtime.plugin.db.log_token_usage.assert_not_awaited()
        release.set()
        await asyncio.wait_for(logged.wait(), 1)
        assert health.snapshot("shared")["status"] == "healthy"
        assert runtime.plugin.db.log_token_usage.await_args.kwargs["extra_info"] == {
            "status": "success",
            "manual_test": False,
        }
    finally:
        await health.close()
