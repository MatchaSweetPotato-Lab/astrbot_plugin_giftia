"""Behavioral tests for batch boundaries, timers, and failure outcomes."""

import asyncio
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from xml.etree.ElementTree import fromstring

import pytest
import pytest_asyncio
from core.conversation import chat_manager as chat_module
from core.conversation.chat_manager import ChatManager
from core.conversation.reply_pipeline import ReplyPipeline
from core.llm.prompt import build_decision_prompt, build_reply_prompt
from core.memory.passive_memory import PassiveMemoryManager
from core.utils.schemas import MessageData, Status, XmlLlmResult

from astrbot.api.message_components import At, Image, Plain, Reply

WAIT_STEPS = chat_module.SESSION_WAIT_STEPS


@pytest.fixture
def runtime(monkeypatch):
    # Existing boundary tests bypass elapsed time; timer tests restore the policy.
    monkeypatch.setattr(
        chat_module, "SESSION_WAIT_STEPS", tuple((n, c, 0.0) for n, c, _ in WAIT_STEPS)
    )
    monkeypatch.setattr(chat_module, "SESSION_MIN_INTERVAL", 0.0)
    histories = defaultdict(list)
    outcomes = {}
    locks = defaultdict(asyncio.Lock)

    @asynccontextmanager
    async def acquire(origin):
        async with locks[origin]:
            yield

    async def status(bot, session, ids, value):
        for mid in ids:
            outcomes[(session, mid)] = value

    async def history(bot, session, limit):
        return list(histories[session])[-limit:]

    plugin = SimpleNamespace(
        _terminated=False,
        bot_map={"bot": {"nickname": "Bot", "decision_conf": {}}},
        running_tasks={},
        parse_locks=defaultdict(asyncio.Lock),
        replying_status={},
        active_reply_counters={},
        msg_number=300,
        db=SimpleNamespace(
            chat_history_repo=SimpleNamespace(
                update_processing_status=AsyncMock(side_effect=status)
            ),
            update_message_decision=AsyncMock(),
        ),
        data_cache=SimpleNamespace(
            get_recent_message=AsyncMock(side_effect=history),
            is_bot_muted=Mock(return_value=False),
        ),
        passive_memory_manager=SimpleNamespace(reset_silence_counter=AsyncMock()),
    )
    manager = ChatManager(plugin)
    manager.decision_engine.check_whitelists = Mock(return_value=True)
    manager.decision_engine.get_trigger = Mock(return_value=False)
    manager.decision_engine.evaluate_decision = AsyncMock(
        return_value=(False, None, None, None)
    )
    manager.action_dispatcher.dispatch_actions = AsyncMock()
    manager.reply_pipeline.commit_pending_session_recalled_memories = Mock()
    monkeypatch.setattr(
        "core.conversation.chat_manager.session_lock_manager",
        SimpleNamespace(acquire_lock=acquire),
    )

    async def send(
        mid, session="group", user="user", *, text=None, chain=None, private=False
    ):
        text = mid if text is None else text
        chain = [Plain(text)] if chain is None else chain
        origin_type = "FriendMessage" if private else "GroupMessage"
        event = SimpleNamespace(
            unified_msg_origin=f"adapter:{origin_type}:{session}",
            get_group_id=lambda: "" if private else session,
            get_sender_id=lambda: user,
            get_self_id=lambda: "self",
            get_sender_name=lambda: user,
            get_messages=lambda: chain,
        )
        msg = MessageData(message_id=mid, content=text, user_id=user, nickname=user)
        async with plugin.parse_locks[f"bot:{event.unified_msg_origin}"]:
            histories[session].append(msg)
            await manager._enqueue_message(event, "bot", session, msg)
        return event

    async def drain():
        async def wait():
            while plugin.running_tasks:
                await asyncio.gather(*list(plugin.running_tasks.values()))
                await asyncio.sleep(0)

        await asyncio.wait_for(wait(), 2)

    return SimpleNamespace(
        plugin=plugin,
        manager=manager,
        send=send,
        drain=drain,
        histories=histories,
        outcomes=outcomes,
        locks=locks,
    )


@pytest_asyncio.fixture
async def scheduled_runtime(runtime, monkeypatch):
    """Run the production scheduling policy against explicit virtual deadlines."""
    clock = SimpleNamespace(now=100.0, waits=[], waiting=asyncio.Event(), deadlines={})

    async def wait_for(awaitable, timeout):
        waiter = asyncio.ensure_future(awaitable)
        expired = asyncio.get_running_loop().create_future()
        clock.deadlines[expired] = clock.now + timeout
        clock.waits.append(timeout)
        clock.waiting.set()
        try:
            done, _ = await asyncio.wait(
                (waiter, expired), return_when=asyncio.FIRST_COMPLETED
            )
            if waiter in done:
                return waiter.result()
            raise asyncio.TimeoutError
        finally:
            clock.deadlines.pop(expired, None)
            waiter.cancel()
            expired.cancel()
            await asyncio.gather(waiter, expired, return_exceptions=True)

    async def advance(seconds):
        clock.waiting.clear()
        clock.now += seconds
        for expired, deadline in list(clock.deadlines.items()):
            if deadline <= clock.now and not expired.done():
                expired.set_result(None)
        await asyncio.sleep(0)

    monkeypatch.setattr(chat_module, "SESSION_WAIT_STEPS", WAIT_STEPS)
    monkeypatch.setattr(chat_module, "SESSION_MIN_INTERVAL", 2.0)
    monkeypatch.setattr(
        chat_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock.now, time=time.time),
    )
    monkeypatch.setattr(
        chat_module,
        "asyncio",
        SimpleNamespace(**(vars(asyncio) | {"wait_for": wait_for})),
    )
    clock.advance = advance
    runtime.clock = clock
    try:
        yield runtime
    finally:
        tasks = list(runtime.plugin.running_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_no_reply_only_completes_decision_snapshot(runtime):
    started, release = asyncio.Event(), asyncio.Event()
    batches = []

    async def decide(**kwargs):
        batches.append([m.message_id for m in kwargs["pending_messages"]])
        if len(batches) == 1:
            started.set()
            await asyncio.wait_for(release.wait(), 2)
        return False, None, None, None

    runtime.manager.decision_engine.evaluate_decision.side_effect = decide
    await runtime.send("A")
    await asyncio.wait_for(started.wait(), 2)
    await runtime.send("B", user="second")
    await runtime.send("C")
    release.set()
    await runtime.drain()
    assert batches == [["A"], ["B", "C"]]
    assert set(runtime.outcomes.values()) == {"handled"}


@pytest.mark.asyncio
async def test_reply_absorbs_decision_arrivals_but_not_reply_arrivals(runtime):
    deciding, decision_release = asyncio.Event(), asyncio.Event()
    replying, reply_release = asyncio.Event(), asyncio.Event()
    batches, replies = [], []

    async def decide(**kwargs):
        batches.append([m.message_id for m in kwargs["pending_messages"]])
        if len(batches) == 1:
            deciding.set()
            await asyncio.wait_for(decision_release.wait(), 2)
        return len(batches) == 1, None, None, None

    async def reply(**kwargs):
        replies.append([m.message_id for m in kwargs["reply_messages"]])
        replying.set()
        await asyncio.wait_for(reply_release.wait(), 2)
        yield XmlLlmResult()

    runtime.manager.decision_engine.evaluate_decision.side_effect = decide
    runtime.manager.reply_pipeline.dispatch_llm_reply_loop = reply
    await runtime.send("A")
    await asyncio.wait_for(deciding.wait(), 2)
    await runtime.send("B")
    decision_release.set()
    await asyncio.wait_for(replying.wait(), 2)
    assert runtime.outcomes[("group", "B")] == "processing"
    await runtime.send("C")
    await runtime.send("D")
    reply_release.set()
    await runtime.drain()
    assert batches == [["A"], ["C", "D"]]
    assert replies == [["A", "B"]]
    assert set(runtime.outcomes.values()) == {"handled"}


@pytest.mark.asyncio
@pytest.mark.parametrize("message_time", ["2026-10-18T10:01:41", ""])
async def test_only_reply_prompt_orders_arrivals_before_previous_reply(
    runtime, message_time
):
    started, release = asyncio.Event(), asyncio.Event()
    decision_prompts, reply_prompts = [], []

    async def decide(**kwargs):
        decision_prompts.append(
            build_decision_prompt(
                user_id="user",
                group_data="",
                recent_messages=kwargs["recent_messages"],
                pending_messages=kwargs["pending_messages"],
                bot_status=None,
            )
        )
        return True, None, None, None

    async def reply(**kwargs):
        reply_prompts.append(
            build_reply_prompt(
                recent_messages=kwargs["recent_messages"],
                reply_messages=kwargs["reply_messages"],
                media_captions=[],
                bot_status=None,
            )
        )
        if len(reply_prompts) == 1:
            started.set()
            await asyncio.wait_for(release.wait(), 2)
            runtime.histories["group"].append(
                MessageData(
                    message_id="R", user_id="self", time=message_time, content="sent"
                )
            )
        yield XmlLlmResult()

    runtime.manager.decision_engine.evaluate_decision.side_effect = decide
    runtime.manager.reply_pipeline.dispatch_llm_reply_loop = reply
    await runtime.send("A")
    runtime.histories["group"][-1].time = message_time
    await asyncio.wait_for(started.wait(), 2)
    await runtime.send("B")
    runtime.histories["group"][-1].time = message_time
    release.set()
    await runtime.drain()

    for prompt, tag, history_ids, current_ids in (
        (decision_prompts[1], "pending_messages", ["A", "R"], ["B"]),
        (reply_prompts[1], "reply_messages", ["A"], ["B", "R"]),
    ):
        root = fromstring("<context>" + prompt + "</context>")
        assert [
            msg.get("message_id") for msg in root.find("recent_messages")
        ] == history_ids
        assert [msg.get("message_id") for msg in root.find(tag)] == current_ids
    assert runtime.outcomes == {("group", "A"): "handled", ("group", "B"): "handled"}


@pytest.mark.asyncio
@pytest.mark.parametrize("active_counter", [0, 1, 10])
@pytest.mark.parametrize("has_reply", [True, False])
async def test_reply_preserves_unsummarized_memory_during_active_conversation(
    runtime, active_counter, has_reply
):
    plugin = runtime.plugin
    cursor_key = "passive_memory:last_summarized_id:bot:group"
    stored = {cursor_key: 90}

    async def save(key, value):
        stored[key] = value

    plugin.passive_memory_enabled = True
    plugin.active_reply_counters["bot:group"] = active_counter
    plugin.db.get_kv_data = AsyncMock(
        side_effect=lambda key, default: stored.get(key, default)
    )
    plugin.db.upsert_kv_data = AsyncMock(side_effect=save)
    plugin.db.get_database_id_by_message_id = AsyncMock(
        side_effect=lambda **kwargs: int(kwargs["message_id"])
    )
    plugin.passive_memory_manager = PassiveMemoryManager(plugin)
    runtime.manager.decision_engine.evaluate_decision.return_value = (
        True,
        None,
        None,
        None,
    )

    async def reply(**kwargs):
        yield XmlLlmResult(msg_chains=[[Plain("reply")]] if has_reply else [])

    runtime.manager.reply_pipeline.dispatch_llm_reply_loop = reply
    await runtime.send("101")
    await runtime.send("102")
    await runtime.drain()

    # Replies never advance memory progress or skip the history they read.
    expected_cursor = 90
    assert stored[cursor_key] == expected_cursor

    await runtime.send("103")
    await runtime.drain()

    # A follow-up reply must preserve messages still waiting for summarization.
    assert stored[cursor_key] == expected_cursor
    if has_reply:
        assert stored["passive_memory:silent_count:bot:group"] == 0
        assert plugin.active_reply_counters["bot:group"] == 10
    else:
        assert stored == {cursor_key: 90}
        assert plugin.active_reply_counters["bot:group"] == active_counter


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "count,characters,expected",
    [
        (1, 0, 15.0),
        (1, 1, 15.0),
        (1, 10, 15.0),
        (1, 11, 10.0),
        (1, 30, 10.0),
        (1, 31, 6.0),
        (1, 60, 6.0),
        (1, 61, 3.0),
        (1, 100, 3.0),
        (1, 101, 1.5),
        (1, 160, 1.5),
        (1, 161, 0.8),
        (2, 0, 10.0),
        (3, 0, 6.0),
        (4, 0, 6.0),
        (5, 0, 3.0),
        (7, 0, 3.0),
        (8, 0, 1.5),
        (11, 0, 1.5),
        (12, 0, 0.8),
        (2, 161, 0.8),
        (6, 40, 3.0),
        (4, 101, 1.5),
    ],
)
async def test_dynamic_wait_uses_the_shorter_count_or_text_tier(
    scheduled_runtime, count, characters, expected
):
    runtime = scheduled_runtime
    for index in range(count):
        await runtime.send(str(index), text="字" * characters if index == 0 else "")
    await asyncio.wait_for(runtime.clock.waiting.wait(), 1)
    assert runtime.clock.waits == [pytest.approx(expected)]
    runtime.manager.decision_engine.evaluate_decision.assert_not_awaited()
    await runtime.clock.advance(expected)
    await runtime.drain()
    call = runtime.manager.decision_engine.evaluate_decision.await_args
    assert len(call.kwargs["pending_messages"]) == count
    assert set(runtime.outcomes.values()) == {"handled"}


@pytest.mark.asyncio
async def test_new_arrivals_shorten_the_first_message_deadline(scheduled_runtime):
    runtime = scheduled_runtime
    await runtime.send("A")
    await asyncio.wait_for(runtime.clock.waiting.wait(), 1)
    assert runtime.clock.waits[-1] == 15.0
    await runtime.clock.advance(4)
    await runtime.send("B")
    await asyncio.wait_for(runtime.clock.waiting.wait(), 1)
    assert runtime.clock.waits[-1] == 6.0
    await runtime.clock.advance(6)
    await runtime.drain()
    call = runtime.manager.decision_engine.evaluate_decision.await_args
    assert [m.message_id for m in call.kwargs["pending_messages"]] == ["A", "B"]


@pytest.mark.asyncio
async def test_arrivals_in_the_same_tier_never_extend_wait(scheduled_runtime):
    runtime = scheduled_runtime
    await runtime.send("A", text="字" * 40)
    await asyncio.wait_for(runtime.clock.waiting.wait(), 1)
    assert runtime.clock.waits[-1] == 6.0
    await runtime.clock.advance(5)
    await runtime.send("B")
    await asyncio.wait_for(runtime.clock.waiting.wait(), 1)
    assert runtime.clock.waits[-1] == 1.0
    await runtime.clock.advance(1)
    await runtime.drain()
    runtime.manager.decision_engine.evaluate_decision.assert_awaited_once()


@pytest.mark.asyncio
async def test_arrivals_past_the_shorter_deadline_start_immediately(scheduled_runtime):
    runtime = scheduled_runtime
    await runtime.send("A")
    await asyncio.wait_for(runtime.clock.waiting.wait(), 1)
    await runtime.clock.advance(4)
    for mid in ["B", "C", "D", "E"]:
        await runtime.send(mid)
    await runtime.drain()
    assert runtime.clock.waits == [15.0]
    call = runtime.manager.decision_engine.evaluate_decision.await_args
    assert [m.message_id for m in call.kwargs["pending_messages"]] == list("ABCDE")


@pytest.mark.asyncio
async def test_cooldown_wakes_without_another_message(scheduled_runtime):
    runtime = scheduled_runtime
    starts = []

    async def decide(**kwargs):
        starts.append(runtime.clock.now)
        return False, None, None, None

    runtime.manager.decision_engine.evaluate_decision.side_effect = decide
    await runtime.send("A", text="字" * 161)
    await asyncio.wait_for(runtime.clock.waiting.wait(), 1)
    await runtime.clock.advance(0.8)
    await runtime.drain()
    await runtime.send("B", text="字" * 161)
    await runtime.send("C")
    await asyncio.wait_for(runtime.clock.waiting.wait(), 1)
    assert runtime.clock.waits[-1] == 2.0
    assert len(starts) == 1
    await runtime.clock.advance(2)
    await runtime.drain()
    assert starts[1] - starts[0] == pytest.approx(2.0)
    calls = runtime.manager.decision_engine.evaluate_decision.await_args_list
    assert [[m.message_id for m in c.kwargs["pending_messages"]] for c in calls] == [
        ["A"],
        ["B", "C"],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["decision", "reply", "shared_lock"])
async def test_time_spent_busy_counts_toward_the_next_deadline(
    scheduled_runtime, phase
):
    runtime = scheduled_runtime
    started, release = asyncio.Event(), asyncio.Event()
    batches = []

    async def decide(**kwargs):
        batches.append([m.message_id for m in kwargs["pending_messages"]])
        if len(batches) == 1 and phase == "decision":
            started.set()
            await release.wait()
        return len(batches) == 1 and phase == "reply", None, None, None

    async def reply(**kwargs):
        started.set()
        await release.wait()
        yield XmlLlmResult()

    runtime.manager.decision_engine.evaluate_decision.side_effect = decide
    runtime.manager.reply_pipeline.dispatch_llm_reply_loop = reply
    if phase == "shared_lock":
        lock = runtime.locks["adapter:GroupMessage:group"]
        await lock.acquire()
    await runtime.send("A", text="字" * 161)
    await asyncio.wait_for(runtime.clock.waiting.wait(), 1)
    await runtime.clock.advance(0.8)
    if phase != "shared_lock":
        await asyncio.wait_for(started.wait(), 1)
    await runtime.send("B")
    await runtime.clock.advance(16)
    if phase == "shared_lock":
        lock.release()
    else:
        release.set()
    await runtime.drain()
    assert runtime.clock.waits == [pytest.approx(0.8)]
    assert batches == ([["A", "B"]] if phase == "shared_lock" else [["A"], ["B"]])


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("force_reply", [False, True])
@pytest.mark.parametrize("mentions_bot", [False, True])
async def test_private_and_mention_messages_follow_the_same_wait(
    scheduled_runtime, private, force_reply, mentions_bot
):
    runtime = scheduled_runtime
    runtime.manager.decision_engine.get_trigger.return_value = force_reply
    chain = [At(qq="self")] if mentions_bot else [Plain("你好")]
    await runtime.send("A", chain=chain, private=private)
    await asyncio.wait_for(runtime.clock.waiting.wait(), 1)
    assert runtime.clock.waits == [15.0]
    await runtime.clock.advance(15)
    await runtime.drain()
    call = runtime.manager.decision_engine.evaluate_decision.await_args
    assert call.kwargs["force_reply"] is force_reply


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chain,characters",
    [
        ([At(qq="1234567890123456789", name="很长的昵称" * 20)], 0),
        ([At(qq="self"), Plain(" 你好\n世界\t ")], 4),
        ([Reply(id="quote", chain=[Plain("引用正文" * 100)]), Plain("收到")], 2),
        ([Image(file="https://example.invalid/" + "x" * 200), Plain("看看")], 2),
        (
            [
                Plain(
                    "[@很长的昵称](mqqapi://markdown/mention?at_type=1&at_tinyid=1234567890)"
                    "收到消息了吗"
                ),
                At(qq="1234567890", name="不同的群名片"),
                Plain("收到消息了吗"),
            ],
            6,
        ),
    ],
)
async def test_wait_counts_only_original_body_characters(
    scheduled_runtime, chain, characters
):
    runtime = scheduled_runtime
    original = deepcopy(chain)
    await runtime.send("A", text="解析包装、账号和媒体转述" * 100, chain=chain)
    state = next(iter(runtime.manager.sessions.values()))
    assert state.pending[0].text_length == characters
    assert chain == original
    await asyncio.wait_for(runtime.clock.waiting.wait(), 1)
    assert runtime.clock.waits == [15.0]
    await runtime.clock.advance(15)
    await runtime.drain()


@pytest.mark.asyncio
async def test_wait_uses_combined_body_lengths_from_all_pending_messages(
    scheduled_runtime,
):
    runtime = scheduled_runtime
    for mid in ["A", "B"]:
        await runtime.send(mid, text="字" * 31)
    await asyncio.wait_for(runtime.clock.waiting.wait(), 1)
    assert runtime.clock.waits == [3.0]
    await runtime.clock.advance(3)
    await runtime.drain()


@pytest.mark.asyncio
async def test_cancelled_timer_marks_waiting_messages_interrupted(scheduled_runtime):
    runtime = scheduled_runtime
    await runtime.send("A")
    await asyncio.wait_for(runtime.clock.waiting.wait(), 1)
    task = next(iter(runtime.plugin.running_tasks.values()))
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert runtime.outcomes[("group", "A")] == "interrupted"
    assert runtime.clock.deadlines == {}
    runtime.manager.decision_engine.evaluate_decision.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_timer_runs_without_new_messages_and_ignores_legacy_settings(
    runtime, monkeypatch
):
    runtime.plugin.session_debounce_time = 999
    runtime.plugin.session_max_debounce_time = 999
    runtime.plugin.decision_interval = 999
    monkeypatch.setattr(chat_module, "SESSION_WAIT_STEPS", WAIT_STEPS)
    monkeypatch.setattr(chat_module, "SESSION_MIN_INTERVAL", 2.0)
    await runtime.send("A", text="字" * 161)
    await runtime.drain()
    assert runtime.outcomes[("group", "A")] == "handled"
    runtime.manager.decision_engine.evaluate_decision.assert_awaited_once()


@pytest.mark.asyncio
async def test_different_sessions_can_decide_simultaneously(runtime):
    first, second, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def decide(**kwargs):
        (first if kwargs["group_or_user_id"] == "one" else second).set()
        await asyncio.wait_for(release.wait(), 2)
        return False, None, None, None

    runtime.manager.decision_engine.evaluate_decision.side_effect = decide
    await runtime.send("A", "one")
    await asyncio.wait_for(first.wait(), 2)
    await runtime.send("B", "two")
    await asyncio.wait_for(second.wait(), 1)
    release.set()
    await runtime.drain()


@pytest.mark.asyncio
async def test_reminder_lock_defers_decision_until_reminder_finishes(runtime):
    lock = runtime.locks["adapter:GroupMessage:group"]
    async with lock:
        await runtime.send("A")
        await asyncio.sleep(0)
        runtime.manager.decision_engine.evaluate_decision.assert_not_awaited()
        await runtime.send("B")
    await runtime.drain()
    call = runtime.manager.decision_engine.evaluate_decision.call_args
    assert [m.message_id for m in call.kwargs["pending_messages"]] == ["A", "B"]


@pytest.mark.asyncio
async def test_large_batches_keep_unseen_messages_for_later(runtime):
    # Persisted limits from earlier releases cannot override the fixed capacity.
    runtime.plugin.batch_max_messages = 2
    messages = [f"m{index:03}" for index in range(101)]
    for mid in messages:
        await runtime.send(mid)
    await runtime.drain()
    calls = runtime.manager.decision_engine.evaluate_decision.await_args_list
    assert [[m.message_id for m in c.kwargs["pending_messages"]] for c in calls] == [
        messages[:50],
        messages[50:100],
        messages[100:],
    ]
    assert not calls[0].kwargs["recent_messages"]
    assert [m.message_id for m in calls[1].kwargs["recent_messages"]] == messages[38:50]
    assert [m.message_id for m in calls[2].kwargs["recent_messages"]] == messages[
        88:100
    ]
    assert len(runtime.outcomes) == 101
    assert set(runtime.outcomes.values()) == {"handled"}


@pytest.mark.asyncio
async def test_duplicate_platform_delivery_is_not_reprocessed(runtime):
    await runtime.send("A")
    await runtime.drain()
    await runtime.send("A")
    await runtime.drain()
    runtime.manager.decision_engine.evaluate_decision.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["decision", "reply", "partial_reply"])
async def test_failed_batch_is_not_completed_or_replayed(runtime, phase):
    started, release = asyncio.Event(), asyncio.Event()
    first = True

    async def decide(**kwargs):
        nonlocal first
        if not first:
            return False, None, None, None
        first = False
        if phase == "decision":
            started.set()
            await asyncio.wait_for(release.wait(), 2)
            raise RuntimeError("model unavailable after retries")
        return True, None, None, None

    async def reply(**kwargs):
        if phase == "partial_reply":
            yield XmlLlmResult()
        started.set()
        await asyncio.wait_for(release.wait(), 2)
        raise RuntimeError("reply failed")
        yield  # pragma: no cover

    runtime.manager.decision_engine.evaluate_decision.side_effect = decide
    runtime.manager.reply_pipeline.dispatch_llm_reply_loop = reply
    await runtime.send("A")
    await asyncio.wait_for(started.wait(), 2)
    await runtime.send("B")
    release.set()
    await runtime.drain()
    assert runtime.outcomes[("group", "A")] == (
        "partial_failed" if phase == "partial_reply" else "failed"
    )
    assert runtime.outcomes[("group", "B")] == "handled"
    assert runtime.manager.decision_engine.evaluate_decision.await_count == 2


@pytest.mark.asyncio
async def test_cancellation_marks_active_and_pending_batches_interrupted(runtime):
    started = asyncio.Event()

    async def decide(**kwargs):
        started.set()
        await asyncio.Event().wait()

    runtime.manager.decision_engine.evaluate_decision.side_effect = decide
    await runtime.send("A")
    await asyncio.wait_for(started.wait(), 2)
    await runtime.send("B")
    runtime.plugin._terminated = True
    tasks = list(runtime.plugin.running_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert runtime.outcomes == {
        ("group", "A"): "interrupted",
        ("group", "B"): "interrupted",
    }


@pytest.mark.parametrize("decision", [True, False])
def test_batch_prompt_deduplicates_history_and_retains_all_senders(decision):
    a = MessageData(message_id="A", content="first", user_id="one")
    b = MessageData(message_id="B", content="second", user_id="two")
    kwargs = {
        "user_id": "one",
        "group_data": "",
        "recent_messages": [a, b],
        "bot_status": Status(),
    }
    if decision:
        prompt = build_decision_prompt(**kwargs, pending_messages=[a, b])
        tag = "pending_messages"
    else:
        prompt = build_reply_prompt(**kwargs, media_captions=[], reply_messages=[a, b])
        tag = "reply_messages"
    assert "<recent_messages>" not in prompt
    assert f"<{tag}>" in prompt
    assert prompt.count('message_id="A"') == 1
    assert prompt.count('message_id="B"') == 1
    assert 'user_id="one"' in prompt and 'user_id="two"' in prompt


@pytest.mark.parametrize("decision", [True, False])
@pytest.mark.parametrize("cached_pending", [True, False])
def test_only_reply_prompt_interleaves_later_context_after_first_pending(
    decision, cached_pending
):
    history = MessageData(message_id="H", time="2026-10-18T10:01:22")
    a = MessageData(
        message_id="A", time="2026-10-18T10:01:41", user_id="one", content="first"
    )
    b = MessageData(
        message_id="B", time="2026-10-18T10:01:44", user_id="two", content="second"
    )
    own = MessageData(
        message_id="R", time="2026-10-18T10:02:00", user_id="self", content="sent"
    )
    log = MessageData(
        time="2026-10-18T10:02:01", role="operation_log", content="tool completed"
    )
    recent = [history, a, b, own, log] if cached_pending else [history, own, log]
    pending = [a, b]
    kwargs = {
        "user_id": "one",
        "group_data": "",
        "recent_messages": recent,
        "bot_status": None,
    }
    if decision:
        prompt = build_decision_prompt(**kwargs, pending_messages=pending)
        tag = "pending_messages"
    else:
        prompt = build_reply_prompt(**kwargs, media_captions=[], reply_messages=pending)
        tag = "reply_messages"

    root = fromstring("<context>" + prompt + "</context>")
    assert [msg.get("message_id") for msg in root.find("recent_messages")] == (
        ["H", "R", None] if decision else ["H"]
    )
    assert [msg.get("message_id") for msg in root.find(tag)] == (
        ["A", "B"] if decision else ["A", "B", "R", None]
    )
    log_tag = "recent_messages" if decision else tag
    assert root.find(f"{log_tag}/operation_log").text.strip() == "tool completed"
    assert [msg.message_id for msg in recent] == (
        ["H", "A", "B", "R", ""] if cached_pending else ["H", "R", ""]
    )
    assert pending == [a, b]


@pytest.mark.parametrize("decision", [True, False])
@pytest.mark.parametrize("message_time", ["2026-10-18T10:01:41", ""])
def test_batch_prompt_preserves_snapshot_order_and_current_copies_at_ties(
    decision, message_time
):
    history = MessageData(message_id="H", time=message_time)
    stale = MessageData(message_id="A", time=message_time, content="stale")
    current = MessageData(message_id="A", time=message_time, content="updated")
    own = MessageData(message_id="R", time=message_time, content="sent")
    kwargs = {
        "user_id": "one",
        "group_data": "",
        "recent_messages": [history, stale, own],
        "bot_status": None,
    }
    if decision:
        prompt = build_decision_prompt(**kwargs, pending_messages=[current])
        tag = "pending_messages"
    else:
        prompt = build_reply_prompt(
            **kwargs, media_captions=[], reply_messages=[current]
        )
        tag = "reply_messages"

    root = fromstring("<context>" + prompt + "</context>")
    assert [msg.get("message_id") for msg in root.find("recent_messages")] == (
        ["H", "R"] if decision else ["H"]
    )
    assert [msg.get("message_id") for msg in root.find(tag)] == (
        ["A"] if decision else ["A", "R"]
    )
    assert root.find(f"{tag}/message").text == "updated"
    assert stale.content == "stale"
    assert prompt.count('message_id="A"') == 1


@pytest.mark.parametrize("decision", [True, False])
def test_only_reply_prompt_orders_full_dates_and_timezone_offsets(decision):
    history = MessageData(message_id="H", time="2026-10-17T15:59:59+00:00")
    a = MessageData(message_id="A", time="2026-10-17T23:59:59.500+08:00")
    own = MessageData(message_id="R", time="2026-10-18T00:00:00+08:00")
    b = MessageData(message_id="B", time="2026-10-17T16:00:01+00:00")
    kwargs = {
        "user_id": "one",
        "group_data": "",
        "recent_messages": [history, own],
        "bot_status": None,
    }
    if decision:
        prompt = build_decision_prompt(**kwargs, pending_messages=[b, a])
        tag = "pending_messages"
    else:
        prompt = build_reply_prompt(**kwargs, media_captions=[], reply_messages=[b, a])
        tag = "reply_messages"

    root = fromstring("<context>" + prompt + "</context>")
    assert [msg.get("message_id") for msg in root.find("recent_messages")] == (
        ["H", "R"] if decision else ["H"]
    )
    assert [msg.get("message_id") for msg in root.find(tag)] == (
        ["B", "A"] if decision else ["A", "R", "B"]
    )


@pytest.mark.parametrize("decision", [True, False])
def test_batch_prompt_without_targets_keeps_context_as_history(decision):
    history = [
        MessageData(message_id="H", time="2026-10-18T10:01:22"),
        MessageData(message_id="R", time="2026-10-18T10:02:00", user_id="self"),
    ]
    kwargs = {
        "user_id": "one",
        "group_data": "",
        "recent_messages": history,
        "bot_status": None,
    }
    if decision:
        prompt = build_decision_prompt(**kwargs, pending_messages=[])
        tag = "pending_messages"
    else:
        prompt = build_reply_prompt(
            **kwargs, media_captions=[], remind_message="reminder"
        )
        tag = "reply_messages"

    root = fromstring("<context>" + prompt + "</context>")
    assert [msg.get("message_id") for msg in root.find("recent_messages")] == ["H", "R"]
    assert root.find(tag) is None


@pytest.mark.asyncio
async def test_tool_round_keeps_user_snapshot_and_appends_own_output():
    original = MessageData(message_id="A", content="original", user_id="user")
    later = MessageData(message_id="B", content="later", user_id="user")
    own = MessageData(message_id="R", content="already sent", user_id="self")
    plugin = SimpleNamespace(
        passive_memory_enabled=True,
        passive_memory_manager=SimpleNamespace(observe_messages=AsyncMock()),
        bot_map={"bot": {"llm_reply_conf": {"provider_ids": ["reply"]}}},
        tools_config={"max_loop": 3},
        embedding_conf={"enabled": False},
        msg_number=20,
        data_cache=SimpleNamespace(
            get_recent_message=AsyncMock(return_value=[original, later, own]),
            get_bot_status=AsyncMock(return_value=Status()),
            set_bot_status=AsyncMock(),
            get_group_profile=AsyncMock(return_value=""),
            get_user_profile_record=AsyncMock(return_value={}),
            get_user_relation=AsyncMock(return_value=None),
            build_active_user_briefs=AsyncMock(return_value=[]),
        ),
        emoji_manager=SimpleNamespace(get_random_stickers=AsyncMock(return_value=[])),
        context=SimpleNamespace(
            persona_manager=SimpleNamespace(
                get_persona_v3_by_id=lambda _: {"prompt": "persona"}
            )
        ),
        db=SimpleNamespace(
            get_relation_changes=AsyncMock(return_value=[]),
            get_relation_score=AsyncMock(return_value={}),
            slang_repo=SimpleNamespace(get_entries=AsyncMock(return_value=[])),
            update_message_reply_decision=AsyncMock(),
        ),
        call_llm=SimpleNamespace(
            call_llm_reply=AsyncMock(
                side_effect=[XmlLlmResult(tools_to_call=[("test", {})]), XmlLlmResult()]
            )
        ),
    )
    event = SimpleNamespace(
        get_self_id=lambda: "self",
        get_sender_id=lambda: "user",
        get_sender_name=lambda: "User",
        get_group_id=lambda: "",
        get_group=AsyncMock(),
    )
    pipeline = ReplyPipeline(plugin)
    pipeline.media_captioner.get_cached_media_captions = AsyncMock(return_value=[])
    pipeline.tool_executor.execute_queries = AsyncMock()
    async for _ in pipeline.dispatch_llm_reply_loop(
        event,
        "bot",
        "Bot",
        "group",
        reply_messages=[original],
        recent_messages=[original],
    ):
        pass
    prompts = [
        call.kwargs["user_prompt"]
        for call in plugin.call_llm.call_llm_reply.await_args_list
    ]
    assert len(prompts) == 2
    assert all("later" not in prompt for prompt in prompts)
    assert "already sent" in prompts[1]
    root = fromstring("<context>" + prompts[1] + "</context>")
    assert root.find("recent_messages") is None
    assert [msg.get("message_id") for msg in root.find("reply_messages")] == ["A", "R"]
    assert all(prompt.count('message_id="A"') == 1 for prompt in prompts)
    observed = [
        {msg.message_id for msg in call.args[3]}
        for call in plugin.passive_memory_manager.observe_messages.await_args_list
    ]
    assert observed == [{"A"}, {"A", "R"}]


@pytest.mark.asyncio
async def test_new_worker_starts_when_old_worker_finishes_during_enqueue(runtime):
    deciding, decision_release = asyncio.Event(), asyncio.Event()
    saving, save_release = asyncio.Event(), asyncio.Event()
    original_status = (
        runtime.plugin.db.chat_history_repo.update_processing_status.side_effect
    )

    async def decide(**kwargs):
        if kwargs["pending_messages"][0].message_id == "A":
            deciding.set()
            await asyncio.wait_for(decision_release.wait(), 2)
        return False, None, None, None

    async def save(bot, session, ids, value):
        if ids == ["B"] and value == "pending":
            saving.set()
            await asyncio.wait_for(save_release.wait(), 2)
        await original_status(bot, session, ids, value)

    runtime.manager.decision_engine.evaluate_decision.side_effect = decide
    runtime.plugin.db.chat_history_repo.update_processing_status.side_effect = save
    await runtime.send("A")
    await asyncio.wait_for(deciding.wait(), 2)
    next_arrival = asyncio.create_task(runtime.send("B"))
    await asyncio.wait_for(saving.wait(), 2)
    decision_release.set()
    await runtime.drain()
    save_release.set()
    await next_arrival
    await runtime.drain()
    assert runtime.outcomes[("group", "B")] == "handled"
    assert runtime.manager.decision_engine.evaluate_decision.await_count == 2


@pytest.mark.asyncio
async def test_context_load_failure_finishes_failed_batch_without_busy_loop(runtime):
    runtime.plugin.data_cache.get_recent_message.side_effect = RuntimeError(
        "storage unavailable"
    )
    await runtime.send("A")
    await runtime.drain()
    assert runtime.outcomes[("group", "A")] == "failed"
    runtime.manager.decision_engine.evaluate_decision.assert_not_awaited()


@pytest.mark.asyncio
async def test_absorbed_messages_respect_batch_capacity(runtime):
    started, release = asyncio.Event(), asyncio.Event()
    batches, replies = [], []
    arrivals = [f"B{index:03}" for index in range(51)]

    async def decide(**kwargs):
        batches.append([m.message_id for m in kwargs["pending_messages"]])
        if len(batches) == 1:
            started.set()
            await asyncio.wait_for(release.wait(), 2)
        return len(batches) == 1, None, None, None

    async def reply(**kwargs):
        replies.append([m.message_id for m in kwargs["reply_messages"]])
        assert not set(arrivals[49:]) & {
            m.message_id for m in kwargs["recent_messages"]
        }
        yield XmlLlmResult()

    runtime.manager.decision_engine.evaluate_decision.side_effect = decide
    runtime.manager.reply_pipeline.dispatch_llm_reply_loop = reply
    await runtime.send("A")
    await asyncio.wait_for(started.wait(), 2)
    for mid in arrivals:
        await runtime.send(mid)
    release.set()
    await runtime.drain()
    assert replies == [["A", *arrivals[:49]]]
    assert batches == [["A"], arrivals[49:]]
    assert len(runtime.outcomes) == 52
    assert set(runtime.outcomes.values()) == {"handled"}


@pytest.mark.asyncio
async def test_processing_status_migration_and_exact_batch_updates():
    import aiosqlite
    from core.database.repositories.chat_history import ChatHistoryRepository
    from core.database.schema import initialize_database

    async with aiosqlite.connect(":memory:") as conn:
        await initialize_database(conn)
        repo = ChatHistoryRepository(conn)
        for bot, group, mid in [
            ("bot", "group", "A"),
            ("bot", "group", "B"),
            ("other", "group", "A"),
        ]:
            await repo.insert_message(
                bot, MessageData(group_or_user_id=group, message_id=mid)
            )
        await repo.update_processing_status("bot", "group", ["A"], "handled")
        await repo.update_processing_status("bot", "group", ["B"], "processing")
        await initialize_database(conn)
        async with conn.execute(
            "SELECT bot_name, message_id, processing_status, reply_decision FROM chat_history ORDER BY id"
        ) as cursor:
            rows = await cursor.fetchall()
        assert rows == [
            ("bot", "A", "handled", 2),
            ("bot", "B", "interrupted", 2),
            ("other", "A", "history", 2),
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("native,expected_calls", [(False, 4), (True, 1)])
async def test_model_retry_does_not_replay_native_tool_attempts(native, expected_calls):
    from core.llm.call_llm import CallLLM

    tool_manager = SimpleNamespace(
        get_full_tool_set=lambda: SimpleNamespace(tools=[]),
        is_builtin_tool=lambda _: False,
    )
    context = SimpleNamespace(
        tool_loop_agent=AsyncMock(side_effect=RuntimeError("connection lost")),
        get_llm_tool_manager=lambda: tool_manager,
    )
    caller = CallLLM(context, Mock(), {"reply_retry_times": 2}, {}, plugin=None)
    caller._log_token_usage_safely = AsyncMock()
    event = SimpleNamespace(get_platform_name=lambda: "test")
    result = await caller.call_llm_reply(
        event,
        "group",
        ["primary", "fallback"],
        "persona",
        "input",
        use_source_tools=native,
    )
    assert result is None
    assert context.tool_loop_agent.await_count == expected_calls
