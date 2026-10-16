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

    async def run(**kwargs):
        return [
            result
            async for result in pipeline.dispatch_llm_reply_loop(
                event, "bot", "Bot", "group", recent_messages=[], **kwargs
            )
        ]

    return SimpleNamespace(
        plugin=plugin,
        cache=cache,
        pipeline=pipeline,
        search=plugin.passive_memory_manager.search_and_filter_memories,
        run=run,
    )


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
async def test_zero_injection_limit_disables_automatic_work_but_preserves_explicit_search(
    runtime,
):
    runtime.cache.merge_session_recalled_memories("bot", "group", [memory("cached")])
    runtime.plugin.embedding_conf["inject_limit"] = 0
    runtime.plugin.call_llm.call_llm_reply.side_effect = [
        XmlLlmResult(search_memories=[("group", "explicit query")]),
        XmlLlmResult(),
    ]
    runtime.search.return_value = [memory("tool-result")]
    await runtime.run(
        reply_messages=[MessageData(user_id="alice", content="automatic query")],
        relevant_memories=[memory("decision-result")],
    )
    runtime.search.assert_awaited_once()
    assert runtime.search.call_args.kwargs["query"] == "explicit query"
    prompts = [
        call.kwargs["user_prompt"]
        for call in runtime.plugin.call_llm.call_llm_reply.await_args_list
    ]
    assert all("<session_recalled_memories>" not in prompt for prompt in prompts)
    assert all("decision-result" in prompt for prompt in prompts)
    assert "tool-result" in prompts[1]
    runtime.cache.get_memories.assert_not_awaited()


@pytest.mark.asyncio
async def test_recall_survives_tool_turns_and_retired_mode_switch_is_ignored(runtime):
    runtime.plugin.embedding_conf["session_recall_enabled"] = False
    runtime.pipeline.media_captioner.get_cached_media_captions.return_value = [
        MediaCaption(hash_val="cat", caption="smirking cat")
    ]
    runtime.plugin.call_llm.call_llm_reply.side_effect = [
        XmlLlmResult(search_memories=[("group", "explicit query")]),
        XmlLlmResult(),
    ]
    runtime.search.side_effect = [[memory("automatic")], [memory("explicit")]]
    messages = [
        MessageData(message_id="a1", user_id="alice", content="[图片:cat]"),
        MessageData(message_id="b1", user_id="bob", content="[图片:missing]"),
        MessageData(message_id="a2", user_id="alice", content="again"),
    ]
    pending = []
    await runtime.run(reply_messages=messages, pending_recall_memories=pending)
    calls = runtime.search.await_args_list
    assert len(calls) == 2 and "smirking cat" in calls[0].kwargs["query"]
    assert calls[1].kwargs["query"] == "explicit query"
    prompts = [
        call.kwargs["user_prompt"]
        for call in runtime.plugin.call_llm.call_llm_reply.await_args_list
    ]
    assert all("automatic" in prompt for prompt in prompts)
    assert "explicit" in prompts[1]
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
