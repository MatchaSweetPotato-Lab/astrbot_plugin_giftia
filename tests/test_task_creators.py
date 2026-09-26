"""Verify task attribution across batch replies and tool correction turns."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from core.conversation.action_dispatcher import ActionDispatcher
from core.conversation.reply_pipeline import ReplyPipeline
from core.llm.preset_prompts import build_xml_instructions
from core.llm.xml_parse import XmlParse
from core.utils.scheduler import Scheduler
from core.utils.schemas import MessageData, Status

from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)


@pytest.fixture
def runtime():
    history = [
        MessageData(
            message_id="A", user_id="alice", nickname="Alice", content="Remind Bob"
        ),
        MessageData(
            message_id="B", user_id="bob", nickname="Bob", content="Unrelated reply"
        ),
    ]

    async def add_message(bot, session, message):
        history.append(message)

    bot_conf = {"llm_reply_conf": {"provider_ids": ["reply"]}}
    plugin = SimpleNamespace(
        bot_map={"bot": bot_conf},
        get_bot_config=lambda _: bot_conf,
        tools_config={"max_loop": 3},
        embedding_conf={"enabled": False, "session_recall_enabled": False},
        msg_number=20,
        get_caption_config=lambda _: {},
        data_cache=SimpleNamespace(
            get_recent_message=AsyncMock(side_effect=lambda *args: list(history)),
            get_bot_status=AsyncMock(return_value=Status()),
            set_bot_status=AsyncMock(),
            get_group_profile=AsyncMock(return_value=""),
            get_user_profile_record=AsyncMock(return_value={}),
            get_user_relation=AsyncMock(return_value=None),
            build_active_user_briefs=AsyncMock(return_value=[]),
            is_bot_muted=Mock(return_value=False),
            add_message=AsyncMock(side_effect=add_message),
        ),
        task_board=SimpleNamespace(
            get_active_tasks=AsyncMock(return_value=[]),
            max_active_tasks=lambda: 3,
            create_task=AsyncMock(
                return_value=(True, "created", SimpleNamespace(task_id="task"))
            ),
            close_task=AsyncMock(
                return_value=(True, "closed", SimpleNamespace(task_id="task"))
            ),
        ),
        task_manager=SimpleNamespace(
            add_job=Mock(return_value="created"),
            get_prefix_jobs=Mock(return_value=[]),
            get_session_jobs_data=Mock(return_value=[]),
        ),
        emoji_manager=SimpleNamespace(get_random_stickers=AsyncMock(return_value=[])),
        context=SimpleNamespace(
            persona_manager=SimpleNamespace(
                get_persona_v3_by_id=lambda _: {"prompt": "persona"}
            )
        ),
        db=SimpleNamespace(
            slang_repo=SimpleNamespace(get_entries=AsyncMock(return_value=[])),
            update_message_reply_decision=AsyncMock(),
        ),
        call_llm=SimpleNamespace(call_llm_reply=AsyncMock()),
    )
    event = MagicMock(spec=AiocqhttpMessageEvent)
    event.platform_meta = SimpleNamespace(id="adapter")
    event.unified_msg_origin = "adapter:GroupMessage:group"
    event.get_platform_name.return_value = "aiocqhttp"
    event.get_group_id.return_value = "group"
    event.get_self_id.return_value = "bot-id"
    event.get_sender_id.return_value = "bob"
    event.get_sender_name.return_value = "Bob"
    event.get_group = AsyncMock(return_value="")
    pipeline = ReplyPipeline(plugin)
    pipeline.media_captioner.get_cached_media_captions = AsyncMock(return_value=[])
    dispatcher = ActionDispatcher(plugin)
    dispatcher._dispatch_aiocqhttp_outputs = AsyncMock()
    dispatcher._dispatch_generic_outputs = AsyncMock()
    parser = XmlParse(None, None)

    async def run(*xmls, **kwargs):
        results = [await parser.decode_llm_xml(xml, "group") for xml in xmls]
        assert all(result is not None for result in results)
        plugin.call_llm.call_llm_reply.side_effect = results
        chunks = []
        async for chunk in pipeline.dispatch_llm_reply_loop(
            event, "bot", "Bot", "group", **kwargs
        ):
            chunks.append(chunk)
            await dispatcher.dispatch_actions(event, "bot", "Bot", "group", chunk)
        return chunks

    return SimpleNamespace(
        plugin=plugin,
        event=event,
        history=history,
        run=run,
        pipeline=pipeline,
        dispatcher=dispatcher,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["aiocqhttp", "qq_official_webhook"])
@pytest.mark.parametrize("private", [False, True])
async def test_scheduled_creator_is_explicit_and_does_not_change_routing(
    runtime, platform, private
):
    runtime.event.get_platform_name.return_value = platform
    if private:
        runtime.event.get_group_id.return_value = ""
        runtime.event.unified_msg_origin = "adapter:FriendMessage:group"
    await runtime.run(
        '<schedule_task user_id="alice" creator_nickname="Wrong" '
        'group_id="elsewhere" time="0 9 * * *">Remind Bob</schedule_task>'
    )
    runtime.plugin.task_manager.add_job.assert_called_once()
    job = runtime.plugin.task_manager.add_job.call_args.kwargs["kwargs"]
    assert job["user_id"] == "alice"
    assert job["user_name"] == "Alice"
    assert job["remind_message"] == "Remind Bob"
    assert job["group_id"] == ("" if private else "group")
    assert job["group_or_user_id"] == "group"
    assert job["unified_msg_origin"] == runtime.event.unified_msg_origin
    assert job["adapter_id"] == "adapter"


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["task_board", "short_task"])
@pytest.mark.parametrize("platform", ["aiocqhttp", "generic"])
async def test_short_task_creator_comes_from_each_request(runtime, tag, platform):
    runtime.event.get_platform_name.return_value = platform
    await runtime.run(
        f'<{tag} action="create" user_id="alice" creator_nickname="Wrong">For Bob</{tag}>'
        f'<{tag} action="create" user_id="bob">For Alice</{tag}>'
    )
    calls = runtime.plugin.task_board.create_task.await_args_list
    assert [
        (c.kwargs["creator_user_id"], c.kwargs["creator_nickname"]) for c in calls
    ] == [("alice", "Alice"), ("bob", "Bob")]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["schedule_task", "task_board"])
@pytest.mark.parametrize("creator", ["", "unknown", "recalled", "operation", "late"])
async def test_invalid_creator_returns_feedback_then_allows_correction(
    runtime, tool, creator
):
    runtime.history.extend(
        [
            MessageData(user_id="recalled", is_recalled=1),
            MessageData(user_id="operation", role="operation_log"),
        ]
    )
    frozen = list(runtime.history)
    runtime.history.append(MessageData(user_id="late", nickname="Late"))
    attributes = 'time="0 9 * * *"' if tool == "schedule_task" else 'action="create"'
    creator_attr = f'user_id="{creator}"' if creator else ""
    chunks = await runtime.run(
        f"<{tool} {attributes} {creator_attr}>For Bob</{tool}>",
        f'<{tool} {attributes} user_id="alice">For Bob</{tool}>',
        recent_messages=frozen,
    )
    calls = (
        runtime.plugin.task_manager.add_job.call_args_list
        if tool == "schedule_task"
        else runtime.plugin.task_board.create_task.await_args_list
    )
    assert len(calls) == 1
    assert len(chunks) == 2
    error = chunks[0].xml_tool_results[0]["results"]
    assert "user_id" in error and "failed" in error
    assert (
        "user_id"
        in runtime.plugin.call_llm.call_llm_reply.await_args_list[1].kwargs[
            "user_prompt"
        ]
    )
    assert "late" not in chunks[1].task_creator_names
    assert (
        runtime.plugin.data_cache.add_message.await_args_list[0].args[2].role
        == "operation_log"
    )


@pytest.mark.asyncio
async def test_correction_does_not_replay_successful_task(runtime):
    await runtime.run(
        '<schedule_task user_id="alice" time="0 9 * * *">First</schedule_task>'
        '<task_board action="create">Second</task_board>',
        '<task_board action="create" user_id="bob">Second</task_board>',
    )
    runtime.plugin.task_manager.add_job.assert_called_once()
    runtime.plugin.task_board.create_task.assert_awaited_once()
    assert (
        runtime.plugin.task_board.create_task.await_args.kwargs["creator_user_id"]
        == "bob"
    )


@pytest.mark.asyncio
async def test_reminder_creator_survives_history_expiry(runtime):
    runtime.history.clear()
    await runtime.run(
        '<schedule_task user_id="alice" time="0 9 * * *">Follow-up</schedule_task>',
        remind_message="Alice(alice): Follow-up",
        task_creator_names={"alice": "Alice"},
    )
    job = runtime.plugin.task_manager.add_job.call_args.kwargs["kwargs"]
    assert (job["user_id"], job["user_name"]) == ("alice", "Alice")


@pytest.mark.asyncio
async def test_queried_task_preserves_original_creator_when_recreated(runtime):
    runtime.plugin.task_manager.get_prefix_jobs.return_value = ["创建者：Carol(carol)"]
    runtime.plugin.task_manager.get_session_jobs_data.return_value = [
        {"user_id": "carol", "user_name": "Carol"}
    ]
    await runtime.run(
        "<all_task/>",
        '<schedule_task user_id="carol" time="0 9 * * *">Recreated</schedule_task>',
    )
    job = runtime.plugin.task_manager.add_job.call_args.kwargs["kwargs"]
    assert (job["user_id"], job["user_name"]) == ("carol", "Carol")
    prompt = runtime.plugin.call_llm.call_llm_reply.await_args_list[1].kwargs[
        "user_prompt"
    ]
    assert "Carol(carol)" in prompt


def test_task_instructions_describe_creator_parameter():
    prompt = build_xml_instructions(enabled_features=None)
    assert '<schedule_task user_id="创建者ID"' in prompt
    assert '<task_board action="create" user_id="创建者ID"' in prompt
    assert "user_id 为任务创建者的用户 ID，必填。" in prompt


def test_scheduled_query_displays_stored_creator_without_modifying_legacy_job():
    kwargs = {"user_id": "alice", "user_name": "Alice", "remind_message": "For Bob"}
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.scheduler = SimpleNamespace(
        get_jobs=lambda: [
            SimpleNamespace(id="bot_group_123", name="remind", kwargs=kwargs)
        ]
    )
    assert "创建者：Alice(alice)" in scheduler.get_prefix_jobs("bot_group")[0]
    assert kwargs == {
        "user_id": "alice",
        "user_name": "Alice",
        "remind_message": "For Bob",
    }
