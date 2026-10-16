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


async def message(
    runtime, text, user_id="user", day=10, role="message", hour=12, media_ids=None
):
    """Persist a source message with an actual database identity.

    Args:
        runtime: Isolated test runtime.
        text: Direct source speech.
        user_id: Source account.
        day: Distinct source date.
        role: Stored message role.
        hour: Source hour within the selected day.
        media_ids: Directly attached media identifiers.

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
        media_id_list=list(media_ids or []),
    )
    await runtime.db.insert_message("bot", msg)
    rows = await runtime.db.get_messages_by_id_range("bot", "group", 1, 10000)
    return next(row for row in rows if row.message_id == msg.message_id)


def atom(msg, **updates):
    """Create a proposed atom supported by a real source.

    Args:
        msg: Original source message.
        updates: Fields overriding the standard proposal.

    Returns:
        Model response item.
    """
    return {
        "associated_user_ids": [msg.user_id],
        "topic": "game",
        "text": msg.content,
        "evidence": [{"source_id": msg.db_id}],
        **updates,
    }


async def extract(runtime, msg, items, media=None):
    """Run production extraction against a synthetic model response.

    Args:
        runtime: Isolated test runtime.
        msg: New source message.
        items: Proposed memories from the mock model.
        media: Available completed media captions.

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
        {"alias_observation_messages": [msg], "memory_media": media or {}},
    )


async def rows(runtime):
    """Read all facts in creation order.

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
    assert await extract(runtime, msg, [atom(msg, topic="preferred_name")])
    saved = (await rows(runtime))[0]
    metadata = json.loads(saved["metadata"])
    assert not {"kind", "status", "expires_at", "reviewed_revision"} & saved.keys()
    assert metadata["evidence"][0]["source_id"] == msg.db_id
    assert metadata["evidence"][0]["message_id"] == msg.message_id
    assert metadata["evidence"][0]["time"] == msg.time
    assert metadata["evidence"][0]["quote"] == msg.content
    assert metadata["occurred_at"] == ""
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
async def test_id_only_references_snapshot_full_direct_messages_and_deduplicate(
    runtime,
):
    first_text = (
        "  我喜欢简短回复，保留原来的标点“🙂”。\n  " + "这是一条较长的原始发言。" * 60
    )
    first = await message(
        runtime,
        '<quote sender_id="other"><quote>嵌套转述</quote>他人的发言</quote>'
        + first_text,
    )
    second = await message(
        runtime, "没错，长篇回复看起来累，简洁些就好。\n谢谢。", day=11
    )
    proposal = atom(
        first,
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
            "media_ids": [],
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
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call_name,nickname,display_name",
    [
        ("小明", "平台昵称A", "小明"),
        ("小明 & <朋友>", "平台昵称A", "小明 & <朋友>"),
        (None, "平台昵称A", "平台昵称A"),
        (None, "", "11111"),
    ],
)
async def test_summary_receives_scoped_call_names_without_rewriting_sources(
    runtime, call_name, nickname, display_name
):
    from xml.etree.ElementTree import fromstring

    first = await message(runtime, "我和 @22222 是同学", user_id="11111")
    second = await message(runtime, "我们是同学", user_id="22222")
    await runtime.db.conn.executemany(
        "UPDATE chat_history SET nickname=? WHERE id=?",
        [(nickname, first.db_id), ("平台昵称B", second.db_id)],
    )
    await runtime.db.conn.commit()
    for bot_name, session_id, user_id, name in [
        ("bot", "group", "11111", call_name),
        ("bot", "group", "22222", "小白"),
        ("other-bot", "group", "11111", "其他Bot的称呼"),
        ("bot", "other-group", "11111", "其他会话的称呼"),
        ("bot", "group", "33333", "本批未发言用户的称呼"),
    ]:
        await runtime.db.upsert_user_profile(
            bot_name,
            session_id,
            user_id,
            profile_fields={"call_name": name},
        )
    narrative = f"{display_name}和小白是同学。"
    completion = json.dumps(
        {
            "memories": [
                atom(
                    first,
                    text=narrative,
                    associated_user_ids=["11111", "22222"],
                    topic="同学关系",
                )
            ]
        }
    )
    runtime.manager._call_summary_llm = AsyncMock(
        side_effect=lambda task, *args, **kwargs: (
            completion
            if task == "原子记忆归纳"
            else "<summary_user_profile>无</summary_user_profile>"
        )
    )
    assert await runtime.manager._run_background_summarize(
        "bot", "group", "self", first.db_id, second.db_id
    )
    summary_call = next(
        call
        for call in runtime.manager._call_summary_llm.await_args_list
        if call.args[0] == "原子记忆归纳"
    )
    system_prompt, user_prompt = summary_call.args[1:]
    root = fromstring(user_prompt)
    members = root.find("active_users")
    assert members is not None and list(members) == []
    member_line = next(
        line for line in members.text.splitlines() if line.startswith("- 11111")
    )
    if call_name:
        assert f"你对他的称呼: {call_name}" in member_line
    else:
        assert "你对他的称呼" not in member_line
    if nickname:
        assert f"群内昵称: {nickname}" in member_line
    assert "你对他的称呼: 小白" in members.text
    for omitted in ("其他Bot的称呼", "其他会话的称呼", "本批未发言用户的称呼"):
        assert omitted not in user_prompt
    source = root.find("new_messages/message")
    assert source.attrib["user_id"] == "11111"
    assert source.attrib["nickname"] == nickname
    assert source.text == first.content
    assert "优先使用 active_users" in system_prompt
    saved = (await rows(runtime))[0]
    metadata = json.loads(saved["metadata"])
    assert saved["text"] == narrative
    assert metadata["associated_user_ids"] == ["11111", "22222"]
    assert metadata["evidence"][0]["quote"] == first.content
    assert metadata["evidence"][0]["user_id"] == "11111"
    originals = await runtime.db.get_messages_by_id_range(
        "bot", "group", first.db_id, second.db_id
    )
    assert originals[0].nickname == nickname
    assert originals[0].content == first.content


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
    invalid = atom(msg)
    if fault == "empty":
        msg.content = " \n "
    elif fault == "source":
        invalid["evidence"][0]["source_id"] += 1
    elif fault == "outside_batch":
        outside = await message(runtime, "这条消息不在归纳输入中", user_id="other")
        invalid["evidence"][0]["source_id"] = outside.db_id
    elif fault == "participant":
        invalid["associated_user_ids"] = ["stranger"]
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
async def test_quoted_only_content_and_unavailable_media_are_not_direct_evidence(
    runtime,
):
    msg = await message(runtime, '<quote sender_id="other">我喜欢长回复</quote>')
    assert not await extract(
        runtime,
        msg,
        [
            atom(
                msg,
                evidence=[{"source_id": msg.db_id}],
            )
        ],
    )
    msg.content += "[图片:image]"
    msg.media_id_list = ["image"]
    assert not await extract(
        runtime,
        msg,
        [
            atom(
                msg,
                evidence=[{"source_id": msg.db_id}],
            )
        ],
    )
    assert await extract(
        runtime,
        msg,
        [atom(msg)],
        media={"image": {"text": "图片文字", "caption": "图片内容"}},
    )
    evidence = json.loads((await rows(runtime))[0]["metadata"])["evidence"]
    assert evidence == [
        {
            "source_id": msg.db_id,
            "media_ids": ["image"],
            "quote": "[图片:image]\n媒体转述：图片文字\n图片内容",
            "user_id": msg.user_id,
            "message_id": msg.message_id,
            "time": msg.time,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "unattached",
        "empty_caption",
        "command",
        "quoted",
        "forwarded",
        "bare_placeholder",
    ],
)
async def test_media_references_cannot_bypass_source_validation(runtime, fault):
    from xml.etree.ElementTree import fromstring

    msg = await message(runtime, "[图片:image]")
    msg.media_id_list = ["image"]
    media = {"image": {"caption": "一张聊天截图"}}
    if fault == "unattached":
        msg.media_id_list = ["another-image"]
    elif fault == "empty_caption":
        media["image"] = {"caption": "  "}
    elif fault == "command":
        msg.content = "/保存 [图片:image]"
    elif fault == "quoted":
        msg.content = '<quote sender_id="other">[图片:image]</quote>'
    elif fault == "forwarded":
        msg.content = "[合并转发:forward]"
    elif fault == "bare_placeholder":
        msg.content = "[图片]"
    proposal = atom(msg)
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
async def test_message_evidence_collects_text_and_direct_media_once(runtime):
    from xml.etree.ElementTree import fromstring

    msg = await message(
        runtime,
        '这是我的近况<quote sender_id="other">[图片:quoted]</quote>'
        "[图片:image][语音:audio][视频:video][图片:missing][图片:empty]",
        media_ids=["image", "audio", "video", "image", "missing", "empty", "quoted"],
    )
    media = {
        "image": {"text": "照片文字", "caption": "照片内容"},
        "audio": {"text": "语音转写"},
        "video": {"caption": "视频转述"},
        "empty": {"caption": "  "},
        "quoted": {"caption": "引用中的媒体内容"},
        "unattached": {"caption": "其他消息的媒体内容"},
    }
    assert await extract(
        runtime,
        msg,
        [atom(msg, evidence=[{"source_id": msg.db_id}, {"source_id": msg.db_id}])],
        media=media,
    )

    evidence = json.loads((await rows(runtime))[0]["metadata"])["evidence"]
    assert len(evidence) == 1
    assert evidence[0]["media_ids"] == ["image", "audio", "video"]
    assert evidence[0]["source_id"] == msg.db_id
    for text in ("这是我的近况", "照片文字", "照片内容", "语音转写", "视频转述"):
        assert text in evidence[0]["quote"]
    assert "引用中的媒体内容" not in evidence[0]["quote"]
    assert "其他消息的媒体内容" not in evidence[0]["quote"]
    prompt = fromstring(runtime.manager._call_summary_llm.call_args.args[2])
    available = {item.attrib["id"] for item in prompt.findall("available_media/media")}
    assert "quoted" not in available and "unattached" not in available
    attached = json.loads(prompt.find("new_messages/message").attrib["media_ids"])
    assert "quoted" not in attached


@pytest.mark.asyncio
async def test_vector_failure_does_not_lose_memory_and_index_can_retry(runtime):
    runtime.plugin.embedding_conf["enabled"] = True
    msg = await message(runtime, "以后用中文和我说话")
    assert await extract(runtime, msg, [atom(msg, topic="language")])
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
    await runtime.db.conn.execute("UPDATE memories SET revision=revision+1")
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
async def test_bounded_batches_report_real_committed_source_range(runtime, monkeypatch):
    monkeypatch.setattr("core.memory.passive_tasks.MEMORY_SUMMARY_BATCH_SIZE", 2)
    # Removed configuration keys must not override the internal batch limits.
    runtime.plugin.conf["memory_config"]["memory_summary_batch_size"] = 1
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
async def test_memory_table_initialization_has_current_fields_and_preserves_index_state(
    tmp_path,
):
    async with aiosqlite.connect(tmp_path / "memory.db") as conn:
        conn.row_factory = aiosqlite.Row
        await initialize_database(conn)
        await conn.execute(
            "INSERT INTO memories (bot_name, group_or_user_id, memory_id, text, vector) "
            "VALUES ('bot', 'group', 'saved', '已有记忆', X'01')"
        )
        await conn.commit()
        await initialize_database(conn)
        async with conn.execute("SELECT * FROM memories") as cursor:
            saved = await cursor.fetchone()
        assert saved["text"] == "已有记忆"
        assert not {"kind", "status", "expires_at", "reviewed_revision"} & set(
            saved.keys()
        )
        assert saved["revision"] == 1
        assert saved["indexed_revision"] == 0
        async with conn.execute("PRAGMA index_list(memories)") as cursor:
            indexes = {row["name"] for row in await cursor.fetchall()}
        assert "idx_memory_id_unique" in indexes
        assert "idx_memory_maintenance" not in indexes


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
        return json.dumps({"memories": [atom(msg)]})

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
    assert not await extract(runtime, msg, [atom(msg)])
    assert await rows(runtime) == []


@pytest.mark.asyncio
async def test_web_edit_preserves_fact_id_and_evidence(runtime, monkeypatch):
    import core.web.memory_api as memory_api

    msg = await message(runtime, "以后叫我小明")
    assert await extract(runtime, msg, [atom(msg, topic="name")])
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
    assert after["created_at"] == before["created_at"]
    assert (
        json.loads(after["metadata"])["evidence"]
        == json.loads(before["metadata"])["evidence"]
    )
    assert after["revision"] == before["revision"] + 1
    assert not {"kind", "status", "expires_at"} & api._memory_row_to_dict(after).keys()
    body["bot_name"] = "other-bot"
    assert (await api.update_memory())["status"] == "error"
    assert (await rows(runtime))[0]["revision"] == after["revision"]


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
    assert await extract(runtime, msg, [atom(msg, topic="name")])
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
async def test_self_reported_fact_keeps_narrative_and_evidence_during_cleanup(runtime):
    msg = await message(runtime, "我平时经常和@小B(B)互发表情开玩笑")
    assert await extract(
        runtime,
        msg,
        [atom(msg, associated_user_ids=["user", "B"], topic="emoji_teasing")],
    )
    original = (await rows(runtime))[0]
    await runtime.db.conn.execute("DELETE FROM chat_history")
    await runtime.db.conn.commit()
    current = (await rows(runtime))[0]
    assert current["memory_id"] == original["memory_id"]
    assert current["text"] == msg.content
    metadata = json.loads(current["metadata"])
    assert metadata["associated_user_ids"] == ["B", "user"]
    assert metadata["evidence"][0]["quote"] == msg.content
    assert metadata["evidence"][0]["source_id"] == msg.db_id


def test_interaction_target_metadata_excludes_nested_quoted_mentions():
    from core.memory.atomic import DirectSpeech

    parsed = DirectSpeech(
        '<quote sender_id="B"><quote sender_id="C">@假名(fake) 转引</quote>旧话</quote>@小D(D) 嘿嘿'
    )
    assert parsed.reply_user_ids == ["B"]
    assert parsed.mentioned_user_ids == ["D"]
    assert "旧话" not in "".join(parsed.parts)


@pytest.mark.asyncio
async def test_multiple_participants_keep_their_roles_in_the_narrative(runtime):
    msg = await message(runtime, "@小B(B)邀请@小C(C)参加活动，我负责记录", user_id="A")
    text = "我看到 B 邀请 C 参加活动，A 负责记录"
    assert await extract(
        runtime, msg, [atom(msg, text=text, associated_user_ids=["A", "B", "C"])]
    )
    saved = (await rows(runtime))[0]
    metadata = json.loads(saved["metadata"])
    assert saved["text"] == text
    assert metadata["associated_user_ids"] == ["A", "B", "C"]
    assert metadata["evidence"][0]["quote"] == msg.content
    assert not {"subject_id", "object_id", "origin"} & metadata.keys()


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
async def test_wakeup_preserves_unfinished_summary_backlog_during_cleanup(
    runtime, monkeypatch
):
    monkeypatch.setattr("core.memory.passive_tasks.MEMORY_SUMMARY_BATCH_SIZE", 1)
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
    "source,narrative,target",
    [
        ("@B 我给你发了个坏笑表情", "我留意到小明给小白发了坏笑表情", "B"),
        (
            "就这么说定了，周五我和你一起整理照片",
            "我和小明约好周五一起整理照片",
            "self",
        ),
        ("我喜欢简短回复，以后不用写太长", "我记得小明喜欢简短回复", ""),
        ("我平时习惯深夜画画", "我记得小明说过，他平时习惯深夜画画", ""),
        ("我和 @B 是同学", "我记得小明和小白是同学", "B"),
    ],
)
async def test_bot_perspective_text_preserves_actual_actors_and_source_quotes(
    runtime, source, narrative, target
):
    from xml.etree.ElementTree import fromstring

    from core.llm.prompt import build_reply_prompt

    msg = await message(runtime, source)
    assert await extract(
        runtime,
        msg,
        [
            atom(
                msg,
                text=narrative,
                associated_user_ids=[
                    uid for uid in ("user", target) if uid and uid != "self"
                ],
            )
        ],
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
    assert metadata["associated_user_ids"] == sorted(
        uid for uid in ("user", target) if uid and uid != "self"
    )
    assert metadata["evidence"][0]["quote"] == source
    assert metadata["evidence"][0]["user_id"] == "user"
    assert not {"subject_id", "object_id", "origin"} & metadata.keys()
    injected = fromstring(
        "<context>"
        + build_reply_prompt(
            recent_messages=[],
            media_captions=[],
            bot_status=None,
            relevant_memories=[saved],
        )
        + "</context>"
    )
    assert injected.find("relevant_memories/memory").text == narrative


@pytest.mark.asyncio
async def test_new_facts_append_without_rewriting_history_or_starting_other_model_tasks(
    runtime,
):
    first = await message(runtime, "我最喜欢卡夫卡")
    assert await extract(runtime, first, [atom(first, topic="角色喜好")])
    original = (await rows(runtime))[0]
    latest = await message(runtime, "现在最喜欢黑天鹅，卡夫卡第二", day=11)
    assert await extract(runtime, latest, [atom(latest, topic="角色喜好")])
    stored = await rows(runtime)
    assert len(stored) == 2
    assert stored[0] == original
    assert stored[1]["text"] == latest.content
    assert runtime.manager._call_summary_llm.await_count == 1
    runtime.plugin.ltm.search_memory.assert_not_awaited()
    async with runtime.db.conn.execute(
        "SELECT key FROM kv_store WHERE key LIKE 'atomic_memory:consolidation:%'"
    ) as cursor:
        assert await cursor.fetchall() == []
    assert not hasattr(runtime.manager, "consolidate_memories")
    assert not hasattr(runtime.manager, "consolidation_tasks")


@pytest.mark.asyncio
async def test_fact_writes_and_source_checkpoint_rollback_together(runtime):
    msg = await message(runtime, "以后叫我小明")
    await runtime.db.conn.execute(
        "CREATE TRIGGER reject_checkpoint BEFORE INSERT ON kv_store "
        "WHEN NEW.key LIKE 'atomic_memory:batch:%' "
        "BEGIN SELECT RAISE(ABORT, 'checkpoint failure'); END"
    )
    await runtime.db.conn.commit()
    with pytest.raises(aiosqlite.IntegrityError, match="checkpoint failure"):
        await extract(runtime, msg, [atom(msg)])
    assert await rows(runtime) == []
    assert (
        await runtime.db.get_kv_data("passive_memory:last_summarized_id:bot:group", 0)
        == 0
    )
    await runtime.db.conn.execute("DROP TRIGGER reject_checkpoint")
    await runtime.db.conn.commit()
    assert await extract(runtime, msg, [atom(msg)])
    assert len(await rows(runtime)) == 1


def test_extraction_schema_only_requests_facts_and_evidence():
    from core.memory.atomic import MemoryRecords

    fields = MemoryRecords.model_json_schema()["$defs"]["MemoryRecord"]["properties"]
    assert set(fields) == {
        "associated_user_ids",
        "topic",
        "text",
        "occurred_at",
        "importance",
        "evidence",
    }
