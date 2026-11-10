"""Exercise shared admission, failure classification and race-safe recovery."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from core.llm.call_llm import CallLLM
from core.llm.llm_tools import InspectForwardMessageTool
from core.llm.provider_health import (
    InvalidProbeResponseError,
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


def probe_reply(*, prompt, **kwargs):
    """Echo the requested challenge for successful connectivity tests.

    Args:
        prompt: Probe prompt ending with the challenge.
        **kwargs: Other SDK arguments.

    Returns:
        An SDK-like response containing the exact challenge.
    """
    return SimpleNamespace(completion_text=prompt.rsplit("\n", 1)[-1])


@pytest_asyncio.fixture
async def runtime():
    ticks = [100.0]
    context = SimpleNamespace()
    health = ProviderHealth(context, clock=lambda: ticks[0])
    health.CHECK_INTERVAL = 0.01
    recorded = asyncio.Event()
    recorder = AsyncMock(side_effect=lambda **_: recorded.set())
    yield SimpleNamespace(
        health=health,
        ticks=ticks,
        context=context,
        recorded=recorded,
        recorder=recorder,
    )
    await health.close()


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
    assert all(
        call.kwargs["request_max_retries"] == 1 for call in operation.await_args_list
    )


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
    blocked = AsyncMock()
    with pytest.raises(ProviderUnavailableError):
        await health.call(blocked, chat_provider_id="model")
    blocked.assert_not_awaited()
    entered, release = asyncio.Event(), asyncio.Event()

    async def fail(**kwargs):
        entered.set()
        await release.wait()
        raise ServiceError()

    probe = asyncio.create_task(
        health.call(fail, chat_provider_id="model", force_probe=True)
    )
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
        AsyncMock(side_effect=probe_reply),
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
        AsyncMock(side_effect=probe_reply),
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
        AsyncMock(side_effect=probe_reply),
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
@pytest.mark.parametrize("manually_disabled", [False, True])
async def test_every_conversation_model_path_shares_health_and_skips_current_list(
    runtime, manually_disabled
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
    if manually_disabled:
        await runtime.health.update_settings(
            SimpleNamespace(upsert_kv_data=AsyncMock()), enabled=False, provider_id="bad"
        )
    else:
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
        and call.kwargs["request_max_retries"] == 1
        for call in context.llm_generate.await_args_list
    )
    assert context.tool_loop_agent.await_args.kwargs["chat_provider_id"] == "good"
    assert context.tool_loop_agent.await_args.kwargs["request_max_retries"] == 1
    assert caller.provider_health is get_provider_health(plugin)


@pytest.mark.asyncio
async def test_background_recovery_runs_without_business_requests(runtime):
    health = runtime.health
    with pytest.raises(ServiceError):
        await health.call(
            AsyncMock(side_effect=ServiceError(401)), chat_provider_id="model"
        )
    runtime.context.llm_generate = AsyncMock(
        side_effect=probe_reply
    )
    health.start(runtime.recorder)
    monitor = health._monitor_task
    health.start(runtime.recorder)
    assert health._monitor_task is monitor
    await asyncio.sleep(health.CHECK_INTERVAL * 2)
    runtime.context.llm_generate.assert_not_awaited()
    runtime.ticks[0] += 600
    await asyncio.wait_for(runtime.recorded.wait(), 1)
    assert health.snapshot("model")["status"] == "healthy"
    assert health.snapshot("model")["failures"] == 0
    arguments = runtime.context.llm_generate.await_args.kwargs
    assert arguments["chat_provider_id"] == "model"
    assert arguments["request_max_retries"] == 1
    assert arguments["prompt"].rsplit("\n", 1)[-1].startswith("GIFTIA_PROBE_")
    assert set(arguments) == {"chat_provider_id", "prompt", "request_max_retries"}
    usage = runtime.recorder.await_args.kwargs
    assert usage["type"] == "provider_test"
    assert usage["extra_info"] == {"status": "success", "manual_test": False}
    await asyncio.sleep(health.CHECK_INTERVAL * 2)
    runtime.context.llm_generate.assert_awaited_once()
    assert health.snapshot("unused")["status"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [ServiceError(), EmptyModelOutputError(), ValueError()]
)
async def test_failed_background_probes_keep_cooldown_even_for_neutral_errors(
    runtime, error
):
    health = runtime.health
    for _ in range(2):
        with pytest.raises(ServiceError):
            await health.call(
                AsyncMock(side_effect=ServiceError()), chat_provider_id="model"
            )
    runtime.context.llm_generate = AsyncMock(side_effect=error)
    runtime.ticks[0] += 60
    health.start(runtime.recorder)
    await asyncio.wait_for(runtime.recorded.wait(), 1)
    status = health.snapshot("model")
    assert status["status"] == "cooldown"
    assert status["retry_in_seconds"] == 120
    assert status["failures"] == (3 if isinstance(error, ServiceError) else 2)
    assert runtime.recorder.await_args.kwargs["extra_info"] == {
        "status": "api_failed",
        "manual_test": False,
    }
    await asyncio.sleep(health.CHECK_INTERVAL * 3)
    runtime.context.llm_generate.assert_awaited_once()
    blocked = AsyncMock()
    with pytest.raises(ProviderUnavailableError):
        await health.call(blocked, chat_provider_id="model")
    blocked.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_background_text_never_restores_health(runtime):
    health = runtime.health
    with pytest.raises(ServiceError):
        await health.call(
            AsyncMock(side_effect=ServiceError(429)), chat_provider_id="model"
        )
    runtime.context.llm_generate = AsyncMock(
        return_value=SimpleNamespace(completion_text=" \n ")
    )
    runtime.ticks[0] += 60
    health.start(runtime.recorder)
    await asyncio.wait_for(runtime.recorded.wait(), 1)
    assert health.snapshot("model")["status"] == "cooldown"
    assert health.snapshot("model")["retry_in_seconds"] == 120


@pytest.mark.asyncio
async def test_background_failures_back_off_and_never_enter_business(runtime):
    health = runtime.health
    for _ in range(2):
        with pytest.raises(ServiceError):
            await health.call(
                AsyncMock(side_effect=ServiceError()), chat_provider_id="model"
            )
    runtime.context.llm_generate = AsyncMock(side_effect=ServiceError())
    health.start(runtime.recorder)
    previous = 60
    business = AsyncMock()
    for expected in (120, 300, 600, 600):
        runtime.recorded.clear()
        runtime.ticks[0] += previous
        with pytest.raises(ProviderUnavailableError):
            await health.call(business, chat_provider_id="model")
        await asyncio.wait_for(runtime.recorded.wait(), 1)
        assert health.snapshot("model")["retry_in_seconds"] == expected
        previous = expected
    business.assert_not_awaited()
    assert runtime.context.llm_generate.await_count == 4


@pytest.mark.asyncio
async def test_slow_background_provider_does_not_block_other_recovery_or_business(
    runtime,
):
    health = runtime.health
    for provider_id in ("slow", "fast"):
        with pytest.raises(ServiceError):
            await health.call(
                AsyncMock(side_effect=ServiceError(401)),
                chat_provider_id=provider_id,
            )
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(*, chat_provider_id, **kwargs):
        if chat_provider_id == "slow":
            entered.set()
            await release.wait()
        return probe_reply(**kwargs)

    runtime.context.llm_generate = AsyncMock(side_effect=generate)
    runtime.ticks[0] += 600
    health.start(runtime.recorder)
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.wait_for(runtime.recorded.wait(), 1)
    assert health.snapshot("slow")["status"] == "probing"
    assert health.snapshot("fast")["status"] == "healthy"
    business = AsyncMock(return_value="business reply")
    with pytest.raises(ProviderUnavailableError):
        await health.call(business, chat_provider_id="slow")
    assert await health.call(business, chat_provider_id="other") == "business reply"
    business.assert_awaited_once_with(chat_provider_id="other", request_max_retries=1)
    release.set()


@pytest.mark.asyncio
async def test_background_probe_timeout_keeps_provider_unavailable(runtime):
    health = runtime.health
    with pytest.raises(ServiceError):
        await health.call(
            AsyncMock(side_effect=ServiceError(429)), chat_provider_id="model"
        )
    cancelled = asyncio.Event()

    async def generate(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    health.PROBE_TIMEOUT = 0.01
    runtime.context.llm_generate = AsyncMock(side_effect=generate)
    runtime.ticks[0] += 60
    health.start(runtime.recorder)
    await asyncio.wait_for(runtime.recorded.wait(), 1)
    assert cancelled.is_set()
    assert health.snapshot("model")["status"] == "cooldown"
    assert health.snapshot("model")["retry_in_seconds"] == 120


@pytest.mark.asyncio
async def test_shutdown_cancels_and_awaits_background_probe_without_marking_failure(
    runtime,
):
    health = runtime.health
    with pytest.raises(ServiceError):
        await health.call(
            AsyncMock(side_effect=ServiceError(429)), chat_provider_id="model"
        )
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def generate(**kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    runtime.context.llm_generate = AsyncMock(side_effect=generate)
    runtime.ticks[0] += 60
    health.start(runtime.recorder)
    await asyncio.wait_for(entered.wait(), 1)
    await health.close()
    await health.close()
    assert cancelled.is_set()
    assert health._monitor_task is None
    assert health.snapshot("model")["status"] == "probe_due"
    assert health.snapshot("model")["failures"] == 1
    runtime.recorder.assert_not_awaited()


@pytest.mark.asyncio
async def test_token_log_errors_do_not_override_successful_probe(runtime):
    runtime.context.llm_generate = AsyncMock(
        side_effect=probe_reply
    )
    result = await runtime.health.probe(
        "model", log_token_usage=AsyncMock(side_effect=RuntimeError("DB unavailable"))
    )
    assert result.completion_text.startswith("GIFTIA_PROBE_")
    assert runtime.health.snapshot("model")["status"] == "healthy"


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [None, 42, [], {}, "", " \n ", "OK", "ＯＫ", "\x00�", "<html>OK</html>"])
async def test_probe_rejects_non_llm_and_malformed_output_and_records_usage(runtime, output):
    runtime.context.llm_generate = AsyncMock(
        return_value=SimpleNamespace(
            completion_text=output, usage={"input": 10, "output": 2, "total": 12}
        )
    )
    with pytest.raises(InvalidProbeResponseError):
        await runtime.health.probe("model", log_token_usage=runtime.recorder, manual=True)
    snapshot = runtime.health.snapshot("model")
    assert snapshot["status"] == "cooldown"
    assert snapshot["reason"] == "invalid_probe_response"
    assert snapshot["last_success"] is None
    usage = runtime.recorder.await_args.kwargs
    assert usage["total_tokens"] == 12
    assert usage["extra_info"] == {"status": "api_failed", "manual_test": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix,suffix", [("", ""), (" \r\n", "\t\n"), ("```\n", "\n```"), ("", "\u200b"), ("", "\x00"), ("", "\u00a0"), ('"', '"'), ("Here is the response: ", "")])
async def test_probe_requires_exact_challenge_except_ascii_boundary_whitespace(runtime, prefix, suffix):
    runtime.context.llm_generate = AsyncMock(
        side_effect=lambda **kwargs: SimpleNamespace(
            completion_text=prefix + kwargs["prompt"].rsplit("\n", 1)[-1] + suffix
        )
    )
    if not prefix.strip(" \t\r\n") and not suffix.strip(" \t\r\n"):
        await runtime.health.probe("model", log_token_usage=runtime.recorder)
        assert runtime.health.snapshot("model")["status"] == "healthy"
    else:
        with pytest.raises(InvalidProbeResponseError):
            await runtime.health.probe("model", log_token_usage=runtime.recorder)
        assert runtime.health.snapshot("model")["status"] == "cooldown"


@pytest.mark.asyncio
async def test_probe_cannot_replay_a_previous_successful_challenge(runtime):
    runtime.context.llm_generate = AsyncMock(side_effect=probe_reply)
    previous = await runtime.health.probe("model", log_token_usage=runtime.recorder)
    runtime.context.llm_generate.side_effect = None
    runtime.context.llm_generate.return_value = previous
    with pytest.raises(InvalidProbeResponseError):
        await runtime.health.probe("model", log_token_usage=runtime.recorder)
    prompts = [call.kwargs["prompt"] for call in runtime.context.llm_generate.await_args_list]
    assert prompts[0] != prompts[1]
    assert runtime.health.snapshot("model")["status"] == "cooldown"


@pytest.mark.asyncio
async def test_global_control_bypasses_cooldown_but_keeps_manual_disables(runtime):
    health = runtime.health
    database = SimpleNamespace(upsert_kv_data=AsyncMock())
    with pytest.raises(ServiceError):
        await health.call(AsyncMock(side_effect=ServiceError(429)), chat_provider_id="model")
    await health.update_settings(database, enabled=False, provider_id="disabled")
    await health.update_settings(database, enabled=False)
    for _ in range(3):
        with pytest.raises(ServiceError):
            await health.call(AsyncMock(side_effect=ServiceError()), chat_provider_id="model")
    assert health.snapshot("model")["status"] == "unmanaged"
    assert health.snapshot("model")["failures"] == 0
    assert health.snapshot("disabled")["status"] == "disabled"
    blocked = AsyncMock()
    with pytest.raises(ProviderUnavailableError):
        await health.call(blocked, chat_provider_id="disabled")
    runtime.context.llm_generate = blocked
    with pytest.raises(ProviderUnavailableError):
        await health.probe("model", log_token_usage=runtime.recorder, manual=True)
    health.start(runtime.recorder)
    runtime.ticks[0] += 1000
    await asyncio.sleep(health.CHECK_INTERVAL * 3)
    blocked.assert_not_awaited()
    runtime.recorder.assert_not_awaited()
    await health.update_settings(database, enabled=True)
    assert health.snapshot("model")["status"] == "unknown"
    assert health.snapshot("disabled")["status"] == "disabled"


@pytest.mark.asyncio
async def test_disabled_provider_survives_configuration_change_and_is_never_probed(runtime):
    health = runtime.health
    database = SimpleNamespace(upsert_kv_data=AsyncMock())
    with pytest.raises(ServiceError):
        await health.call(AsyncMock(side_effect=ServiceError(429)), chat_provider_id="model")
    await health.update_settings(database, enabled=False, provider_id="model")
    runtime.context.provider_manager = SimpleNamespace(
        inst_map={"model": SimpleNamespace(provider_config={"key": "changed"})}
    )
    blocked = AsyncMock()
    runtime.context.llm_generate = blocked
    for force_probe in (False, True):
        with pytest.raises(ProviderUnavailableError):
            await health.call(blocked, chat_provider_id="model", force_probe=force_probe)
    health.start(runtime.recorder)
    runtime.ticks[0] += 1000
    await asyncio.sleep(health.CHECK_INTERVAL * 3)
    blocked.assert_not_awaited()
    assert health.snapshot("model")["status"] == "disabled"
    await health.update_settings(database, enabled=True, provider_id="model")
    assert health.snapshot("model")["status"] == "unknown"
    await health.call(AsyncMock(return_value="reply"), chat_provider_id="model")


@pytest.mark.asyncio
@pytest.mark.parametrize("global_control", [False, True])
async def test_disabling_cancels_inflight_probe_and_ignores_its_late_success(runtime, global_control):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def generate(**kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            return probe_reply(**kwargs)

    runtime.context.llm_generate = AsyncMock(side_effect=generate)
    task = asyncio.create_task(runtime.health.probe("model", log_token_usage=runtime.recorder, manual=True))
    await entered.wait()
    await runtime.health.update_settings(
        SimpleNamespace(upsert_kv_data=AsyncMock()),
        enabled=False,
        provider_id=None if global_control else "model",
    )
    with pytest.raises(ProviderUnavailableError):
        await task
    assert cancelled.is_set()
    snapshot = runtime.health.snapshot("model")
    assert snapshot["status"] == ("unmanaged" if global_control else "disabled")
    assert snapshot["last_success"] is None


@pytest.mark.asyncio
async def test_late_business_result_cannot_reset_manual_disable(runtime):
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(**kwargs):
        entered.set()
        await release.wait()
        return "reply"

    task = asyncio.create_task(runtime.health.call(generate, chat_provider_id="model"))
    await entered.wait()
    await runtime.health.update_settings(SimpleNamespace(upsert_kv_data=AsyncMock()), enabled=False, provider_id="model")
    release.set()
    assert await task == "reply"
    assert runtime.health.snapshot("model")["status"] == "disabled"
    assert runtime.health.snapshot("model")["last_success"] is None


@pytest.mark.asyncio
async def test_shutdown_also_cancels_manual_probes_without_a_monitor(runtime):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def generate(**kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    runtime.context.llm_generate = AsyncMock(side_effect=generate)
    task = asyncio.create_task(runtime.health.probe("model", log_token_usage=runtime.recorder, manual=True))
    await entered.wait()
    await runtime.health.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    runtime.recorder.assert_not_awaited()
