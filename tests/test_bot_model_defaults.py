import asyncio
import json
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from core.bot.bot_config_manager import DEFAULT_BOT_CONFIG, BotConfigManager
from core.conversation.decision_engine import DecisionEngine
from core.utils.schemas import Decision, MessageData, Status

from astrbot.api.message_components import At


@pytest.mark.parametrize("legacy_enabled", [True, False, None])
def test_model_switches_are_removed_on_load_and_save(tmp_path, legacy_enabled):
    manager = BotConfigManager.__new__(BotConfigManager)
    manager.config_file = tmp_path / "bots_config.json"
    legacy = {} if legacy_enabled is None else {"enabled": legacy_enabled}
    bot = {
        "name": "bot",
        "enabled": False,
        "decision_conf": {**legacy, "provider_ids": ["decision"]},
        "llm_reply_conf": {
            **legacy,
            "provider_ids": ["reply"],
            "persona_id": "custom",
        },
    }
    manager.config_file.write_text(json.dumps([bot]))
    loaded = manager.load_bots()
    assert manager.save_bots(loaded)
    saved = json.loads(manager.config_file.read_text())[0]
    for section in ("decision_conf", "llm_reply_conf"):
        assert "enabled" not in DEFAULT_BOT_CONFIG[section]
        assert "enabled" not in loaded[0][section]
        assert "enabled" not in saved[section]
        assert saved[section]["provider_ids"] == bot[section]["provider_ids"]
    assert saved["llm_reply_conf"]["persona_id"] == "custom"
    assert saved["enabled"] is False


@pytest.fixture
def runtime():
    plugin = SimpleNamespace(
        bot_map={"bot": {"decision_conf": {"provider_ids": ["decision"]}}},
        user_debounce_time=0,
        user_throttle_time=0,
        group_throttle_time=0,
        active_reply_counters={},
        replying_status={},
        user_locks=defaultdict(asyncio.Lock),
        group_locks=defaultdict(asyncio.Lock),
        concurrent_strategy="stall",
        tools_config={},
        get_caption_config=lambda bot: {},
        db=SimpleNamespace(
            update_message_decision=AsyncMock(),
            slang_repo=SimpleNamespace(get_entries=AsyncMock(return_value=[])),
        ),
        data_cache=SimpleNamespace(
            is_bot_muted=Mock(return_value=False),
            get_recent_message=AsyncMock(return_value=[]),
            get_bot_status=AsyncMock(return_value=Status()),
            get_group_profile=AsyncMock(return_value=""),
            get_user_profile_record=AsyncMock(return_value={}),
            get_user_relation=AsyncMock(return_value=None),
            build_active_user_briefs=AsyncMock(return_value=[]),
        ),
        call_llm=SimpleNamespace(
            call_llm_decision=AsyncMock(
                return_value=Decision(reply_decision=0, use_rag=2)
            )
        ),
    )
    event = SimpleNamespace(
        get_group_id=Mock(return_value="100"),
        get_sender_id=Mock(return_value="200"),
        get_self_id=Mock(return_value="999"),
        get_messages=Mock(return_value=[]),
        get_group=AsyncMock(return_value=""),
        _has_send_oper=False,
    )
    message = MessageData(
        nickname="User",
        user_id="200",
        group_or_user_id="100",
        time="2026-09-12T00:00:00",
        message_id="message",
        content="hello",
    )
    return SimpleNamespace(
        plugin=plugin, event=event, message=message, engine=DecisionEngine(plugin)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario,active,overrides,expected_decision",
    [
        ("group", 1, {}, 0),
        ("group", 1, {"enabled": False}, 0),
        ("group", 0, {"proactive_probability": 100}, 0),
        ("group", 0, {"keyword_trigger_enabled": True, "keyword_rules": ["hello"]}, 0),
        ("group", 0, {}, None),
        ("group", 1, {"provider_ids": []}, None),
        ("at", 1, {"at_behavior": "force_reply"}, 3),
        ("at", 0, {"at_behavior": "activate_and_decide"}, 0),
        ("at", 0, {"at_behavior": "activate_and_decide", "enabled": False}, 0),
        ("at", 1, {"at_behavior": "decide_in_window_force_outside"}, 0),
        ("at", 0, {"at_behavior": "decide_in_window_force_outside"}, 3),
        ("at", 0, {"at_behavior": "activate_and_decide", "provider_ids": []}, 3),
        ("private", 0, {"at_behavior": "force_reply", "provider_ids": []}, 3),
        ("private", 0, {"at_behavior": "activate_and_decide"}, 3),
        ("private", 1, {"at_behavior": "decide_in_window_force_outside"}, 3),
    ],
)
async def test_group_decisions_and_direct_reply_policies(
    runtime, scenario, active, overrides, expected_decision
):
    plugin, event = runtime.plugin, runtime.event
    plugin.bot_map["bot"]["decision_conf"].update(overrides)
    if scenario == "at":
        event.get_messages.return_value = [At(qq="999")]
    elif scenario == "private":
        event.get_group_id.return_value = ""
    session = event.get_group_id() or event.get_sender_id()
    runtime.message.group_or_user_id = session
    plugin.active_reply_counters[f"bot:{session}"] = active

    result = await runtime.engine.evaluate_decision(
        event, "bot", "Bot", session, runtime.message
    )

    assert result == (expected_decision == 3, None, None, None)
    assert plugin.call_llm.call_llm_decision.await_count == (expected_decision == 0)
    if expected_decision is None:
        plugin.db.update_message_decision.assert_not_awaited()
    else:
        plugin.db.update_message_decision.assert_awaited_once_with(
            bot_name="bot",
            group_or_user_id=session,
            message_id="message",
            reply_decision=expected_decision,
            use_rag=2,
        )
