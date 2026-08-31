import asyncio
import logging
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    import astrbot
except ImportError:
    import types
    astrbot_module = types.ModuleType("astrbot")
    astrbot_api_module = types.ModuleType("astrbot.api")
    astrbot_api_module.logger = logging.getLogger("astrbot")
    astrbot_module.api = astrbot_api_module
    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = astrbot_api_module

from core.conversation.chat_manager import ChatManager


class SelfActionFilterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = MagicMock()
        self.plugin.adapter_id_map = {"adapter_1": "bot_arisu"}
        self.plugin.bot_map = {"bot_arisu": {"nickname": "Arisu"}}
        self.plugin.active_reply_counters = {}
        self.plugin.replying_status = {}
        self.plugin.passive_memory_manager.check_and_trigger_passive_memory = AsyncMock()
        self.chat_manager = ChatManager(self.plugin)
        self.chat_manager.decision_engine.check_whitelists = MagicMock(return_value=True)

    def _create_mock_event(self, self_id: str, sender_id: str, raw_message: dict | None = None):
        event = MagicMock()
        event.get_self_id = MagicMock(return_value=self_id)
        event.get_sender_id = MagicMock(return_value=sender_id)
        event.get_group_id = MagicMock(return_value="10001")
        event.platform_meta = MagicMock()
        event.platform_meta.id = "adapter_1"
        event.send = AsyncMock()

        message_obj = MagicMock()
        message_obj.raw_message = raw_message or {}
        event.message_obj = message_obj
        return event

    async def test_self_normal_message_is_skipped(self):
        event = self._create_mock_event(
            self_id="3970706156",
            sender_id="3970706156",
            raw_message={"post_type": "message", "message": "hello"},
        )
        with patch.object(self.chat_manager, "job", new_callable=AsyncMock) as mock_job:
            await self.chat_manager.handle_message(event)
            mock_job.assert_not_called()

    async def test_self_poke_notice_is_skipped(self):
        event = self._create_mock_event(
            self_id="3970706156",
            sender_id="3970706156",
            raw_message={
                "post_type": "notice",
                "notice_type": "notify",
                "sub_type": "poke",
                "user_id": 3970706156,
                "target_id": 1614170952,
            },
        )
        with patch.object(self.chat_manager, "job", new_callable=AsyncMock) as mock_job:
            await self.chat_manager.handle_message(event)
            mock_job.assert_not_called()

    async def test_self_reaction_notice_is_skipped(self):
        event = self._create_mock_event(
            self_id="3970706156",
            sender_id="3970706156",
            raw_message={
                "post_type": "notice",
                "notice_type": "group_msg_emoji_like",
                "user_id": 3970706156,
                "message_id": 12345,
            },
        )
        with patch.object(self.chat_manager, "job", new_callable=AsyncMock) as mock_job:
            await self.chat_manager.handle_message(event)
            mock_job.assert_not_called()

    async def test_user_poke_bot_is_processed(self):
        event = self._create_mock_event(
            self_id="3970706156",
            sender_id="1614170952",
            raw_message={
                "post_type": "notice",
                "notice_type": "notify",
                "sub_type": "poke",
                "user_id": 1614170952,
                "target_id": 3970706156,
            },
        )
        with patch.object(self.chat_manager, "job", new_callable=AsyncMock) as mock_job:
            await self.chat_manager.handle_message(event)
            await asyncio.sleep(0.01)
            mock_job.assert_called_once_with(event)

    async def test_group_ban_notice_is_processed_for_state_handling(self):
        event = self._create_mock_event(
            self_id="3970706156",
            sender_id="1614170952",
            raw_message={
                "post_type": "notice",
                "notice_type": "group_ban",
                "sub_type": "ban",
                "operator_id": 1614170952,
                "user_id": 3970706156,
                "duration": 600,
            },
        )
        with patch.object(self.chat_manager, "job", new_callable=AsyncMock) as mock_job:
            await self.chat_manager.handle_message(event)
            await asyncio.sleep(0.01)
            mock_job.assert_called_once_with(event)


if __name__ == "__main__":
    unittest.main()
