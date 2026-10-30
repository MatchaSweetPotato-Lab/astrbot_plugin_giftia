"""Verify compact recall context and removal of model-driven memory deletion."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from xml.etree.ElementTree import fromstring

import pytest

from core.bot.bot_config_manager import (
    DEFAULT_INTERACTIVE_FEATURES,
    INTERACTIVE_FEATURES_METADATA,
    BotConfigManager,
)
from core.conversation.decision_engine import DecisionEngine
from core.llm.native_tools import SearchMemoryTool
from core.llm.preset_prompts import build_xml_instructions
from core.llm.prompt import build_memory_items, build_reply_prompt
from core.llm.xml_parse import XmlParse
from core.utils.schemas import (
    Decision,
    MessageData,
    SessionRecallMemory,
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


@pytest.mark.parametrize("cached", [False, True])
def test_memory_context_keeps_one_chronological_source_locator(cached):
    metadata = {
        "source_session_id": 'other&"group',
        "source_label": "来源群聊：other",
        "evidence": [
            {"message_id": "late", "time": "2026-10-30T04:00:00Z"},
            {"message_id": "early", "time": "2026-10-30T10:00:00+08:00"},
            {
                "source_id": 999,
                "message_id": '-9007199254740993&"',
                "time": "2026-10-29T23:00:00-04:00",
                "quote": "hidden original quote",
            },
            {"message_id": "undated", "time": "invalid"},
            {"source_id": 123},
            {"message_id": False},
            None,
        ],
    }
    record = {"id": "internal-id", "text": "我记得这件事。", "metadata": metadata}
    if cached:
        memories = [
            SessionRecallMemory(
                memory_id=record["id"], text=record["text"], metadata=json.dumps(metadata)
            )
        ]
    else:
        memories = [record]
    prompt = build_reply_prompt(
        recent_messages=[],
        media_captions=[],
        bot_status=None,
        current_session_id="current",
        **{
            "session_recalled_memories" if cached else "relevant_memories": memories
        },
    )
    root = fromstring("<context>" + prompt + "</context>")
    [rendered] = root.findall(".//memory")
    assert rendered.attrib == {
        "source": '来源群聊：other&"group',
        "message_id": '-9007199254740993&"',
    }
    assert rendered.text == record["text"]
    guide = root.find("memory_usage_rules").text
    for field in ("get_message_context", "message_id", "source", "group_id"):
        assert field in guide
    for omitted in (
        "internal-id",
        "hidden original quote",
        "source_id=",
        "source_session_id",
        "undated",
    ):
        assert omitted not in prompt
    assert metadata["evidence"][0]["message_id"] == "late"


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("current_session", ["200", "current"])
def test_memory_source_is_relative_to_current_session_and_kept_only_once(
    cached, private, current_session
):
    metadata = {
        "source_session_id": "200",
        "source_label": "来源群聊：stale-session",
        "associated_user_ids": ["200"] if private else ["member"],
        "evidence": [{"message_id": "original"}],
    }
    record = {"id": "fact", "text": "一个事实", "metadata": metadata}
    if cached:
        memories = [
            SessionRecallMemory(
                memory_id="fact", text=record["text"], metadata=json.dumps(metadata)
            )
        ]
    else:
        memories = [record]
    prompt = build_reply_prompt(
        [],
        [],
        None,
        current_session_id=current_session,
        **{"session_recalled_memories" if cached else "relevant_memories": memories},
    )
    root = fromstring("<context>" + prompt + "</context>")
    [memory] = root.findall(".//memory")
    expected = {"message_id": "original"}
    if current_session != "200":
        expected["source"] = f"来源{'私聊' if private else '群聊'}：200"
    assert memory.attrib == expected
    guide = root.find("memory_usage_rules").text
    if current_session == "200":
        assert "隐私" not in guide
    else:
        assert "带 source 属性的记忆" in guide
        assert "这些记忆若可能涉及隐私" in guide
    assert "source_session_id" not in prompt and "stale-session" not in prompt
    assert metadata["source_session_id"] == "200"
    assert metadata["source_label"] == "来源群聊：stale-session"


@pytest.mark.parametrize(
    "metadata,scope,expected",
    [
        (
            {"source_session_id": "200", "evidence": [{"source_id": 7}]},
            {},
            {"source": "来源群聊：200"},
        ),
        ({"evidence": [{"message_id": "real"}]}, {}, {}),
        (
            {"source_session_id": "200", "evidence": "broken"},
            {},
            {"source": "来源群聊：200"},
        ),
        (
            {"source_session_id": "200", "evidence": [None, {"message_id": []}]},
            {},
            {"source": "来源群聊：200"},
        ),
        (
            {"evidence": [{"message_id": -123}]},
            {"group_or_user_id": "300"},
            {"message_id": "-123"},
        ),
    ],
)
def test_legacy_memory_locators_require_real_message_ids_and_known_sessions(
    metadata, scope, expected
):
    rendered = fromstring(
        build_memory_items(
            [{"text": "旧记忆", "metadata": metadata, **scope}],
            current_session_id="300",
        )
    )
    assert rendered.attrib == expected


@pytest.mark.asyncio
async def test_tool_search_keeps_structured_records_and_deduplicates_by_internal_id(
    monkeypatch,
):
    records = [
        {"id": "m1", "text": "相同正文", "metadata": {"topic": "游戏"}},
        {"id": "m2", "text": "相同正文", "metadata": {"topic": "观星"}},
    ]
    search = AsyncMock(return_value=records)
    monkeypatch.setattr("core.llm.native_tools.search_memories_with_rerank", search)
    plugin = SimpleNamespace(
        embedding_conf={"enabled": True},
        adapter_id_map={"adapter": "bot"},
        bot_map={"bot": {}},
    )
    event = SimpleNamespace(
        platform_meta=SimpleNamespace(id="adapter"),
        get_group_id=lambda: "group",
        get_sender_id=lambda: "A",
        get_sender_name=lambda: "Alice",
    )
    context = SimpleNamespace(context=SimpleNamespace(event=event, extra={}))
    tool = SearchMemoryTool(plugin=plugin)
    result = json.loads(await tool.call(context, "过往约定"))
    assert result["count"] == 2
    assert result["memory_context"].count("相同正文") == 2
    assert "m1" not in result["memory_context"]
    search.return_value = []
    result = json.loads(await tool.call(context, "过往约定"))
    assert result["count"] == 0 and result["memory_context"] == ""


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
async def test_retired_memory_tags_cannot_create_actions():
    cache = SimpleNamespace(delete_memory=AsyncMock())
    result = await XmlParse(cache, None).decode_llm_xml(
        '<delete_memory id="m1"/><search_memory>过往约定</search_memory><message>我想起来了</message>',
        "group",
    )
    assert result is not None
    assert not hasattr(result, "delete_memories")
    assert not hasattr(result, "search_memories")
    cache.delete_memory.assert_not_awaited()
    instructions = build_xml_instructions(["memory_query", "schedule_task"])
    assert "<search_memory>" not in instructions and "delete_memory" not in instructions


def test_function_tools_are_removed_from_bot_features_and_legacy_settings():
    manager = BotConfigManager.__new__(BotConfigManager)
    manager.plugin = Mock()
    retired = {"memory_query", "memory_query_delete", "schedule_task"}
    assert not retired.intersection(DEFAULT_INTERACTIVE_FEATURES)
    assert not retired.intersection(
        item["key"] for item in INTERACTIVE_FEATURES_METADATA
    )
    for old, expected in [
        (None, DEFAULT_INTERACTIVE_FEATURES),
        ([], []),
        (["memory_query_delete"], []),
        (["memory_query_delete", "memory_query", "schedule_task", "poke"], ["poke"]),
        (
            ["memory_query(记忆查询)", "schedule_task(定时任务)", "task_board"],
            ["task_board"],
        ),
    ]:
        assert (
            manager.normalize_bot_config({"enabled_interactive_features": old})[
                "enabled_interactive_features"
            ]
            == expected
        )
