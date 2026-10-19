"""Regression tests for per-user automatic recall and the unified session pool."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.etree.ElementTree import fromstring

import pytest

from core.conversation.media_captioner import MediaCaptioner
from core.conversation.reply_pipeline import ReplyPipeline
from core.database.data_cache import DataCache
from core.utils.schemas import MediaCaption, MessageData, XmlLlmResult


def memory(memory_id, score=0.9, text=None):
    """Create a structured retrieval result.

    Args:
        memory_id: Internal record identifier.
        score: Relevance assigned to this query.
        text: Optional narrative overriding the identifier.

    Returns:
        A memory record with scoring and lifecycle metadata.
    """
    return {
        "id": memory_id,
        "text": text or memory_id,
        "score": score,
        "metadata": {"kind": "event", "status": "active"},
    }


@pytest.fixture
def runtime():
    cache = DataCache(db=SimpleNamespace(), http_manager=None, ltm=None)
    for name, value in {
        "get_recent_message": [],
        "get_bot_status": None,
        "get_group_profile": "",
        "get_user_profile_record": {},
        "get_user_relation": None,
        "build_active_user_briefs": [],
    }.items():
        setattr(cache, name, AsyncMock(return_value=value))
    cache.set_bot_status = AsyncMock()
    cache.record_memory_hits = AsyncMock()
    cache.get_memories = AsyncMock(side_effect=AssertionError("Retired injection path"))
    plugin = SimpleNamespace(
        bot_map={"bot": {"llm_reply_conf": {"provider_ids": ["reply"]}}},
        tools_config={"max_loop": 3},
        msg_number=20,
        embedding_conf={
            "enabled": True,
            "threshold": 0.7,
            "inject_limit": 3,
            "session_recall_search_limit": 3,
        },
        rerank_conf={"enabled": True, "top_k": 3, "threshold": 0.45},
        data_cache=cache,
        passive_memory_manager=SimpleNamespace(
            search_and_filter_memories=AsyncMock(return_value=[])
        ),
        ltm=SimpleNamespace(
            rerank_memories=AsyncMock(
                side_effect=lambda **kwargs: kwargs["memories"][: kwargs["top_k"]]
            )
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
        call_llm=SimpleNamespace(call_llm_reply=AsyncMock(return_value=XmlLlmResult())),
    )
    event = SimpleNamespace(
        get_self_id=lambda: "self",
        get_sender_id=lambda: "alice",
        get_sender_name=lambda: "Alice",
        get_group_id=lambda: "group",
        get_group=AsyncMock(return_value=""),
    )
    pipeline = ReplyPipeline(plugin)
    pipeline.media_captioner.get_cached_media_captions = AsyncMock(return_value=[])

    async def run(recent_messages=None, **kwargs):
        return [
            result
            async for result in pipeline.dispatch_llm_reply_loop(
                event,
                "bot",
                "Bot",
                "group",
                recent_messages=recent_messages or [],
                **kwargs,
            )
        ]

    return SimpleNamespace(
        plugin=plugin,
        cache=cache,
        pipeline=pipeline,
        event=event,
        search=plugin.passive_memory_manager.search_and_filter_memories,
        run=run,
    )


@pytest.mark.asyncio
async def test_reply_loads_speaker_scoped_affinity_stats_and_keeps_recent_reasons(
    runtime,
):
    runtime.plugin.db.get_relation_score.return_value = {
        "total_count": 50,
        "base_relation": 5,
        "interaction_score": 70.8,
        "recent_score": 70,
        "long_term_score": 72,
        "relation": 75.8,
        "recent_count": 10,
    }
    runtime.plugin.db.get_relation_changes.return_value = [
        {
            "delta": 1,
            "reason": f"具体互动 {index}",
            "occurred_at": "2026-10-18T12:00:00+08:00",
        }
        for index in range(3)
    ]
    await runtime.run(reply_messages=[MessageData(user_id="alice", content="hello")])
    runtime.plugin.db.get_relation_changes.assert_awaited_once_with(
        bot_name="bot", group_or_user_id="group", user_id="alice", limit=3
    )
    runtime.plugin.db.get_relation_score.assert_awaited_once_with(
        bot_name="bot", group_or_user_id="group", user_id="alice"
    )
    prompt = runtime.plugin.call_llm.call_llm_reply.call_args.kwargs["user_prompt"]
    profile = fromstring("<context>" + prompt + "</context>").find("user_profile")
    assert profile.attrib["user_id"] == "alice"
    assert "近期分：70.0（最近10次）" in profile.text
    assert "长期分：72.0（共50次）" in profile.text
    assert "好感度：75.8" in profile.text
    assert "基础分：5；互动分：70.8" in profile.text
    assert all(f"具体互动 {index}" in profile.text for index in range(3))


@pytest.mark.asyncio
@pytest.mark.parametrize("brief_limit", [1, 10])
async def test_reply_includes_each_participant_profile_beyond_brief_limit(
    runtime, brief_limit
):
    users = [f"member-{index}" for index in range(12)]
    runtime.plugin.tools_config["active_user_brief_limit"] = brief_limit
    runtime.cache.build_active_user_briefs = AsyncMock(
        wraps=runtime.cache.__class__.build_active_user_briefs.__get__(runtime.cache)
    )
    runtime.cache.get_user_profile_record.side_effect = lambda **kwargs: {
        "call_name": f"称呼-{kwargs['user_id']}",
        "attitude": f"态度-{kwargs['user_id']}",
    }
    runtime.cache.get_user_relation.return_value = (50.0, "")
    runtime.plugin.db.get_relation_score.side_effect = lambda **kwargs: {
        "base_relation": users.index(kwargs["user_id"]),
        "interaction_score": 60,
        "relation": 60 + users.index(kwargs["user_id"]),
        "recent_score": 61,
        "long_term_score": 62,
        "recent_count": 10,
        "total_count": 20,
    }
    runtime.plugin.db.get_relation_changes.side_effect = lambda **kwargs: [
        {"delta": 1, "reason": f"{kwargs['user_id']} 的评价 <独立> & 明确"}
    ]
    history = MessageData(user_id="history", nickname="History", content="earlier")
    messages = [
        MessageData(message_id=uid, user_id=uid, nickname=uid, content="hello")
        for uid in users
    ] + [
        MessageData(user_id="member-0", nickname="Latest name", content="again"),
        MessageData(user_id="self", content="bot output"),
        MessageData(user_id="tool", role="operation_log", content="tool output"),
        MessageData(user_id="recalled", is_recalled=1, content="withdrawn"),
        MessageData(content="no sender"),
    ]
    originals = deepcopy(messages)
    await runtime.run(reply_messages=messages, recent_messages=[history])

    prompt = runtime.plugin.call_llm.call_llm_reply.call_args.kwargs["user_prompt"]
    root = fromstring("<context>" + prompt + "</context>")
    profiles = root.findall("user_profile")
    assert [profile.get("user_id") for profile in profiles] == users
    assert profiles[0].get("nickname") == "Latest name"
    assert "好感度标准：" not in prompt
    assert root.find("relation_score_guide") is None
    for index, profile in enumerate(profiles):
        uid = users[index]
        assert "好感度标准：" not in profile.text
        assert f"你的称呼：称呼-{uid}" in profile.text
        assert f"互动态度：态度-{uid}" in profile.text
        assert f"好感度：{60 + index}.0" in profile.text
        assert f"基础分：{index}；互动分：60.0" in profile.text
        assert "近期分：61.0（最近10次）" in profile.text
        assert "长期分：62.0（共20次）" in profile.text
        assert f"{uid} 的评价 <独立> & 明确" in profile.text
        assert all(
            f"{other} 的评价" not in profile.text for other in users if other != uid
        )
    assert [
        user.get("user_id") for user in root.findall("active_user_briefs/user")
    ] == ["history"]
    for msg in root.findall("reply_messages/message"):
        if msg.get("user_id") in users:
            assert msg.get("nickname") == f"称呼-{msg.get('user_id')}"
    for lookup in (
        runtime.plugin.db.get_relation_changes,
        runtime.plugin.db.get_relation_score,
    ):
        assert [call.kwargs["user_id"] for call in lookup.await_args_list] == users
        assert all(
            call.kwargs["bot_name"] == "bot"
            and call.kwargs["group_or_user_id"] == "group"
            for call in lookup.await_args_list
        )
    assert all(
        call.kwargs["limit"] == 3
        for call in runtime.plugin.db.get_relation_changes.await_args_list
    )
    assert messages == originals


@pytest.mark.asyncio
async def test_reply_profiles_follow_the_rendered_timeline(runtime):
    runtime.cache.get_user_relation.return_value = (50.0, "")
    earlier = MessageData(message_id="H", user_id="history", time="2026-10-18T10:00:00")
    target = MessageData(message_id="A", user_id="alice", time="2026-10-18T10:01:00")
    later = MessageData(message_id="C", user_id="carol", time="2026-10-18T10:02:00")
    own = MessageData(message_id="R", user_id="self", time="2026-10-18T10:03:00")
    history = [earlier, target, later, own]
    targets = [target]
    await runtime.run(reply_messages=targets, recent_messages=history)
    prompt = runtime.plugin.call_llm.call_llm_reply.call_args.kwargs["user_prompt"]
    root = fromstring("<context>" + prompt + "</context>")
    assert [profile.get("user_id") for profile in root.findall("user_profile")] == [
        "alice",
        "carol",
    ]
    assert [msg.get("message_id") for msg in root.find("reply_messages")] == [
        "A",
        "C",
        "R",
    ]
    assert targets == [target]
    assert history == [earlier, target, later, own]


@pytest.mark.asyncio
async def test_reminder_without_reply_messages_keeps_recipient_profile(runtime):
    runtime.cache.get_user_relation.return_value = (50.0, "")
    await runtime.run(remind_message="scheduled reminder")
    runtime.plugin.db.get_relation_score.assert_awaited_once_with(
        bot_name="bot", group_or_user_id="group", user_id="alice"
    )
    prompt = runtime.plugin.call_llm.call_llm_reply.call_args.kwargs["user_prompt"]
    root = fromstring("<context>" + prompt + "</context>")
    assert root.find("user_profile").attrib == {"user_id": "alice", "nickname": "Alice"}


@pytest.mark.asyncio
async def test_foreign_pool_memories_follow_current_participants_without_losing_cache(
    runtime,
):
    foreign = memory("foreign-alice", text="Alice's foreign fact")
    foreign["metadata"] = {
        "associated_user_ids": ["alice"],
        "source_session_id": "other-group",
        "source_label": "来源群聊：other-group",
    }
    runtime.cache.merge_session_recalled_memories(
        "bot", "group", [foreign, memory("local", text="local fact")]
    )
    await runtime.run(reply_messages=[MessageData(user_id="bob", content="hello")])
    prompt = runtime.plugin.call_llm.call_llm_reply.call_args.kwargs["user_prompt"]
    assert "Alice's foreign fact" not in prompt
    assert "local fact" in prompt
    await runtime.run(reply_messages=[MessageData(user_id="alice", content="hello")])
    prompt = runtime.plugin.call_llm.call_llm_reply.call_args.kwargs["user_prompt"]
    assert "Alice's foreign fact" in prompt
    assert "来源群聊：other-group" in prompt


@pytest.mark.asyncio
async def test_reply_keeps_all_profiles_across_tool_turns_without_new_arrivals(runtime):
    runtime.cache.get_user_relation.return_value = (50.0, "")
    runtime.plugin.call_llm.call_llm_reply.side_effect = [
        XmlLlmResult(tools_to_call=[("test", {})]),
        XmlLlmResult(),
    ]
    runtime.pipeline.tool_executor.execute_queries = AsyncMock()
    runtime.cache.get_recent_message.return_value = [
        MessageData(message_id="C", user_id="carol", content="new arrival"),
        MessageData(message_id="R", user_id="self", content="bot output"),
    ]
    targets = [
        MessageData(message_id="A", user_id="alice", content="first"),
        MessageData(message_id="B", user_id="bob", content="second"),
    ]
    await runtime.run(reply_messages=targets, recent_messages=list(targets))
    prompts = [
        fromstring("<context>" + call.kwargs["user_prompt"] + "</context>")
        for call in runtime.plugin.call_llm.call_llm_reply.await_args_list
    ]
    assert len(prompts) == 2
    for root in prompts:
        assert [profile.get("user_id") for profile in root.findall("user_profile")] == [
            "alice",
            "bob",
        ]
        assert all(msg.get("user_id") != "carol" for msg in root.iter("message"))
    assert [msg.get("message_id") for msg in prompts[1].find("reply_messages")] == [
        "A",
        "B",
        "R",
    ]


@pytest.mark.asyncio
async def test_reply_observes_configured_backfill_and_current_batch(runtime):
    backfill = [
        MessageData(message_id=f"history-{i}", user_id="alice", content=f"past {i}")
        for i in range(20)
    ]
    wakeup = MessageData(message_id="wakeup", user_id="alice", content="hello")
    runtime.cache.get_recent_message.return_value = backfill
    runtime.plugin.passive_memory_enabled = True
    observe = AsyncMock()
    runtime.plugin.passive_memory_manager.observe_messages = observe
    async for _ in runtime.pipeline.dispatch_llm_reply_loop(
        runtime.event, "bot", "Bot", "group", reply_messages=[wakeup]
    ):
        pass
    runtime.cache.get_recent_message.assert_awaited_once_with("bot", "group", 20)
    observe.assert_awaited_once_with("bot", "group", "self", [*backfill, wakeup])


@pytest.mark.asyncio
async def test_interleaved_users_search_separately_with_caption_only_and_original_order(
    runtime,
):
    messages = [
        MessageData(user_id="alice", nickname="Alice", content="first thought"),
        MessageData(user_id="bob", nickname="Bob", content="different topic"),
        MessageData(
            user_id="alice",
            nickname="Alice",
            content='@Bob <quote sender_id="bob">previous reply</quote> [图片:cat]',
            media_id_list=["cat"],
        ),
        MessageData(user_id="carol", nickname="Carol", content="[图片:missing]"),
    ]
    original = deepcopy(messages)
    captions = [
        MediaCaption(
            hash_val="cat",
            caption="a cat smirking",
            text="OCR excluded",
            genre="genre excluded",
            character="character excluded",
            source="source excluded",
        )
    ]
    runtime.search.side_effect = [[memory("alice-memory")], [memory("bob-memory")]]
    await runtime.pipeline._search_current_recall_memories(
        "bot", "group", messages, None, [], media_captions=captions
    )

    calls = runtime.search.await_args_list
    assert len(calls) == 2
    first, second = [call.kwargs["query"] for call in calls]
    assert first.index("first thought") < first.index("a cat smirking")
    assert "previous reply" in first and "@Bob" in first
    assert "different topic" not in first
    assert second == "Bob (bob): different topic"
    assert not any(
        token in first
        for token in (
            "OCR excluded",
            "genre excluded",
            "character excluded",
            "source excluded",
            "[图片:",
        )
    )
    assert [
        call.kwargs["query"]
        for call in runtime.plugin.ltm.rerank_memories.await_args_list
    ] == [
        first,
        second,
    ]
    assert all(call.kwargs["recent_messages"] == messages for call in calls)
    assert [call.kwargs["user_ids"] for call in calls] == [["alice"], ["bob"]]
    assert all(call.kwargs["threshold"] == 0.7 for call in calls)
    assert messages == original


@pytest.mark.asyncio
@pytest.mark.parametrize("media_type", ["图片", "语音", "视频", "合并转发"])
@pytest.mark.parametrize(
    "caption_state", ["missing", "empty", "unfinished", "text-only"]
)
async def test_media_without_a_completed_nonempty_caption_never_triggers_search(
    runtime, media_type, caption_state
):
    captions = []
    if caption_state != "missing":
        captions = [
            MediaCaption(
                hash_val="media",
                caption="description" if caption_state == "unfinished" else "   ",
                text="transcript without a caption",
                is_captioned=caption_state != "unfinished",
            )
        ]
    result = await runtime.pipeline._search_current_recall_memories(
        "bot",
        "group",
        [
            MessageData(
                user_id="alice", nickname="Alice", content=f"[{media_type}:media]"
            )
        ],
        None,
        [],
        media_captions=captions,
    )
    assert result == []
    runtime.search.assert_not_awaited()
    runtime.plugin.ltm.rerank_memories.assert_not_awaited()
    runtime.cache.record_memory_hits.assert_not_awaited()


@pytest.mark.asyncio
async def test_uncaptioned_media_does_not_discard_plain_text_from_the_same_user(
    runtime,
):
    await runtime.pipeline._search_current_recall_memories(
        "bot",
        "group",
        [
            MessageData(user_id="alice", nickname="Alice", content="[图片:missing]"),
            MessageData(user_id="alice", nickname="Alice", content="hello [语音]"),
        ],
        None,
        [],
    )
    runtime.search.assert_awaited_once()
    assert runtime.search.call_args.kwargs["query"] == "Alice (alice): hello"


@pytest.mark.asyncio
async def test_collected_sticker_annotation_cannot_turn_an_empty_caption_into_a_query(
    runtime,
):
    runtime.plugin.conf = {}
    media = MediaCaption(hash_val="saved", text="OCR without a caption", caption="")
    runtime.cache.get_caption_by_hash = AsyncMock(return_value=media)
    runtime.plugin.emoji_manager.has_sticker = AsyncMock(return_value=True)
    messages = [MessageData(user_id="alice", content="[图片:saved]")]
    captions = await MediaCaptioner(runtime.plugin).get_cached_media_captions(
        "bot", messages
    )
    assert captions == [media] and media.caption == ""
    await runtime.pipeline._search_current_recall_memories(
        "bot", "group", messages, None, [], media_captions=captions
    )
    runtime.search.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("rerank", [True, False])
async def test_cross_user_hits_are_deduplicated_ranked_limited_and_counted_once(
    runtime, rerank
):
    runtime.plugin.embedding_conf["inject_limit"] = 2
    runtime.plugin.rerank_conf["enabled"] = rerank
    first = [memory("shared", 0.7), memory("alice", 0.6)]
    second = [memory("shared", 0.98), memory("bob", 0.9)]
    if not rerank:
        for record, distance in zip([*first, *second], [0.3, 0.4, 0.02, 0.1]):
            record.pop("score")
            record["_distance"] = distance
    runtime.search.side_effect = [first, second]
    results = await runtime.pipeline._search_current_recall_memories(
        "bot",
        "group",
        [
            MessageData(user_id="alice", content="one"),
            MessageData(user_id="bob", content="two"),
        ],
        None,
        [],
    )
    assert results == second
    runtime.cache.record_memory_hits.assert_awaited_once_with(second)
    runtime.pipeline.commit_pending_session_recalled_memories("bot", "group", results)
    assert results == []
    pooled = runtime.cache.get_session_recalled_memories("bot", "group")
    assert {record.memory_id for record in pooled} == {"shared", "bob"}
    assert all(record.hit_count == 1 for record in pooled)


@pytest.mark.asyncio
async def test_hit_statistics_failure_does_not_prevent_reply_recall(runtime):
    record = memory("remembered")
    runtime.search.return_value = [record]
    runtime.cache.record_memory_hits.side_effect = RuntimeError(
        "statistics unavailable"
    )
    assert await runtime.pipeline._search_current_recall_memories(
        "bot", "group", [MessageData(content="hello")], None, []
    ) == [record]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reminder,expected",
    [("drink water [图片:missing]", "drink water"), ("[图片:missing]", None)],
)
async def test_reminder_recall_also_skips_uncaptioned_media(
    runtime, reminder, expected
):
    await runtime.pipeline._search_current_recall_memories(
        "bot", "group", [], reminder, []
    )
    if expected is None:
        runtime.search.assert_not_awaited()
    else:
        runtime.search.assert_awaited_once()
        assert runtime.search.call_args.kwargs["query"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("current_count", [0, 1, 3])
async def test_current_and_cached_automatic_memories_share_one_prompt_budget(
    runtime, current_count
):
    runtime.plugin.embedding_conf["inject_limit"] = 2
    runtime.cache.merge_session_recalled_memories(
        "bot", "group", [memory("older-one"), memory("older-two")]
    )
    current = [
        memory(f"current-{index}", 0.99 - index * 0.01)
        for index in range(current_count)
    ]
    runtime.search.return_value = current
    pending = []
    await runtime.run(
        reply_messages=[MessageData(user_id="alice", content="hello")],
        relevant_memories=[memory("explicit")],
        pending_recall_memories=pending,
    )
    prompt = runtime.plugin.call_llm.call_llm_reply.call_args.kwargs["user_prompt"]
    root = fromstring("<context>" + prompt + "</context>")
    automatic = root.findall("session_recalled_memories/memory")
    assert len(automatic) == 2
    assert [node.text for node in automatic[: min(current_count, 2)]] == [
        record["text"] for record in current[:2]
    ]
    assert root.find("relevant_memories/memory").text == "explicit"
    assert root.find("long_memories") is None
    runtime.cache.get_memories.assert_not_awaited()
    assert {
        record.memory_id
        for record in runtime.cache.get_session_recalled_memories("bot", "group")
    } == {"older-one", "older-two"}


@pytest.mark.asyncio
async def test_zero_injection_limit_disables_automatic_work_but_recognizes_native_tools(
    runtime,
):
    runtime.cache.merge_session_recalled_memories("bot", "group", [memory("cached")])
    runtime.plugin.embedding_conf["inject_limit"] = 0
    runtime.plugin.call_llm.call_llm_reply.return_value = XmlLlmResult(
        native_tools_called=["search_memory"]
    )
    await runtime.run(
        reply_messages=[MessageData(user_id="alice", content="automatic query")],
        relevant_memories=[memory("decision-result")],
    )
    runtime.search.assert_not_awaited()
    runtime.plugin.db.update_message_reply_decision.assert_not_awaited()
    prompts = [
        call.kwargs["user_prompt"]
        for call in runtime.plugin.call_llm.call_llm_reply.await_args_list
    ]
    assert all("<session_recalled_memories>" not in prompt for prompt in prompts)
    assert all("decision-result" in prompt for prompt in prompts)
    assert len(prompts) == 1
    runtime.cache.get_memories.assert_not_awaited()


@pytest.mark.asyncio
async def test_recall_survives_tool_turns_and_retired_mode_switch_is_ignored(runtime):
    runtime.plugin.embedding_conf["session_recall_enabled"] = False
    runtime.pipeline.media_captioner.get_cached_media_captions.return_value = [
        MediaCaption(hash_val="cat", caption="smirking cat")
    ]
    runtime.plugin.call_llm.call_llm_reply.side_effect = [
        XmlLlmResult(tools_to_call=[("other_tool", {})]),
        XmlLlmResult(),
    ]
    runtime.search.return_value = [memory("automatic")]
    messages = [
        MessageData(message_id="a1", user_id="alice", content="[图片:cat]"),
        MessageData(message_id="b1", user_id="bob", content="[图片:missing]"),
        MessageData(message_id="a2", user_id="alice", content="again"),
    ]
    pending = []
    await runtime.run(reply_messages=messages, pending_recall_memories=pending)
    calls = runtime.search.await_args_list
    assert len(calls) == 1 and "smirking cat" in calls[0].kwargs["query"]
    prompts = [
        call.kwargs["user_prompt"]
        for call in runtime.plugin.call_llm.call_llm_reply.await_args_list
    ]
    assert all("automatic" in prompt for prompt in prompts)
    assert len(prompts) == 2
    for prompt in prompts:
        assert (
            prompt.index('message_id="a1"')
            < prompt.index('message_id="b1"')
            < prompt.index('message_id="a2"')
        )
    assert runtime.cache.get_session_recalled_memories("bot", "group") == []
    runtime.pipeline.commit_pending_session_recalled_memories("bot", "group", pending)
    assert [
        record.memory_id
        for record in runtime.cache.get_session_recalled_memories("bot", "group")
    ] == ["automatic"]


@pytest.mark.asyncio
async def test_failed_reply_does_not_commit_automatic_recalls(runtime):
    runtime.search.return_value = [memory("automatic")]
    runtime.plugin.call_llm.call_llm_reply.side_effect = RuntimeError("reply failed")
    pending = []
    with pytest.raises(RuntimeError, match="reply failed"):
        await runtime.run(
            reply_messages=[MessageData(user_id="alice", content="hello")],
            pending_recall_memories=pending,
        )
    assert [record["id"] for record in pending] == ["automatic"]
    assert runtime.cache.get_session_recalled_memories("bot", "group") == []


@pytest.mark.asyncio
async def test_disabled_embeddings_never_inject_the_pool_or_recent_memories(runtime):
    runtime.cache.merge_session_recalled_memories("bot", "group", [memory("cached")])
    runtime.plugin.embedding_conf["enabled"] = False
    await runtime.run(reply_messages=[MessageData(user_id="alice", content="hello")])
    runtime.search.assert_not_awaited()
    prompt = runtime.plugin.call_llm.call_llm_reply.call_args.kwargs["user_prompt"]
    assert (
        "<session_recalled_memories>" not in prompt and "<long_memories>" not in prompt
    )
    runtime.cache.get_memories.assert_not_awaited()
