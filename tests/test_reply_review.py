"""Integration tests for the single review and bounded reply regeneration."""

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from xml.etree.ElementTree import fromstring

import aiosqlite
import pytest
import pytest_asyncio
from core.bot.bot_config_manager import DEFAULT_BOT_CONFIG, BotConfigManager
from core.conversation import chat_manager as chat_module
from core.conversation.chat_manager import ChatManager
from core.database.data_cache import DataCache
from core.database.database import Database
from core.database.schema import initialize_database
from core.llm.call_llm import CallLLM
from core.llm.prompt import build_decision_prompt, build_reply_review_prompt
from core.utils.schemas import (
    MediaCaption,
    MessageData,
    Status,
    TTSRequest,
    XmlLlmResult,
)
from core.web import bot_api as bot_api_module
from core.web.bot_api import BotApi

from astrbot.api.message_components import At, Image, Plain, Reply


@pytest_asyncio.fixture
async def runtime(monkeypatch, tmp_path):
    """Run the actual batch and reply pipeline against an isolated database."""
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
    monkeypatch.setattr(
        chat_module,
        "SESSION_WAIT_STEPS",
        tuple((n, c, 0) for n, c, _ in chat_module.SESSION_WAIT_STEPS),
    )
    monkeypatch.setattr(chat_module, "SESSION_MIN_INTERVAL", 0)
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await initialize_database(conn)
        db = Database(conn)
        cache = DataCache(db, None, None, msg_number=20)
        for name, value in {
            "get_bot_status": Status(),
            "get_group_profile": "",
            "get_user_profile_record": {},
            "get_user_relation": None,
            "build_active_user_briefs": [],
        }.items():
            setattr(cache, name, AsyncMock(return_value=value))
        cache.set_bot_status = AsyncMock()
        cache.is_bot_muted = Mock(return_value=False)
        plugin = SimpleNamespace(
            db=db,
            data_cache=cache,
            bot_map={
                "bot": {
                    "nickname": "Bot",
                    "decision_conf": {},
                    "llm_reply_conf": {
                        "provider_ids": ["reply"],
                        "review_provider_id": "review",
                    },
                }
            },
            tools_config={"max_loop": 3},
            embedding_conf={"enabled": False},
            msg_number=20,
            parse_locks=defaultdict(asyncio.Lock),
            replying_status={},
            active_reply_counters={},
            running_tasks={},
            _terminated=False,
            passive_memory_manager=SimpleNamespace(reset_silence_counter=AsyncMock()),
            emoji_manager=SimpleNamespace(
                get_random_stickers=AsyncMock(return_value=[])
            ),
            context=SimpleNamespace(
                persona_manager=SimpleNamespace(
                    get_persona_v3_by_id=lambda _: {"prompt": "persona"}
                )
            ),
            call_llm=SimpleNamespace(
                call_llm_reply=AsyncMock(
                    side_effect=lambda **_: XmlLlmResult(
                        msg_chains=[[Plain("draft")]], msg_texts=["draft"]
                    )
                ),
                call_llm_reply_review=AsyncMock(return_value=True),
            ),
        )
        manager = ChatManager(plugin)
        manager.decision_engine.check_whitelists = Mock(return_value=True)
        manager.decision_engine.get_trigger = Mock(return_value=False)

        async def decide(**kwargs):
            for message in kwargs["pending_messages"]:
                await db.update_message_decision(
                    "bot", "group", message.message_id, 1, 1
                )
            return True, None, None, None

        manager.decision_engine.evaluate_decision = AsyncMock(side_effect=decide)
        manager.reply_pipeline.media_captioner.get_cached_media_captions = AsyncMock(
            return_value=[]
        )
        manager.reply_pipeline.media_captioner.analyze_and_add_stickers = AsyncMock()
        manager.action_dispatcher.dispatch_actions = AsyncMock()
        counter = 0

        async def send(
            mid, *, user="user", role="message", recalled=False, enqueue=True
        ):
            nonlocal counter
            counter += 1
            event = SimpleNamespace(
                unified_msg_origin="adapter:GroupMessage:group",
                get_group_id=lambda: "group",
                get_sender_id=lambda: user,
                get_sender_name=lambda: user,
                get_self_id=lambda: "self",
                get_messages=lambda: [Plain(mid)],
                get_group=AsyncMock(return_value=""),
            )
            message = MessageData(
                group_or_user_id="group",
                message_id=mid,
                content=mid,
                user_id=user,
                nickname=user,
                role=role,
                time=(datetime(2026, 10, 23) + timedelta(seconds=counter)).isoformat(),
            )
            async with plugin.parse_locks[f"bot:{event.unified_msg_origin}"]:
                await cache.add_message("bot", "group", message)
                if recalled:
                    await db.update_message_recall("bot", "group", [mid], 1)
                    message.is_recalled = 1
                if enqueue:
                    await manager._enqueue_message(event, "bot", "group", message)
            return message

        async def drain():
            async def finish():
                while plugin.running_tasks:
                    await asyncio.gather(*list(plugin.running_tasks.values()))
                    await asyncio.sleep(0)

            await asyncio.wait_for(finish(), 3)

        yield SimpleNamespace(
            plugin=plugin, manager=manager, send=send, drain=drain, decide=decide
        )
        tasks = list(plugin.running_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize(
    "value,expected", [(None, ""), ("", ""), (" review ", "review")]
)
def test_optional_review_provider_round_trip(tmp_path, value, expected):
    manager = BotConfigManager.__new__(BotConfigManager)
    manager.config_file = tmp_path / "bots_config.json"
    assert DEFAULT_BOT_CONFIG["llm_reply_conf"]["review_provider_id"] == ""
    assert manager.save_bots(
        [{"name": "bot", "llm_reply_conf": {"review_provider_id": value}}]
    )
    assert manager.load_bots()[0]["llm_reply_conf"]["review_provider_id"] == expected


@pytest.mark.asyncio
async def test_bot_api_preserves_review_for_old_clients_but_allows_explicit_disable(
    tmp_path, monkeypatch
):
    manager = BotConfigManager.__new__(BotConfigManager)
    manager.config_file = tmp_path / "bots_config.json"
    manager.save_bots([{"name": "bot"}])
    api = BotApi(SimpleNamespace(bot_config_manager=manager, sync_bot_maps=Mock()))
    for reply_conf, expected in (
        ({"review_provider_id": "review"}, "review"),
        ({"provider_ids": ["reply"]}, "review"),
        ({"review_provider_id": ""}, ""),
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
        assert (
            manager.load_bots()[0]["llm_reply_conf"]["review_provider_id"] == expected
        )


def test_review_prompt_preserves_routing_and_excludes_other_outputs():
    original = MessageData(message_id="A", content="old <instruction>", user_id="alice")
    new = MessageData(
        message_id="B",
        content="new [图片:photo]",
        user_id="bob",
        media_id_list=["photo"],
    )
    draft = XmlLlmResult(
        msg_chains=[
            [
                Reply(id="A"),
                At(qq="alice"),
                Plain("answer <text>"),
                Image(file="secret"),
            ],
            [Image(file="sticker")],
            [Plain("second answer")],
        ],
        tts_segments=[TTSRequest(text="speech secret")],
        send_stickers=["sticker secret"],
    )
    prompt = build_reply_review_prompt(
        [original],
        [new],
        draft,
        [MediaCaption(hash_val="photo", caption="a changed destination")],
        nickname="Bot",
        self_id="self",
        session_id="group",
        group_profile="rule <text>",
    )
    root = fromstring(f"<root>{prompt}</root>")
    assert root.find("reply_messages/message").text == "old <instruction>"
    assert "a changed destination" in root.find("new_messages/message").text
    messages = root.findall("pending_reply/message")
    assert len(messages) == 2
    assert messages[0].get("quote") == "A"
    assert messages[0].find("at").get("user_id") == "alice"
    assert messages[1].text == "second answer"
    assert all(
        secret not in prompt for secret in ("speech secret", "sticker", "secret")
    )
    assert original.content == "old <instruction>"
    assert new.content == "new [图片:photo]"


def test_review_message_names_match_decision_and_preserve_nickname_fallback():
    original = MessageData(
        message_id="A",
        user_id="alice",
        nickname="Alice on platform",
        content="question",
    )
    new_messages = [
        MessageData(
            message_id="B", user_id="bob", nickname="Bob on platform", content="update"
        ),
        MessageData(
            message_id="C",
            user_id="carol",
            nickname="Carol on platform",
            content="followup",
        ),
        MessageData(
            message_id="D",
            user_id="alice",
            nickname="Alice renamed",
            content="correction",
        ),
    ]
    briefs = [
        {"user_id": "bob", "call_name": "小波"},
        {"user_id": "carol", "call_name": ""},
    ]
    decision = build_decision_prompt(
        user_id="alice",
        group_data="",
        recent_messages=[],
        pending_messages=[original, *new_messages],
        bot_status=None,
        user_profile={"call_name": "小爱"},
        active_user_briefs=briefs,
    )
    review = build_reply_review_prompt(
        [original],
        new_messages,
        XmlLlmResult(msg_chains=[[Plain("小爱和小波，收到。")]]),
        [],
        nickname="Bot",
        self_id="self",
        session_id="group",
        user_profiles=[{"user_id": "alice", "user_profile": {"call_name": "小爱"}}],
        active_user_briefs=briefs,
    )
    decision_root = fromstring(f"<root>{decision}</root>")
    review_root = fromstring(f"<root>{review}</root>")
    expected = {"A": "小爱", "B": "小波", "C": "Carol on platform", "D": "小爱"}
    assert {
        msg.get("message_id"): msg.get("nickname")
        for msg in decision_root.findall("pending_messages/message")
    } == expected
    assert {
        msg.get("message_id"): msg.get("nickname")
        for tag in ("reply_messages", "new_messages")
        for msg in review_root.findall(f"{tag}/message")
    } == expected
    assert [msg.nickname for msg in [original, *new_messages]] == [
        "Alice on platform",
        "Bob on platform",
        "Carol on platform",
        "Alice renamed",
    ]


@pytest.mark.asyncio
async def test_review_loads_call_names_for_new_participants_once(runtime):
    plugin = runtime.plugin
    names = {"alice": "小爱", "bob": "小波"}
    plugin.data_cache.get_user_profile_record.side_effect = lambda **kwargs: {
        "call_name": names.get(kwargs["user_id"], "")
    }

    async def decide(**kwargs):
        await runtime.decide(**kwargs)
        return (
            runtime.manager.decision_engine.evaluate_decision.await_count == 1,
            None,
            None,
            None,
        )

    async def generate(**kwargs):
        await runtime.send("B", user="bob")
        await runtime.send("C", user="bob")
        await runtime.send("D", user="carol")
        return XmlLlmResult(msg_chains=[[Plain("小爱，收到小波的补充了。")]])

    runtime.manager.decision_engine.evaluate_decision.side_effect = decide
    plugin.call_llm.call_llm_reply.side_effect = generate
    await runtime.send("A", user="alice")
    await runtime.drain()
    plugin.call_llm.call_llm_reply_review.assert_awaited_once()
    prompt = plugin.call_llm.call_llm_reply_review.await_args.kwargs["user_prompt"]
    root = fromstring(f"<root>{prompt}</root>")
    assert root.find("reply_messages/message").get("nickname") == "小爱"
    assert [msg.get("nickname") for msg in root.findall("new_messages/message")] == [
        "小波",
        "小波",
        "carol",
    ]
    assert [
        call.kwargs["user_id"]
        for call in plugin.data_cache.get_user_profile_record.await_args_list
    ] == ["alice", "bob", "carol"]
    stored = await plugin.db.get_messages("group", "bot")
    assert [message.nickname for message in stored] == ["alice", "bob", "bob", "carol"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "completion,expected",
    [
        ('<review pass="true" reason="unrelated"/>', True),
        ('<review pass="false" reason="cancelled"/>', False),
        ('```xml\n<review pass="true" reason="unrelated"/>\n```', True),
        ('<review pass="maybe"/>', None),
        ('<decision reply="true"/>', None),
        ("", None),
        ('<review pass="true"/><review pass="false"/>', None),
    ],
)
async def test_review_provider_calls_once_and_validates_verdict(completion, expected):
    context = SimpleNamespace(
        llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text=completion))
    )
    caller = CallLLM(context, Mock(), {}, {})
    caller._log_token_usage_safely = AsyncMock()
    assert (
        await caller.call_llm_reply_review("review", "input", "bot", "group")
        is expected
    )
    context.llm_generate.assert_awaited_once()
    assert "tools" not in context.llm_generate.await_args.kwargs
    assert (
        caller._log_token_usage_safely.await_args.kwargs["type_name"] == "reply_review"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [TimeoutError(), RuntimeError("provider unavailable")]
)
async def test_review_provider_failure_is_not_a_rejection(error):
    context = SimpleNamespace(llm_generate=AsyncMock(side_effect=error))
    caller = CallLLM(context, Mock(), {}, {})
    caller._log_token_usage_safely = AsyncMock()
    assert await caller.call_llm_reply_review("review", "input", "bot", "group") is None
    context.llm_generate.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["disabled", "no_arrivals", "native", "ignored"])
async def test_review_skip_conditions(runtime, scenario):
    plugin = runtime.plugin
    if scenario == "disabled":
        plugin.bot_map["bot"]["llm_reply_conf"]["review_provider_id"] = ""

    async def generate(**kwargs):
        if plugin.call_llm.call_llm_reply.await_count == 1:
            if scenario == "ignored":
                await runtime.send("own output", user="self", enqueue=False)
                await runtime.send("operation", role="operation_log", enqueue=False)
                await runtime.send("withdrawn", recalled=True, enqueue=False)
                await plugin.db.insert_message(
                    "other", MessageData(group_or_user_id="group", message_id="other")
                )
                await plugin.db.insert_message(
                    "bot", MessageData(group_or_user_id="other", message_id="other")
                )
            elif scenario != "no_arrivals":
                await runtime.send("B")
        return XmlLlmResult(
            msg_chains=[[Plain("draft")]],
            native_tools_called=["search_memory"] if scenario == "native" else [],
        )

    plugin.call_llm.call_llm_reply.side_effect = generate
    await runtime.send("A")
    await runtime.drain()
    plugin.call_llm.call_llm_reply_review.assert_not_awaited()
    assert runtime.manager.action_dispatcher.dispatch_actions.await_count >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", [True, None])
async def test_review_pass_preserves_next_batch_even_with_review_arrivals(
    runtime, verdict
):
    plugin = runtime.plugin

    async def generate(**kwargs):
        if plugin.call_llm.call_llm_reply.await_count == 1:
            await runtime.send("B")
        return XmlLlmResult(msg_chains=[[Plain("draft")]])

    async def review(**kwargs):
        await runtime.send("C")
        return verdict

    plugin.call_llm.call_llm_reply.side_effect = generate
    plugin.call_llm.call_llm_reply_review.side_effect = review
    await runtime.send("A")
    await runtime.drain()
    plugin.call_llm.call_llm_reply_review.assert_awaited_once()
    batches = [
        [msg.message_id for msg in call.kwargs["pending_messages"]]
        for call in runtime.manager.decision_engine.evaluate_decision.await_args_list
    ]
    assert batches == [["A"], ["B", "C"]]
    assert runtime.manager.action_dispatcher.dispatch_actions.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_again", [True, False])
async def test_rejected_draft_restores_batch_and_regenerates_without_review(
    runtime, reply_again
):
    plugin = runtime.plugin
    decisions = []

    async def decide(**kwargs):
        ids = [msg.message_id for msg in kwargs["pending_messages"]]
        decisions.append(ids)
        if len(decisions) == 2:
            async with plugin.db.query(
                "SELECT reply_decision, use_rag FROM chat_history WHERE message_id='A'"
            ) as cursor:
                row = await cursor.fetchone()
            assert tuple(row) == (2, 2)
        await runtime.decide(**kwargs)
        return (
            (len(decisions) == 1 or (len(decisions) == 2 and reply_again)),
            None,
            None,
            None,
        )

    async def generate(**kwargs):
        if plugin.call_llm.call_llm_reply.await_count == 1:
            await runtime.send("B")
            return XmlLlmResult(
                msg_chains=[[Plain("rejected")]],
                status=Status(mood="rejected"),
                add_stickers=["must not be collected"],
                tts_segments=[TTSRequest(text="must not be spoken")],
            )
        await runtime.send("D")
        return XmlLlmResult(
            msg_chains=[[Plain("accepted")]], status=Status(mood="accepted")
        )

    async def review(**kwargs):
        await runtime.send("C")
        return False

    runtime.manager.decision_engine.evaluate_decision.side_effect = decide
    plugin.call_llm.call_llm_reply.side_effect = generate
    plugin.call_llm.call_llm_reply_review.side_effect = review
    await runtime.send("A")
    await runtime.drain()
    assert decisions[:2] == [["A"], ["A", "B", "C"]]
    plugin.call_llm.call_llm_reply_review.assert_awaited_once()
    sent = runtime.manager.action_dispatcher.dispatch_actions.await_args_list
    assert len(sent) == int(reply_again)
    if reply_again:
        assert sent[0].kwargs["llm_result"].msg_chains[0][0].text == "accepted"
        prompt = plugin.call_llm.call_llm_reply.await_args.kwargs["user_prompt"]
        root = fromstring(f"<root>{prompt}</root>")
        assert [msg.get("message_id") for msg in root.find("reply_messages")] == [
            "A",
            "B",
            "C",
        ]
        assert root.find("recent_messages") is None
        assert decisions[2] == ["D"]
    assert all(
        call.kwargs["status"].mood != "rejected"
        for call in plugin.data_cache.set_bot_status.await_args_list
    )
    runtime.manager.reply_pipeline.media_captioner.analyze_and_add_stickers.assert_not_awaited()
    async with plugin.db.query("SELECT processing_status FROM chat_history") as cursor:
        assert all(row[0] == "handled" for row in await cursor.fetchall())


@pytest.mark.asyncio
async def test_full_batch_retains_conflicting_overflow_as_regeneration_context(
    runtime, monkeypatch
):
    monkeypatch.setattr(chat_module, "SESSION_BATCH_LIMIT", 2)
    plugin = runtime.plugin
    decision_contexts = []

    async def decide(**kwargs):
        decision_contexts.append(kwargs["review_context"])
        if len(decision_contexts) == 1:
            await runtime.send("B")
        await runtime.decide(**kwargs)
        return len(decision_contexts) <= 2, None, None, None

    async def generate(**kwargs):
        if plugin.call_llm.call_llm_reply.await_count == 1:
            await runtime.send("C")
        return XmlLlmResult(msg_chains=[[Plain("draft")]])

    runtime.manager.decision_engine.evaluate_decision.side_effect = decide
    plugin.call_llm.call_llm_reply.side_effect = generate
    plugin.call_llm.call_llm_reply_review.return_value = False
    await runtime.send("A")
    await runtime.drain()
    batches = [
        [msg.message_id for msg in call.kwargs["pending_messages"]]
        for call in runtime.manager.decision_engine.evaluate_decision.await_args_list
    ]
    assert batches == [["A"], ["A", "B"], ["C"]]
    assert 'message_id="C"' in decision_contexts[1]
    prompt = plugin.call_llm.call_llm_reply.await_args.kwargs["user_prompt"]
    root = fromstring(f"<root>{prompt}</root>")
    assert [msg.get("message_id") for msg in root.find("reply_messages")] == ["A", "B"]
    assert root.find("deferred_messages/message").get("message_id") == "C"
    plugin.call_llm.call_llm_reply_review.assert_awaited_once()


@pytest.mark.asyncio
async def test_tool_followup_does_not_review_after_first_turn(runtime):
    plugin = runtime.plugin

    async def generate(**kwargs):
        if plugin.call_llm.call_llm_reply.await_count == 1:
            await runtime.send("B")
            return XmlLlmResult(tools_to_call=[("search", {})])
        return XmlLlmResult(msg_chains=[[Plain("tool followup")]])

    async def decide(**kwargs):
        await runtime.decide(**kwargs)
        return (
            runtime.manager.decision_engine.evaluate_decision.await_count == 1,
            None,
            None,
            None,
        )

    runtime.manager.decision_engine.evaluate_decision.side_effect = decide
    plugin.call_llm.call_llm_reply.side_effect = generate
    await runtime.send("A")
    await runtime.drain()
    assert plugin.call_llm.call_llm_reply.await_count == 2
    plugin.call_llm.call_llm_reply_review.assert_not_awaited()


@pytest.mark.asyncio
async def test_each_new_batch_has_an_independent_review_opportunity(runtime):
    plugin = runtime.plugin

    async def generate(**kwargs):
        if plugin.call_llm.call_llm_reply.await_count < 3:
            await runtime.send(f"arrival-{plugin.call_llm.call_llm_reply.await_count}")
        return XmlLlmResult(msg_chains=[[Plain("draft")]])

    plugin.call_llm.call_llm_reply.side_effect = generate
    await runtime.send("A")
    await runtime.drain()
    assert plugin.call_llm.call_llm_reply.await_count == 3
    assert plugin.call_llm.call_llm_reply_review.await_count == 2


@pytest.mark.asyncio
async def test_review_tokens_are_separate_from_decision_and_reply(runtime):
    db = runtime.plugin.db
    await db.log_token_usage(
        "bot", "group", "reply_review", "review", "model", 10, 2, 12
    )
    stats = await db.get_token_stats(bot_name="bot")
    assert stats["summary"]["total_tokens"] == 12
    assert stats["summary"]["reply_review_tokens"] == 12
    assert stats["summary"]["reply_review_prompt_tokens"] == 10
    assert stats["summary"]["decision_tokens"] == 0
    assert stats["summary"]["reply_tokens"] == 0


@pytest.mark.asyncio
async def test_cancellation_during_review_reset_retains_all_original_messages(
    runtime, monkeypatch
):
    plugin = runtime.plugin
    resetting = asyncio.Event()
    original_update = plugin.db.update_message_decision

    async def update(bot, session, message_id, decision, rag):
        if decision == 2:
            resetting.set()
            await asyncio.Event().wait()
        await original_update(bot, session, message_id, decision, rag)

    async def generate(**kwargs):
        await runtime.send("B")
        return XmlLlmResult(msg_chains=[[Plain("draft")]])

    monkeypatch.setattr(plugin.db, "update_message_decision", update)
    plugin.call_llm.call_llm_reply.side_effect = generate
    plugin.call_llm.call_llm_reply_review.return_value = False
    await runtime.send("A")
    await asyncio.wait_for(resetting.wait(), 3)
    task = next(iter(plugin.running_tasks.values()))
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    async with plugin.db.query(
        "SELECT message_id, processing_status FROM chat_history ORDER BY id"
    ) as cursor:
        assert [tuple(row) for row in await cursor.fetchall()] == [
            ("A", "interrupted"),
            ("B", "interrupted"),
        ]
