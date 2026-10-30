"""Verify native tool loops, scoped reminders and result-driven final replies."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from xml.etree.ElementTree import fromstring

import pytest
import pytest_asyncio
from astrbot.api.star import Context
from astrbot.core.agent.tool import ToolSet
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from core.conversation.action_dispatcher import ActionDispatcher
from core.handlers.commands import CommandHandler
from core.llm.call_llm import CallLLM
from core.llm.native_tools import (
    NATIVE_CONTEXT_KEY,
    NATIVE_TOOL_CONFIG_KEYS,
    CreateScheduledTaskTool,
    DeleteScheduledTaskTool,
    ListScheduledTasksTool,
    SearchMemoryTool,
)
from core.llm.prompt import build_reply_prompt
from core.llm.xml_parse import XmlParse
from core.utils.scheduler import Scheduler
from core.utils.schemas import MessageData


@pytest_asyncio.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "astrbot.api.star.StarTools.get_data_dir", lambda *args: tmp_path
    )
    scheduler = Scheduler()
    scheduler.register_func("remind", AsyncMock())
    scheduler.scheduler.start(paused=True)
    event = MagicMock(spec=AiocqhttpMessageEvent)
    event.platform_meta = SimpleNamespace(id="adapter")
    event.unified_msg_origin = "adapter:GroupMessage:group"
    event.get_group_id.return_value = "group"
    event.get_sender_id.return_value = "bob"
    event.get_sender_name.return_value = "Bob"
    event.get_self_id.return_value = "bot-id"
    event.get_platform_name.return_value = "aiocqhttp"
    context = Context.__new__(Context)
    plugin = SimpleNamespace(
        adapter_id_map={"adapter": "bot"},
        bot_map={"bot": {"nickname": "Giftia", "enabled_interactive_features": []}},
        task_manager=scheduler,
        context=context,
        tools_config={"max_loop": 7},
        embedding_conf={"enabled": True, "inject_limit": 0},
        rerank_conf={"enabled": False},
        passive_memory_manager=SimpleNamespace(
            search_and_filter_memories=AsyncMock(return_value=[])
        ),
        data_cache=SimpleNamespace(
            record_memory_hits=AsyncMock(), add_message=AsyncMock()
        ),
    )
    state = {
        "bot_name": "bot",
        "session_id": "group",
        "request_id": "request-1",
        "creator_names": {"alice": "Alice", "bob": "Bob"},
        "user_ids": ["alice", "bob"],
    }
    run = SimpleNamespace(
        context=SimpleNamespace(
            event=event, extra={NATIVE_CONTEXT_KEY: json.dumps(state)}
        )
    )
    tools = {
        tool.name: tool
        for tool in [
            SearchMemoryTool(plugin=plugin),
            CreateScheduledTaskTool(plugin=plugin),
            ListScheduledTasksTool(plugin=plugin),
            DeleteScheduledTaskTool(plugin=plugin),
        ]
    }
    context.get_llm_tool_manager = lambda: SimpleNamespace(
        get_full_tool_set=lambda: ToolSet(tools=list(tools.values())),
        get_func=tools.get,
        is_builtin_tool=lambda _: False,
    )
    context.tool_loop_agent = AsyncMock(
        return_value=SimpleNamespace(
            completion_text="<message>finished</message>",
            reasoning_content="",
            tools_call_name=[],
        )
    )
    caller = CallLLM(
        context, XmlParse(None, None), {"reply_retry_times": 2}, {}, plugin=plugin
    )
    caller._log_token_usage_safely = AsyncMock()
    try:
        yield SimpleNamespace(
            plugin=plugin,
            event=event,
            scheduler=scheduler,
            run=run,
            state=state,
            tools=tools,
            caller=caller,
            context=context,
        )
    finally:
        scheduler.scheduler.shutdown(wait=False)
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("platform", ["aiocqhttp", "qq_official_webhook", "generic"])
async def test_create_preserves_explicit_creator_and_current_routing(
    runtime, private, platform
):
    rt = runtime
    rt.event.get_platform_name.return_value = platform
    if private:
        rt.event.get_group_id.return_value = ""
        rt.event.get_sender_id.return_value = "group"
    result = json.loads(
        await rt.tools["create_scheduled_task"].call(
            rt.run, "alice", "2099-01-01T09:00:00+08:00", "Remind Bob"
        )
    )
    assert result["status"] == "created" and result["next_run_time"]
    job = rt.scheduler.scheduler.get_job(result["task_id"])
    assert job.kwargs["user_id"] == "alice" and job.kwargs["user_name"] == "Alice"
    assert job.kwargs["bot_name"] == "bot" and job.kwargs["group_or_user_id"] == "group"
    assert job.kwargs["group_id"] == ("" if private else "group")
    assert job.kwargs["unified_msg_origin"] == rt.event.unified_msg_origin
    assert (
        job.kwargs["adapter_id"] == "adapter"
        and job.kwargs["platform_name"] == platform
    )


@pytest.mark.asyncio
async def test_same_run_creation_is_idempotent_but_new_requests_are_distinct(runtime):
    rt = runtime
    results = await asyncio.gather(
        *[
            rt.tools["create_scheduled_task"].call(
                rt.run, "alice", "0 9 * * *", "same reminder"
            )
            for _ in range(2)
        ]
    )
    values = [json.loads(result) for result in results]
    assert {result["status"] for result in values} == {"created", "exists"}
    assert len(rt.scheduler.scheduler.get_jobs()) == 1
    state = {**rt.state, "request_id": "request-2"}
    rt.run.context.extra[NATIVE_CONTEXT_KEY] = json.dumps(state)
    result = json.loads(
        await rt.tools["create_scheduled_task"].call(
            rt.run, "alice", "0 9 * * *", "same reminder"
        )
    )
    assert result["status"] == "created"
    assert len(rt.scheduler.scheduler.get_jobs()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("creator", ["", "unknown", "recalled", "operation", "late"])
async def test_unknown_creator_fails_before_writing_and_can_be_corrected(
    runtime, creator
):
    rt = runtime
    bad = json.loads(
        await rt.tools["create_scheduled_task"].call(
            rt.run, creator, "0 9 * * *", "reminder"
        )
    )
    assert bad["status"] == "error" and "user_id" in bad["message"]
    assert rt.scheduler.scheduler.get_jobs() == []
    corrected = json.loads(
        await rt.tools["create_scheduled_task"].call(
            rt.run, "alice", "0 9 * * *", "reminder"
        )
    )
    assert corrected["status"] == "created"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expression,content",
    [
        ("invalid", "reminder"),
        ("2000-01-01T00:00:00+08:00", "reminder"),
        ("", "reminder"),
        ("0 9 * * *", ""),
    ],
)
async def test_invalid_reminder_returns_an_error_without_claiming_success(
    runtime, expression, content
):
    result = json.loads(
        await runtime.tools["create_scheduled_task"].call(
            runtime.run, "alice", expression, content
        )
    )
    assert result["status"] == "error"
    assert runtime.scheduler.scheduler.get_jobs() == []


@pytest.mark.asyncio
async def test_listing_then_deleting_uses_verified_scope_and_preserves_queried_creator(
    runtime,
):
    rt = runtime
    for task_id, bot, session, creator, system in [
        ("old-carol", "bot", "group", "carol", False),
        ("bot_group_foreign", "bot", "elsewhere", "alice", False),
        ("wrong-bot", "other", "group", "alice", False),
        ("system_hidden", "bot", "group", "alice", True),
        ("misleading-system", "bot", "group", "alice", True),
    ]:
        rt.scheduler.add_job(
            task_id,
            "remind",
            "0 9 * * *",
            kwargs={
                "bot_name": bot,
                "group_or_user_id": session,
                "user_id": creator,
                "user_name": creator.title(),
                "remind_message": "reminder",
                "system_job": system,
            },
        )
    before = json.loads(
        await rt.tools["create_scheduled_task"].call(
            rt.run, "carol", "0 10 * * *", "new"
        )
    )
    assert before["status"] == "error"
    listed = json.loads(await rt.tools["list_scheduled_tasks"].call(rt.run))
    assert [task["task_id"] for task in listed["tasks"]] == ["old-carol"]
    recreated = json.loads(
        await rt.tools["create_scheduled_task"].call(
            rt.run, "carol", "0 10 * * *", "new"
        )
    )
    assert recreated["status"] == "created"
    assert (
        rt.scheduler.scheduler.get_job(recreated["task_id"]).kwargs["user_name"]
        == "Carol"
    )
    for rejected in [
        "bot_group_foreign",
        "wrong-bot",
        "system_hidden",
        "misleading-system",
        "missing",
    ]:
        result = json.loads(
            await rt.tools["delete_scheduled_task"].call(rt.run, rejected)
        )
        assert result["status"] == "error"
    deleted = json.loads(
        await rt.tools["delete_scheduled_task"].call(rt.run, "old-carol")
    )
    assert (
        deleted["status"] == "deleted"
        and rt.scheduler.scheduler.get_job("old-carol") is None
    )


@pytest.mark.asyncio
async def test_memory_tool_keeps_user_scope_sources_and_multiple_searches(runtime):
    rt = runtime
    rt.plugin.passive_memory_manager.search_and_filter_memories.return_value = [
        {
            "id": "hidden-id",
            "text": "Alice likes photography",
            "metadata": {
                "source_label": "来源群聊：other",
                "source_session_id": "other",
                "evidence": [{"message_id": "original-message", "quote": "hidden quote"}],
            },
        }
    ]
    first = json.loads(await rt.tools["search_memory"].call(rt.run, "photography"))
    second = json.loads(await rt.tools["search_memory"].call(rt.run, "camera", "carol"))
    assert first["status"] == second["status"] == "success"
    assert "来源群聊：other" in first["memory_context"]
    for result in (first, second):
        memory = fromstring(result["memory_context"])
        assert memory.attrib["message_id"] == "original-message"
        assert memory.attrib["source"] == "来源群聊：other"
        assert "source_session_id" not in result["memory_context"]
        assert set(result) == {"status", "count", "memory_context"}
    assert (
        "hidden quote" not in first["memory_context"]
        and "hidden-id" not in first["memory_context"]
    )
    calls = rt.plugin.passive_memory_manager.search_and_filter_memories.await_args_list
    assert [call.kwargs["user_ids"] for call in calls] == [["alice", "bob"], ["carol"]]
    assert all(
        call.kwargs["bot_name"] == "bot" and call.kwargs["group_or_user_id"] == "group"
        for call in calls
    )
    assert rt.plugin.data_cache.record_memory_hits.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(NATIVE_TOOL_CONFIG_KEYS))
async def test_disabled_plugin_switches_and_mismatched_scope_cannot_execute(
    runtime, name
):
    rt = runtime
    arguments = {
        "search_memory": {"query": "topic"},
        "create_scheduled_task": {
            "user_id": "alice",
            "time_expr": "0 9 * * *",
            "content": "reminder",
        },
        "list_scheduled_tasks": {},
        "delete_scheduled_task": {"task_id": "anything"},
    }[name]
    config_key = NATIVE_TOOL_CONFIG_KEYS[name]
    rt.plugin.tools_config[config_key] = False
    disabled = json.loads(await rt.tools[name].call(rt.run, **arguments))
    assert disabled["status"] == "error" and "插件配置" in disabled["message"]
    rt.plugin.tools_config.pop(config_key)
    rt.run.context.extra[NATIVE_CONTEXT_KEY] = json.dumps(
        {**rt.state, "session_id": "elsewhere"}
    )
    assert (
        json.loads(await rt.tools[name].call(rt.run, **arguments))["status"] == "error"
    )
    rt.plugin.passive_memory_manager.search_and_filter_memories.assert_not_awaited()
    assert rt.scheduler.scheduler.get_jobs() == []


def test_memory_and_scheduled_tool_switches_default_to_enabled():
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
            encoding="utf-8"
        )
    )
    items = schema["tools_config"]["items"]
    for key in ("search_memory_enabled", "scheduled_tasks_enabled"):
        assert items[key]["type"] == "bool" and items[key]["default"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_tools,force_xml", [(False, False), (True, False), (False, True), (True, True)]
)
async def test_context_rules_stay_in_system_and_are_stable_across_recall_results(
    runtime, source_tools, force_xml
):
    systems = []
    for source_session in (None, "group", "other"):
        memories = (
            [
                {
                    "text": "本轮专属的记忆正文",
                    "metadata": {"source_session_id": source_session},
                }
            ]
            if source_session
            else []
        )
        user_prompt = build_reply_prompt(
            [],
            [],
            None,
            reply_messages=[MessageData(user_id="alice", content="本轮专属的聊天正文")],
            group_profile="本会话的自定义群规",
            relevant_memories=memories,
            current_session_id="group",
        )
        result = await runtime.caller.call_llm_reply(
            runtime.event,
            "group",
            ["provider"],
            "persona",
            user_prompt,
            use_source_tools=source_tools,
            force_xml_tools=force_xml,
        )
        assert result is not None
        call = runtime.context.tool_loop_agent.call_args.kwargs
        system = call["system_prompt"]
        systems.append(system)
        assert call["prompt"] == user_prompt
        context_start = system.index("# 上下文说明与使用规则")
        output_start = system.index("# 交互规范与输出格式")
        assert context_start < output_start
        context = system[context_start:output_start]
        for section in ("## 会话消息", "## 记忆", "## 成员画像与状态", "## 媒体转述说明"):
            assert section in context
        for rule in (
            "get_message_context",
            "带 source 属性的记忆来自其他会话；",
            "这些记忆若可能涉及隐私",
            "非本会话互动记录若可能涉及隐私",
            "任务默认保持进行中",
            "仅供理解对话",
        ):
            assert rule in context
            assert rule not in user_prompt
        assert "`<recent_messages>` 是" not in system[output_start:]
        for data in ("本轮专属的记忆正文", "本轮专属的聊天正文", "本会话的自定义群规"):
            assert data not in system
        assert "本会话的自定义群规" in user_prompt
    assert len(set(systems)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("force_xml", [False, True])
@pytest.mark.parametrize(
    "memory_enabled,tasks_enabled",
    [(True, True), (True, False), (False, True), (False, False)],
)
async def test_global_switches_filter_memory_and_all_reminder_tools_in_both_modes(
    runtime,
    force_xml,
    memory_enabled,
    tasks_enabled,
):
    rt = runtime
    rt.plugin.tools_config.update(
        search_memory_enabled=memory_enabled,
        scheduled_tasks_enabled=tasks_enabled,
    )
    result = await rt.caller.call_llm_reply(
        rt.event,
        "group",
        ["provider"],
        "persona",
        "input",
        use_source_tools=True,
        force_xml_tools=force_xml,
        tool_context=rt.state,
    )
    assert result is not None
    call = rt.context.tool_loop_agent.call_args.kwargs
    available = set()
    if memory_enabled:
        available.add("search_memory")
    if tasks_enabled:
        available.update(
            {"create_scheduled_task", "list_scheduled_tasks", "delete_scheduled_task"}
        )
    native_names = (
        {tool.name for tool in call["tools"].tools} if "tools" in call else set()
    )
    assert native_names == (set() if force_xml else available)
    for name in NATIVE_TOOL_CONFIG_KEYS:
        assert (f'<tool_call name="{name}"' in call["system_prompt"]) == (
            force_xml and name in available
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_tools,force_xml",
    [(False, False), (True, False), (False, True), (True, True)],
)
async def test_all_function_tools_follow_the_selected_global_calling_mode(
    runtime, source_tools, force_xml
):
    rt = runtime
    generic = SearchMemoryTool(plugin=rt.plugin, name="generic_tool")
    rt.context.get_llm_tool_manager = lambda: SimpleNamespace(
        get_full_tool_set=lambda: ToolSet(tools=[*rt.tools.values(), generic]),
        is_builtin_tool=lambda _: False,
    )
    result = await rt.caller.call_llm_reply(
        rt.event,
        "group",
        ["provider"],
        "persona",
        "input",
        use_source_tools=source_tools,
        force_xml_tools=force_xml,
        tool_context=rt.state,
    )
    assert result is not None
    call = rt.context.tool_loop_agent.call_args.kwargs
    names = {tool.name for tool in call["tools"].tools} if "tools" in call else set()
    expected_names = {*NATIVE_TOOL_CONFIG_KEYS, "generic_tool"}
    assert names == (expected_names if source_tools and not force_xml else set())
    for name in expected_names:
        assert (f'<tool_call name="{name}"' in call["system_prompt"]) == force_xml
    assert ("agent_context" in call) == (source_tools and not force_xml)
    if source_tools and not force_xml:
        assert call["max_steps"] == 7
    if force_xml:
        assert "不使用原生 function calling" in call["system_prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("force_xml", [False, True])
async def test_persona_limits_tools_independently_of_bot_xml_features(
    runtime,
    force_xml,
):
    rt = runtime
    await rt.caller.call_llm_reply(
        rt.event,
        "group",
        ["provider"],
        "persona",
        "input",
        persona_tools=["list_scheduled_tasks", "search_memory"],
        enabled_features=[],
        tool_context=rt.state,
        use_source_tools=True,
        force_xml_tools=force_xml,
    )
    call = rt.context.tool_loop_agent.call_args.kwargs
    if force_xml:
        assert "tools" not in call
        assert '<tool_call name="list_scheduled_tasks"' in call["system_prompt"]
        assert '<tool_call name="search_memory"' in call["system_prompt"]
        assert '<tool_call name="create_scheduled_task"' not in call["system_prompt"]
        assert '<tool_call name="delete_scheduled_task"' not in call["system_prompt"]
    else:
        assert {tool.name for tool in call["tools"].tools} == {
            "list_scheduled_tasks",
            "search_memory",
        }


@pytest.mark.asyncio
async def test_native_loop_can_correct_list_delete_and_confirm_before_final_xml(
    runtime,
):
    rt = runtime
    statuses = []

    async def loop(**kwargs):
        run = SimpleNamespace(context=kwargs["agent_context"])
        for expression in ["invalid", "0 9 * * *"]:
            result = json.loads(
                await rt.tools["create_scheduled_task"].call(
                    run, "alice", expression, "reminder"
                )
            )
            statuses.append(result["status"])
        listed = json.loads(await rt.tools["list_scheduled_tasks"].call(run))
        assert len(listed["tasks"]) == 1
        deleted = json.loads(
            await rt.tools["delete_scheduled_task"].call(
                run, listed["tasks"][0]["task_id"]
            )
        )
        statuses.append(deleted["status"])
        rt.event.send.assert_not_called()
        return SimpleNamespace(
            completion_text="<message>任务已经删除</message>",
            reasoning_content="",
            tools_call_name=[
                "create_scheduled_task",
                "list_scheduled_tasks",
                "delete_scheduled_task",
            ],
        )

    rt.context.tool_loop_agent.side_effect = loop
    result = await rt.caller.call_llm_reply(
        rt.event,
        "group",
        ["provider"],
        "persona",
        "input",
        tool_context=rt.state,
        use_source_tools=True,
    )
    assert statuses == ["error", "created", "deleted"]
    assert result.native_tools_called == [
        "create_scheduled_task",
        "list_scheduled_tasks",
        "delete_scheduled_task",
    ]
    assert (
        rt.context.tool_loop_agent.await_count == 1
        and rt.scheduler.scheduler.get_jobs() == []
    )


@pytest.mark.asyncio
async def test_native_failure_does_not_replay_completed_schedule_side_effects(runtime):
    rt = runtime

    async def loop(**kwargs):
        result = json.loads(
            await rt.tools["create_scheduled_task"].call(
                SimpleNamespace(context=kwargs["agent_context"]),
                "alice",
                "0 9 * * *",
                "reminder",
            )
        )
        assert result["status"] == "created"
        raise RuntimeError("connection lost after executing the tool")

    rt.context.tool_loop_agent.side_effect = loop
    result = await rt.caller.call_llm_reply(
        rt.event,
        "group",
        ["primary", "fallback"],
        "persona",
        "input",
        tool_context=rt.state,
        use_source_tools=True,
    )
    assert result is None and rt.context.tool_loop_agent.await_count == 1
    assert len(rt.scheduler.scheduler.get_jobs()) == 1


@pytest.mark.asyncio
async def test_retired_xml_tags_are_inert_but_all_tools_can_export_standard_xml(
    runtime,
):
    rt = runtime
    result = await XmlParse(None, None).decode_llm_xml(
        '<search_memory>topic</search_memory><schedule_task user_id="alice" time="0 9 * * *">reminder</schedule_task><all_task/><delete_task task_id="anything"/><message>hello</message>',
        "group",
    )
    assert result is not None
    for field_name in [
        "search_memories",
        "schedule_tasks",
        "all_tasks",
        "delete_schedule_tasks",
    ]:
        assert not hasattr(result, field_name)
    commands = CommandHandler(rt.plugin)
    for name in NATIVE_TOOL_CONFIG_KEYS:
        async for _ in commands.tool_xml(rt.event, name):
            pass
        declaration = rt.event.send.call_args.args[0].chain[0].nodes[0].content[0].text
        assert f'<tool_call name="{name}"' in declaration
    assert rt.scheduler.scheduler.get_jobs() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["aiocqhttp", "qq_official_webhook", "generic"])
@pytest.mark.parametrize("recall_scope", ["session", "user", "all"])
async def test_xml_tools_preserve_scope_creators_and_idempotency_across_outputs(
    runtime,
    platform,
    recall_scope,
):
    rt = runtime
    rt.plugin.memory_recall_scope = recall_scope
    rt.event.get_platform_name.return_value = platform
    rt.plugin.get_bot_config = rt.plugin.bot_map.get
    rt.plugin.min_reply_interval = rt.plugin.max_reply_interval = 0
    rt.scheduler.add_job(
        "old-carol",
        "remind",
        "0 9 * * *",
        kwargs={
            "bot_name": "bot",
            "group_or_user_id": "group",
            "user_id": "carol",
            "user_name": "Carol",
            "remind_message": "old",
        },
    )
    parser = XmlParse(None, None)
    result = await parser.decode_llm_xml(
        '<tool_call name="list_scheduled_tasks">{}</tool_call>'
        '<tool_call name="create_scheduled_task">{"user_id":"carol","time_expr":"0 10 * * *","content":"new"}</tool_call>'
        '<tool_call name="search_memory">{"query":"old agreement"}</tool_call>',
        "group",
    )
    result.tool_context = rt.state
    dispatcher = ActionDispatcher(rt.plugin)
    dispatch = (
        dispatcher._dispatch_generic_outputs
        if platform == "generic"
        else dispatcher._dispatch_aiocqhttp_outputs
    )
    await dispatch(rt.event, "bot", "Giftia", "group", result)
    outputs = [json.loads(item["results"]) for item in result.xml_tool_results]
    assert [item["status"] for item in outputs] == [
        "success",
        "created",
        "success",
    ]
    assert rt.state["creator_names"]["carol"] == "Carol"
    search_users = (
        rt.plugin.passive_memory_manager.search_and_filter_memories.call_args.kwargs[
            "user_ids"
        ]
    )
    assert search_users == (["alice", "bob"] if recall_scope == "user" else None)
    continuation = await parser.decode_llm_xml(
        '<tool_call name="create_scheduled_task">{"user_id":"carol","time_expr":"0 10 * * *","content":"new"}</tool_call>'
        f'<tool_call name="delete_scheduled_task">{json.dumps({"task_id": outputs[1]["task_id"]})}</tool_call>',
        "group",
    )
    continuation.tool_context = rt.state
    await dispatch(rt.event, "bot", "Giftia", "group", continuation)
    retried, deleted = [
        json.loads(item["results"]) for item in continuation.xml_tool_results
    ]
    assert retried["status"] == "exists" and retried["task_id"] == outputs[1]["task_id"]
    assert deleted["status"] == "deleted"
    assert [job.id for job in rt.scheduler.scheduler.get_jobs()] == ["old-carol"]
