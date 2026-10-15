import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import pytest
import pytest_asyncio
from core.database.data_cache import DataCache
from core.database.database import Database
from core.database.schema import initialize_database
from core.memory.passive_memory import PassiveMemoryManager
from core.utils.schemas import MessageData


@pytest_asyncio.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "astrbot.api.star.StarTools.get_data_dir", lambda *args: tmp_path
    )
    db = await Database.connect()
    ltm = SimpleNamespace(
        embedding_conf={"enabled": False},
        delete_memory=AsyncMock(return_value=True),
        add_memory=AsyncMock(return_value=None),
        search_memory=AsyncMock(return_value=[]),
    )
    plugin = SimpleNamespace(
        db=db,
        ltm=ltm,
        embedding_conf=ltm.embedding_conf,
        conf={"memory_config": {}},
        passive_memory_enabled=True,
        passive_memory_provider_ids=["summary"],
        bot_map={"bot": {"nickname": "Giftia"}},
        active_reply_counters={},
        msg_number=5,
        passive_memory_silence_threshold=1,
        passive_memory_overflow_threshold=2,
    )
    plugin.data_cache = DataCache(db, None, ltm, plugin=plugin)
    manager = PassiveMemoryManager(plugin)

    async def complete(task_name, *args, **kwargs):
        return (
            "<summary_user_profile>无</summary_user_profile>"
            if task_name == "关系画像维护"
            else '{"memories": []}'
        )

    manager._call_summary_llm = AsyncMock(side_effect=complete)
    plugin.passive_memory_manager = manager
    runtime = SimpleNamespace(plugin=plugin, db=db, manager=manager)
    try:
        yield runtime
    finally:
        await manager.close()
        await db.close()


async def message(runtime, text, user_id="user", day=10, role="message", hour=12):
    """Persist a source message with an actual database identity.

    Args:
        runtime: Isolated test runtime.
        text: Direct source speech.
        user_id: Source account.
        day: Distinct source date.
        role: Stored message role.
        hour: Source hour within the selected day.

    Returns:
        Persisted message including its database ID.
    """
    msg = MessageData(
        user_id=user_id,
        nickname=user_id,
        content=text,
        group_or_user_id="group",
        message_id=f"{user_id}-{day}-{hour}-{text}",
        time=f"2020-01-{day:02}T{hour:02}:00:00+08:00",
        role=role,
    )
    await runtime.db.insert_message("bot", msg)
    rows = await runtime.db.get_messages_by_id_range("bot", "group", 1, 10000)
    return next(row for row in rows if row.message_id == msg.message_id)


def atom(msg, kind="event", **updates):
    """Create a proposed atom supported by a real source.

    Args:
        msg: Original source message.
        kind: Atomic semantic type.
        updates: Fields overriding the standard proposal.

    Returns:
        Model response item.
    """
    return {
        "kind": kind,
        "subject_id": "user",
        "object_id": "self" if kind in {"event", "agreement"} else "",
        "topic": "game",
        "text": msg.content,
        "evidence": [{"source_id": msg.db_id}],
        **updates,
    }


async def extract(runtime, msg, items):
    """Run production extraction against a synthetic model response.

    Args:
        runtime: Isolated test runtime.
        msg: New source message.
        items: Proposed memories from the mock model.

    Returns:
        Extraction success status.
    """
    runtime.manager._call_summary_llm = AsyncMock(
        return_value=json.dumps({"memories": items})
    )
    return await runtime.manager._run_memory_summary_task(
        "bot",
        "group",
        "self",
        "Giftia",
        {"alias_observation_messages": [msg], "memory_media": {}},
    )


async def leave_latest_batch_pending(runtime):
    """Finish earlier extraction jobs without changing their memory contents.

    Args:
        runtime: Isolated test runtime.
    """
    while True:
        async with runtime.db.conn.execute(
            "SELECT COUNT(*) FROM kv_store WHERE key LIKE 'atomic_memory:consolidation:%'"
        ) as cursor:
            count = (await cursor.fetchone())[0]
        if count <= 1:
            return
        runtime.manager._call_summary_llm = AsyncMock(return_value='{"memories": []}')
        assert await runtime.manager.consolidate_memories("bot", "group", "self")


async def rows(runtime):
    """Read all atoms including retired states.

    Args:
        runtime: Isolated test runtime.

    Returns:
        Stored rows in creation order.
    """
    async with runtime.db.conn.execute("SELECT * FROM memories ORDER BY id") as cursor:
        return [dict(row) for row in await cursor.fetchall()]


@pytest.mark.asyncio
async def test_extraction_without_embeddings_retains_provenance_and_atomic_checkpoint(
    runtime,
):
    msg = await message(runtime, "以后叫我小明")
    assert msg.db_id > 0
    assert await extract(
        runtime, msg, [atom(msg, "preference", topic="preferred_name")]
    )
    saved = (await rows(runtime))[0]
    metadata = json.loads(saved["metadata"])
    assert saved["kind"] == "preference"
    assert metadata["evidence"][0]["source_id"] == msg.db_id
    assert metadata["evidence"][0]["message_id"] == msg.message_id
    assert metadata["evidence"][0]["time"] == msg.time
    assert metadata["evidence"][0]["quote"] == msg.content
    assert saved["indexed_revision"] == 0
    assert (
        await runtime.db.get_kv_data("passive_memory:last_summarized_id:bot:group")
        == msg.db_id
    )
    assert await runtime.manager._run_memory_summary_task(
        "bot",
        "group",
        "self",
        "Giftia",
        {"alias_observation_messages": [msg]},
    )
    runtime.manager._call_summary_llm.assert_awaited_once()
    assert len(await rows(runtime)) == 1
    runtime.plugin.ltm.add_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_id_only_references_snapshot_full_direct_messages_and_deduplicate(runtime):
    first_text = '  我喜欢简短回复，保留原来的标点“🙂”。\n  ' + "这是一条较长的原始发言。" * 60
    first = await message(
        runtime,
        '<quote sender_id="other"><quote>嵌套转述</quote>他人的发言</quote>'
        + first_text,
    )
    second = await message(runtime, "没错，长篇回复看起来累，简洁些就好。\n谢谢。", day=11)
    proposal = atom(
        first,
        "preference",
        text="我记得小明喜欢简短回复",
        evidence=[
            {"source_id": first.db_id},
            {"source_id": second.db_id},
            {"source_id": first.db_id},
        ],
    )
    runtime.manager._call_summary_llm = AsyncMock(
        return_value=json.dumps({"memories": [proposal]})
    )
    assert await runtime.manager._run_memory_summary_task(
        "bot",
        "group",
        "self",
        "Giftia",
        {"alias_observation_messages": [first, second]},
    )
    metadata = json.loads((await rows(runtime))[0]["metadata"])
    assert metadata["evidence"] == [
        {
            "source_id": msg.db_id,
            "media_id": "",
            "quote": direct,
            "user_id": msg.user_id,
            "message_id": msg.message_id,
            "time": msg.time,
        }
        for msg, direct in ((first, first_text), (second, second.content))
    ]
    system_prompt = runtime.manager._call_summary_llm.call_args.args[1]
    schema = json.loads(system_prompt.rsplit("JSON Schema:\n", 1)[1])
    assert set(schema["$defs"]["MemoryEvidence"]["properties"]) == {
        "source_id",
        "media_id",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("matches", [(True, False), (False, False), (True, True)])
async def test_only_new_memories_with_historical_matches_enter_consolidation(
    runtime, matches
):
    from xml.etree.ElementTree import fromstring

    await runtime.db.conn.executemany(
        "INSERT INTO memories (bot_name, group_or_user_id, memory_id, text, metadata) "
        "VALUES ('bot', 'group', ?, ?, ?)",
        [
            ("old-a", "user 曾经做过木雕", json.dumps({"user_id": "user"})),
            (
                "old-b",
                "user 曾经观测星空",
                json.dumps(
                    {
                        "schema_version": 2,
                        "subject_id": "user",
                        "object_id": "self",
                        "topic": "astronomy",
                    }
                ),
            ),
        ],
    )
    await runtime.db.conn.commit()
    msg = await message(runtime, "今天做木雕，晚上观测星空")
    assert await extract(
        runtime,
        msg,
        [
            atom(msg, topic="carving", text="我看到 user 今天做木雕"),
            atom(msg, topic="astronomy", text="我和 user 晚上观测星空"),
        ],
    )
    extraction = fromstring(runtime.manager._call_summary_llm.call_args.args[2])
    assert extraction.findall("existing_memories/memory") == []
    runtime.plugin.ltm.search_memory.assert_not_awaited()
    saved = await rows(runtime)
    new = saved[2:]
    batch = await runtime.db.memory_maintenance_repo.pending_consolidation(
        "bot", "group"
    )
    assert set(batch["payload"]["memory_ids"]) == {row["memory_id"] for row in new}
    await runtime.db.conn.execute(
        "UPDATE memories SET indexed_revision=revision WHERE id<=?",
        (batch["payload"]["history_end"],),
    )
    await runtime.db.conn.commit()
    runtime.plugin.embedding_conf["enabled"] = True
    runtime.plugin.ltm.search_memory.side_effect = [
        [{"id": row["memory_id"]} for row in new]
        + ([{"id": "old-a"}] if matches[0] else []),
        [{"id": row["memory_id"]} for row in new]
        + ([{"id": "old-b"}] if matches[1] else []),
    ]
    runtime.manager._call_summary_llm = AsyncMock(return_value='{"memories": []}')
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    assert (
        await runtime.db.memory_maintenance_repo.pending_consolidation("bot", "group")
        is None
    )
    calls = runtime.plugin.ltm.search_memory.call_args_list
    assert [call.kwargs["query"] for call in calls] == [row["text"] for row in new]
    assert all(
        set(call.kwargs["exclude_ids"]) >= {row["memory_id"] for row in new}
        for call in calls
    )
    if any(matches):
        runtime.manager._call_summary_llm.assert_awaited_once()
        prompt = fromstring(runtime.manager._call_summary_llm.call_args.args[2])
        supplied_new = [
            json.loads(item.text) for item in prompt.findall("new_memories/memory")
        ]
        supplied_old = [
            json.loads(item.text) for item in prompt.findall("existing_memories/memory")
        ]
        assert {row["memory_id"] for row in supplied_new} == {
            row["memory_id"] for row, matched in zip(new, matches) if matched
        }
        assert {row["memory_id"] for row in supplied_old} == {
            name for name, matched in zip(("old-a", "old-b"), matches) if matched
        }
    else:
        runtime.manager._call_summary_llm.assert_not_awaited()
    assert [
        (row["text"], row["revision"], row["status"]) for row in await rows(runtime)
    ] == [(row["text"], row["revision"], row["status"]) for row in saved]
    assert not await runtime.manager.consolidate_memories("bot", "group", "self")


@pytest.mark.asyncio
async def test_maintenance_text_recall_rechecks_scope_state_and_current_content(
    runtime,
):
    await runtime.db.conn.executemany(
        "INSERT INTO memories (bot_name, group_or_user_id, memory_id, text, status, expires_at, revision, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, '{\"schema_version\":2}')",
        [
            ("bot", "group", "current", "天文观测的最新修订", "active", None, 2),
            ("bot", "group", "closed", "天文观测", "completed", None, 1),
            ("bot", "group", "retired", "天文观测", "superseded", None, 1),
            ("bot", "group", "expired", "天文观测", "active", "2020-01-01", 1),
            ("other-bot", "group", "other-bot", "天文观测", "active", None, 1),
            ("bot", "other-group", "other-group", "天文观测", "active", None, 1),
        ],
    )
    await runtime.db.conn.commit()
    found = await runtime.db.memory_maintenance_repo.related_snapshot(
        "bot",
        "group",
        "继续天文观测",
        ["current", "closed", "retired", "expired", "other-bot", "other-group"],
    )
    assert [row["memory_id"] for row in found] == ["current"]
    assert found[0]["revision"] == 2 and found[0]["text"] == "天文观测的最新修订"


@pytest.mark.asyncio
async def test_maintenance_recall_budget_preserves_a_hit_for_each_message(runtime):
    runtime.plugin.embedding_conf["top_k"] = 3
    runtime.db.memory_maintenance_repo.related_snapshot = AsyncMock(
        side_effect=lambda bot, session, query, candidates, **kwargs: [
            {"memory_id": f"{query}-{rank}"} for rank in range(3)
        ]
    )
    selected, per_message = await runtime.manager._recall_maintenance_memories(
        "bot", "group", [f"message-{index}" for index in range(200)]
    )
    assert len(selected) == 200
    assert per_message == [[f"message-{index}-0"] for index in range(200)]


@pytest.mark.asyncio
async def test_upgrade_does_not_enqueue_or_consolidate_historical_memories(runtime):
    await runtime.db.conn.executemany(
        "INSERT INTO memories (bot_name, group_or_user_id, memory_id, text, metadata) "
        "VALUES ('bot', 'group', ?, '历史事件', ?)",
        [
            ("legacy", '{"user_id":"user"}'),
            ("version-1", '{"schema_version":1}'),
            ("malformed", "invalid JSON"),
            ("version-2", '{"schema_version":2}'),
        ],
    )
    await runtime.db.conn.commit()
    before = await rows(runtime)
    await runtime.manager._remember_session("bot", "group", "self")
    await runtime.manager.maintain_memories()
    assert not runtime.manager.consolidation_tasks
    runtime.manager._call_summary_llm.assert_not_awaited()
    runtime.plugin.ltm.search_memory.assert_not_awaited()
    assert (
        await runtime.db.memory_maintenance_repo.pending_consolidation("bot", "group")
        is None
    )
    assert not await runtime.manager.consolidate_memories("bot", "group", "self")
    assert (
        "暂无待整理的新增记忆批次"
        in await runtime.manager.force_consolidate_memories("bot", "group", "self")
    )
    assert await rows(runtime) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response", ["broken", "{}", '{"memories": [{"kind": "unknown"}]}']
)
async def test_invalid_batch_keeps_progress_and_can_retry(runtime, response):
    await message(runtime, "以后不要叫我老板")
    runtime.manager._call_summary_llm = AsyncMock(return_value=response)
    assert not await runtime.manager._run_background_summarize(
        "bot", "group", "self", 1, 100
    )
    assert await rows(runtime) == []
    assert (
        await runtime.db.get_kv_data("passive_memory:last_summarized_id:bot:group", 0)
        == 0
    )
    runtime.manager._call_summary_llm = AsyncMock(return_value='{"memories": []}')
    assert await runtime.manager._run_background_summarize(
        "bot", "group", "self", 1, 100
    )
    assert (
        await runtime.db.get_kv_data("passive_memory:last_summarized_id:bot:group") == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "empty",
        "source",
        "outside_batch",
        "participant",
        "third_party",
        "command",
        "recalled",
        "operation",
        "bot",
    ],
)
async def test_invalid_evidence_rejects_entire_batch(runtime, fault):
    msg = await message(runtime, "我喜欢简短回复")
    invalid = atom(msg, "preference")
    if fault == "empty":
        msg.content = " \n "
    elif fault == "source":
        invalid["evidence"][0]["source_id"] += 1
    elif fault == "outside_batch":
        outside = await message(runtime, "这条消息不在归纳输入中", user_id="other")
        invalid["evidence"][0]["source_id"] = outside.db_id
    elif fault == "participant":
        invalid["subject_id"] = "stranger"
    elif fault == "third_party":
        msg.user_id = "other"
    elif fault == "command":
        msg.content = "/" + msg.content
    elif fault == "recalled":
        msg.is_recalled = 1
    elif fault == "operation":
        msg.role = "operation_log"
    elif fault == "bot":
        msg.user_id = "self"
    assert not await extract(runtime, msg, [atom(msg), invalid])
    assert await rows(runtime) == []


@pytest.mark.asyncio
async def test_quoted_only_content_and_unavailable_media_are_not_direct_evidence(runtime):
    msg = await message(runtime, '<quote sender_id="other">我喜欢长回复</quote>')
    assert not await extract(
        runtime,
        msg,
        [
            atom(
                msg,
                "preference",
                evidence=[{"source_id": msg.db_id}],
            )
        ],
    )
    msg.media_id_list = ["image"]
    assert not await extract(
        runtime,
        msg,
        [
            atom(
                msg,
                "preference",
                evidence=[{"source_id": msg.db_id, "media_id": "image"}],
            )
        ],
    )
    runtime.manager._call_summary_llm = AsyncMock(
        return_value=json.dumps(
            {
                "memories": [
                    atom(
                        msg,
                        "preference",
                        evidence=[
                            {
                                "source_id": msg.db_id,
                                "media_id": "image",
                            }
                        ],
                    )
                ]
            }
        )
    )
    assert await runtime.manager._run_memory_summary_task(
        "bot",
        "group",
        "self",
        "Giftia",
        {
            "alias_observation_messages": [msg],
            "memory_media": {"image": {"text": "图片文字", "caption": "图片内容"}},
        },
    )
    evidence = json.loads((await rows(runtime))[0]["metadata"])["evidence"]
    assert evidence == [
        {
            "source_id": msg.db_id,
            "media_id": "image",
            "quote": "图片文字\n图片内容",
            "user_id": msg.user_id,
            "message_id": msg.message_id,
            "time": msg.time,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["unattached", "empty_caption", "command"])
async def test_media_references_cannot_bypass_source_validation(runtime, fault):
    from xml.etree.ElementTree import fromstring

    msg = await message(runtime, "[图片:image]")
    msg.media_id_list = ["image"]
    media = {"image": {"caption": "一张聊天截图"}}
    if fault == "unattached":
        msg.media_id_list = ["another-image"]
    elif fault == "empty_caption":
        media["image"] = {"caption": "  "}
    else:
        msg.content = "/保存 [图片:image]"
    proposal = atom(msg, evidence=[{"source_id": msg.db_id, "media_id": "image"}])
    runtime.manager._call_summary_llm = AsyncMock(
        return_value=json.dumps({"memories": [proposal]})
    )
    assert not await runtime.manager._run_memory_summary_task(
        "bot",
        "group",
        "self",
        "Giftia",
        {"alias_observation_messages": [msg], "memory_media": media},
    )
    assert await rows(runtime) == []
    assert (
        await runtime.db.get_kv_data("passive_memory:last_summarized_id:bot:group", 0)
        == 0
    )
    if fault == "command":
        user_prompt = runtime.manager._call_summary_llm.call_args.args[2]
        rendered = fromstring(user_prompt).find("new_messages/message")
        assert rendered.attrib["evidence_allowed"] == "false"


@pytest.mark.asyncio
async def test_agreement_completion_and_expiration_are_distinct(runtime):
    initial = await message(runtime, "我们周末一起玩游戏")
    assert await extract(runtime, initial, [atom(initial, "agreement")])
    saved = (await rows(runtime))[0]
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    completed = await message(runtime, "刚才我们已经一起玩完了", day=11)
    assert await extract(runtime, completed, [atom(completed)])
    newest = (await rows(runtime))[-1]
    assert (await rows(runtime))[0]["status"] == "active"
    runtime.manager._call_summary_llm = AsyncMock(
        return_value=json.dumps(
            {
                "memories": [
                    atom(
                        completed,
                        "agreement",
                        memory_id=saved["memory_id"],
                        status="completed",
                        evidence=[],
                        source_memory_ids=[saved["memory_id"], newest["memory_id"]],
                    )
                ]
            }
        )
    )
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    current = (await rows(runtime))[0]
    assert current["status"] == "completed"
    assert len(json.loads(current["metadata"])["evidence"]) == 2
    other = await message(runtime, "我们明天一起玩另一个游戏", day=12)
    assert await extract(
        runtime,
        other,
        [
            atom(
                other,
                "agreement",
                topic="other_game",
                expires_at="2020-01-13T00:00:00+08:00",
            )
        ],
    )
    assert (
        await runtime.db.memory_maintenance_repo.expire_agreements("bot", "group") == 1
    )
    assert [row["status"] for row in await rows(runtime)] == [
        "completed",
        "active",
        "expired",
    ]


@pytest.mark.asyncio
async def test_derived_habit_preserves_independent_sources_and_events(runtime):
    for day in (10, 11, 12):
        msg = await message(runtime, f"今晚又来找你玩游戏，第{day}天", day=day)
        assert await extract(runtime, msg, [atom(msg)])
    original = await rows(runtime)
    proposal = {
        "kind": "habit",
        "subject_id": "user",
        "topic": "evening_games",
        "text": "经常晚上找我玩游戏",
        "origin": "derived",
        "source_memory_ids": [row["memory_id"] for row in original],
    }
    await leave_latest_batch_pending(runtime)
    runtime.manager._call_summary_llm = AsyncMock(
        return_value=json.dumps({"memories": [proposal]})
    )
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    stored = await rows(runtime)
    assert len(stored) == 4
    assert all(row["status"] == "active" for row in stored)
    habit = stored[-1]
    assert habit["kind"] == "habit"
    evidence = json.loads(habit["metadata"])["evidence"]
    assert len({ref["source_id"] for ref in evidence}) == 3
    assert not await runtime.manager.consolidate_memories("bot", "group", "self")
    runtime.manager._call_summary_llm.assert_awaited_once()

    new = await message(runtime, "今晚又来找你玩游戏，第13天", day=13)
    assert await extract(runtime, new, [atom(new)])
    latest = (await rows(runtime))[-1]
    proposal["source_memory_ids"] = [habit["memory_id"], latest["memory_id"]]
    proposal["text"] = "持续在晚上找我玩游戏"
    runtime.manager._call_summary_llm = AsyncMock(
        return_value=json.dumps({"memories": [proposal]})
    )
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    stored = await rows(runtime)
    habits = [row for row in stored if row["kind"] == "habit"]
    assert len(habits) == 1
    assert len(json.loads(habits[0]["metadata"])["evidence"]) == 4


@pytest.mark.asyncio
async def test_consolidation_recalls_reviewed_habit_and_keeps_unrecalled_provenance(
    runtime,
):
    for day in (10, 11, 12):
        msg = await message(runtime, f"晚上又来找你玩游戏，第{day}次", day=day)
        assert await extract(runtime, msg, [atom(msg)])
    sources = await rows(runtime)
    proposal = {
        "kind": "habit",
        "subject_id": "user",
        "object_id": "self",
        "topic": "evening_games",
        "text": "我留意到 user 经常晚上找我玩游戏",
        "origin": "derived",
        "source_memory_ids": [row["memory_id"] for row in sources],
    }
    await leave_latest_batch_pending(runtime)
    runtime.manager._call_summary_llm = AsyncMock(
        return_value=json.dumps({"memories": [proposal]})
    )
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    habit = (await rows(runtime))[-1]
    await runtime.db.conn.executemany(
        "INSERT INTO memories (bot_name, group_or_user_id, memory_id, text, kind, reviewed_revision, metadata) "
        "VALUES ('bot', 'group', ?, ?, 'preference', 1, '{\"schema_version\":2}')",
        [(f"noise-{index}", f"无关天气记录{index}") for index in range(80)],
    )
    await runtime.db.conn.execute("UPDATE memories SET indexed_revision=revision")
    await runtime.db.conn.commit()
    reviewed = await runtime.db.memory_maintenance_repo.snapshot(
        "bot", "group", limit=20, reviewed_only=True
    )
    assert habit["memory_id"] not in {row["memory_id"] for row in reviewed}
    runtime.plugin.embedding_conf.update(enabled=True, top_k=1)
    runtime.plugin.ltm.search_memory.return_value = [{"id": habit["memory_id"]}]
    latest = await message(runtime, "晚上又来找你玩游戏，今天是第四次", day=13)
    assert await extract(runtime, latest, [atom(latest)])
    newest = (await rows(runtime))[-1]
    proposal.update(
        memory_id=habit["memory_id"],
        source_memory_ids=[habit["memory_id"], newest["memory_id"]],
    )
    runtime.manager._call_summary_llm = AsyncMock(
        return_value=json.dumps({"memories": [proposal]})
    )
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    from xml.etree.ElementTree import fromstring

    prompt = fromstring(runtime.manager._call_summary_llm.call_args.args[2])
    supplied = [
        json.loads(node.text) for node in prompt.findall("existing_memories/memory")
    ]
    assert {row["memory_id"] for row in supplied} == {habit["memory_id"]}
    assert (
        json.loads(prompt.find("new_memories/memory").text)["memory_id"]
        == newest["memory_id"]
    )
    saved = await rows(runtime)
    habits = [row for row in saved if row["kind"] == "habit"]
    assert len(habits) == 1 and habits[0]["revision"] == habit["revision"] + 1
    metadata = json.loads(habits[0]["metadata"])
    assert len(metadata["evidence"]) == 4
    assert set(metadata["source_memory_ids"]) == {
        *[row["memory_id"] for row in sources],
        newest["memory_id"],
    }
    assert all(row["status"] == "active" for row in saved[:3])


@pytest.mark.asyncio
async def test_summary_review_cannot_close_agreement_without_new_evidence(runtime):
    original = await message(runtime, "我们约好一起进行天文观测")
    assert await extract(
        runtime, original, [atom(original, "agreement", topic="astronomy")]
    )
    saved = (await rows(runtime))[0]
    latest = await message(runtime, "我又想起天文观测", day=11)
    assert not await extract(
        runtime,
        latest,
        [
            atom(
                original,
                "agreement",
                memory_id=saved["memory_id"],
                topic="astronomy",
                status="completed",
                evidence=[],
                source_memory_ids=[saved["memory_id"]],
            )
        ],
    )
    assert (await rows(runtime))[0]["status"] == "active"
    assert (
        await runtime.db.get_kv_data("passive_memory:last_summarized_id:bot:group")
        == original.db_id
    )


@pytest.mark.asyncio
async def test_summary_rejects_legacy_sources_without_advancing_progress(runtime):
    await runtime.db.conn.executemany(
        "INSERT INTO memories (bot_name, group_or_user_id, memory_id, text, metadata) "
        "VALUES ('bot', 'group', ?, ?, ?)",
        [
            (
                f"legacy-{index}",
                f"user 晚上玩游戏{index}",
                json.dumps({"user_id": "user"}),
            )
            for index in range(3)
        ],
    )
    await runtime.db.conn.commit()
    msg = await message(runtime, "我晚上想玩游戏")
    assert not await extract(
        runtime,
        msg,
        [
            {
                "kind": "habit",
                "subject_id": "user",
                "topic": "evening_games",
                "text": "我留意到 user 每天晚上玩游戏",
                "origin": "derived",
                "source_memory_ids": [f"legacy-{index}" for index in range(3)],
            }
        ],
    )
    assert len(await rows(runtime)) == 3
    assert (
        await runtime.db.get_kv_data("passive_memory:last_summarized_id:bot:group", 0)
        == 0
    )


@pytest.mark.asyncio
async def test_repeated_source_summaries_cannot_establish_habit(runtime):
    msg = await message(runtime, "今天晚上来玩游戏")
    assert await extract(
        runtime,
        msg,
        [
            atom(msg, topic=f"game_{index}", text=f"同一来源玩游戏摘要{index}")
            for index in range(3)
        ],
    )
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    latest = await message(runtime, "今天晚上再来玩游戏", day=11)
    assert await extract(runtime, latest, [atom(latest)])
    existing = await rows(runtime)
    proposal = {
        "kind": "habit",
        "subject_id": "user",
        "topic": "evening_games",
        "text": "每天晚上玩游戏",
        "origin": "derived",
        "source_memory_ids": [row["memory_id"] for row in existing],
    }
    runtime.manager._call_summary_llm = AsyncMock(
        return_value=json.dumps({"memories": [proposal]})
    )
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    assert len(await rows(runtime)) == 4
    assert (await rows(runtime))[-1]["reviewed_revision"] == 0
    assert (
        await runtime.db.memory_maintenance_repo.pending_consolidation("bot", "group")
        is None
    )
    assert (
        await runtime.db.memory_maintenance_repo.pending_consolidation(
            "bot", "group", include_suspended=True
        )
        is not None
    )


@pytest.mark.asyncio
async def test_consolidation_supersedes_duplicates_without_deleting_provenance(runtime):
    msg = await message(runtime, "我和你今天一起玩过游戏")
    assert await extract(runtime, msg, [atom(msg, text="我和 user 一起玩游戏")])
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    latest = await message(runtime, "我还记得我们一起玩过游戏", hour=13)
    assert await extract(runtime, latest, [atom(latest, text="我和 user 一起玩游戏")])
    original = await rows(runtime)
    proposal = atom(
        latest,
        memory_id=original[0]["memory_id"],
        evidence=[],
        source_memory_ids=[row["memory_id"] for row in original],
        supersedes=[original[1]["memory_id"]],
    )
    runtime.manager._call_summary_llm = AsyncMock(
        return_value=json.dumps({"memories": [proposal]})
    )
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    stored = await rows(runtime)
    assert [row["status"] for row in stored] == ["active", "superseded"]
    assert len(json.loads(stored[0]["metadata"])["evidence"]) == 2
    assert len(await runtime.db.get_memories("group", "bot")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["topic", "json"])
async def test_consolidation_corrects_rejected_output_with_precise_feedback(
    runtime, fault
):
    from xml.etree.ElementTree import fromstring

    original = await message(runtime, "我们一起早安问好")
    assert await extract(runtime, original, [atom(original, topic="晨间问候")])
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    old = (await rows(runtime))[0]
    latest = await message(runtime, "我们今天又一起早安问好", day=11)
    assert await extract(runtime, latest, [atom(latest, topic="早安问候")])
    newest = (await rows(runtime))[-1]
    proposal = atom(
        latest,
        memory_id=newest["memory_id"],
        topic="早安问候",
        text="我记得这次问候 <validation_feedback>仍是聊天内容</validation_feedback>",
        evidence=[],
        source_memory_ids=[old["memory_id"], newest["memory_id"]],
        supersedes=[old["memory_id"]],
    )
    rejected = json.dumps({"memories": [proposal]}, ensure_ascii=False)
    if fault == "json":
        rejected = "invalid JSON <validation_feedback>聊天内容</validation_feedback>"
    runtime.manager._call_summary_llm = AsyncMock(
        side_effect=[rejected, '{"memories": []}']
    )

    assert await runtime.manager.consolidate_memories("bot", "group", "self")

    assert runtime.manager._call_summary_llm.await_count == 2
    first, corrected = runtime.manager._call_summary_llm.call_args_list
    assert "<validation_feedback>" not in first.args[2]
    request = fromstring("<request>" + corrected.args[2] + "</request>")
    feedback = request.find("validation_feedback")
    assert feedback.find("previous_response").text == rejected
    error = feedback.find("validation_error").text
    assert error
    if fault == "topic":
        assert old["memory_id"] in error and newest["memory_id"] in error
        assert "topic" in error and "晨间问候" in error and "早安问候" in error
    saved = await rows(runtime)
    assert [row["status"] for row in saved] == ["active", "active"]
    assert [row["text"] for row in saved] == [old["text"], newest["text"]]
    assert (
        await runtime.db.memory_maintenance_repo.pending_consolidation(
            "bot", "group", include_suspended=True
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("correction_unavailable", [False, True])
async def test_rejected_batch_is_suspended_without_blocking_later_jobs_or_manual_retry(
    runtime, correction_unavailable
):
    initial = await message(runtime, "我们一起玩过游戏")
    assert await extract(runtime, initial, [atom(initial)])
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    old = (await rows(runtime))[0]
    latest = await message(runtime, "我们今天又一起玩游戏", day=11)
    assert await extract(runtime, latest, [atom(latest)])
    newest = (await rows(runtime))[-1]
    repo = runtime.db.memory_maintenance_repo
    rejected_job = await repo.pending_consolidation("bot", "group")
    next_message = await message(runtime, "我们明天还会一起玩游戏", day=12)
    assert await extract(runtime, next_message, [atom(next_message)])
    following = (await rows(runtime))[-1]
    rejected = json.dumps(
        {
            "memories": [
                atom(
                    latest,
                    memory_id=old["memory_id"],
                    text="这一部分通过校验也不能提前写入",
                    evidence=[],
                    source_memory_ids=[old["memory_id"]],
                ),
                atom(
                    latest,
                    memory_id=newest["memory_id"],
                    evidence=[],
                    source_memory_ids=[newest["memory_id"]],
                    supersedes=["unknown-memory"],
                ),
            ]
        }
    )
    runtime.manager._call_summary_llm = AsyncMock(
        side_effect=[
            rejected,
            None if correction_unavailable else rejected,
            '{"memories": []}',
        ]
    )
    runtime.manager._schedule_memory_consolidation("bot", "group", "self")
    await asyncio.wait_for(runtime.manager.consolidation_tasks["bot:group"], 5)

    assert runtime.manager._call_summary_llm.await_count == 3
    assert await repo.pending_consolidation("bot", "group") is None
    retained = json.loads(await runtime.db.get_kv_data(rejected_job["key"]))
    assert retained["status"] == "suspended"
    assert retained["pending_ids"] == rejected_job["payload"]["pending_ids"]
    attempts = retained["failure"]["attempts"]
    assert len(attempts) == 2
    assert attempts[0]["response"] == rejected
    assert "unknown-memory" in attempts[0]["error"]
    assert "<memory_input" in attempts[0]["user_prompt"]
    assert "<validation_feedback>" in attempts[1]["user_prompt"]
    assert retained["failure"]["system_prompt"].startswith("你是 Giftia")
    if correction_unavailable:
        assert attempts[1]["response"] == ""
        assert "no response" in attempts[1]["error"]
    current = {row["memory_id"]: row for row in await rows(runtime)}
    assert current[old["memory_id"]]["text"] == old["text"]
    assert all(row["status"] == "active" for row in current.values())
    assert current[newest["memory_id"]]["reviewed_revision"] == 0
    assert current[following["memory_id"]]["reviewed_revision"] == following["revision"]
    await runtime.manager.maintain_memories()
    assert not runtime.manager.consolidation_tasks
    assert runtime.manager._call_summary_llm.await_count == 3

    runtime.manager._call_summary_llm = AsyncMock(return_value='{"memories": []}')
    result = await runtime.manager.force_consolidate_memories("bot", "group", "self")
    assert "已处理一批" in result
    await asyncio.wait_for(runtime.manager.consolidation_tasks["bot:group"], 5)
    assert await runtime.db.get_kv_data(rejected_job["key"]) is None
    runtime.manager._call_summary_llm.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_resolved_target_preserves_itself_and_source_evidence(runtime):
    original = await message(runtime, "以后叫我小明")
    assert await extract(
        runtime, original, [atom(original, "preference", topic="name")]
    )
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    old = (await rows(runtime))[0]
    latest = await message(runtime, "我确认还是叫小明", day=11)
    assert await extract(runtime, latest, [atom(latest, topic="name")])
    newest = (await rows(runtime))[-1]
    proposal = atom(
        latest,
        "preference",
        topic="name",
        evidence=[],
        source_memory_ids=[old["memory_id"], newest["memory_id"]],
        supersedes=[old["memory_id"]],
    )
    runtime.manager._call_summary_llm = AsyncMock(
        return_value=json.dumps({"memories": [proposal]})
    )

    assert await runtime.manager.consolidate_memories("bot", "group", "self")

    saved = await rows(runtime)
    assert len(saved) == 2
    assert saved[0]["memory_id"] == old["memory_id"]
    assert saved[0]["revision"] == old["revision"] + 1
    assert all(row["status"] == "active" for row in saved)
    assert len(json.loads(saved[0]["metadata"])["evidence"]) == 2
    runtime.manager._call_summary_llm.assert_awaited_once()


@pytest.mark.asyncio
async def test_manual_retry_reports_suspension_and_resumes_remaining_batch_chunks(
    runtime,
):
    original = await message(runtime, "我们一起玩游戏")
    assert await extract(runtime, original, [atom(original)])
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    latest = await message(runtime, "我们一起玩游戏后又聊了游戏", day=11)
    assert await extract(
        runtime,
        latest,
        [
            atom(latest, text="我和 user 一起玩游戏"),
            atom(latest, text="我和 user 一起聊游戏"),
        ],
    )
    new = (await rows(runtime))[1:]
    repo = runtime.db.memory_maintenance_repo
    job = await repo.pending_consolidation("bot", "group")
    runtime.plugin.conf["memory_config"]["memory_consolidation_batch_size"] = 1
    proposal = atom(
        latest,
        memory_id=new[0]["memory_id"],
        evidence=[],
        source_memory_ids=[new[0]["memory_id"]],
        supersedes=["missing"],
    )
    runtime.manager._call_summary_llm = AsyncMock(
        return_value=json.dumps({"memories": [proposal]})
    )

    result = await runtime.manager.force_consolidate_memories("bot", "group", "self")

    assert "本批已暂挂" in result
    await asyncio.wait_for(runtime.manager.consolidation_tasks["bot:group"], 5)
    assert runtime.manager._call_summary_llm.await_count == 2
    suspended = json.loads(await runtime.db.get_kv_data(job["key"]))
    assert suspended["pending_ids"] == job["payload"]["pending_ids"]

    runtime.manager._call_summary_llm = AsyncMock(return_value='{"memories": []}')
    result = await runtime.manager.force_consolidate_memories("bot", "group", "self")

    assert "已处理一批" in result
    await asyncio.wait_for(runtime.manager.consolidation_tasks["bot:group"], 5)
    assert runtime.manager._call_summary_llm.await_count == 2
    assert await runtime.db.get_kv_data(job["key"]) is None
    assert all(
        row["reviewed_revision"] == row["revision"] for row in await rows(runtime)
    )


@pytest.mark.parametrize(
    "fault",
    ["subject_id", "object_id", "topic", "missing_id", "self_id", "repeated_id"],
)
def test_supersession_rejections_identify_records_and_field_conflicts(fault):
    from core.memory.atomic import MemoryChanges, validate_memory_changes

    identity = {"subject_id": "user", "object_id": "self", "topic": "game"}
    other_identity = dict(identity)
    if fault in identity:
        other_identity[fault] = "different"
    existing = [
        {
            "memory_id": memory_id,
            "kind": "event",
            "status": "active",
            "text": "示例记忆",
            "metadata": json.dumps({"schema_version": 2, **fields}),
        }
        for memory_id, fields in [("keep", identity), ("retire", other_identity)]
    ]
    ref = (
        "missing"
        if fault == "missing_id"
        else "keep"
        if fault == "self_id"
        else "retire"
    )
    proposal = {
        "memory_id": "keep",
        "kind": "event",
        **identity,
        "text": "我记得 user 和我一起玩游戏",
        "source_memory_ids": ["keep"],
        "supersedes": [ref],
    }
    changes = [proposal]
    if fault == "repeated_id":
        changes.append({**proposal, "memory_id": ""})

    with pytest.raises(ValueError) as error:
        validate_memory_changes(
            MemoryChanges.model_validate({"memories": changes}),
            existing,
            [],
            {},
            "self",
            consolidating=True,
        )

    assert ref in str(error.value)
    if fault in identity:
        assert "keep" in str(error.value)
        assert fault in str(error.value) and "different" in str(error.value)


@pytest.mark.asyncio
async def test_stale_rejection_cannot_overwrite_changed_consolidation_job(runtime):
    msg = await message(runtime, "我喜欢简短回复")
    assert await extract(runtime, msg, [atom(msg, "preference")])
    repo = runtime.db.memory_maintenance_repo
    stale_job = await repo.pending_consolidation("bot", "group")
    updated = json.dumps(
        {**stale_job["payload"], "status": "suspended", "failure": "newer"}
    )
    await runtime.db.upsert_kv_data(stale_job["key"], updated)

    assert not await repo.suspend_consolidation(stale_job, {"attempts": []})

    assert await runtime.db.get_kv_data(stale_job["key"]) == updated


@pytest.mark.asyncio
async def test_optimistic_revision_check_preserves_concurrent_edit(runtime):
    msg = await message(runtime, "以后叫我小明")
    assert await extract(runtime, msg, [atom(msg, "preference", topic="name")])
    original = (await rows(runtime))[0]
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    new = await message(runtime, "以后还是叫我小明吧", day=11)
    assert await extract(runtime, new, [atom(new, "preference", topic="name")])
    newest = (await rows(runtime))[-1]

    async def during_inference(*args, **kwargs):
        await runtime.db.conn.execute(
            "UPDATE memories SET text='人工修正', revision=revision+1 WHERE memory_id=?",
            (original["memory_id"],),
        )
        await runtime.db.conn.commit()
        return json.dumps(
            {
                "memories": [
                    atom(
                        new,
                        "preference",
                        memory_id=original["memory_id"],
                        topic="name",
                        evidence=[],
                        source_memory_ids=[original["memory_id"], newest["memory_id"]],
                    )
                ]
            }
        )

    runtime.manager._call_summary_llm = AsyncMock(side_effect=during_inference)
    assert not await runtime.manager.consolidate_memories("bot", "group", "self")
    assert (await rows(runtime))[0]["text"] == "人工修正"
    assert (
        await runtime.db.get_kv_data("passive_memory:last_summarized_id:bot:group")
        == new.db_id
    )
    assert (
        await runtime.db.memory_maintenance_repo.pending_consolidation("bot", "group")
        is not None
    )


@pytest.mark.asyncio
async def test_vector_failure_does_not_lose_memory_and_index_can_retry(runtime):
    runtime.plugin.embedding_conf["enabled"] = True
    msg = await message(runtime, "以后用中文和我说话")
    assert await extract(runtime, msg, [atom(msg, "preference", topic="language")])
    original = (await rows(runtime))[0]
    assert original["indexed_revision"] == 0
    runtime.plugin.ltm.add_memory.return_value = (original["memory_id"], b"vector")
    await runtime.manager._sync_memory_vectors("bot", "group")
    indexed = (await rows(runtime))[0]
    assert indexed["indexed_revision"] == indexed["revision"]
    assert indexed["vector"] == b"vector"
    assert (
        runtime.plugin.ltm.add_memory.call_args.kwargs["memory_id"]
        == indexed["memory_id"]
    )
    runtime.plugin.ltm.search_memory.return_value = [
        {"id": indexed["memory_id"], "metadata": "{}", "_distance": 0.1}
    ]
    recalled = await runtime.manager.search_and_filter_memories(
        "bot", "group", "语言", [msg]
    )
    assert len(recalled) == 1
    await runtime.db.conn.execute(
        "UPDATE memories SET status='superseded', revision=revision+1"
    )
    await runtime.db.conn.commit()
    assert (
        await runtime.manager.search_and_filter_memories("bot", "group", "语言", [msg])
        == []
    )
    assert (
        await runtime.manager.search_and_filter_memories(
            "other-bot", "group", "语言", [msg]
        )
        == []
    )


@pytest.mark.asyncio
async def test_bounded_batches_report_real_committed_source_range(runtime):
    runtime.plugin.conf["memory_config"]["memory_summary_batch_size"] = 2
    for day in (10, 11, 12):
        await message(runtime, f"第{day}天有新的对话", day=day)
    assert "到 2" in await runtime.manager.force_trigger_passive_memory(
        "bot", "group", "self"
    )
    assert (
        await runtime.db.get_kv_data("passive_memory:last_summarized_id:bot:group") == 2
    )
    assert await runtime.db.get_kv_data("passive_memory:silence_armed:bot:group") == 1
    assert "到 3" in await runtime.manager.force_trigger_passive_memory(
        "bot", "group", "self"
    )
    assert await runtime.db.get_kv_data("passive_memory:silence_armed:bot:group") == 0


@pytest.mark.asyncio
async def test_automatic_summary_is_serialized_and_shutdown_cancels_worker(runtime):
    await message(runtime, "我们周末一起玩")
    await runtime.db.upsert_kv_data("passive_memory:silence_armed:bot:group", 1)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def block(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    runtime.manager._call_summary_llm = AsyncMock(side_effect=block)
    await runtime.manager.check_and_trigger_passive_memory("bot", "group", "self")
    await asyncio.wait_for(started.wait(), 1)
    await runtime.manager.check_and_trigger_passive_memory("bot", "group", "self")
    runtime.manager._call_summary_llm.assert_awaited_once()
    assert (
        await runtime.db.get_kv_data("passive_memory:last_summarized_id:bot:group", 0)
        == 0
    )
    assert "正在" in await runtime.manager.force_trigger_passive_memory(
        "bot", "group", "self"
    )
    await runtime.manager.close()
    assert cancelled.is_set()
    assert not runtime.manager.summary_tasks


@pytest.mark.asyncio
async def test_periodic_tick_flushes_quiet_conversation_without_new_arrival(runtime):
    await message(runtime, "还有最后一句对话")
    await runtime.db.upsert_kv_data("passive_memory:silence_armed:bot:group", 1)
    await runtime.db.upsert_kv_data(
        "atomic_memory:session:bot:group",
        json.dumps(
            {
                "bot_name": "bot",
                "session_id": "group",
                "self_id": "self",
            }
        ),
    )
    await runtime.manager.maintain_memories()
    assert (
        await runtime.db.get_kv_data("passive_memory:last_summarized_id:bot:group") == 1
    )
    assert await runtime.db.get_kv_data("passive_memory:silence_armed:bot:group") == 0


@pytest.mark.asyncio
async def test_manual_memory_save_read_and_delete_work_without_embeddings(runtime):
    memory_id = await runtime.plugin.data_cache.add_memory(
        "bot", "group", "人工记忆", "user", ["user"]
    )
    memories = await runtime.plugin.data_cache.get_memories("bot", "group")
    assert [item.memory_id for item in memories] == [memory_id]
    assert await runtime.plugin.data_cache.delete_memory(memory_id)
    assert await runtime.plugin.data_cache.get_memories("bot", "group") == []
    runtime.plugin.ltm.add_memory.assert_not_awaited()
    runtime.plugin.ltm.delete_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_memory_migration_preserves_text_and_index_state(tmp_path):
    async with aiosqlite.connect(tmp_path / "old.db") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(
            "CREATE TABLE memories (id INTEGER PRIMARY KEY, bot_name TEXT, group_or_user_id TEXT, "
            "memory_id TEXT, text TEXT, vector BLOB, metadata TEXT, created_at DATETIME, updated_at DATETIME)"
        )
        await conn.execute(
            "INSERT INTO memories VALUES (1, 'bot', 'group', 'old', '旧记忆', X'01', '{}', '2020-01-01', '2020-01-01')"
        )
        await conn.commit()
        await initialize_database(conn)
        await initialize_database(conn)
        async with conn.execute("SELECT * FROM memories") as cursor:
            saved = await cursor.fetchone()
        assert saved["text"] == "旧记忆"
        assert saved["kind"] == "event"
        assert saved["status"] == "active"
        assert saved["indexed_revision"] == 1
        assert saved["reviewed_revision"] == 0


@pytest.mark.asyncio
async def test_new_arrival_during_summary_stays_pending_for_followup(runtime):
    first = await message(runtime, "第一批对话")
    await runtime.db.upsert_kv_data("passive_memory:silence_armed:bot:group", 1)
    started, release = asyncio.Event(), asyncio.Event()

    async def complete(task_name, *args, **kwargs):
        if task_name == "原子记忆归纳":
            started.set()
            await release.wait()
            return '{"memories": []}'
        return "<summary_user_profile>无</summary_user_profile>"

    runtime.manager._call_summary_llm = AsyncMock(side_effect=complete)
    await runtime.manager.check_and_trigger_passive_memory("bot", "group", "self")
    await asyncio.wait_for(started.wait(), 1)
    task = runtime.manager.summary_tasks["bot:group"]
    second = await message(runtime, "归纳过程中到达的新对话", day=11)
    await runtime.manager.check_and_trigger_passive_memory("bot", "group", "self")
    release.set()
    await asyncio.wait_for(task, 2)
    assert (
        await runtime.db.get_kv_data("passive_memory:last_summarized_id:bot:group")
        == first.db_id
    )
    assert await runtime.db.get_kv_data("passive_memory:silence_armed:bot:group") == 1
    await runtime.manager.force_trigger_passive_memory("bot", "group", "self")
    assert (
        await runtime.db.get_kv_data("passive_memory:last_summarized_id:bot:group")
        == second.db_id
    )


@pytest.mark.asyncio
async def test_source_recalled_during_inference_aborts_transaction(runtime):
    msg = await message(runtime, "我喜欢简短回复")

    async def recall(*args, **kwargs):
        await runtime.db.conn.execute(
            "UPDATE chat_history SET is_recalled=1 WHERE id=?", (msg.db_id,)
        )
        await runtime.db.conn.commit()
        return json.dumps({"memories": [atom(msg, "preference")]})

    runtime.manager._call_summary_llm = AsyncMock(side_effect=recall)
    assert not await runtime.manager._run_memory_summary_task(
        "bot",
        "group",
        "self",
        "Giftia",
        {"alias_observation_messages": [msg]},
    )
    assert await rows(runtime) == []
    assert (
        await runtime.db.get_kv_data("passive_memory:last_summarized_id:bot:group", 0)
        == 0
    )


@pytest.mark.asyncio
async def test_cross_bot_source_cannot_be_committed_to_another_partition(runtime):
    msg = await message(runtime, "我喜欢简短回复")
    await runtime.db.conn.execute("UPDATE chat_history SET bot_name='other-bot'")
    await runtime.db.conn.commit()
    assert not await extract(runtime, msg, [atom(msg, "preference")])
    assert await rows(runtime) == []


@pytest.mark.asyncio
async def test_consolidation_batches_exclude_all_siblings_and_have_no_interval(runtime):
    runtime.plugin.conf["memory_config"]["memory_consolidation_batch_size"] = 10
    msg = await message(runtime, "这是同一批中的一些独立事件")
    assert await extract(
        runtime,
        msg,
        [
            atom(msg, topic=f"event_{index}", text=f"独立事件{index}")
            for index in range(15)
        ],
    )
    runtime.manager._call_summary_llm = AsyncMock(return_value='{"memories": []}')
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    job = await runtime.db.memory_maintenance_repo.pending_consolidation("bot", "group")
    assert len(job["payload"]["pending_ids"]) == 5
    assert len(job["payload"]["memory_ids"]) == 15
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    assert (
        await runtime.db.memory_maintenance_repo.pending_consolidation("bot", "group")
        is None
    )
    runtime.manager._call_summary_llm.assert_not_awaited()
    new = await message(runtime, "又有一个独立事件", day=11)
    assert await extract(runtime, new, [atom(new)])
    runtime.manager._call_summary_llm = AsyncMock(return_value='{"memories": []}')
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    runtime.manager._call_summary_llm.assert_awaited_once()


@pytest.mark.asyncio
async def test_web_edit_preserves_atom_id_state_and_evidence(runtime, monkeypatch):
    import core.web.memory_api as memory_api

    msg = await message(runtime, "以后叫我小明")
    assert await extract(runtime, msg, [atom(msg, "preference", topic="name")])
    before = (await rows(runtime))[0]
    body = {
        "memory_id": before["memory_id"],
        "bot_name": "bot",
        "group_or_user_id": "group",
        "text": "希望称呼为小明",
    }
    monkeypatch.setattr(
        memory_api, "request", SimpleNamespace(json=AsyncMock(return_value=body))
    )
    monkeypatch.setattr(memory_api, "json_response", lambda value: value)
    monkeypatch.setattr(
        memory_api,
        "error_response",
        lambda value: {"status": "error", "message": value},
    )
    api = memory_api.MemoryApi()
    api.giftia = runtime.plugin
    response = await api.update_memory()
    assert response["status"] == "success"
    after = (await rows(runtime))[0]
    assert after["memory_id"] == before["memory_id"]
    assert after["kind"] == "preference"
    assert after["created_at"] == before["created_at"]
    assert (
        json.loads(after["metadata"])["evidence"]
        == json.loads(before["metadata"])["evidence"]
    )
    assert after["revision"] == before["revision"] + 1
    assert api._memory_row_to_dict(after)["status"] == "active"
    body["bot_name"] = "other-bot"
    assert (await api.update_memory())["status"] == "error"
    assert (await rows(runtime))[0]["revision"] == after["revision"]


@pytest.mark.asyncio
async def test_expired_agreement_is_removed_from_temporary_recall_pool(runtime):
    cache = runtime.plugin.data_cache
    cache.merge_session_recalled_memories(
        "bot",
        "group",
        [
            {
                "id": "expired",
                "text": "已经过期的约定",
                "metadata": json.dumps(
                    {"status": "active", "expires_at": "2020-01-01T00:00:00+00:00"}
                ),
            }
        ],
    )
    assert cache.get_session_recalled_memories("bot", "group") == []


def test_retired_portrait_fields_and_titles_do_not_enter_context():
    from core.llm.llm_tools import SearchUserProfileTool
    from core.llm.prompt import build_active_user_briefs, build_user_profile_block

    profile = {
        "user_id": "user",
        "call_name": "小明",
        "attitude": "友好",
        "personality": "旧性格",
        "interests": "旧兴趣",
        "extra": "旧补充",
        "agreements": "早已完成的约定",
        "title": "旧头衔",
    }
    context = build_user_profile_block("user", profile, (3, "旧头衔"))
    context += build_active_user_briefs([profile])
    context += SearchUserProfileTool()._format_profile_result(profile)
    assert "小明" in context
    assert "好感度" in context
    assert "早已完成的约定" not in context
    for retired in ("旧性格", "旧兴趣", "旧补充", "旧头衔"):
        assert retired not in context


@pytest.mark.asyncio
async def test_vector_sync_serializes_and_retries_when_summaries_are_disabled(runtime):
    msg = await message(runtime, "以后叫我小明")
    assert await extract(runtime, msg, [atom(msg, "preference", topic="name")])
    saved = (await rows(runtime))[0]
    runtime.plugin.embedding_conf["enabled"] = True
    started, release = asyncio.Event(), asyncio.Event()

    async def index(**kwargs):
        started.set()
        await release.wait()
        return kwargs["memory_id"], b"vector"

    runtime.plugin.ltm.add_memory.side_effect = index
    first = asyncio.create_task(runtime.manager._sync_memory_vectors("bot", "group"))
    await asyncio.wait_for(started.wait(), 1)
    second = asyncio.create_task(runtime.manager._sync_memory_vectors("bot", "group"))
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 2)
    runtime.plugin.ltm.add_memory.assert_awaited_once()
    await runtime.db.conn.execute(
        "UPDATE memories SET revision=revision+1, text='人工修正'"
    )
    await runtime.db.conn.commit()
    runtime.plugin.passive_memory_enabled = False
    runtime.plugin.passive_memory_provider_ids = []
    runtime.manager._call_summary_llm.reset_mock()
    await runtime.manager.maintain_memories()
    current = (await rows(runtime))[0]
    assert current["memory_id"] == saved["memory_id"]
    assert current["revision"] == current["indexed_revision"]
    assert runtime.plugin.ltm.add_memory.await_count == 2
    runtime.manager._call_summary_llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_shutdown_cancels_manual_summary_before_resources_close(runtime):
    await message(runtime, "当前对话还在等待总结")
    started = asyncio.Event()

    async def block(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    runtime.manager._call_summary_llm = AsyncMock(side_effect=block)
    task = asyncio.create_task(
        runtime.manager.force_trigger_passive_memory("bot", "group", "self")
    )
    await asyncio.wait_for(started.wait(), 1)
    await runtime.manager.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not runtime.manager.maintenance_tasks
    assert (
        await runtime.db.get_kv_data("passive_memory:last_summarized_id:bot:group", 0)
        == 0
    )


@pytest.mark.asyncio
async def test_history_clear_during_empty_inference_does_not_advance_cursor(runtime):
    msg = await message(runtime, "不需要记录的普通闲聊")

    async def clear(*args, **kwargs):
        await runtime.db.delete_chat_history("bot", "group")
        return '{"memories": []}'

    runtime.manager._call_summary_llm = AsyncMock(side_effect=clear)
    assert not await runtime.manager._run_memory_summary_task(
        "bot",
        "group",
        "self",
        "Giftia",
        {"alias_observation_messages": [msg]},
    )
    assert (
        await runtime.db.get_kv_data("passive_memory:last_summarized_id:bot:group", 0)
        == 0
    )


@pytest.mark.asyncio
async def test_partial_agreement_update_preserves_identity_deadline_and_importance(
    runtime,
):
    original = await message(runtime, "我们约好周末一起玩游戏")
    assert await extract(
        runtime,
        original,
        [atom(original, "agreement", importance=9, expires_at="2099-01-01T00:00:00Z")],
    )
    before = (await rows(runtime))[0]
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    completed = await message(runtime, "我们已经一起玩完了", day=11)
    assert await extract(runtime, completed, [atom(completed)])
    newest = (await rows(runtime))[-1]
    update = atom(
        completed,
        "agreement",
        memory_id=before["memory_id"],
        status="completed",
        evidence=[],
        source_memory_ids=[before["memory_id"], newest["memory_id"]],
    )
    update.pop("object_id")
    runtime.manager._call_summary_llm = AsyncMock(
        return_value=json.dumps({"memories": [update]})
    )
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    after = (await rows(runtime))[0]
    assert after["status"] == "completed"
    assert after["importance"] == 9
    assert after["expires_at"] == before["expires_at"]
    assert json.loads(after["metadata"])["object_id"] == "self"


@pytest.mark.asyncio
async def test_same_day_directed_emoji_habit_from_group_events(runtime):
    from xml.etree.ElementTree import fromstring

    for hour in (8, 14, 20):
        msg = await message(
            runtime,
            '<quote sender_id="B" message_id="old">B的旧消息</quote>[图片:smirk]',
            user_id="A",
            hour=hour,
        )
        msg.media_id_list = ["smirk"]
        await runtime.db.conn.execute(
            "UPDATE chat_history SET media_ids=? WHERE id=?",
            (json.dumps(["smirk"]), msg.db_id),
        )
        await runtime.db.conn.commit()
        item = {
            "kind": "event",
            "subject_id": "A",
            "object_id": "B",
            "topic": "smirk_teasing",
            "text": "A回复B时用坏笑表情开玩笑",
            "evidence": [{"source_id": msg.db_id, "media_id": "smirk"}],
        }
        runtime.manager._call_summary_llm = AsyncMock(
            return_value=json.dumps({"memories": [item]})
        )
        assert await runtime.manager._run_memory_summary_task(
            "bot",
            "group",
            "self",
            "Giftia",
            {
                "alias_observation_messages": [msg],
                "memory_media": {"smirk": {"caption": "一张坏笑表情，写着嘿嘿"}},
            },
        )
        prompt = fromstring(runtime.manager._call_summary_llm.call_args.args[2])
        rendered = prompt.find("new_messages/message")
        assert json.loads(rendered.attrib["reply_user_ids"]) == ["B"]
        assert rendered.text == "[图片:smirk]"

    original = await rows(runtime)
    proposal = {
        "kind": "habit",
        "subject_id": "A",
        "object_id": "B",
        "topic": "smirk_teasing",
        "text": "近期A在本群经常用坏笑表情调侃B",
        "origin": "derived",
        "source_memory_ids": [row["memory_id"] for row in original],
    }
    await leave_latest_batch_pending(runtime)
    runtime.manager._call_summary_llm = AsyncMock(
        return_value=json.dumps({"memories": [proposal]})
    )
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    stored = await rows(runtime)
    assert len(stored) == 4
    assert all(
        row["kind"] == "event" and row["status"] == "active" for row in stored[:3]
    )
    habit = json.loads(stored[-1]["metadata"])
    assert habit["origin"] == "derived"
    assert habit["subject_id"] == "A"
    assert habit["object_id"] == "B"
    assert habit["associated_user_ids"] == ["A", "B"]
    assert {ref["user_id"] for ref in habit["evidence"]} == {"A"}
    assert {ref["media_id"] for ref in habit["evidence"]} == {"smirk"}
    assert habit["observed_from"] == "2020-01-10T00:00:00+00:00"
    assert habit["observed_until"] == "2020-01-10T12:00:00+00:00"


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["actor", "target"])
async def test_directed_habit_rejects_mixed_actors_or_targets(runtime, mismatch):
    for index, hour in enumerate((8, 14, 20)):
        actor = "other" if mismatch == "actor" and index == 2 else "user"
        target = "C" if mismatch == "target" and index == 2 else "B"
        msg = await message(
            runtime, f"@群友({target}) 嘿嘿，第{index}次互动", user_id=actor, hour=hour
        )
        assert await extract(
            runtime,
            msg,
            [atom(msg, subject_id=actor, object_id=target, topic="teasing")],
        )
    original = await rows(runtime)
    proposal = {
        "kind": "habit",
        "subject_id": "user",
        "object_id": "B",
        "topic": "teasing",
        "text": "经常调侃B",
        "origin": "derived",
        "source_memory_ids": [row["memory_id"] for row in original],
    }
    await leave_latest_batch_pending(runtime)
    runtime.manager._call_summary_llm = AsyncMock(
        return_value=json.dumps({"memories": [proposal]})
    )
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    assert len(await rows(runtime)) == 3
    assert (
        await runtime.db.memory_maintenance_repo.pending_consolidation("bot", "group")
        is None
    )


@pytest.mark.asyncio
async def test_self_reported_habit_preserves_observed_origin_during_cleanup(runtime):
    msg = await message(runtime, "我平时经常和@小B(B)互发表情开玩笑")
    assert await extract(
        runtime, msg, [atom(msg, "habit", object_id="B", topic="emoji_teasing")]
    )
    original = (await rows(runtime))[0]
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    latest = await message(runtime, "我平时还是经常和@小B(B)互发表情开玩笑", day=11)
    assert await extract(
        runtime, latest, [atom(latest, "habit", object_id="B", topic="emoji_teasing")]
    )
    newest = (await rows(runtime))[-1]
    proposal = {
        "kind": "habit",
        "subject_id": "user",
        "object_id": "B",
        "topic": "emoji_teasing",
        "memory_id": original["memory_id"],
        "text": "本人自述经常与B互发表情开玩笑",
        "origin": "derived",
        "source_memory_ids": [original["memory_id"], newest["memory_id"]],
        "supersedes": [newest["memory_id"]],
    }
    runtime.manager._call_summary_llm = AsyncMock(
        return_value=json.dumps({"memories": [proposal]})
    )
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    current = await rows(runtime)
    assert len(current) == 2 and current[1]["status"] == "superseded"
    assert json.loads(current[0]["metadata"])["origin"] == "observed"
    assert len(json.loads(current[0]["metadata"])["evidence"]) == 2


def test_interaction_target_metadata_excludes_nested_quoted_mentions():
    from core.memory.atomic import DirectSpeech

    parsed = DirectSpeech(
        '<quote sender_id="B"><quote sender_id="C">@假名(fake) 转引</quote>旧话</quote>@小D(D) 嘿嘿'
    )
    assert parsed.reply_user_ids == ["B"]
    assert parsed.mentioned_user_ids == ["D"]
    assert "旧话" not in "".join(parsed.parts)


@pytest.mark.asyncio
async def test_target_identity_does_not_establish_targets_personal_traits(runtime):
    msg = await message(runtime, "@小B(B) 我猜你喜欢这个表情", user_id="A")
    assert not await extract(runtime, msg, [atom(msg, "preference", subject_id="B")])
    assert await rows(runtime) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor_value", [None, 0, -1, "", "999invalid", "1.5", 1.5])
async def test_history_cleanup_protects_missing_or_invalid_memory_cursor(
    runtime, cursor_value
):
    first = await message(runtime, "尚未归纳的旧消息", day=10)
    latest = await message(runtime, "最新消息", day=11)
    if cursor_value is not None:
        await runtime.db.upsert_kv_data(
            "passive_memory:last_summarized_id:bot:group", cursor_value
        )
    assert (
        await runtime.db.auto_clean_chat_history(
            max_count_per_session=1, max_age_days=1, min_keep_per_session=1
        )
        == 0
    )
    remaining = await runtime.db.get_messages_by_id_range(
        "bot", "group", 1, latest.db_id
    )
    assert [msg.db_id for msg in remaining] == [first.db_id, latest.db_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("text_cursor", [False, True])
@pytest.mark.parametrize(
    "max_count,max_age,min_keep,kept_indexes",
    [
        (3, 0, 2, [3, 4, 5]),
        (0, 1, 2, [4, 5]),
        (3, 1, 5, [1, 2, 3, 4, 5]),
        (0, 0, 1, [0, 1, 2, 3, 4, 5]),
    ],
)
async def test_history_cleanup_combines_memory_boundary_and_retention_rules(
    runtime, text_cursor, max_count, max_age, min_keep, kept_indexes
):
    messages = [
        await message(runtime, f"第 {index} 条历史", day=10 + index)
        for index in range(6)
    ]
    cursor = messages[3].db_id
    await runtime.db.upsert_kv_data(
        "passive_memory:last_summarized_id:bot:group",
        str(cursor) if text_cursor else cursor,
    )
    deleted = await runtime.db.auto_clean_chat_history(
        max_count_per_session=max_count,
        max_age_days=max_age,
        min_keep_per_session=min_keep,
    )
    remaining = await runtime.db.get_messages_by_id_range(
        "bot", "group", 1, messages[-1].db_id
    )
    assert deleted == len(messages) - len(kept_indexes)
    assert [msg.db_id for msg in remaining] == [
        messages[index].db_id for index in kept_indexes
    ]


@pytest.mark.asyncio
async def test_history_cleanup_uses_each_bot_and_conversation_cursor(runtime):
    scopes = [("bot", "group"), ("other-bot", "group"), ("bot", "other-group")]
    ids = {scope: [] for scope in scopes}
    for index in range(4):
        for bot, session in scopes:
            await runtime.db.insert_message(
                bot,
                MessageData(
                    user_id="user",
                    nickname="user",
                    group_or_user_id=session,
                    message_id=f"{bot}-{session}-{index}",
                    content="旧消息",
                    time="2020-01-01T12:00:00+08:00",
                ),
            )
            ids[(bot, session)].append(
                await runtime.db.get_max_message_id(bot, session)
            )
    await runtime.db.upsert_kv_data(
        "passive_memory:last_summarized_id:bot:group", ids[scopes[0]][2]
    )
    await runtime.db.upsert_kv_data(
        "passive_memory:last_summarized_id:other-bot:group", ids[scopes[1]][1]
    )
    assert (
        await runtime.db.auto_clean_chat_history(
            max_count_per_session=1, max_age_days=1, min_keep_per_session=1
        )
        == 5
    )
    for scope, expected in [
        (scopes[0], ids[scopes[0]][3:]),
        (scopes[1], ids[scopes[1]][2:]),
        (scopes[2], ids[scopes[2]]),
    ]:
        remaining = await runtime.db.get_messages_by_id_range(*scope, 1, ids[scope][-1])
        assert [msg.db_id for msg in remaining] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "completion", [None, "invalid JSON", '{"memories": [{"kind": "habit"}]}']
)
async def test_failed_extraction_leaves_sources_protected_from_cleanup(
    runtime, completion
):
    handled = await message(runtime, "已经归纳的事件", day=10)
    assert await extract(runtime, handled, [atom(handled)])
    pending = await message(runtime, "归纳失败的事件", day=11)
    latest = await message(runtime, "下一条待处理消息", day=12)
    runtime.manager._call_summary_llm = AsyncMock(return_value=completion)
    assert not await runtime.manager._run_memory_summary_task(
        "bot", "group", "self", "Giftia", {"alias_observation_messages": [pending]}
    )
    assert (
        await runtime.db.auto_clean_chat_history(
            max_count_per_session=1, max_age_days=1, min_keep_per_session=1
        )
        == 1
    )
    remaining = await runtime.db.get_messages_by_id_range(
        "bot", "group", 1, latest.db_id
    )
    assert [msg.db_id for msg in remaining] == [pending.db_id, latest.db_id]
    assert (
        await runtime.db.get_kv_data("passive_memory:last_summarized_id:bot:group")
        == handled.db_id
    )


@pytest.mark.asyncio
async def test_history_cleanup_during_inference_preserves_sources_until_commit(runtime):
    handled = await message(runtime, "已保存的第一条事件", day=10)
    assert await extract(runtime, handled, [atom(handled)])
    pending = await message(runtime, "正在归纳的第二条事件", day=11)
    latest = await message(runtime, "后续待处理消息", day=12)
    started, release = asyncio.Event(), asyncio.Event()

    async def complete(*args, **kwargs):
        started.set()
        await release.wait()
        return json.dumps({"memories": [atom(pending)]})

    runtime.manager._call_summary_llm = AsyncMock(side_effect=complete)
    task = asyncio.create_task(
        runtime.manager._run_memory_summary_task(
            "bot", "group", "self", "Giftia", {"alias_observation_messages": [pending]}
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert (
            await runtime.db.auto_clean_chat_history(
                max_count_per_session=1, max_age_days=1, min_keep_per_session=1
            )
            == 1
        )
        remaining = await runtime.db.get_messages_by_id_range(
            "bot", "group", 1, latest.db_id
        )
        assert [msg.db_id for msg in remaining] == [pending.db_id, latest.db_id]
    finally:
        release.set()
        await asyncio.wait_for(task, 2)
    assert task.result() is True
    assert (
        await runtime.db.get_kv_data("passive_memory:last_summarized_id:bot:group")
        == pending.db_id
    )
    assert (
        await runtime.db.auto_clean_chat_history(
            max_count_per_session=1, max_age_days=1, min_keep_per_session=1
        )
        == 1
    )
    remaining = await runtime.db.get_messages_by_id_range(
        "bot", "group", 1, latest.db_id
    )
    assert [msg.db_id for msg in remaining] == [latest.db_id]
    assert [
        json.loads(row["metadata"])["evidence"][0]["quote"]
        for row in await rows(runtime)
    ] == [handled.content, pending.content]


@pytest.mark.asyncio
async def test_cleaned_source_history_still_supports_habit_consolidation(runtime):
    sources = []
    for day in (10, 11, 12):
        msg = await message(runtime, f"今晚又来找你玩游戏，第{day}天", day=day)
        assert await extract(runtime, msg, [atom(msg)])
        sources.append(msg)
    latest = await message(runtime, "还没归纳的后续话题", day=13)
    original = await rows(runtime)
    assert (
        await runtime.db.auto_clean_chat_history(
            max_count_per_session=1, max_age_days=1, min_keep_per_session=1
        )
        == 3
    )
    remaining = await runtime.db.get_messages_by_id_range(
        "bot", "group", 1, latest.db_id
    )
    assert [msg.db_id for msg in remaining] == [latest.db_id]
    assert await rows(runtime) == original
    await leave_latest_batch_pending(runtime)
    runtime.manager._call_summary_llm = AsyncMock(
        return_value=json.dumps(
            {
                "memories": [
                    {
                        "kind": "habit",
                        "subject_id": "user",
                        "topic": "evening_games",
                        "text": "近期常在晚上找我玩游戏",
                        "origin": "derived",
                        "source_memory_ids": [row["memory_id"] for row in original],
                    }
                ]
            }
        )
    )
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    habit = (await rows(runtime))[-1]
    metadata = json.loads(habit["metadata"])
    assert habit["kind"] == "habit"
    assert {ref["source_id"]: ref["quote"] for ref in metadata["evidence"]} == {
        msg.db_id: msg.content for msg in sources
    }
    assert metadata["source_memory_ids"] == sorted(row["memory_id"] for row in original)


@pytest.mark.asyncio
async def test_successful_empty_extraction_releases_history_for_cleanup(runtime):
    messages = [
        await message(runtime, f"无需长期保存的对话 {index}", day=10 + index)
        for index in range(3)
    ]
    latest = await message(runtime, "尚未处理的消息", day=13)
    runtime.manager._call_summary_llm = AsyncMock(return_value='{"memories": []}')
    assert await runtime.manager._run_memory_summary_task(
        "bot", "group", "self", "Giftia", {"alias_observation_messages": messages}
    )
    assert await rows(runtime) == []
    assert (
        await runtime.db.auto_clean_chat_history(
            max_count_per_session=1, max_age_days=1, min_keep_per_session=1
        )
        == 3
    )
    remaining = await runtime.db.get_messages_by_id_range(
        "bot", "group", 1, latest.db_id
    )
    assert [msg.db_id for msg in remaining] == [latest.db_id]


@pytest.mark.asyncio
async def test_wakeup_after_failed_forced_summary_cannot_release_pending_history(
    runtime,
):
    pending = await message(runtime, "强制归纳失败后必须保留的消息", day=10)
    runtime.manager._call_summary_llm = AsyncMock(return_value="invalid JSON")
    result = await runtime.manager.force_trigger_passive_memory("bot", "group", "self")
    assert "失败" in result
    assert await runtime.db.get_kv_data("passive_memory:silence_armed:bot:group") == 1
    wakeup = await message(runtime, "再次唤醒机器人", day=11)
    await runtime.manager.mark_silence_summary_armed("bot", "group", wakeup.message_id)
    assert (
        await runtime.db.get_kv_data("passive_memory:last_summarized_id:bot:group", 0)
        == 0
    )
    assert (
        await runtime.db.auto_clean_chat_history(
            max_count_per_session=1, max_age_days=1, min_keep_per_session=1
        )
        == 0
    )

    async def complete(task_name, *args, **kwargs):
        if task_name == "原子记忆归纳":
            return json.dumps({"memories": [atom(pending)]})
        return "<summary_user_profile>无</summary_user_profile>"

    runtime.manager._call_summary_llm = AsyncMock(side_effect=complete)
    result = await runtime.manager.force_trigger_passive_memory("bot", "group", "self")
    assert "已归纳" in result
    assert (
        await runtime.db.get_kv_data("passive_memory:last_summarized_id:bot:group")
        == wakeup.db_id
    )
    assert (
        await runtime.db.auto_clean_chat_history(
            max_count_per_session=1, max_age_days=1, min_keep_per_session=1
        )
        == 1
    )
    assert (
        json.loads((await rows(runtime))[0]["metadata"])["evidence"][0]["quote"]
        == pending.content
    )


@pytest.mark.asyncio
async def test_wakeup_preserves_unfinished_summary_backlog_during_cleanup(runtime):
    runtime.plugin.conf["memory_config"]["memory_summary_batch_size"] = 1
    original = [
        await message(runtime, f"积压消息 {index}", day=10 + index)
        for index in range(3)
    ]
    result = await runtime.manager.force_trigger_passive_memory("bot", "group", "self")
    assert "已归纳" in result
    assert (
        await runtime.db.get_kv_data("passive_memory:last_summarized_id:bot:group")
        == original[0].db_id
    )
    assert await runtime.db.get_kv_data("passive_memory:silence_armed:bot:group") == 1
    wakeup = await message(runtime, "积压处理期间再次唤醒", day=13)
    await runtime.manager.mark_silence_summary_armed("bot", "group", wakeup.message_id)
    assert (
        await runtime.db.get_kv_data("passive_memory:last_summarized_id:bot:group")
        == original[0].db_id
    )
    assert (
        await runtime.db.auto_clean_chat_history(
            max_count_per_session=1, max_age_days=1, min_keep_per_session=1
        )
        == 1
    )
    remaining = await runtime.db.get_messages_by_id_range(
        "bot", "group", 1, wakeup.db_id
    )
    assert [msg.db_id for msg in remaining] == [
        original[1].db_id,
        original[2].db_id,
        wakeup.db_id,
    ]


@pytest.mark.asyncio
async def test_wakeup_cursor_update_serializes_with_forced_extraction(
    runtime, monkeypatch
):
    await message(runtime, "唤醒前的未观察历史", day=10)
    wakeup = await message(runtime, "本次唤醒消息", day=11)
    started, release = asyncio.Event(), asyncio.Event()
    original_lookup = runtime.db.get_database_id_by_message_id

    async def lookup(*args, **kwargs):
        started.set()
        await release.wait()
        return await original_lookup(*args, **kwargs)

    monkeypatch.setattr(runtime.db, "get_database_id_by_message_id", lookup)
    task = asyncio.create_task(
        runtime.manager.mark_silence_summary_armed("bot", "group", wakeup.message_id)
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        result = await runtime.manager.force_trigger_passive_memory(
            "bot", "group", "self"
        )
        assert "正在进行记忆维护" in result
        runtime.manager._call_summary_llm.assert_not_awaited()
        assert (
            await runtime.db.auto_clean_chat_history(
                max_count_per_session=1, max_age_days=1, min_keep_per_session=1
            )
            == 0
        )
    finally:
        release.set()
        await asyncio.wait_for(task, 2)
    assert not runtime.manager.passive_memory_locks["bot:group"].locked()
    assert (
        await runtime.db.get_kv_data("passive_memory:last_summarized_id:bot:group")
        == wakeup.db_id - 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,source,narrative,target",
    [
        ("event", "@B 我给你发了个坏笑表情", "我留意到小明给小白发了坏笑表情", "B"),
        (
            "agreement",
            "就这么说定了，周五我和你一起整理照片",
            "我和小明约好周五一起整理照片",
            "self",
        ),
        ("preference", "我喜欢简短回复，以后不用写太长", "我记得小明喜欢简短回复", ""),
        ("habit", "我平时习惯深夜画画", "我记得小明说过，他平时习惯深夜画画", ""),
        ("relationship", "我和 @B 是同学", "我记得小明和小白是同学", "B"),
    ],
)
async def test_bot_perspective_text_preserves_actual_actors_and_source_quotes(
    runtime, kind, source, narrative, target
):
    from xml.etree.ElementTree import fromstring

    from core.llm.prompt import build_reply_prompt

    msg = await message(runtime, source)
    assert await extract(
        runtime, msg, [atom(msg, kind, text=narrative, object_id=target)]
    )
    _, system_prompt, user_prompt = runtime.manager._call_summary_llm.call_args.args
    assert system_prompt.startswith("你是 Giftia（ID：self）。\n")
    assert "{nickname}" not in system_prompt and "{self_id}" not in system_prompt
    owner = fromstring(user_prompt)
    assert owner.attrib["self_id"] == "self"
    assert owner.find("new_messages/message").attrib["user_id"] == "user"
    assert owner.find("new_messages/message").text == source
    saved = (await rows(runtime))[0]
    metadata = json.loads(saved["metadata"])
    assert saved["text"] == narrative
    assert metadata["subject_id"] == "user"
    assert metadata["object_id"] == target
    assert metadata["evidence"][0]["quote"] == source
    assert metadata["evidence"][0]["user_id"] == "user"
    assert metadata["origin"] == "observed"
    injected = fromstring(
        "<context>"
        + build_reply_prompt(
            recent_messages=[],
            media_captions=[],
            bot_status=None,
            long_memories=await runtime.db.get_memories("group", "bot"),
        )
        + "</context>"
    )
    assert injected.find("long_memories/memory").text == narrative


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source,old_text,new_text,target",
    [
        (
            "就这么说定了，周五我和你一起整理照片",
            "流萤与用户约好周五一起整理照片",
            "我和小明约好周五一起整理照片",
            "self",
        ),
        (
            "@B 我给你发了个坏笑表情",
            "用户给 B 发了坏笑表情",
            "我留意到小明给小白发了坏笑表情",
            "B",
        ),
    ],
)
async def test_consolidation_can_rephrase_old_text_without_changing_memory_identity(
    runtime, source, old_text, new_text, target
):
    runtime.plugin.bot_map["bot"]["nickname"] = "流萤"
    msg = await message(runtime, source)
    assert await extract(runtime, msg, [atom(msg, text=old_text, object_id=target)])
    before = (await rows(runtime))[0]
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    latest = await message(runtime, source, day=11)
    assert await extract(
        runtime, latest, [atom(latest, text=old_text, object_id=target)]
    )
    proposal = atom(
        msg,
        memory_id=before["memory_id"],
        text=new_text,
        object_id=target,
        evidence=[],
        source_memory_ids=[before["memory_id"]],
    )
    runtime.manager._call_summary_llm = AsyncMock(
        return_value=json.dumps({"memories": [proposal]})
    )
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    _, system_prompt, _ = runtime.manager._call_summary_llm.call_args.args
    assert system_prompt.startswith("你是 流萤（ID：self）。\n")
    assert "{nickname}" not in system_prompt and "{self_id}" not in system_prompt
    stored = await rows(runtime)
    assert len(stored) == 2
    after = stored[0]
    assert after["memory_id"] == before["memory_id"]
    assert after["created_at"] == before["created_at"]
    assert after["kind"] == before["kind"]
    assert after["status"] == "active"
    assert after["text"] == new_text
    assert after["revision"] == before["revision"] + 1
    old_meta, new_meta = json.loads(before["metadata"]), json.loads(after["metadata"])
    for field in [
        "subject_id",
        "object_id",
        "topic",
        "associated_user_ids",
        "evidence",
    ]:
        assert new_meta[field] == old_meta[field]


@pytest.mark.asyncio
async def test_unmatched_memory_can_be_recalled_by_a_later_addition(runtime):
    from xml.etree.ElementTree import fromstring

    first = await message(runtime, "今天一起进行天文观测")
    assert await extract(runtime, first, [atom(first, topic="astronomy")])
    original = (await rows(runtime))[0]
    runtime.manager._call_summary_llm = AsyncMock(return_value='{"memories": []}')
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    runtime.manager._call_summary_llm.assert_not_awaited()
    assert (
        await runtime.db.memory_maintenance_repo.pending_consolidation("bot", "group")
        is None
    )
    latest = await message(runtime, "我们又一起进行天文观测", day=11)
    assert await extract(runtime, latest, [atom(latest, topic="astronomy")])
    runtime.manager._call_summary_llm = AsyncMock(return_value='{"memories": []}')
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    prompt = fromstring(runtime.manager._call_summary_llm.call_args.args[2])
    assert (
        json.loads(prompt.find("existing_memories/memory").text)["memory_id"]
        == original["memory_id"]
    )
    assert (await rows(runtime))[0]["status"] == "active"


@pytest.mark.asyncio
async def test_shared_legacy_match_is_updated_once_only_after_new_additions(runtime):
    from xml.etree.ElementTree import fromstring

    await runtime.db.conn.execute(
        "INSERT INTO memories (bot_name, group_or_user_id, memory_id, text, metadata, created_at) "
        "VALUES ('bot', 'group', 'legacy-tea', 'user 喜欢绿茶', ?, '2019-01-01')",
        (json.dumps({"user_id": "user", "old_note": "preserved"}),),
    )
    await runtime.db.conn.commit()
    await runtime.manager.maintain_memories()
    runtime.manager._call_summary_llm.assert_not_awaited()
    msg = await message(runtime, "我喜欢绿茶，平时喜欢喝绿茶")
    assert await extract(
        runtime,
        msg,
        [
            atom(msg, "preference", topic="tea", text="user 喜欢绿茶"),
            atom(msg, "preference", topic="tea", text="user 平时喜欢喝绿茶"),
        ],
    )
    new = (await rows(runtime))[1:]
    await runtime.db.conn.execute(
        "UPDATE memories SET indexed_revision=revision WHERE memory_id='legacy-tea'"
    )
    await runtime.db.conn.commit()
    runtime.plugin.embedding_conf["enabled"] = True
    runtime.plugin.ltm.search_memory.return_value = [{"id": "legacy-tea"}]
    proposal = {
        "memory_id": "legacy-tea",
        "kind": "preference",
        "subject_id": "user",
        "topic": "tea",
        "text": "我记得 user 喜欢喝绿茶",
        "source_memory_ids": ["legacy-tea", *[row["memory_id"] for row in new]],
        "supersedes": [row["memory_id"] for row in new],
    }
    runtime.manager._call_summary_llm = AsyncMock(
        return_value=json.dumps({"memories": [proposal]})
    )
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    runtime.manager._call_summary_llm.assert_awaited_once()
    prompt = fromstring(runtime.manager._call_summary_llm.call_args.args[2])
    assert len(prompt.findall("existing_memories/memory")) == 1
    assert len(prompt.findall("new_memories/memory")) == 2
    saved = await rows(runtime)
    assert [row["status"] for row in saved] == ["active", "superseded", "superseded"]
    assert saved[0]["revision"] == 2 and saved[0]["created_at"] == "2019-01-01"
    metadata = json.loads(saved[0]["metadata"])
    assert metadata["schema_version"] == 2 and metadata["old_note"] == "preserved"
    assert len(metadata["evidence"]) == 1
    assert (
        await runtime.db.memory_maintenance_repo.pending_consolidation("bot", "group")
        is None
    )


@pytest.mark.asyncio
async def test_retrieval_and_model_failures_retain_the_new_batch_for_retry(runtime):
    original = await message(runtime, "我喜欢天文观测")
    assert await extract(runtime, original, [atom(original)])
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    old_id = (await rows(runtime))[0]["memory_id"]
    latest = await message(runtime, "我今天又来天文观测", day=11)
    assert await extract(runtime, latest, [atom(latest)])
    repo = runtime.db.memory_maintenance_repo
    before = await repo.pending_consolidation("bot", "group")
    await runtime.db.conn.execute(
        "UPDATE memories SET indexed_revision=revision WHERE memory_id=?", (old_id,)
    )
    await runtime.db.conn.commit()
    runtime.plugin.embedding_conf["enabled"] = True
    runtime.plugin.ltm.search_memory.side_effect = RuntimeError("Embedding unavailable")
    runtime.manager._call_summary_llm = AsyncMock(return_value='{"memories": []}')
    assert not await runtime.manager.consolidate_memories("bot", "group", "self")
    runtime.manager._call_summary_llm.assert_not_awaited()
    assert await repo.pending_consolidation("bot", "group") == before
    runtime.plugin.ltm.search_memory.side_effect = None
    runtime.plugin.ltm.search_memory.return_value = [{"id": old_id}]
    runtime.manager._call_summary_llm = AsyncMock(return_value=None)
    assert not await runtime.manager.consolidate_memories("bot", "group", "self")
    assert await repo.pending_consolidation("bot", "group") == before
    runtime.manager._call_summary_llm = AsyncMock(return_value='{"memories": []}')
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    assert await repo.pending_consolidation("bot", "group") is None
    assert len(await rows(runtime)) == 2
    assert (
        await runtime.db.get_kv_data("passive_memory:last_summarized_id:bot:group")
        == latest.db_id
    )


@pytest.mark.asyncio
async def test_extraction_commit_and_consolidation_job_are_atomic(runtime):
    msg = await message(runtime, "今天我进行天文观测")
    await runtime.db.conn.execute(
        "CREATE TRIGGER reject_memory_job BEFORE INSERT ON kv_store "
        "WHEN NEW.key LIKE 'atomic_memory:consolidation:%' "
        "BEGIN SELECT RAISE(ABORT, 'queue write failed'); END"
    )
    await runtime.db.conn.commit()
    with pytest.raises(aiosqlite.IntegrityError, match="queue write failed"):
        await extract(runtime, msg, [atom(msg)])
    assert await rows(runtime) == []
    assert (
        await runtime.db.memory_maintenance_repo.pending_consolidation("bot", "group")
        is None
    )
    assert (
        await runtime.db.get_kv_data("passive_memory:last_summarized_id:bot:group", 0)
        == 0
    )
    await runtime.db.conn.execute("DROP TRIGGER reject_memory_job")
    await runtime.db.conn.commit()
    assert await extract(runtime, msg, [atom(msg)])
    assert len(await rows(runtime)) == 1
    assert (
        await runtime.db.memory_maintenance_repo.pending_consolidation("bot", "group")
        is not None
    )
    assert (
        await runtime.db.get_kv_data("passive_memory:last_summarized_id:bot:group")
        == msg.db_id
    )


@pytest.mark.asyncio
async def test_replaying_completed_extraction_does_not_requeue_consolidation(runtime):
    msg = await message(runtime, "今天我进行天文观测")
    assert await extract(runtime, msg, [atom(msg)])
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    assert await extract(runtime, msg, [atom(msg)])
    runtime.manager._call_summary_llm.assert_not_awaited()
    assert len(await rows(runtime)) == 1
    assert (
        await runtime.db.memory_maintenance_repo.pending_consolidation("bot", "group")
        is None
    )


@pytest.mark.asyncio
async def test_restart_can_resume_a_new_batch_without_enqueuing_historical_rows(
    runtime,
):
    msg = await message(runtime, "今天我进行天文观测")
    assert await extract(runtime, msg, [atom(msg)])
    resumed = PassiveMemoryManager(runtime.plugin)
    resumed._call_summary_llm = AsyncMock(return_value='{"memories": []}')
    try:
        assert await resumed.consolidate_memories("bot", "group")
        resumed._call_summary_llm.assert_not_awaited()
        assert (
            await runtime.db.memory_maintenance_repo.pending_consolidation(
                "bot", "group"
            )
            is None
        )
    finally:
        await resumed.close()


@pytest.mark.asyncio
async def test_background_extraction_starts_an_independent_consolidation_worker(
    runtime,
):
    await runtime.db.conn.execute(
        "INSERT INTO memories (bot_name, group_or_user_id, memory_id, text, metadata) "
        "VALUES ('bot', 'group', 'legacy', '一起天文观测', '{\"user_id\":\"user\"}')"
    )
    await runtime.db.conn.commit()
    msg = await message(runtime, "今天我们一起天文观测")
    started = asyncio.Event()

    async def complete(task_name, *args, **kwargs):
        if task_name == "原子记忆归纳":
            return json.dumps({"memories": [atom(msg)]})
        if task_name == "关系画像维护":
            return "<summary_user_profile>无</summary_user_profile>"
        started.set()
        await asyncio.Event().wait()

    runtime.manager._call_summary_llm = AsyncMock(side_effect=complete)
    assert await runtime.manager._run_background_summarize(
        "bot", "group", "self", msg.db_id, msg.db_id
    )
    await asyncio.wait_for(started.wait(), 1)
    assert (
        await runtime.db.get_kv_data("passive_memory:last_summarized_id:bot:group")
        == msg.db_id
    )
    assert "bot:group" in runtime.manager.consolidation_tasks
    await runtime.manager.close()
    assert not runtime.manager.consolidation_tasks
    assert (
        await runtime.db.memory_maintenance_repo.pending_consolidation("bot", "group")
        is not None
    )
    assert [
        call.args[0] for call in runtime.manager._call_summary_llm.call_args_list
    ].count("原子记忆归纳") == 1


@pytest.mark.asyncio
async def test_empty_extraction_never_starts_a_consolidation_worker(runtime):
    msg = await message(runtime, "随便闲聊一句")
    assert await runtime.manager._run_background_summarize(
        "bot", "group", "self", msg.db_id, msg.db_id
    )
    assert not runtime.manager.consolidation_tasks
    assert (
        await runtime.db.memory_maintenance_repo.pending_consolidation("bot", "group")
        is None
    )
    runtime.plugin.ltm.search_memory.assert_not_awaited()
    assert all(
        call.args[0] != "关联记忆整理"
        for call in runtime.manager._call_summary_llm.call_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("external", [False, True])
async def test_semantic_recall_excludes_new_batch_before_limit_and_propagates_failures(
    external,
):
    from core.memory.memory import LTM

    ltm = object.__new__(LTM)
    ltm._lazy_initialized = True
    ltm.embedding_conf = {"use_external_provider": external}
    ltm.embed_provider = SimpleNamespace(get_embedding=AsyncMock(return_value=[0.1]))
    ltm.embed_model = SimpleNamespace(embed=Mock(return_value=[[0.1]]))
    builder = Mock()
    builder.where.return_value = builder
    builder.limit.return_value = builder
    builder.to_list.return_value = [{"id": "old", "_distance": 0.1}]
    ltm.table = SimpleNamespace(search=Mock(return_value=builder))
    found = await ltm.search_memory(
        "bot", "group", "query", limit=1, exclude_ids=["new'one"], raise_errors=True
    )
    assert [item["id"] for item in found] == ["old"]
    assert "id NOT IN ('new''one')" in builder.where.call_args.args[0]
    assert builder.where.call_args.kwargs["prefilter"] is True
    builder.to_list.side_effect = RuntimeError("Index unavailable")
    with pytest.raises(RuntimeError, match="Index unavailable"):
        await ltm.search_memory("bot", "group", "query", raise_errors=True)
    assert await ltm.search_memory("bot", "group", "query") == []


@pytest.mark.asyncio
async def test_strict_rerank_failures_are_retryable(runtime):
    from core.memory.memory import LTM

    ltm = object.__new__(LTM)
    ltm._lazy_initialized = True
    ltm.rerank_conf = {"use_external_provider": True}
    ltm.rerank_provider = SimpleNamespace(
        rerank=AsyncMock(side_effect=RuntimeError("Rerank unavailable"))
    )
    records = [{"id": "old", "text": "history"}]
    with pytest.raises(RuntimeError, match="Rerank unavailable"):
        await ltm.rerank_memories("query", records, raise_errors=True)
    assert await ltm.rerank_memories("query", records) == records


@pytest.mark.asyncio
async def test_incomplete_index_does_not_turn_a_related_memory_into_a_no_match(runtime):
    original = await message(runtime, "我喜欢天文观测")
    assert await extract(runtime, original, [atom(original)])
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    old_id = (await rows(runtime))[0]["memory_id"]
    latest = await message(runtime, "我今天又来天文观测", day=11)
    assert await extract(runtime, latest, [atom(latest)])
    runtime.plugin.embedding_conf["enabled"] = True
    runtime.plugin.ltm.search_memory.return_value = []
    runtime.manager._call_summary_llm = AsyncMock(return_value='{"memories": []}')
    assert not await runtime.manager.consolidate_memories("bot", "group", "self")
    runtime.manager._call_summary_llm.assert_not_awaited()
    assert (
        await runtime.db.memory_maintenance_repo.pending_consolidation("bot", "group")
        is not None
    )
    runtime.plugin.ltm.add_memory.side_effect = lambda **kwargs: (
        kwargs["memory_id"],
        b"vector",
    )
    runtime.plugin.ltm.search_memory.return_value = [{"id": old_id}]
    assert await runtime.manager.consolidate_memories("bot", "group", "self")
    runtime.manager._call_summary_llm.assert_awaited_once()
    assert (
        await runtime.db.memory_maintenance_repo.pending_consolidation("bot", "group")
        is None
    )


@pytest.mark.asyncio
async def test_consolidation_runs_while_profile_maintenance_still_holds_summary_lock(
    runtime,
):
    await runtime.db.conn.execute(
        "INSERT INTO memories (bot_name, group_or_user_id, memory_id, text, metadata) "
        "VALUES ('bot', 'group', 'legacy', '一起天文观测', '{\"user_id\":\"user\"}')"
    )
    await runtime.db.conn.commit()
    msg = await message(runtime, "今天我们一起天文观测")
    profile_started, release_profile, consolidated = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    async def complete(task_name, *args, **kwargs):
        if task_name == "原子记忆归纳":
            return json.dumps({"memories": [atom(msg)]})
        if task_name == "关系画像维护":
            profile_started.set()
            await release_profile.wait()
            return "<summary_user_profile>无</summary_user_profile>"
        consolidated.set()
        return '{"memories": []}'

    async def summarize():
        lock = runtime.manager.passive_memory_locks.setdefault(
            "bot:group", asyncio.Lock()
        )
        async with lock:
            return await runtime.manager._run_background_summarize(
                "bot", "group", "self", msg.db_id, msg.db_id
            )

    runtime.manager._call_summary_llm = AsyncMock(side_effect=complete)
    task = asyncio.create_task(summarize())
    try:
        await asyncio.wait_for(profile_started.wait(), 1)
        await asyncio.wait_for(consolidated.wait(), 1)
        assert not task.done()
        release_profile.set()
        assert await task
    finally:
        release_profile.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_new_batch_wakes_a_consolidation_worker_about_to_exit(runtime):
    looked_empty, release = asyncio.Event(), asyncio.Event()
    consume = runtime.manager.consolidate_memories
    calls = 0

    async def empty_then_consume(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            looked_empty.set()
            await release.wait()
            return False
        return await consume(*args, **kwargs)

    runtime.manager.consolidate_memories = empty_then_consume
    runtime.manager._schedule_memory_consolidation("bot", "group", "self")
    await asyncio.wait_for(looked_empty.wait(), 1)
    msg = await message(runtime, "今天我们一起天文观测")
    assert await extract(runtime, msg, [atom(msg)])
    runtime.manager._schedule_memory_consolidation("bot", "group", "self")
    worker = runtime.manager.consolidation_tasks["bot:group"]
    release.set()
    await asyncio.wait_for(worker, 1)
    assert (
        await runtime.db.memory_maintenance_repo.pending_consolidation("bot", "group")
        is None
    )
    assert not runtime.manager.consolidation_tasks
