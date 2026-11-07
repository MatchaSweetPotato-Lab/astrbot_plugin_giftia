"""Verify invalid reply recovery without replaying native tool effects."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from core.bot.bot_config_manager import DEFAULT_BOT_CONFIG, BotConfigManager
from core.llm.call_llm import CallLLM
from core.llm.xml_parse import XmlParse
from core.web import bot_api as bot_api_module
from core.web.bot_api import BotApi

from astrbot.core.agent.tool import ToolSet
from astrbot.core.exceptions import EmptyModelOutputError


@pytest.fixture
def runtime():
    context = SimpleNamespace(
        tool_loop_agent=AsyncMock(),
        get_llm_tool_manager=lambda: SimpleNamespace(
            get_full_tool_set=lambda: ToolSet(), is_builtin_tool=lambda _: False
        ),
    )
    conf = {"invalid_reply_fallback_provider_ids": ["backup-a", "backup-b"]}
    plugin = SimpleNamespace(
        bot_map={"bot": {"llm_reply_conf": conf}},
        adapter_id_map={"adapter": "bot"},
        tools_config={},
    )
    event = SimpleNamespace(
        platform_meta=SimpleNamespace(id="adapter"),
        get_platform_name=lambda: "test",
    )
    caller = CallLLM(
        context, XmlParse(None, None), {"reply_retry_times": 2}, {}, plugin=plugin
    )
    caller._log_token_usage_safely = AsyncMock()
    return SimpleNamespace(context=context, caller=caller, event=event, conf=conf)


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize(
    "completion,reasoning,empty_error",
    [
        ("", "", False),
        ("   ", "", False),
        ("", "unfinished reasoning", False),
        ("The response was blocked.", "", False),
        ("<unknown>blocked</unknown>", "", False),
        ("<think><message>draft</message></think>", "", False),
        ("<root/>", "", False),
        ("", "", True),
    ],
)
async def test_invalid_output_immediately_uses_separate_fallback(
    runtime, native, completion, reasoning, empty_error
):
    rt = runtime
    rt.context.tool_loop_agent.side_effect = [
        EmptyModelOutputError()
        if empty_error
        else SimpleNamespace(
            completion_text=completion, reasoning_content=reasoning, tools_call_name=[]
        ),
        SimpleNamespace(
            completion_text="<message>recovered</message>",
            reasoning_content="",
            tools_call_name=[],
        ),
    ]
    providers = ["primary", "regular-secondary"]
    result = await rt.caller.call_llm_reply(
        rt.event,
        "group",
        providers,
        "persona",
        "original input",
        use_source_tools=native,
    )
    assert result.msg_texts == ["recovered"]
    calls = rt.context.tool_loop_agent.await_args_list
    assert [c.kwargs["chat_provider_id"] for c in calls] == ["primary", "backup-a"]
    assert calls[0].kwargs["system_prompt"] == calls[1].kwargs["system_prompt"]
    assert [c.kwargs["prompt"] for c in calls] == ["original input"] * 2
    assert providers == ["primary", "regular-secondary"]
    usage = rt.caller._log_token_usage_safely.await_args_list
    assert [c.kwargs["extra_info"]["status"] for c in usage] == [
        "parse_failed",
        "success",
    ]
    assert [c.kwargs["extra_info"]["output_fallback"] for c in usage] == [False, True]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "completion",
    [
        "<status>心情: 安静</status>",
        "<status/>",
        "<message>Cannot help with this request.</message>",
        "<image url='https://example.com/picture.png'/>",
        "<tts>hello</tts>",
        '<poke user_id="alice"/>',
        '<tool_call name="lookup">{}</tool_call>',
    ],
)
async def test_valid_silence_media_and_actions_do_not_trigger_fallback(
    runtime, completion
):
    rt = runtime
    rt.context.tool_loop_agent.return_value = SimpleNamespace(
        completion_text=completion, reasoning_content="", tools_call_name=[]
    )
    result = await rt.caller.call_llm_reply(
        rt.event, "group", ["primary"], "persona", "input"
    )
    assert result is not None
    rt.context.tool_loop_agent.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_fallback_stops_and_skips_duplicate_providers(runtime):
    rt = runtime
    rt.conf["invalid_reply_fallback_provider_ids"] = [
        "primary",
        "backup-a",
        "backup-a",
        "backup-b",
    ]
    rt.context.tool_loop_agent.return_value = SimpleNamespace(
        completion_text="blocked", reasoning_content="", tools_call_name=[]
    )
    result = await rt.caller.call_llm_reply(
        rt.event, "group", ["primary", "regular-secondary"], "persona", "input"
    )
    assert result is None
    assert [
        c.kwargs["chat_provider_id"] for c in rt.context.tool_loop_agent.await_args_list
    ] == ["primary", "backup-a"]


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("configured_empty", [False, True])
@pytest.mark.parametrize(
    "completion",
    ["", "   ", "blocked", "<unknown/>", "<root/>", "<think>draft</think>"],
)
async def test_unconfigured_fallback_preserves_legacy_silence_without_extra_requests(
    runtime, native, configured_empty, completion
):
    rt = runtime
    rt.conf.clear()
    if configured_empty:
        rt.conf["invalid_reply_fallback_provider_ids"] = []
    rt.context.tool_loop_agent.return_value = SimpleNamespace(
        completion_text=completion, reasoning_content="", tools_call_name=[]
    )
    result = await rt.caller.call_llm_reply(
        rt.event,
        "group",
        ["primary", "regular-secondary"],
        "persona",
        "input",
        use_source_tools=native,
    )
    assert result is not None
    assert not result.msg_chains and not result.output_order
    rt.context.tool_loop_agent.assert_awaited_once()
    usage = rt.caller._log_token_usage_safely.await_args.kwargs
    assert usage["extra_info"]["status"] == "success"
    assert usage["extra_info"]["output_fallback"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_unconfigured_empty_output_error_preserves_terminal_legacy_handling(
    runtime, native
):
    rt = runtime
    rt.conf.clear()
    rt.context.tool_loop_agent.side_effect = EmptyModelOutputError()
    result = await rt.caller.call_llm_reply(
        rt.event,
        "group",
        ["primary", "regular-secondary"],
        "persona",
        "input",
        use_source_tools=native,
    )
    assert (result is None) is native
    rt.context.tool_loop_agent.assert_awaited_once()
    usage = rt.caller._log_token_usage_safely.await_args.kwargs
    assert usage["extra_info"]["status"] == "api_failed"
    assert usage["extra_info"]["error"] == "EmptyModelOutputError"


@pytest.mark.asyncio
@pytest.mark.parametrize("native,expected_calls", [(False, 4), (True, 1)])
async def test_unconfigured_reasoning_only_output_preserves_existing_retry_rules(
    runtime, native, expected_calls
):
    rt = runtime
    rt.conf.clear()
    rt.context.tool_loop_agent.return_value = SimpleNamespace(
        completion_text="", reasoning_content="unfinished reasoning", tools_call_name=[]
    )
    result = await rt.caller.call_llm_reply(
        rt.event,
        "group",
        ["primary", "regular-secondary"],
        "persona",
        "input",
        use_source_tools=native,
    )
    assert result is None
    assert rt.context.tool_loop_agent.await_count == expected_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize(
    "output", ["", "   ", "blocked", "<unknown/>", "reasoning", "exception"]
)
async def test_invalid_fallback_output_never_retries_or_reaches_next_provider(
    runtime, native, output
):
    rt = runtime
    rt.context.tool_loop_agent.side_effect = [
        SimpleNamespace(
            completion_text="blocked", reasoning_content="", tools_call_name=[]
        ),
        EmptyModelOutputError()
        if output == "exception"
        else SimpleNamespace(
            completion_text="" if output == "reasoning" else output,
            reasoning_content="unfinished reasoning" if output == "reasoning" else "",
            tools_call_name=[],
        ),
        SimpleNamespace(
            completion_text="<message>must not run</message>",
            reasoning_content="",
            tools_call_name=[],
        ),
    ]
    result = await rt.caller.call_llm_reply(
        rt.event, "group", ["primary"], "persona", "input", use_source_tools=native
    )
    assert result is None
    assert [
        c.kwargs["chat_provider_id"] for c in rt.context.tool_loop_agent.await_args_list
    ] == ["primary", "backup-a"]
    usage = rt.caller._log_token_usage_safely.await_args_list
    assert [c.kwargs["extra_info"]["status"] for c in usage] == [
        "parse_failed",
        "parse_failed",
    ]
    assert [c.kwargs["extra_info"]["output_fallback"] for c in usage] == [False, True]


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_fallback_api_failures_still_allow_retry_and_next_provider(
    runtime, native
):
    rt = runtime
    rt.context.tool_loop_agent.side_effect = [
        SimpleNamespace(
            completion_text="blocked", reasoning_content="", tools_call_name=[]
        ),
        RuntimeError("network failure"),
        RuntimeError("network failure"),
        SimpleNamespace(
            completion_text="<message>recovered</message>",
            reasoning_content="",
            tools_call_name=[],
        ),
    ]
    result = await rt.caller.call_llm_reply(
        rt.event, "group", ["primary"], "persona", "input", use_source_tools=native
    )
    assert result.msg_texts == ["recovered"]
    assert [
        c.kwargs["chat_provider_id"] for c in rt.context.tool_loop_agent.await_args_list
    ] == ["primary", "backup-a", "backup-a", "backup-b"]


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_api_failures_stay_on_regular_provider_chain(runtime, native):
    rt = runtime
    rt.context.tool_loop_agent.side_effect = RuntimeError("network failure")
    result = await rt.caller.call_llm_reply(
        rt.event,
        "group",
        ["primary", "regular-secondary"],
        "persona",
        "input",
        use_source_tools=native,
    )
    assert result is None
    assert [
        c.kwargs["chat_provider_id"] for c in rt.context.tool_loop_agent.await_args_list
    ] == ["primary", "primary", "regular-secondary", "regular-secondary"]


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize(
    "responses,expected_providers,recovered",
    [
        (["api", "invalid", "valid"], ["primary", "primary", "backup-a"], True),
        (
            ["api", "api", "invalid", "valid"],
            ["primary", "primary", "regular-secondary", "backup-a"],
            True,
        ),
        (
            ["invalid", "api", "invalid", "valid"],
            ["primary", "backup-a", "backup-a"],
            False,
        ),
        (
            ["invalid", "api", "api", "invalid", "valid"],
            ["primary", "backup-a", "backup-a", "backup-b"],
            False,
        ),
        (
            ["invalid", "api", "api", "api", "api", "api", "api"],
            [
                "primary",
                "backup-a",
                "backup-a",
                "backup-b",
                "backup-b",
                "backup-c",
                "backup-c",
            ],
            False,
        ),
    ],
)
async def test_mixed_failures_preserve_provider_list_boundaries(
    runtime, native, responses, expected_providers, recovered
):
    rt = runtime
    rt.conf["invalid_reply_fallback_provider_ids"] = [
        "backup-a",
        "backup-b",
        "backup-c",
    ]
    rt.context.tool_loop_agent.side_effect = [
        RuntimeError("network failure")
        if response == "api"
        else SimpleNamespace(
            completion_text="<message>recovered</message>"
            if response == "valid"
            else "blocked",
            reasoning_content="",
            tools_call_name=[],
        )
        for response in responses
    ]
    result = await rt.caller.call_llm_reply(
        rt.event,
        "group",
        ["primary", "regular-secondary"],
        "persona",
        "input",
        use_source_tools=native,
    )
    assert [
        call.kwargs["chat_provider_id"]
        for call in rt.context.tool_loop_agent.await_args_list
    ] == expected_providers
    if recovered:
        assert result.msg_texts == ["recovered"]
    else:
        assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback_attempt", [False, True])
@pytest.mark.parametrize(
    "output", ["invalid", "empty", "exception", "api_error", "valid"]
)
async def test_native_tools_from_earlier_steps_are_never_replayed(
    runtime, output, fallback_attempt
):
    rt = runtime
    effects = []

    async def run(**kwargs):
        if fallback_attempt and kwargs["chat_provider_id"] == "primary":
            return SimpleNamespace(
                completion_text="blocked", reasoning_content="", tools_call_name=[]
            )
        await kwargs["agent_hooks"].on_tool_start(
            None, SimpleNamespace(name="external_action"), {}
        )
        effects.append("executed")
        if output == "exception":
            raise EmptyModelOutputError()
        if output == "api_error":
            raise RuntimeError("connection lost after executing the tool")
        return SimpleNamespace(
            completion_text={"invalid": "blocked", "empty": "", "valid": "<status/>"}[
                output
            ],
            reasoning_content="",
            # Final responses do not necessarily include tools from previous steps.
            tools_call_name=[],
        )

    rt.context.tool_loop_agent.side_effect = run
    result = await rt.caller.call_llm_reply(
        rt.event, "group", ["primary"], "persona", "input", use_source_tools=True
    )
    assert effects == ["executed"]
    assert rt.context.tool_loop_agent.await_count == (2 if fallback_attempt else 1)
    if output in ("valid", "empty"):
        assert result.native_tools_called == ["external_action"]
    else:
        assert result is None


def test_fallback_configuration_round_trip(tmp_path):
    manager = BotConfigManager.__new__(BotConfigManager)
    manager.config_file = tmp_path / "bots_config.json"
    assert (
        DEFAULT_BOT_CONFIG["llm_reply_conf"]["invalid_reply_fallback_provider_ids"]
        == []
    )
    assert manager.save_bots(
        [
            {
                "name": "bot",
                "llm_reply_conf": {
                    "invalid_reply_fallback_provider_ids": [
                        " backup-b ",
                        "",
                        None,
                        "backup-a",
                        "backup-b",
                        " ",
                    ]
                },
            }
        ]
    )
    assert manager.load_bots()[0]["llm_reply_conf"][
        "invalid_reply_fallback_provider_ids"
    ] == ["backup-b", "backup-a"]


@pytest.mark.asyncio
async def test_old_dashboard_save_preserves_fallback_but_explicit_empty_clears_it(
    tmp_path, monkeypatch
):
    manager = BotConfigManager.__new__(BotConfigManager)
    manager.config_file = tmp_path / "bots_config.json"
    manager.save_bots([{"name": "bot"}])
    api = BotApi(SimpleNamespace(bot_config_manager=manager, sync_bot_maps=Mock()))
    for reply_conf, expected in (
        (
            {"invalid_reply_fallback_provider_ids": ["backup-b", "backup-a"]},
            ["backup-b", "backup-a"],
        ),
        ({"provider_ids": ["primary"]}, ["backup-b", "backup-a"]),
        ({"invalid_reply_fallback_provider_ids": []}, []),
    ):
        monkeypatch.setattr(
            bot_api_module,
            "request",
            SimpleNamespace(
                json=AsyncMock(
                    return_value={"name": "bot", "llm_reply_conf": reply_conf}
                )
            ),
        )
        assert (await api.save_bot()).status_code == 200
        saved = json.loads(manager.config_file.read_text())[0]
        assert (
            saved["llm_reply_conf"]["invalid_reply_fallback_provider_ids"] == expected
        )
