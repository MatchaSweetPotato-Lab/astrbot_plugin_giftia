import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from core.conversation.chat_manager import ChatManager
from core.utils.scheduler import Scheduler, job_proxy
from core.utils.schemas import XmlLlmResult

from astrbot.api.message_components import Plain
from astrbot.core.utils.session_lock import session_lock_manager


@pytest.fixture
def runtime(monkeypatch):
    windows = asyncio.Queue()

    async def collect(delay):
        assert delay == 1
        release, finished = asyncio.Event(), asyncio.Event()
        windows.put_nowait((release, finished))
        await release.wait()
        finished.set()

    # Control only the reminder collector, leaving asyncio itself untouched.
    monkeypatch.setattr(
        "core.conversation.chat_manager.asyncio",
        SimpleNamespace(create_task=asyncio.create_task, sleep=collect),
    )
    plugin = SimpleNamespace(
        get_bot_config=Mock(return_value={}),
        data_cache=SimpleNamespace(is_bot_muted=Mock(return_value=False)),
        replying_status={},
        active_reply_counters={},
        bot_map={
            "bot": {
                "decision_conf": {
                    "group_whitelist_mode": "whitelist",
                    "group_whitelist": ["100", "101"],
                    "private_whitelist_mode": "whitelist",
                    "private_whitelist": ["100", "101"],
                }
            },
            "other": {
                "decision_conf": {
                    "group_whitelist_mode": "whitelist",
                    "group_whitelist": ["100", "101"],
                    "private_whitelist_mode": "whitelist",
                    "private_whitelist": ["100", "101"],
                }
            },
        },
        passive_memory_manager=SimpleNamespace(mark_silence_summary_armed=AsyncMock()),
        context=SimpleNamespace(send_message=AsyncMock(return_value=True)),
    )
    manager = ChatManager(plugin)
    manager.fake_event = Mock(side_effect=lambda **kwargs: SimpleNamespace())
    manager.action_dispatcher.dispatch_actions = AsyncMock()
    calls = []

    async def reply(**kwargs):
        calls.append(kwargs)
        yield XmlLlmResult(msg_chains=[[Plain("Combined reminder")]])

    manager.reply_pipeline.dispatch_llm_reply_loop = reply
    manager.reply_pipeline.commit_pending_session_recalled_memories = Mock()
    defaults = {
        "unified_msg_origin": "adapter:GroupMessage:100",
        "adapter_id": "adapter",
        "bot_name": "bot",
        "nickname": "Bot",
        "self_id": "self",
        "platform_name": "aiocqhttp",
        "user_id": "200",
        "user_name": "Alice",
        "group_id": "100",
        "group_or_user_id": "100",
    }

    def trigger(message, **overrides):
        return asyncio.create_task(
            manager.remind_task(**(defaults | {"remind_message": message} | overrides))
        )

    async def release_window():
        release, finished = await asyncio.wait_for(windows.get(), timeout=2)
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=2)

    return SimpleNamespace(
        manager=manager,
        plugin=plugin,
        calls=calls,
        defaults=defaults,
        trigger=trigger,
        windows=windows,
        release_window=release_window,
    )


@pytest.mark.asyncio
async def test_simultaneous_reminders_share_one_reply_and_keep_each_creator(runtime):
    first = runtime.trigger("Drink water")
    second = runtime.trigger("Attend meeting", user_id="201", user_name="Bob")
    await runtime.release_window()
    await asyncio.gather(first, second)

    assert len(runtime.calls) == 1
    prompt = runtime.calls[0]["remind_message"]
    assert "Alice(200): Drink water" in prompt
    assert "Bob(201): Attend meeting" in prompt
    assert "整合成一条消息" in prompt
    runtime.manager.action_dispatcher.dispatch_actions.assert_awaited_once()
    runtime.plugin.passive_memory_manager.mark_silence_summary_armed.assert_awaited_once()
    assert runtime.plugin.replying_status["bot:100"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"bot_name": "other"},
        {
            "unified_msg_origin": "adapter:GroupMessage:101",
            "group_id": "101",
            "group_or_user_id": "101",
        },
        {
            "unified_msg_origin": "other-adapter:GroupMessage:100",
            "adapter_id": "other-adapter",
        },
        {"unified_msg_origin": "adapter:FriendMessage:100", "group_id": ""},
    ],
)
async def test_different_bots_or_sessions_do_not_merge(runtime, overrides):
    first = runtime.trigger("First reminder")
    second = runtime.trigger("Second reminder", **overrides)
    await runtime.release_window()
    await runtime.release_window()
    await asyncio.gather(first, second)

    assert len(runtime.calls) == 2
    prompts = [call["remind_message"] for call in runtime.calls]
    assert sum("First reminder" in prompt for prompt in prompts) == 1
    assert sum("Second reminder" in prompt for prompt in prompts) == 1
    assert all("批量唤醒" not in prompt for prompt in prompts)


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_single_reminder_keeps_existing_prompt(runtime, private):
    overrides = (
        {"group_id": "", "unified_msg_origin": "adapter:FriendMessage:100"}
        if private
        else {}
    )
    task = runtime.trigger("Drink water", **overrides)
    await runtime.release_window()
    await task

    assert runtime.calls[0]["remind_message"] == (
        "[定时任务唤醒] Alice(200): Drink water"
    )


@pytest.mark.asyncio
async def test_collection_closes_before_waiting_for_busy_session(runtime):
    async with session_lock_manager.acquire_lock(
        runtime.defaults["unified_msg_origin"]
    ):
        first = runtime.trigger("First window")
        await runtime.release_window()
        second = runtime.trigger("Later window")
        await runtime.release_window()
        assert not first.done()
        assert not second.done()
        assert runtime.calls == []
    await asyncio.gather(first, second)

    assert len(runtime.calls) == 2
    assert "Later window" not in runtime.calls[0]["remind_message"]
    assert "First window" not in runtime.calls[1]["remind_message"]


@pytest.mark.asyncio
async def test_finishing_reply_does_not_remove_newer_collection(runtime):
    started, finish = asyncio.Event(), asyncio.Event()
    original_reply = runtime.manager.reply_pipeline.dispatch_llm_reply_loop

    async def reply(**kwargs):
        if "First window" in kwargs["remind_message"]:
            started.set()
            await finish.wait()
        async for chunk in original_reply(**kwargs):
            yield chunk

    runtime.manager.reply_pipeline.dispatch_llm_reply_loop = reply
    first = runtime.trigger("First window")
    await runtime.release_window()
    await asyncio.wait_for(started.wait(), timeout=2)
    second = runtime.trigger("Later window")
    release, finished = await asyncio.wait_for(runtime.windows.get(), timeout=2)

    finish.set()
    await first
    third = runtime.trigger("Same later window")
    release.set()
    await asyncio.wait_for(finished.wait(), timeout=2)
    await asyncio.gather(second, third)

    assert len(runtime.calls) == 2
    assert "First window" not in runtime.calls[1]["remind_message"]
    assert "Alice(200): Later window" in runtime.calls[1]["remind_message"]
    assert "Alice(200): Same later window" in runtime.calls[1]["remind_message"]


@pytest.mark.asyncio
async def test_dispatch_failure_reaches_all_jobs_and_next_batch_can_run(runtime):
    runtime.manager.action_dispatcher.dispatch_actions.side_effect = RuntimeError(
        "Send failed"
    )
    first = runtime.trigger("First reminder")
    second = runtime.trigger("Second reminder")
    await runtime.release_window()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, RuntimeError) for result in results)
    assert runtime.plugin.replying_status["bot:100"] == 0

    runtime.manager.action_dispatcher.dispatch_actions.side_effect = None
    retry = runtime.trigger("Next reminder")
    await runtime.release_window()
    await retry
    assert len(runtime.calls) == 2
    assert "First reminder" not in runtime.calls[-1]["remind_message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("during_reply", [False, True])
async def test_cancellation_stops_shared_batch_and_allows_next_run(
    runtime, during_reply
):
    original_reply = runtime.manager.reply_pipeline.dispatch_llm_reply_loop
    started = asyncio.Event()

    async def blocked_reply(**kwargs):
        started.set()
        await asyncio.Event().wait()
        yield  # pragma: no cover

    runtime.manager.reply_pipeline.dispatch_llm_reply_loop = blocked_reply
    first = runtime.trigger("First reminder")
    second = runtime.trigger("Second reminder")
    if during_reply:
        await runtime.release_window()
        await asyncio.wait_for(started.wait(), timeout=2)
    else:
        await asyncio.wait_for(runtime.windows.get(), timeout=2)
    second.cancel()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    runtime.manager.action_dispatcher.dispatch_actions.assert_not_awaited()
    assert runtime.plugin.replying_status.get("bot:100", 0) == 0

    runtime.manager.reply_pipeline.dispatch_llm_reply_loop = original_reply
    task = runtime.trigger("Next reminder")
    await runtime.release_window()
    await task
    assert len(runtime.calls) == 1
    assert runtime.calls[0]["remind_message"] == (
        "[定时任务唤醒] Alice(200): Next reminder"
    )


@pytest.mark.asyncio
async def test_muted_group_skips_entire_batch(runtime):
    first = runtime.trigger("Drink water")
    second = runtime.trigger("Attend meeting")
    runtime.plugin.data_cache.is_bot_muted.return_value = True
    await runtime.release_window()
    await asyncio.gather(first, second)
    assert runtime.calls == []
    runtime.manager.action_dispatcher.dispatch_actions.assert_not_awaited()


@pytest.mark.asyncio
async def test_removed_session_skips_pending_reminders(runtime):
    task = runtime.trigger("Drink water")
    runtime.plugin.bot_map["bot"]["decision_conf"]["group_whitelist"] = []
    await runtime.release_window()
    await task
    assert runtime.calls == []
    runtime.manager.action_dispatcher.dispatch_actions.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("mode", ["blacklist", "whitelist"])
async def test_reminders_use_the_destination_chat_type(runtime, private, mode):
    conf = runtime.plugin.bot_map["bot"]["decision_conf"]
    conf.update(
        group_whitelist_mode=mode,
        group_whitelist=["100"],
        private_whitelist_mode=mode,
        private_whitelist=[],
    )
    task = runtime.trigger(
        "Drink water",
        group_id="" if private else "100",
        group_or_user_id="100",
        user_id="100",
        unified_msg_origin=f"adapter:{'FriendMessage' if private else 'GroupMessage'}:100",
    )
    await runtime.release_window()
    await task
    allowed = private == (mode == "blacklist")
    assert len(runtime.calls) == int(allowed)
    assert runtime.manager.action_dispatcher.dispatch_actions.await_count == int(
        allowed
    )


@pytest.mark.asyncio
async def test_private_reminder_rechecks_access_during_generation_and_send(runtime):
    conf = runtime.plugin.bot_map["bot"]["decision_conf"]
    conf.update(private_whitelist_mode="whitelist", private_whitelist=["200"])

    async def reply(**kwargs):
        conf["private_whitelist"] = []
        await kwargs["event"].send("Late tool output")
        yield XmlLlmResult(msg_chains=[[Plain("Late reminder")]])

    runtime.manager.reply_pipeline.dispatch_llm_reply_loop = reply
    task = runtime.trigger(
        "Drink water",
        group_id="",
        group_or_user_id="200",
        unified_msg_origin="adapter:FriendMessage:200",
    )
    await runtime.release_window()
    await task
    runtime.plugin.context.send_message.assert_not_awaited()
    runtime.manager.action_dispatcher.dispatch_actions.assert_not_awaited()
    assert runtime.plugin.replying_status["bot:200"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("one_off", [False, True])
async def test_colliding_schedules_share_reply_but_keep_independent_triggers(
    runtime, monkeypatch, one_off
):
    registry = {}
    monkeypatch.setattr("core.utils.scheduler.GLOBAL_FUNC_MAP", registry)
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.registered_funcs = registry
    # Use real scheduler jobs and triggers without starting timers or persistence.
    scheduler.scheduler = AsyncIOScheduler()
    scheduler.register_func("remind", runtime.manager.remind_task)
    due = datetime(2026, 9, 11, 9).astimezone()
    second_schedule = due.isoformat() if one_off else "0 9 * * *"
    for task_id, schedule, content in (
        ("hourly", "0 * * * *", "Drink water"),
        ("other", second_schedule, "Attend meeting"),
    ):
        scheduler.add_job(
            task_id,
            "remind",
            schedule,
            kwargs=runtime.defaults | {"remind_message": content},
        )

    hourly = scheduler.scheduler.get_job("hourly")
    other = scheduler.scheduler.get_job("other")
    before = due - timedelta(minutes=1)
    assert hourly.trigger.get_next_fire_time(None, before) == due
    assert other.trigger.get_next_fire_time(None, before) == due
    tasks = [
        asyncio.create_task(job_proxy(*job.args, **job.kwargs))
        for job in (hourly, other)
    ]
    await runtime.release_window()
    await asyncio.gather(*tasks)

    assert len(runtime.calls) == 1
    assert hourly.trigger.get_next_fire_time(due, due) == due + timedelta(hours=1)
    assert other.trigger.get_next_fire_time(due, due) == (
        None if one_off else due + timedelta(days=1)
    )
    assert hourly.kwargs["remind_message"] == "Drink water"
    assert other.kwargs["remind_message"] == "Attend meeting"
