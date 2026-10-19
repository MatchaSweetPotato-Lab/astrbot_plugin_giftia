"""Verify compact recall context and removal of model-driven memory deletion."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from xml.etree.ElementTree import fromstring

import pytest
from core.bot.bot_config_manager import BotConfigManager
from core.conversation.decision_engine import DecisionEngine
from core.conversation.tool_executor import ToolExecutor
from core.llm.preset_prompts import build_xml_instructions
from core.llm.prompt import build_reply_prompt
from core.llm.xml_parse import XmlParse
from core.utils.schemas import (
    Decision,
    FeatureKey,
    MessageData,
    SessionRecallMemory,
    XmlLlmResult,
)


def test_all_recall_sources_share_compact_context_and_current_results_win():
    current = {
        "id": "internal-current",
        "text": "我记得群友甲喜欢 <简短> 回复。",
        "metadata": json.dumps(
            {
                "kind": "preference",
                "occurred_at": "2026-10-10T12:00:00+08:00",
                "subject_id": "A",
                "object_id": "B",
                "origin": "observed",
                "evidence": [{"source_id": 99, "quote": "hidden source snapshot"}],
            }
        ),
    }
    cached = [
        SessionRecallMemory(memory_id="internal-current", text="stale cached version"),
        SessionRecallMemory(
            memory_id="internal-habit",
            text="我多次看到甲晚上上线。",
            metadata=json.dumps(
                {
                    "kind": "habit",
                    "observed_from": "2026-10-01",
                    "observed_until": "2026-10-12",
                }
            ),
        ),
    ]
    legacy = SessionRecallMemory(
        memory_id="internal-legacy",
        text="旧记录 & 原文",
        metadata="broken json",
        created_at="2020-01-01T00:00:00+00:00",
        updated_at="2099-01-01T00:00:00+00:00",
    )
    prompt = build_reply_prompt(
        recent_messages=[],
        media_captions=[],
        bot_status=None,
        relevant_memories=[current, current],
        session_recalled_memories=[*cached, legacy],
    )
    root = fromstring("<context>" + prompt + "</context>")
    assert len(root.findall(".//memory")) == 3
    relevant = root.find("relevant_memories/memory")
    assert relevant.text == current["text"]
    assert set(relevant.attrib) == {"occurred_at"}
    habit = root.find("session_recalled_memories/memory")
    assert set(habit.attrib) == {"observed_from", "observed_until"}
    old = root.findall("session_recalled_memories/memory")[-1]
    assert set(old.attrib) == {"recorded_at"}
    assert old.attrib["recorded_at"].startswith("2020-01-01")
    assert old.text == legacy.text
    for omitted in (
        "internal-",
        "stale cached version",
        "hidden source snapshot",
        "subject_id",
        "object_id",
        "origin=",
        "2099-",
    ):
        assert omitted not in prompt


def test_legacy_classification_does_not_enter_context_or_invent_event_time():
    prompt = build_reply_prompt(
        recent_messages=[],
        media_captions=[],
        bot_status=None,
        relevant_memories=[
            {
                "id": "internal-promise",
                "text": "我和甲约好周末一起观星。",
                "created_at": "2026-10-10T12:00:00+08:00",
                "metadata": {
                    "kind": "agreement",
                    "expires_at": "2026-10-18T23:00:00+08:00",
                },
            }
        ],
    )
    memory = fromstring("<context>" + prompt + "</context>").find(
        "relevant_memories/memory"
    )
    assert set(memory.attrib) == {"recorded_at"}


@pytest.mark.asyncio
async def test_tool_search_keeps_structured_records_and_deduplicates_by_internal_id(
    monkeypatch,
):
    records = [
        {"id": "m1", "text": "相同正文", "metadata": {"topic": "游戏"}},
        {"id": "m2", "text": "相同正文", "metadata": {"topic": "观星"}},
    ]
    search = AsyncMock(return_value=records)
    monkeypatch.setattr(
        "core.conversation.tool_executor.search_memories_with_rerank", search
    )
    executor = ToolExecutor(SimpleNamespace(embedding_conf={"enabled": True}))
    relevant = [records[0]]
    other = []
    result = XmlLlmResult(search_memories=[("", "过往约定")])
    await executor.execute_queries("bot", "group", result, [], relevant, other)
    assert relevant == records
    assert relevant[1]["metadata"]["topic"] == "观星"
    search.return_value = []
    relevant.clear()
    await executor.execute_queries("bot", "group", result, [], relevant, other)
    assert relevant == []
    assert "没有找到相关记忆" in other[0]


@pytest.mark.asyncio
async def test_decision_passes_records_without_aliasing_pending_cache(monkeypatch):
    record = {"id": "m1", "text": "甲曾经约我观星。", "metadata": {"topic": "观星"}}
    monkeypatch.setattr(
        "core.conversation.decision_engine.search_memories_with_rerank",
        AsyncMock(return_value=[record]),
    )
    plugin = SimpleNamespace(
        bot_map={"bot": {"decision_conf": {"provider_ids": ["decision"]}}},
        tools_config={},
        embedding_conf={"enabled": True},
        active_reply_counters={},
        data_cache=SimpleNamespace(
            **{
                name: AsyncMock(return_value=value)
                for name, value in {
                    "get_bot_status": None,
                    "get_group_profile": "",
                    "get_user_profile_record": {},
                    "get_user_relation": None,
                    "build_active_user_briefs": [],
                }.items()
            }
        ),
        db=SimpleNamespace(
            update_message_decision=AsyncMock(),
            slang_repo=SimpleNamespace(get_entries=AsyncMock(return_value=[])),
        ),
        call_llm=SimpleNamespace(
            call_llm_decision=AsyncMock(
                return_value=Decision(
                    reply_decision=1, use_rag=1, rag_query="我和甲有什么约定"
                )
            )
        ),
    )
    event = SimpleNamespace(
        get_sender_id=lambda: "A",
        get_group_id=lambda: "group",
        get_self_id=lambda: "self",
        get_group=AsyncMock(return_value=""),
    )
    engine = DecisionEngine(plugin)
    engine.media_captioner.get_cached_media_captions = AsyncMock(return_value=[])
    reply, relevant, pending, _ = await engine.evaluate_decision(
        event,
        "bot",
        "Giftia",
        "group",
        [MessageData(user_id="A", content="还记得观星吗")],
        [],
    )
    assert reply and relevant == pending == [record]
    pending.clear()
    assert relevant == [record]


@pytest.mark.asyncio
async def test_retired_delete_tag_cannot_create_an_action_and_search_remains_available():
    cache = SimpleNamespace(delete_memory=AsyncMock())
    result = await XmlParse(cache, None).decode_llm_xml(
        '<delete_memory id="m1"/><search_memory>过往约定</search_memory><message>我想起来了</message>',
        "group",
    )
    assert result is not None
    assert not hasattr(result, "delete_memories")
    assert result.search_memories == [("", "过往约定")]
    cache.delete_memory.assert_not_awaited()
    instructions = build_xml_instructions([FeatureKey.MEMORY_QUERY])
    assert "<search_memory>" in instructions and "delete_memory" not in instructions


def test_memory_query_switch_inherits_existing_setting():
    manager = BotConfigManager.__new__(BotConfigManager)
    manager.plugin = Mock()
    for old, expected in [
        ([], []),
        (["memory_query_delete"], ["memory_query"]),
        (["memory_query_delete", "memory_query"], ["memory_query"]),
    ]:
        assert (
            manager.normalize_bot_config({"enabled_interactive_features": old})[
                "enabled_interactive_features"
            ]
            == expected
        )
