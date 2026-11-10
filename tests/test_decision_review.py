"""Exercise review and recovery through both real decision adapters."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from core.llm.call_llm import CallLLM
from core.llm.provider_health import InvalidProbeResponseError
from core.web.bot_api import BotApi

from astrbot.api.provider import ProviderType
from astrbot.core.provider.decision import DecisionService
from astrbot.core.provider.sources.openai_decision_source import OpenAIDecisionProvider
from astrbot.core.provider.sources.system_one_decision_source import (
    SystemOneDecisionProvider,
)


@pytest_asyncio.fixture(params=["system_one_decision", "openai_decision"])
async def decision_runtime(request):
    """Bind the real service and adapter to an in-memory HTTP endpoint.

    Args:
        request: Pytest request selecting the adapter.

    Yields:
        Caller, endpoint controls, captured requests and usage records.
    """
    runtime = SimpleNamespace(
        requests=[], probability=0.8, status_code=200, wrong_choice=False
    )
    is_openai = request.param == "openai_decision"

    def respond(http_request):
        body = json.loads(http_request.content)
        runtime.requests.append(body)
        if runtime.status_code != 200:
            return httpx.Response(runtime.status_code, json={"error": "unavailable"})
        answers = {} if not is_openai else []
        questions = (
            [(question["name"], question) for question in body["questions"]]
            if is_openai
            else body["questions"].items()
        )
        for head, question in questions:
            if question["type"] == "choice":
                state = body["input"] if is_openai else body["state"]
                options = (
                    [option["value"] for option in question["choices"]]
                    if is_openai
                    else list(question["criteria"])
                )
                choice = (
                    next(option for option in options if option != state)
                    if runtime.wrong_choice
                    else state
                )
                probabilities = {option: float(option == choice) for option in options}
                answer = {
                    "type": "choice",
                    "choice": choice,
                    "confidence": 1.0,
                    "probabilities": [
                        {"value": option, "probability": probability}
                        for option, probability in probabilities.items()
                    ]
                    if is_openai
                    else probabilities,
                }
            else:
                answer = (
                    {"type": "predicate", "probability": runtime.probability}
                    if is_openai
                    else {"type": "noul", "noul": runtime.probability}
                )
            if is_openai:
                answers.append({"name": head, **answer})
            else:
                answers[head] = answer
        return httpx.Response(200, json={"answers": answers})

    provider_class = OpenAIDecisionProvider if is_openai else SystemOneDecisionProvider
    provider = provider_class(
        {
            "id": "review",
            "type": request.param,
            "model": "test-model",
            "key": "sk-test",
            "api_base": "https://decision.test/v1",
        },
        {},
        transport=httpx.MockTransport(respond),
    )
    manager = SimpleNamespace(
        inst_map={"review": provider},
        decision_provider_insts=[provider],
        get_provider_by_id=AsyncMock(return_value=provider),
    )
    context = SimpleNamespace(
        get_provider_by_id=manager.inst_map.get,
        provider_manager=manager,
        decision=DecisionService(manager),
        llm_generate=AsyncMock(),
    )
    runtime.provider = provider
    runtime.context = context
    runtime.caller = CallLLM(context, None, {}, {})
    runtime.caller._log_token_usage_safely = AsyncMock()
    runtime.health = runtime.caller.provider_health
    runtime.recorder = AsyncMock()
    try:
        yield runtime
    finally:
        await runtime.health.close()
        await provider.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "probability,expected", [(0, False), (0.49, False), (0.5, True), (1, True)]
)
async def test_decision_review_uses_shared_rules_and_boolean_threshold(
    decision_runtime, probability, expected
):
    runtime = decision_runtime
    runtime.probability = probability
    assert (
        await runtime.caller.call_llm_reply_review(
            "review", "draft and arrivals", "bot", "group"
        )
        is expected
    )
    assert len(runtime.requests) == 1
    body = runtime.requests[0]
    assert body.get("state", body.get("input")) == "draft and arrivals"
    questions = body["questions"]
    question = questions[0] if isinstance(questions, list) else questions["answer"]
    assert "没有明确冲突时通过" in question["instructions"]
    assert "原样发送" in question["instructions"]
    assert "<review" not in question["instructions"]
    runtime.context.llm_generate.assert_not_awaited()
    usage = runtime.caller._log_token_usage_safely.await_args.kwargs
    assert usage["type_name"] == "reply_review"
    assert usage["extra_info"]["passed"] is expected
    assert runtime.health.snapshot("review")["status"] == "healthy"


@pytest.mark.asyncio
@pytest.mark.parametrize("probability", [-0.1, 1.1, None, "true"])
async def test_invalid_decision_review_allows_draft_without_retry(
    decision_runtime, probability
):
    decision_runtime.probability = probability
    assert (
        await decision_runtime.caller.call_llm_reply_review(
            "review", "input", "bot", "group"
        )
        is None
    )
    assert len(decision_runtime.requests) == 1
    assert (
        decision_runtime.caller._log_token_usage_safely.await_args.kwargs["extra_info"][
            "status"
        ]
        == "api_failed"
    )


@pytest.mark.asyncio
async def test_decision_review_failure_cooldown_and_structured_recovery(
    decision_runtime,
):
    runtime = decision_runtime
    runtime.status_code = 503
    for _ in range(2):
        assert (
            await runtime.caller.call_llm_reply_review(
                "review", "input", "bot", "group"
            )
            is None
        )
    assert len(runtime.requests) == 2
    assert runtime.health.snapshot("review")["status"] == "cooldown"
    assert (
        await runtime.caller.call_llm_reply_review("review", "input", "bot", "group")
        is None
    )
    assert len(runtime.requests) == 2
    assert runtime.caller._log_token_usage_safely.await_count == 2

    runtime.status_code = 200
    first = await runtime.health.probe(
        "review", log_token_usage=runtime.recorder, manual=True
    )
    second = await runtime.health.probe("review", log_token_usage=runtime.recorder)
    assert first["probe"].choice != second["probe"].choice
    assert runtime.health.snapshot("review")["status"] == "healthy"
    assert runtime.recorder.await_count == 2
    assert runtime.recorder.await_args.kwargs["type"] == "provider_test"
    runtime.context.llm_generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrong_decision_probe_choice_does_not_restore_health(decision_runtime):
    runtime = decision_runtime
    runtime.wrong_choice = True
    with pytest.raises(InvalidProbeResponseError):
        await runtime.health.probe("review", log_token_usage=runtime.recorder)
    assert len(runtime.requests) == 1
    assert runtime.health.snapshot("review")["status"] == "cooldown"
    assert runtime.health.snapshot("review")["reason"] == "invalid_probe_response"


@pytest.mark.asyncio
@pytest.mark.parametrize("management_enabled", [True, False])
async def test_decision_review_respects_manual_disable(
    decision_runtime, management_enabled
):
    runtime = decision_runtime
    runtime.health.enabled = management_enabled
    runtime.health.disabled_provider_ids.add("review")
    assert (
        await runtime.caller.call_llm_reply_review("review", "input", "bot", "group")
        is None
    )
    assert runtime.requests == []
    runtime.caller._log_token_usage_safely.assert_not_awaited()


@pytest.mark.asyncio
async def test_decision_review_works_with_health_management_off(decision_runtime):
    runtime = decision_runtime
    runtime.health.enabled = False
    assert (
        await runtime.caller.call_llm_reply_review("review", "input", "bot", "group")
        is True
    )
    assert len(runtime.requests) == 1


@pytest.mark.asyncio
async def test_decision_review_timeout_is_not_a_rejection(decision_runtime):
    runtime = decision_runtime
    runtime.context.decision.yes_no = AsyncMock(side_effect=TimeoutError())
    assert (
        await runtime.caller.call_llm_reply_review("review", "input", "bot", "group")
        is None
    )
    runtime.context.decision.yes_no.assert_awaited_once()
    assert set(runtime.context.decision.yes_no.await_args.kwargs) == {
        "provider_id",
        "question",
        "state",
    }


@pytest.mark.parametrize("runtime_only", [True, False])
def test_metadata_keeps_decision_providers_out_of_chat_inventory(runtime_only):
    configured = [
        {"id": "chat", "type": "openai", "provider_type": "chat_completion"},
        {"id": "system", "type": "system_one_decision"},
        {"id": "future", "type": "future_adapter", "provider_type": "decision"},
        {"id": "tts", "type": "openai_tts"},
    ]
    decisions = [
        SimpleNamespace(
            meta=lambda item=item: SimpleNamespace(
                id=item["id"], type=item["type"], provider_type=ProviderType.DECISION
            )
        )
        for item in configured[1:3]
    ]
    context = SimpleNamespace(
        config={"provider": configured if not runtime_only else configured[:1]},
        provider_manager=SimpleNamespace(decision_provider_insts=decisions),
    )
    metadata = BotApi(SimpleNamespace(context=context))._get_available_metadata()
    assert [provider["id"] for provider in metadata["llm_providers"]] == ["chat"]
    assert {provider["id"] for provider in metadata["decision_providers"]} == {
        "system",
        "future",
    }
    assert len(metadata["decision_providers"]) == 2
