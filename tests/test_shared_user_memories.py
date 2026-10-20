"""Exercise cross-conversation user recall against SQLite and LanceDB."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from xml.etree.ElementTree import fromstring

import lancedb
import pytest
import pytest_asyncio
from core.conversation.memory_recall import search_memories_with_rerank
from core.conversation.reply_pipeline import ReplyPipeline
from core.database.data_cache import DataCache
from core.database.database import Database
from core.llm.native_tools import SearchMemoryTool
from core.llm.prompt import build_reply_prompt
from core.memory.memory import LTM, get_memory_schema
from core.memory.passive_memory import PassiveMemoryManager
from core.utils.schemas import MessageData


@pytest_asyncio.fixture(params=[False, True], ids=["local", "external"])
async def runtime(tmp_path, monkeypatch, request):
    monkeypatch.setattr(
        "astrbot.api.star.StarTools.get_data_dir", lambda *args: tmp_path
    )
    db = await Database.connect()
    ltm = LTM.__new__(LTM)
    ltm.embedding_conf = {"enabled": True, "use_external_provider": request.param}
    ltm._lazy_init = Mock()
    ltm.embed_model = SimpleNamespace(embed=lambda _: [[0.0, 0.0]])
    ltm.embed_provider = SimpleNamespace(
        get_embedding=AsyncMock(return_value=[0.0, 0.0])
    )
    ltm.db = lancedb.connect(tmp_path / "vectors")
    ltm.table = ltm.db.create_table("memories", schema=get_memory_schema(2))
    plugin = SimpleNamespace(
        db=db,
        ltm=ltm,
        embedding_conf={
            "enabled": True,
            "threshold": 1.0,
            "limit": 20,
            "inject_limit": 10,
        },
        rerank_conf={"enabled": False},
        msg_number=20,
    )
    plugin.data_cache = DataCache(db, None, ltm, plugin=plugin)
    plugin.data_cache.get_recent_message = AsyncMock(return_value=[])
    plugin.passive_memory_manager = PassiveMemoryManager(plugin)

    async def seed(
        memory_id,
        users=None,
        *,
        session="other-group",
        bot="bot",
        distance=0.2,
        metadata=None,
        indexed=True,
    ):
        saved = (
            metadata if metadata is not None else {"associated_user_ids": users or []}
        )
        encoded = saved if isinstance(saved, str) else json.dumps(saved)
        await db.conn.execute(
            "INSERT INTO memories (bot_name, group_or_user_id, memory_id, text, metadata, revision, indexed_revision) "
            "VALUES (?, ?, ?, ?, ?, 1, ?)",
            (bot, session, memory_id, f"fact {memory_id} <&>", encoded, int(indexed)),
        )
        await db.conn.commit()
        ltm.table.add(
            [
                {
                    "id": memory_id,
                    "bot_name": bot,
                    "group_or_user_id": session,
                    "text": "outdated vector text",
                    "vector": [distance, 0.0],
                    "metadata": "{}",
                    "created_at": "2026-10-20T00:00:00+00:00",
                    "updated_at": "2026-10-20T00:00:00+00:00",
                }
            ]
        )

    try:
        yield SimpleNamespace(
            plugin=plugin,
            db=db,
            manager=plugin.passive_memory_manager,
            cache=plugin.data_cache,
            ltm=ltm,
            seed=seed,
        )
    finally:
        await plugin.passive_memory_manager.close()
        await db.close()


@pytest.mark.asyncio
async def test_user_and_revision_filters_apply_before_vector_limit(runtime):
    rt = runtime
    await rt.seed("wrong-user", ["bob"], session="current", distance=0.0)
    await rt.seed("wrong-bot", ["alice"], bot="other", distance=0.0)
    await rt.seed("unindexed", ["alice"], distance=0.0, indexed=False)
    await rt.seed("foreign-legacy", distance=0.0)
    await rt.seed("deleted", ["alice"], distance=0.0)
    await rt.db.conn.execute("DELETE FROM memories WHERE memory_id='deleted'")
    await rt.db.conn.commit()
    await rt.seed(
        "alice's-memory",
        ["alice"],
        distance=0.4,
        metadata={
            "associated_user_ids": ["alice"],
            "occurred_at": "2026-10-18T12:00:00+08:00",
            "evidence": [{"source_id": 17, "quote": "original evidence"}],
        },
    )
    result = await rt.manager.search_and_filter_memories(
        "bot", "current", "topic", [MessageData(user_id="alice")], limit=1
    )
    assert [item["memory_id"] for item in result] == ["alice's-memory"]
    assert result[0]["text"] == "fact alice's-memory <&>"
    assert result[0]["group_or_user_id"] == "other-group"
    metadata = json.loads(result[0]["metadata"])
    assert metadata["evidence"] == [{"source_id": 17, "quote": "original evidence"}]
    assert metadata["source_label"] == "来源群聊：other-group"
    assert metadata["source_session_id"] == "other-group"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope,expected",
    [
        ("session", {"local-alice", "local-bob", "local-legacy"}),
        ("user", {"local-alice", "foreign-alice", "local-legacy"}),
        (
            "all",
            {
                "local-alice",
                "local-bob",
                "local-legacy",
                "foreign-alice",
                "foreign-bob",
                "foreign-legacy",
            },
        ),
        ("invalid", {"local-alice", "foreign-alice", "local-legacy"}),
        (None, {"local-alice", "foreign-alice", "local-legacy"}),
    ],
)
async def test_shared_search_applies_scope_and_keeps_bot_and_index_isolation(
    runtime, scope, expected
):
    rt = runtime
    rt.plugin.memory_recall_scope = scope
    for memory_id, users, session in [
        ("local-alice", ["alice"], "current"),
        ("local-bob", ["bob"], "current"),
        ("local-legacy", [], "current"),
        ("foreign-alice", ["alice"], "other"),
        ("foreign-bob", ["bob"], "other"),
        ("foreign-legacy", [], "other"),
    ]:
        await rt.seed(memory_id, users, session=session)
    await rt.seed("wrong-bot", ["alice"], bot="other", distance=0.0)
    await rt.seed("unindexed", ["alice"], indexed=False, distance=0.0)
    await rt.seed("deleted", ["alice"], distance=0.0)
    await rt.db.conn.execute("DELETE FROM memories WHERE memory_id='deleted'")
    await rt.db.conn.commit()

    result = await search_memories_with_rerank(
        rt.plugin,
        bot_name="bot",
        group_or_user_id="current",
        query="topic",
        recent_messages=[MessageData(user_id="alice")],
    )

    assert {item["memory_id"] for item in result} == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["session", "all"])
@pytest.mark.parametrize("users", [["alice"], ["unknown"], []])
async def test_explicit_users_only_narrow_session_and_full_retrieval(
    runtime, scope, users
):
    rt = runtime
    rt.plugin.memory_recall_scope = scope
    await rt.seed("local-alice", ["alice"], session="current", distance=0.4)
    await rt.seed("foreign-alice", ["alice"], distance=0.2)
    await rt.seed("local-bob", ["bob"], session="current", distance=0.0)
    await rt.seed("local-legacy", session="current", distance=0.0)
    await rt.seed("other-bot", ["alice"], bot="other", distance=0.0)

    result = await rt.manager.search_and_filter_memories(
        "bot", "current", "topic", [], user_ids=users, limit=1
    )

    expected = []
    if users == ["alice"]:
        expected = ["local-alice" if scope == "session" else "foreign-alice"]
    assert [item["memory_id"] for item in result] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["session", "user", "all"])
async def test_administrative_session_search_remains_local_in_every_mode(
    runtime, scope
):
    rt = runtime
    rt.plugin.memory_recall_scope = scope
    await rt.seed("local", ["alice"], session="current")
    await rt.seed("foreign", ["alice"])

    result = await rt.manager.search_and_filter_memories(
        "bot", "current", "topic", filter_active_users=False
    )

    assert [item["memory_id"] for item in result] == ["local"]
    rt.cache.get_recent_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_association_matches_any_exact_user_and_keeps_bot_isolation(runtime):
    rt = runtime
    await rt.seed("shared", ["alice", "bob"])
    await rt.seed("numeric", [123])
    await rt.seed("prefix", ["alice-2"])
    await rt.seed("other-bot", ["alice"], bot="other")
    for users, expected in [
        (["alice"], {"shared"}),
        (["bob", "123"], {"shared", "numeric"}),
        (["' OR 1=1 --"], set()),
    ]:
        result = await rt.manager.search_and_filter_memories(
            "bot", "current", "topic", [], user_ids=users
        )
        assert {item["memory_id"] for item in result} == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"user_id": "alice"},
        {"associated_user_ids": None},
        {"associated_user_ids": "alice"},
        [],
        "broken json",
    ],
)
async def test_unassociated_or_malformed_legacy_memories_remain_local(
    runtime, metadata
):
    rt = runtime
    await rt.seed("local", session="current", metadata=metadata)
    await rt.seed("foreign", metadata=metadata)
    result = await rt.manager.search_and_filter_memories(
        "bot", "current", "topic", [], user_ids=["alice"]
    )
    assert [item["memory_id"] for item in result] == ["local"]
    assert json.loads(result[0]["metadata"])["source_label"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [{"associated_user_ids": None}, {"associated_user_ids": 123}, "broken json"],
)
async def test_full_retrieval_accepts_foreign_legacy_metadata(runtime, metadata):
    rt = runtime
    rt.plugin.memory_recall_scope = "all"
    await rt.seed("foreign", metadata=metadata)

    result = await rt.manager.search_and_filter_memories("bot", "current", "topic", [])

    assert [item["memory_id"] for item in result] == ["foreign"]
    assert json.loads(result[0]["metadata"])["source_label"] == "来源群聊：other-group"


@pytest.mark.asyncio
async def test_empty_targets_and_unfiltered_legacy_caller_cannot_search_all_users(
    runtime,
):
    rt = runtime
    await rt.seed("alice", ["alice"], session="current")
    await rt.seed("other-alice", ["alice"])
    await rt.seed("local-legacy", session="current")
    result = await rt.manager.search_and_filter_memories(
        "bot", "current", "topic", [], user_ids=[]
    )
    assert [item["memory_id"] for item in result] == ["local-legacy"]
    result = await rt.manager.search_and_filter_memories(
        "bot", "current", "topic", [], filter_active_users=False
    )
    assert {item["memory_id"] for item in result} == {"alice", "local-legacy"}
    rt.cache.get_recent_message.return_value = [
        MessageData(user_id="alice", role="operation_log"),
        MessageData(user_id="alice", is_recalled=1),
    ]
    result = await rt.manager.search_and_filter_memories("bot", "current", "topic")
    assert [item["memory_id"] for item in result] == ["local-legacy"]


@pytest.mark.asyncio
async def test_rehydration_rechecks_user_scope_after_a_concurrent_edit(runtime):
    rt = runtime
    await rt.seed("changed", ["alice"])
    search = rt.ltm.search_memory

    async def edit_after_search(**kwargs):
        result = await search(**kwargs)
        await rt.db.conn.execute(
            "UPDATE memories SET metadata=?, revision=2, indexed_revision=2 WHERE memory_id='changed'",
            (json.dumps({"associated_user_ids": ["bob"]}),),
        )
        await rt.db.conn.commit()
        return result

    rt.ltm.search_memory = edit_after_search
    assert (
        await rt.manager.search_and_filter_memories(
            "bot", "current", "topic", [], user_ids=["alice"]
        )
        == []
    )


@pytest.mark.asyncio
async def test_sources_and_evidence_survive_recall_cache_without_entering_fact_text(
    runtime,
):
    rt = runtime
    source = "other'group <&>"
    await rt.seed(
        "foreign",
        ["alice"],
        session=source,
        metadata={
            "associated_user_ids": ["alice"],
            "evidence": [{"quote": "private original quote"}],
        },
    )
    await rt.seed("private", ["alice"], session="alice")
    await rt.seed("local", ["alice"], session="current")
    result = await search_memories_with_rerank(
        rt.plugin,
        bot_name="bot",
        group_or_user_id="current",
        query="topic",
        recent_messages=[],
        user_ids=["alice"],
    )
    rt.cache.merge_session_recalled_memories("bot", "current", result)
    cached = rt.cache.get_session_recalled_memories("bot", "current")
    assert json.loads(
        next(item.metadata for item in cached if item.memory_id == "foreign")
    )["evidence"] == [{"quote": "private original quote"}]
    prompt = build_reply_prompt([], [], None, session_recalled_memories=cached)
    root = fromstring("<context>" + prompt + "</context>")
    memories = {
        item.text: item.attrib
        for item in root.findall("session_recalled_memories/memory")
    }
    assert memories["fact foreign <&>"]["source"] == f"来源群聊：{source}"
    assert memories["fact private <&>"]["source"] == "来源私聊"
    assert "source" not in memories["fact local <&>"]
    assert "private original quote" not in prompt
    assert "associated_user_ids" not in prompt
    assert root.find("memory_usage_rules") is not None
    async with rt.db.conn.execute("SELECT hit_count, metadata FROM memories") as cursor:
        stored = await cursor.fetchall()
    assert all(row["hit_count"] == 1 for row in stored)
    assert all("source_label" not in json.loads(row["metadata"]) for row in stored)


@pytest.mark.asyncio
@pytest.mark.parametrize("target,expected", [("alice", "alice"), ("", "bob")])
async def test_native_search_targets_user_without_reinterpreting_it_as_a_group(
    runtime, target, expected
):
    rt = runtime
    await rt.seed("alice", ["alice"])
    await rt.seed("bob", ["bob"])
    rt.plugin.adapter_id_map = {"adapter": "bot"}
    rt.plugin.bot_map = {"bot": {}}
    event = SimpleNamespace(
        platform_meta=SimpleNamespace(id="adapter"),
        get_group_id=lambda: "current",
        get_sender_id=lambda: "bob",
        get_sender_name=lambda: "Bob",
    )
    context = SimpleNamespace(context=SimpleNamespace(event=event, extra={}))
    result = json.loads(
        await SearchMemoryTool(plugin=rt.plugin).call(context, "topic", target)
    )
    assert result["count"] == 1
    assert f"fact {expected} &lt;&amp;&gt;" in result["memory_context"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope,target,expected",
    [
        ("session", "", {"local-alice", "local-bob"}),
        ("session", "alice", {"local-alice"}),
        ("user", "", {"local-bob", "foreign-bob"}),
        ("user", "alice", {"local-alice", "foreign-alice"}),
        ("all", "", {"local-alice", "local-bob", "foreign-alice", "foreign-bob"}),
        ("all", "alice", {"local-alice", "foreign-alice"}),
    ],
)
async def test_memory_tool_follows_scope_with_optional_explicit_user(
    runtime, scope, target, expected
):
    rt = runtime
    rt.plugin.memory_recall_scope = scope
    for session, prefix in [("current", "local"), ("other", "foreign")]:
        for user in ["alice", "bob"]:
            await rt.seed(f"{prefix}-{user}", [user], session=session)
    await rt.seed("other-bot", ["alice"], bot="other")
    rt.plugin.adapter_id_map = {"adapter": "bot"}
    rt.plugin.bot_map = {"bot": {}}
    event = SimpleNamespace(
        platform_meta=SimpleNamespace(id="adapter"),
        get_group_id=lambda: "current",
        get_sender_id=lambda: "bob",
        get_sender_name=lambda: "Bob",
    )
    context = SimpleNamespace(context=SimpleNamespace(event=event, extra={}))

    result = json.loads(
        await SearchMemoryTool(plugin=rt.plugin).call(context, "topic", target)
    )

    assert result["status"] == "success"
    assert result["count"] == len(expected)
    assert {
        item.text
        for item in fromstring("<context>" + result["memory_context"] + "</context>")
    } == {f"fact {memory_id} <&>" for memory_id in expected}


@pytest.mark.asyncio
async def test_automatic_batches_search_each_speaker_across_groups(runtime):
    rt = runtime
    await rt.seed("alice", ["alice"])
    await rt.seed("bob", ["bob"])
    await rt.seed("absent", ["carol"], distance=0.0)
    result = await ReplyPipeline(rt.plugin)._search_current_recall_memories(
        "bot",
        "current",
        [
            MessageData(user_id="alice", content="one"),
            MessageData(user_id="bob", content="two"),
        ],
        None,
        [MessageData(user_id="carol", content="earlier")],
    )
    assert {item["memory_id"] for item in result} == {"alice", "bob"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope,expected",
    [
        ("session", {"local-bob"}),
        ("user", {"foreign-alice"}),
        ("all", {"local-bob", "foreign-alice", "absent", "foreign-legacy"}),
    ],
)
async def test_automatic_recall_uses_configured_scope_for_each_query(
    runtime, scope, expected
):
    rt = runtime
    rt.plugin.memory_recall_scope = scope
    await rt.seed("local-bob", ["bob"], session="current")
    await rt.seed("foreign-alice", ["alice"])
    await rt.seed("absent", ["carol"])
    await rt.seed("foreign-legacy")
    await rt.seed("other-bot", ["alice"], bot="other", distance=0.0)

    result = await ReplyPipeline(rt.plugin)._search_current_recall_memories(
        "bot", "current", [MessageData(user_id="alice", content="topic")], None, []
    )

    assert {item["memory_id"] for item in result} == expected


@pytest.mark.asyncio
async def test_full_automatic_recall_protects_users_without_biasing_tools_or_decisions(
    runtime,
):
    rt = runtime
    rt.plugin.memory_recall_scope = "all"
    rt.plugin.embedding_conf.update(
        session_recall_search_limit=3, limit=3, inject_limit=2
    )
    for index in range(3):
        await rt.seed(f"bob-{index}", ["bob"], distance=index * 0.1)
    await rt.seed("foreign-alice", ["alice"], distance=0.6)
    await rt.seed("wrong-bot", ["alice"], bot="other", distance=0.0)
    await rt.seed("unindexed-alice", ["alice"], indexed=False, distance=0.0)
    messages = [MessageData(user_id="alice", content="good morning")]

    automatic = await ReplyPipeline(rt.plugin)._search_current_recall_memories(
        "bot", "current", messages, None, []
    )
    assert {record["memory_id"] for record in automatic} == {"foreign-alice", "bob-0"}
    own = next(record for record in automatic if record["memory_id"] == "foreign-alice")
    assert json.loads(own["metadata"])["source_session_id"] == "other-group"

    decision = await search_memories_with_rerank(
        rt.plugin,
        bot_name="bot",
        group_or_user_id="current",
        query="good morning",
        recent_messages=messages,
    )
    assert {record["memory_id"] for record in decision} == {"bob-0", "bob-1", "bob-2"}

    rt.plugin.adapter_id_map = {"adapter": "bot"}
    rt.plugin.bot_map = {"bot": {}}
    event = SimpleNamespace(
        platform_meta=SimpleNamespace(id="adapter"),
        get_group_id=lambda: "current",
        get_sender_id=lambda: "alice",
        get_sender_name=lambda: "Alice",
    )
    context = SimpleNamespace(context=SimpleNamespace(event=event, extra={}))
    tool = SearchMemoryTool(plugin=rt.plugin)
    broad = json.loads(await tool.call(context, "good morning"))
    assert broad["count"] == 3
    assert "foreign-alice" not in broad["memory_context"]
    targeted = json.loads(await tool.call(context, "good morning", "alice"))
    assert targeted["count"] == 1
    assert "foreign-alice" in targeted["memory_context"]


@pytest.mark.asyncio
async def test_full_automatic_recall_releases_user_slots_when_vector_threshold_rejects_them(
    runtime,
):
    rt = runtime
    rt.plugin.memory_recall_scope = "all"
    rt.plugin.embedding_conf.update(
        session_recall_search_limit=2, inject_limit=2, threshold=0.2
    )
    await rt.seed("irrelevant-alice", ["alice"], distance=0.9)
    await rt.seed("first", ["bob"], distance=0.0)
    await rt.seed("second", ["carol"], distance=0.1)

    result = await ReplyPipeline(rt.plugin)._search_current_recall_memories(
        "bot", "current", [MessageData(user_id="alice", content="travel")], None, []
    )
    assert {record["memory_id"] for record in result} == {"first", "second"}


@pytest.mark.asyncio
async def test_full_automatic_recall_scores_the_complete_union_with_local_and_external_rerank(
    runtime,
):
    rt = runtime
    rt.plugin.memory_recall_scope = "all"
    rt.plugin.embedding_conf.update(session_recall_search_limit=3, inject_limit=1)
    rt.plugin.rerank_conf.update(enabled=True, top_k=1, threshold=0.45)
    rt.ltm.rerank_conf = {
        "use_external_provider": rt.ltm.embedding_conf["use_external_provider"]
    }
    rt.ltm.reranker = SimpleNamespace(
        rerank=lambda query, texts: [0.6 if "alice" in text else 0.99 for text in texts]
    )
    rt.ltm.rerank_provider = SimpleNamespace(
        rerank=AsyncMock(
            side_effect=lambda query, texts, top_n: sorted(
                [
                    SimpleNamespace(
                        index=index, relevance_score=0.6 if "alice" in text else 0.99
                    )
                    for index, text in enumerate(texts)
                ],
                key=lambda result: result.relevance_score,
                reverse=True,
            )[:top_n]
        )
    )
    await rt.seed("alice", ["alice"], distance=0.6)
    for index in range(3):
        await rt.seed(f"bob-{index}", ["bob"], distance=index * 0.1)

    result = await ReplyPipeline(rt.plugin)._search_current_recall_memories(
        "bot", "current", [MessageData(user_id="alice", content="morning")], None, []
    )
    assert [record["memory_id"] for record in result] == ["alice"]
    assert result[0]["score"] == 0.6
    if rt.ltm.rerank_conf["use_external_provider"]:
        assert rt.ltm.rerank_provider.rerank.call_args.kwargs["top_n"] == 4


@pytest.mark.asyncio
async def test_source_cleanup_removes_foreign_cache_copies_and_keeps_other_sources(
    runtime,
):
    rt = runtime
    await rt.seed("removed", ["alice"], session="source")
    await rt.seed("kept", ["alice"], session="kept-source")
    result = await rt.manager.search_and_filter_memories(
        "bot", "current", "topic", [], user_ids=["alice"]
    )
    rt.cache.merge_session_recalled_memories("bot", "current", result)
    await rt.cache.delete_all_memories("bot", "source")
    assert [
        item.memory_id
        for item in rt.cache.get_session_recalled_memories("bot", "current")
    ] == ["kept"]
    remaining = await rt.manager.search_and_filter_memories(
        "bot", "current", "topic", [], user_ids=["alice"]
    )
    assert [item["memory_id"] for item in remaining] == ["kept"]
