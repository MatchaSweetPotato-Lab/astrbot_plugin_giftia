import ast
import asyncio
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from core.conversation.chat_manager import ChatManager
from core.utils.schemas import MessageData, XmlLlmResult


@pytest.fixture
def runtime():
    plugin = SimpleNamespace(
        _terminated=False,
        adapter_id_map={"adapter": "bot"},
        bot_map={"bot": {"nickname": "Bot"}},
        running_tasks={},
        active_reply_counters={},
        replying_status={},
        parse_locks=defaultdict(asyncio.Lock),
        block_command_messages=False,
        passive_memory_enabled=False,
        passive_memory_manager=SimpleNamespace(
            check_and_trigger_passive_memory=AsyncMock(),
        ),
        db=SimpleNamespace(
            get_kv_data=AsyncMock(return_value=None),
            upsert_kv_data=AsyncMock(),
            chat_history_repo=SimpleNamespace(update_processing_status=AsyncMock()),
        ),
        data_cache=SimpleNamespace(
            add_message=AsyncMock(),
            get_recent_message=AsyncMock(return_value=[]),
            is_bot_muted=Mock(return_value=False),
        ),
        get_caption_config=lambda bot: {"defer_caption_enabled": False},
        message_parser=SimpleNamespace(
            parse_user_message=AsyncMock(
                return_value=(MessageData(message_id="m1", content="hello"), [], [])
            ),
        ),
    )
    event = SimpleNamespace(
        platform_meta=SimpleNamespace(id="adapter"),
        unified_msg_origin="adapter:GroupMessage:100",
        message_obj=SimpleNamespace(raw_message={}),
        get_self_id=lambda: "999",
        get_sender_id=lambda: "200",
        get_group_id=lambda: "100",
        get_platform_name=lambda: "aiocqhttp",
        get_extra=lambda key, default=None: default,
        should_call_llm=Mock(),
        stop_event=Mock(),
        _has_send_oper=False,
        send=AsyncMock(),
    )
    manager = ChatManager(plugin)
    manager.decision_engine.check_whitelists = Mock(return_value=True)
    manager.decision_engine.get_trigger = Mock(return_value=False)
    manager.decision_engine.evaluate_decision = AsyncMock(
        return_value=(True, None, [], None)
    )
    manager.action_dispatcher.dispatch_actions = AsyncMock()
    manager.reply_pipeline.commit_pending_session_recalled_memories = Mock()
    return plugin, manager, event


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["parse", "decision", "reply"])
async def test_later_handler_runs_while_message_processing_is_pending(runtime, phase):
    plugin, manager, event = runtime
    started, release = asyncio.Event(), asyncio.Event()
    later_handler = AsyncMock()

    async def parse(*args, **kwargs):
        if phase == "parse":
            started.set()
            await release.wait()
        return MessageData(message_id="m1", content="hello"), [], []

    async def decide(**kwargs):
        if phase == "decision":
            started.set()
            await release.wait()
        return True, None, [], None

    async def reply(**kwargs):
        if phase == "reply":
            started.set()
            await release.wait()
        yield XmlLlmResult()

    plugin.message_parser.parse_user_message.side_effect = parse
    manager.decision_engine.evaluate_decision.side_effect = decide
    manager.reply_pipeline.dispatch_llm_reply_loop = reply

    async def dispatch():
        # Mirror AstrBot's sequential handler dispatch with a shared event.
        await manager.handle_message(event)
        await later_handler(event)

    try:
        await asyncio.wait_for(dispatch(), timeout=1)
        await asyncio.wait_for(started.wait(), timeout=1)
        later_handler.assert_awaited_once_with(event)
        assert any(not task.done() for task in plugin.running_tasks.values())
        event.should_call_llm.assert_called_once_with(True)
        event.stop_event.assert_not_called()
        assert event._has_send_oper is False
    finally:
        release.set()
        while plugin.running_tasks:
            await asyncio.gather(
                *list(plugin.running_tasks.values()), return_exceptions=True
            )
            await asyncio.sleep(0)

    assert plugin.running_tasks == {}
    manager.action_dispatcher.dispatch_actions.assert_awaited_once()


@pytest.mark.asyncio
async def test_command_installs_logging_before_return_without_disabling_default_llm(
    runtime,
):
    plugin, manager, event = runtime
    manager._check_command_info = Mock(return_value=(True, True))
    original_send = event.send

    await manager.handle_message(event)
    assert event._giftia_bypass_logging is True
    assert event.send is not original_send
    event.should_call_llm.assert_not_called()
    await event.send(Mock())
    original_send.assert_awaited_once()
    await asyncio.gather(*plugin.running_tasks.values())
    plugin.message_parser.parse_user_message.assert_not_awaited()
    plugin.data_cache.add_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_background_job_is_logged_and_removed(runtime, monkeypatch):
    plugin, manager, event = runtime
    manager.job = AsyncMock(side_effect=RuntimeError("decision failed"))
    log_error = Mock()
    monkeypatch.setattr("core.conversation.chat_manager.logger.error", log_error)

    await manager.handle_message(event)
    await asyncio.gather(*plugin.running_tasks.values(), return_exceptions=True)

    assert plugin.running_tasks == {}
    log_error.assert_called_once()
    assert log_error.call_args.kwargs["exc_info"] is True
    plugin.passive_memory_manager.check_and_trigger_passive_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_cancelled_before_start_is_removed(runtime):
    plugin, manager, event = runtime
    manager.job = AsyncMock()

    await manager.handle_message(event)
    tasks = list(plugin.running_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    assert plugin.running_tasks == {}
    manager.job.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["access", "self", "terminated"])
async def test_ignored_message_keeps_default_llm_behavior(runtime, reason):
    plugin, manager, event = runtime
    if reason == "access":
        manager.decision_engine.check_whitelists.return_value = False
    elif reason == "self":
        event.get_sender_id = event.get_self_id
    else:
        plugin._terminated = True

    await manager.handle_message(event)

    assert plugin.running_tasks == {}
    event.should_call_llm.assert_not_called()
    event.stop_event.assert_not_called()


@pytest.mark.asyncio
async def test_terminate_cancels_passive_memory_before_closing_resources(runtime):
    plugin, manager, event = runtime
    plugin.passive_memory_enabled = True
    manager.job = AsyncMock()
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def passive_memory(**kwargs):
        manager.job.assert_awaited_once_with(event)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def close_db():
        assert cancelled.is_set()

    plugin.passive_memory_manager.check_and_trigger_passive_memory.side_effect = (
        passive_memory
    )
    plugin.db.close = AsyncMock(side_effect=close_db)
    plugin.context = Mock()
    plugin.http_manager = SimpleNamespace(close_session=AsyncMock())
    plugin.task_manager = Mock()
    plugin.ltm = Mock()
    source = Path(__file__).resolve().parents[1] / "main.py"
    terminate = next(
        node
        for node in ast.walk(ast.parse(source.read_text()))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "terminate"
    )
    namespace = {"asyncio": asyncio, "remove_tools": Mock()}
    exec(
        compile(ast.Module(body=[terminate], type_ignores=[]), str(source), "exec"),
        namespace,
    )

    await manager.handle_message(event)
    await asyncio.wait_for(started.wait(), timeout=1)
    plugin.chat_manager = manager
    await manager._enqueue_message(
        event, "bot", "100", MessageData(message_id="not-started", content="hello")
    )
    await asyncio.wait_for(namespace["terminate"](plugin), timeout=1)

    assert plugin.running_tasks == {}
    assert plugin._terminated is True
    plugin.db.chat_history_repo.update_processing_status.assert_awaited_with(
        "bot", "100", ["not-started"], "interrupted"
    )
    assert not manager.sessions
    plugin.db.close.assert_awaited_once()
    await manager.handle_message(event)
    assert plugin.running_tasks == {}
    manager.job.assert_awaited_once()
