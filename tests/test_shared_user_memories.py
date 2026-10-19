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
