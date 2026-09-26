"""Behavioral tests for batch boundaries, timers, and failure outcomes."""

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from core.conversation.chat_manager import ChatManager
from core.conversation.reply_pipeline import ReplyPipeline
from core.llm.prompt import build_decision_prompt, build_reply_prompt
from core.memory.passive_memory import PassiveMemoryManager
from core.utils.schemas import MessageData, Status, XmlLlmResult

from astrbot.api.message_components import Plain


@pytest.fixture
def runtime(monkeypatch):
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
        session_debounce_time=0,
        session_max_debounce_time=0.1,
        decision_interval=0,
        batch_max_messages=50,
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
        passive_memory_manager=SimpleNamespace(mark_silence_summary_armed=AsyncMock()),
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

    async def send(mid, session="group", user="user"):
        event = SimpleNamespace(
            unified_msg_origin=f"adapter:GroupMessage:{session}",
            get_group_id=lambda: session,
            get_sender_id=lambda: user,
            get_self_id=lambda: "self",
            get_sender_name=lambda: user,
        )
        msg = MessageData(message_id=mid, content=mid, user_id=user, nickname=user)
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
    plugin.session_debounce_time = 0.01
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

    # Only an idle wake-up may skip history before the first batch member.
    expected_cursor = 100 if has_reply and active_counter == 0 else 90
    assert stored[cursor_key] == expected_cursor

    await runtime.send("103")
    await runtime.drain()

    # A follow-up reply must preserve messages still waiting for summarization.
    assert stored[cursor_key] == expected_cursor
    if has_reply:
        assert stored["passive_memory:silence_armed:bot:group"] == 1
        assert stored["passive_memory:silent_count:bot:group"] == 0
        assert plugin.active_reply_counters["bot:group"] == 10
    else:
        assert stored == {cursor_key: 90}
        assert plugin.active_reply_counters["bot:group"] == active_counter


@pytest.mark.asyncio
async def test_cooldown_wakes_without_another_message(runtime):
    runtime.plugin.decision_interval = 0.05
    await runtime.send("A")
    await runtime.drain()
    await runtime.send("B")
    await runtime.send("C")
    assert runtime.manager.decision_engine.evaluate_decision.await_count == 1
    await runtime.drain()
    calls = runtime.manager.decision_engine.evaluate_decision.await_args_list
    assert [[m.message_id for m in c.kwargs["pending_messages"]] for c in calls] == [
        ["A"],
        ["B", "C"],
    ]


@pytest.mark.asyncio
async def test_maximum_debounce_prevents_continuous_arrival_starvation(runtime):
    runtime.plugin.session_debounce_time = 0.1
    runtime.plugin.session_max_debounce_time = 0.04
    started = asyncio.Event()

    async def decide(**kwargs):
        started.set()
        return False, None, None, None

    runtime.manager.decision_engine.evaluate_decision.side_effect = decide
    await runtime.send("A")
    for i in range(4):
        await asyncio.sleep(0.01)
        await runtime.send(str(i))
    await asyncio.wait_for(started.wait(), 0.05)
    await runtime.drain()


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
    runtime.plugin.batch_max_messages = 2
    runtime.plugin.session_debounce_time = 0.01
    for mid in ["A", "B", "C", "D", "E"]:
        await runtime.send(mid)
    await runtime.drain()
    calls = runtime.manager.decision_engine.evaluate_decision.await_args_list
    assert [[m.message_id for m in c.kwargs["pending_messages"]] for c in calls] == [
        ["A", "B"],
        ["C", "D"],
        ["E"],
    ]
    assert not calls[0].kwargs["recent_messages"]
    assert [m.message_id for m in calls[1].kwargs["recent_messages"]] == ["A", "B"]
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


@pytest.mark.asyncio
async def test_tool_round_keeps_user_snapshot_and_appends_own_output():
    original = MessageData(message_id="A", content="original", user_id="user")
    later = MessageData(message_id="B", content="later", user_id="user")
    own = MessageData(message_id="R", content="already sent", user_id="self")
    plugin = SimpleNamespace(
        bot_map={"bot": {"llm_reply_conf": {"provider_ids": ["reply"]}}},
        tools_config={"max_loop": 3},
        embedding_conf={"enabled": False, "session_recall_enabled": False},
        msg_number=20,
        get_caption_config=lambda _: {},
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
    assert all(prompt.count('message_id="A"') == 1 for prompt in prompts)


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
    runtime.plugin.batch_max_messages = 2
    started, release = asyncio.Event(), asyncio.Event()
    batches, replies = [], []

    async def decide(**kwargs):
        batches.append([m.message_id for m in kwargs["pending_messages"]])
        if len(batches) == 1:
            started.set()
            await asyncio.wait_for(release.wait(), 2)
        return len(batches) == 1, None, None, None

    async def reply(**kwargs):
        replies.append([m.message_id for m in kwargs["reply_messages"]])
        assert "C" not in [m.message_id for m in kwargs["recent_messages"]]
        yield XmlLlmResult()

    runtime.manager.decision_engine.evaluate_decision.side_effect = decide
    runtime.manager.reply_pipeline.dispatch_llm_reply_loop = reply
    await runtime.send("A")
    await asyncio.wait_for(started.wait(), 2)
    await runtime.send("B")
    await runtime.send("C")
    release.set()
    await runtime.drain()
    assert replies == [["A", "B"]]
    assert batches == [["A"], ["C"]]


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
