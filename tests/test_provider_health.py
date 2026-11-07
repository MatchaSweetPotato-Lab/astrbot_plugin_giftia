"""Exercise shared admission, failure classification and race-safe recovery."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from core.llm.call_llm import CallLLM
from core.llm.llm_tools import InspectForwardMessageTool
from core.llm.provider_health import (
    ProviderHealth,
    ProviderUnavailableError,
    get_provider_health,
)
from core.llm.xml_parse import XmlParse
from core.memory.passive_tasks import PassiveSummaryTaskMixin

from astrbot.core.exceptions import EmptyModelOutputError


class ServiceError(Exception):
    def __init__(self, status=503, *, headers=None, body=None):
        super().__init__("provider detail containing sk-secret-never-publish")
        self.status_code = status
        self.response = SimpleNamespace(headers=headers or {})
        self.body = body or {}


@pytest.fixture
def runtime():
    ticks = [100.0]
    context = SimpleNamespace()
    health = ProviderHealth(context, clock=lambda: ticks[0])
    return SimpleNamespace(health=health, ticks=ticks, context=context)


@pytest.mark.asyncio
async def test_failures_cool_down_without_consuming_skipped_requests(runtime):
    rt = runtime
    operation = AsyncMock(side_effect=ServiceError())
    for expected in (1, 2):
        with pytest.raises(ServiceError):
            await rt.health.call(operation, chat_provider_id="model")
        assert rt.health.snapshot("model")["failures"] == expected
    snapshot = rt.health.snapshot("model")
    assert snapshot["status"] == "cooldown" and snapshot["retry_in_seconds"] == 60
    assert "sk-secret" not in json.dumps(snapshot)
    with pytest.raises(ProviderUnavailableError):
        await rt.health.call(operation, chat_provider_id="model")
    assert operation.await_count == 2


@pytest.mark.asyncio
async def test_success_resets_failures_even_when_reply_format_is_invalid(runtime):
    operation = AsyncMock(
        side_effect=[
            ServiceError(),
            SimpleNamespace(completion_text="no XML"),
            ServiceError(),
        ]
    )
    for _ in range(2):
        with pytest.raises(ServiceError):
            await runtime.health.call(operation, chat_provider_id="model")
        if operation.await_count == 1:
            await runtime.health.call(operation, chat_provider_id="model")
    assert runtime.health.snapshot("model")["status"] == "degraded"
    assert runtime.health.snapshot("model")["failures"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,reason,minimum",
    [
        (ServiceError(429), "rate_limit", 60),
        (ServiceError(429, headers={"Retry-After": "180"}), "rate_limit", 180),
        (ServiceError(429, headers={"Retry-After": "inf"}), "rate_limit", 60),
        (
            ServiceError(429, headers={"Retry-After": "http-date"}),
            "rate_limit",
            230,
        ),
        (ServiceError(503, headers={"Retry-After": "90"}), "server_error", 90),
        (ServiceError(401), "authentication", 600),
        (ServiceError(403), "authentication", 600),
        (ServiceError(404), "provider_missing", 600),
        (
            ServiceError(429, body={"error": {"code": "insufficient_quota"}}),
            "quota",
            600,
        ),
    ],
)
async def test_explicit_unavailability_uses_error_details_and_retry_after(
    runtime, error, reason, minimum
):
    if error.response.headers.get("Retry-After") == "http-date":
        error.response.headers["Retry-After"] = format_datetime(
            datetime.now(timezone.utc) + timedelta(minutes=4)
        )
    with pytest.raises(ServiceError):
        await runtime.health.call(
            AsyncMock(side_effect=error), chat_provider_id="model"
        )
    snapshot = runtime.health.snapshot("model")
    assert snapshot["status"] == "cooldown"
    assert snapshot["reason"] == reason
    assert snapshot["retry_in_seconds"] >= minimum


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ValueError("bad local input"),
        RuntimeError("tool failed"),
        EmptyModelOutputError(),
        ServiceError(400),
        RuntimeError("The model output failed Gemini platform safety checks."),
        httpx.HTTPStatusError(
            "media URL is missing",
            request=httpx.Request("GET", "https://example.com/image"),
            response=httpx.Response(404),
        ),
    ],
)
async def test_input_output_safety_and_local_errors_do_not_disable_a_model(
    runtime, error
):
    operation = AsyncMock(side_effect=error)
    for _ in range(3):
        with pytest.raises(type(error)):
            await runtime.health.call(operation, chat_provider_id="model")
    assert runtime.health.snapshot("model")["failures"] == 0
    assert runtime.health.snapshot("model")["status"] == "unknown"


@pytest.mark.asyncio
async def test_wrapped_sdk_errors_are_classified(runtime):
    async def request(**kwargs):
        try:
            raise ServiceError(429)
        except ServiceError as error:
            raise RuntimeError(
                "Gemini API rate limit reached or API key issue detected."
            ) from error

    with pytest.raises(RuntimeError):
        await runtime.health.call(request, chat_provider_id="model")
    assert runtime.health.snapshot("model")["reason"] == "rate_limit"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "daily,reason,minimum", [(False, "rate_limit", 180), (True, "quota", 600)]
)
async def test_gemini_structured_quota_and_retry_info(runtime, daily, reason, minimum):
    from google.genai.errors import APIError

    error = APIError(
        429,
        {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": "180s",
                    },
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [
                            {
                                "quotaId": "RequestsPerDay"
                                if daily
                                else "RequestsPerMinute"
                            }
                        ],
                    },
                ],
            }
        },
    )
    with pytest.raises(APIError):
        await runtime.health.call(
            AsyncMock(side_effect=error), chat_provider_id="model"
        )
    snapshot = runtime.health.snapshot("model")
    assert snapshot["reason"] == reason
    assert snapshot["retry_in_seconds"] >= minimum


@pytest.mark.asyncio
async def test_only_one_recovery_probe_runs_and_failure_extends_cooldown(runtime):
    health, ticks = runtime.health, runtime.ticks
    for _ in range(2):
        with pytest.raises(ServiceError):
            await health.call(
                AsyncMock(side_effect=ServiceError()), chat_provider_id="model"
            )
    ticks[0] += 60
    assert health.snapshot("model")["status"] == "probe_due"
    entered, release = asyncio.Event(), asyncio.Event()

    async def fail(**kwargs):
        entered.set()
        await release.wait()
        raise ServiceError()

    probe = asyncio.create_task(health.call(fail, chat_provider_id="model"))
    await entered.wait()
    assert health.snapshot("model")["status"] == "probing"
    blocked = AsyncMock()
    with pytest.raises(ProviderUnavailableError):
        await health.call(blocked, chat_provider_id="model")
    blocked.assert_not_awaited()
    release.set()
    with pytest.raises(ServiceError):
        await probe
    assert health.snapshot("model")["retry_in_seconds"] == 120
    await health.call(
        AsyncMock(return_value=SimpleNamespace(completion_text="OK")),
        chat_provider_id="model",
        force_probe=True,
    )
    assert health.snapshot("model")["status"] == "healthy"


@pytest.mark.asyncio
async def test_cancelled_probe_releases_ownership_without_extending_cooldown(runtime):
    entered = asyncio.Event()

    async def wait(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        runtime.health.call(wait, chat_provider_id="model", force_probe=True)
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.health.snapshot("model")["status"] == "unknown"
    await runtime.health.call(
        AsyncMock(return_value=SimpleNamespace(completion_text="OK")),
        chat_provider_id="model",
        force_probe=True,
    )
    assert runtime.health.snapshot("model")["status"] == "healthy"


@pytest.mark.asyncio
async def test_late_request_result_cannot_undo_manual_recovery(runtime):
    entered, release = asyncio.Event(), asyncio.Event()

    async def late_failure(**kwargs):
        entered.set()
        await release.wait()
        raise ServiceError()

    pending = asyncio.create_task(
        runtime.health.call(late_failure, chat_provider_id="model")
    )
    await entered.wait()
    for _ in range(2):
        with pytest.raises(ServiceError):
            await runtime.health.call(
                AsyncMock(side_effect=ServiceError()), chat_provider_id="model"
            )
    await runtime.health.call(
        AsyncMock(return_value=SimpleNamespace(completion_text="OK")),
        chat_provider_id="model",
        force_probe=True,
    )
    release.set()
    with pytest.raises(ServiceError):
        await pending
    assert runtime.health.snapshot("model")["status"] == "healthy"


@pytest.mark.asyncio
async def test_configuration_change_releases_a_cooling_provider(runtime):
    provider = SimpleNamespace(provider_config={"key": "old"})
    runtime.context.provider_manager = SimpleNamespace(inst_map={"model": provider})
    with pytest.raises(ServiceError):
        await runtime.health.call(
            AsyncMock(side_effect=ServiceError(401)), chat_provider_id="model"
        )
    provider.provider_config["key"] = "new"
    await runtime.health.call(AsyncMock(return_value="ok"), chat_provider_id="model")
    assert runtime.health.snapshot("model")["status"] == "healthy"


@pytest.mark.asyncio
async def test_request_timeout_is_counted_but_tool_errors_are_excluded(runtime):
    async def slow(**kwargs):
        await asyncio.sleep(1)

    for _ in range(2):
        with pytest.raises(TimeoutError):
            await runtime.health.call(
                slow,
                chat_provider_id="timeout",
                request_timeout=0.01,
            )
    assert runtime.health.snapshot("timeout")["status"] == "cooldown"
    for _ in range(3):
        with pytest.raises(ServiceError):
            await runtime.health.call(
                AsyncMock(side_effect=ServiceError()),
                chat_provider_id="tool",
                count_failure=lambda: False,
            )
    assert runtime.health.snapshot("tool")["failures"] == 0


@pytest.mark.asyncio
async def test_every_conversation_model_path_shares_health_and_skips_current_list(
    runtime,
):
    context = SimpleNamespace(llm_generate=AsyncMock(), tool_loop_agent=AsyncMock())
    plugin = SimpleNamespace(
        context=context,
        provider_health=runtime.health,
        bot_map={
            "bot": {
                "llm_reply_conf": {
                    "provider_ids": ["bad", "good"],
                    "invalid_reply_fallback_provider_ids": ["must-not-run"],
                }
            }
        },
        adapter_id_map={"adapter": "bot"},
        tools_config={},
        passive_memory_provider_ids=["bad", "good"],
        db=SimpleNamespace(log_token_usage=AsyncMock()),
    )
    caller = CallLLM(
        context,
        XmlParse(None, None),
        {"decision_retry_times": 2, "reply_retry_times": 2},
        {"media_caption_provider_ids": ["bad", "good"]},
        plugin=plugin,
    )
    caller._log_token_usage_safely = AsyncMock()
    for _ in range(2):
        with pytest.raises(ServiceError):
            await get_provider_health(plugin).call(
                AsyncMock(side_effect=ServiceError()), chat_provider_id="bad"
            )
    context.llm_generate.return_value = SimpleNamespace(
        completion_text='<decision reply="true" use_rag="false"/>'
    )
    assert await caller.call_llm_decision(["bad", "good"], "", "input") is not None
    assert await caller.call_llm_reply_review("bad", "input", "bot", "group") is None
    event = SimpleNamespace(
        platform_meta=SimpleNamespace(id="adapter"), get_platform_name=lambda: "test"
    )
    context.tool_loop_agent.return_value = SimpleNamespace(
        completion_text="<message>hello</message>",
        reasoning_content="",
        tools_call_name=[],
    )
    assert (
        await caller.call_llm_reply(event, "group", ["bad", "good"], "", "input")
        is not None
    )
    context.llm_generate.return_value = SimpleNamespace(
        completion_text=json.dumps(
            {"caption": "ok", "isUseful": True, "name": "Sticker"}
        )
    )
    for method, arguments in (
        (caller.call_llm_image_caption, {"image_urls": ["base64://aGVsbG8="]}),
        (caller.call_llm_audio_caption, {"audio_urls": ["audio.wav"]}),
        (
            caller.call_llm_video_caption,
            {"video_url": "data:video/mp4;base64,aGVsbG8="},
        ),
        (
            caller.call_llm_sticker_analysis,
            {
                "image_urls": ["base64://aGVsbG8="],
                "categories": [],
                "media_id": "sticker",
            },
        ),
    ):
        assert await method(**arguments) is not None
    summary = PassiveSummaryTaskMixin()
    summary.plugin = plugin
    assert await summary._call_summary_llm("test", "", "input")
    forward = InspectForwardMessageTool(plugin=plugin)
    assert await forward._generate_forward_summary(
        plugin, "bot", "id", [{"content": "hello"}], [], 1000
    )
    assert all(
        call.kwargs["chat_provider_id"] == "good"
        for call in context.llm_generate.await_args_list
    )
    assert context.tool_loop_agent.await_args.kwargs["chat_provider_id"] == "good"
    assert caller.provider_health is get_provider_health(plugin)
