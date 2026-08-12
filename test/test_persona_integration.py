import pytest
from unittest.mock import MagicMock, AsyncMock

from core.bot.bot_config_manager import BotConfigManager, DEFAULT_BOT_CONFIG
from core.web.bot_api import BotApi


def test_bot_config_manager_persona_id():
    manager = BotConfigManager(plugin=MagicMock())
    normalized = manager.normalize_bot_config({
        "name": "TestBot",
        "llm_reply_conf": {
            "enabled": True,
            "provider_ids": ["provider1"],
            "persona_id": "custom_persona"
        }
    })
    assert normalized["llm_reply_conf"]["persona_id"] == "custom_persona"

    # Default fallback
    normalized_default = manager.normalize_bot_config({})
    assert normalized_default["llm_reply_conf"]["persona_id"] == "default"


def test_bot_api_get_available_metadata_personas():
    mock_giftia = MagicMock()
    mock_context = MagicMock()
    mock_persona_mgr = MagicMock()

    mock_persona_mgr.personas_v3 = [
        {"name": "default", "prompt": "Default prompt", "tools": None},
        {"name": "alice", "prompt": "Alice prompt", "tools": ["search"]},
    ]
    mock_context.persona_manager = mock_persona_mgr
    mock_giftia.context = mock_context

    bot_api = BotApi(giftia=mock_giftia)
    metadata = bot_api._get_available_metadata()

    assert "personas" in metadata
    personas = metadata["personas"]
    assert len(personas) == 2
    assert personas[0]["name"] == "default"
    assert personas[1]["name"] == "alice"
