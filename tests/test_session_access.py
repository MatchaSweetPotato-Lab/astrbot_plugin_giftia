import ast
import asyncio
import json
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from core.bot.bot_config_manager import BotConfigManager
from core.conversation.chat_manager import ChatManager
from core.handlers.commands import CommandHandler
from core.utils.schemas import MessageData, XmlLlmResult
from core.web.bot_api import BotApi

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Plain
from astrbot.core.star.filter.command import CommandFilter, GreedyStr
from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter


def plugin_method(name):
    source = Path(__file__).resolve().parents[1] / "main.py"
    node = next(
        node
        for node in ast.walk(ast.parse(source.read_text()))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )
    decorators = node.decorator_list
    node.decorator_list = []
    namespace = {"AstrMessageEvent": AstrMessageEvent, "GreedyStr": GreedyStr}
    exec(
        compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"),
        namespace,
    )
    return namespace[name], decorators


@pytest.fixture
def runtime(tmp_path):
    plugin = SimpleNamespace(
        bot_map={},
        adapter_id_map={},
        active_reply_counters={},
        running_tasks={},
        passive_memory_enabled=False,
        block_command_messages=False,
        message_parser=SimpleNamespace(
            parse_user_message=AsyncMock(), chain_to_result=AsyncMock()
        ),
        data_cache=SimpleNamespace(add_message=AsyncMock()),
        get_caption_config=lambda bot: {"defer_caption_enabled": False},
        parse_locks=defaultdict(asyncio.Lock),
        replying_status={},
    )
    manager = BotConfigManager.__new__(BotConfigManager)
    manager.plugin = plugin
    manager.config_file = tmp_path / "bots_config.json"
    manager.bots = []
    assert manager.save_bots(
        [
            {
                "name": "bot",
                "adapter_ids": ["adapter", "second-adapter"],
                "decision_conf": {
                    "group_whitelist_mode": "whitelist",
                    "group_whitelist": ["100", "101", "200"],
                },
            },
            {
                "name": "other",
                "adapter_ids": ["other-adapter"],
                "decision_conf": {
                    "group_whitelist_mode": "whitelist",
                    "group_whitelist": ["100"],
                },
            },
        ]
    )
    plugin.bot_config_manager = manager
    sync, _ = plugin_method("sync_bot_maps")
    plugin.sync_bot_maps = lambda: sync(plugin)
    plugin.sync_bot_maps()
    plugin.cmd_handler = CommandHandler(plugin)
    plugin.chat_manager = ChatManager(plugin)
    return plugin


@pytest.fixture
def event():
    extras = {}
    return SimpleNamespace(
        platform_meta=SimpleNamespace(id="adapter"),
        unified_msg_origin="adapter:GroupMessage:100",
        get_group_id=Mock(return_value="100"),
        get_sender_id=Mock(return_value="200"),
        get_self_id=Mock(return_value="999"),
        get_messages=Mock(return_value=[At(qq="999")]),
        get_message_str=Mock(return_value="说话"),
        is_at_or_wake_command=True,
        is_admin=Mock(return_value=True),
        message_obj=SimpleNamespace(raw_message={}),
        get_extra=lambda key, default=None: extras.get(key, default),
        set_extra=lambda key, value: extras.__setitem__(key, value),
        send=AsyncMock(),
    )


def test_bot_config_defaults_to_blacklist_mode(runtime):
    normalized = runtime.bot_config_manager.normalize_bot_config({"name": "fresh"})
    assert normalized["decision_conf"]["group_whitelist_mode"] == "blacklist"
    assert normalized["decision_conf"]["group_whitelist"] == []
    assert normalized["decision_conf"]["private_whitelist_mode"] == "whitelist"
    assert normalized["decision_conf"]["private_whitelist"] == []


@pytest.mark.parametrize("mode", ["blacklist", "whitelist"])
@pytest.mark.parametrize("sessions", [[], ["100", "200"]])
def test_legacy_shared_access_is_preserved_without_aliasing(runtime, mode, sessions):
    normalized = runtime.bot_config_manager.normalize_bot_config(
        {"decision_conf": {"group_whitelist_mode": mode, "group_whitelist": sessions}}
    )
    conf = normalized["decision_conf"]
    assert conf["private_whitelist_mode"] == mode
    assert conf["private_whitelist"] == sessions
    assert conf["private_whitelist"] is not conf["group_whitelist"]
    assert runtime.bot_config_manager.save_bots([normalized])
    assert runtime.bot_config_manager.load_bots()[0]["decision_conf"] == conf


@pytest.mark.parametrize("kind", ["group", "private"])
@pytest.mark.parametrize(
    "raw_mode,mode",
    [
        (True, "whitelist"),
        (False, "blacklist"),
        ("白名单模式", "whitelist"),
        ("黑名单", "blacklist"),
    ],
)
def test_access_config_normalizes_both_lists(runtime, kind, raw_mode, mode):
    conf = runtime.bot_config_manager.normalize_bot_config(
        {
            "decision_conf": {
                f"{kind}_whitelist_mode": raw_mode,
                f"{kind}_whitelist": [100, " 100 ", "", "  ", "200"],
            }
        }
    )["decision_conf"]
    assert conf[f"{kind}_whitelist_mode"] == mode
    assert conf[f"{kind}_whitelist"] == ["100", "200"]


@pytest.mark.parametrize("group_mode", ["blacklist", "whitelist"])
@pytest.mark.parametrize("private_mode", ["blacklist", "whitelist"])
def test_matching_group_and_user_ids_use_independent_lists(
    runtime, event, group_mode, private_mode
):
    conf = runtime.bot_map["bot"]["decision_conf"]
    conf.update(
        group_whitelist_mode=group_mode,
        group_whitelist=["100"],
        private_whitelist_mode=private_mode,
        private_whitelist=[],
    )
    gate = runtime.chat_manager.decision_engine
    assert gate.check_whitelists(event) is (group_mode == "whitelist")
    event.get_group_id.return_value = ""
    event.get_sender_id.return_value = "100"
    event.unified_msg_origin = "adapter:FriendMessage:100"
    assert gate.check_whitelists(event) is (private_mode == "blacklist")
    conf["private_whitelist"] = ["100"]
    assert gate.check_whitelists(event) is (private_mode == "whitelist")
    event.get_group_id.return_value = "100"
    assert gate.check_whitelists(event) is (group_mode == "whitelist")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["blacklist", "whitelist"])
@pytest.mark.parametrize("from_private", [False, True])
async def test_private_commands_persist_only_private_access(
    runtime, event, mode, from_private
):
    bots = runtime.bot_config_manager.load_bots()
    bots[0]["decision_conf"].update(private_whitelist_mode=mode, private_whitelist=[])
    assert runtime.bot_config_manager.save_bots(bots)
    runtime.sync_bot_maps()
    group_before = runtime.bot_map["bot"]["decision_conf"]["group_whitelist"].copy()
    other_before = runtime.bot_map["other"]
    if from_private:
        event.get_group_id.return_value = ""
        event.get_sender_id.return_value = "100"
        event.unified_msg_origin = "adapter:FriendMessage:100"
    target = "" if from_private else "100"
    gate = runtime.chat_manager.decision_engine
    for method, enabled in (
        ("enable_private_session", True),
        ("disable_private_session", False),
    ):
        wrapper, _ = plugin_method(method)
        for _ in range(2):
            _ = [c async for c in wrapper(runtime, event, target)]
        runtime.sync_bot_maps()
        assert gate.is_session_allowed("bot", "100", is_private=True) is enabled
        assert runtime.bot_map["bot"]["decision_conf"]["private_whitelist"] == (
            ["100"] if enabled == (mode == "whitelist") else []
        )
        assert (
            runtime.bot_map["bot"]["decision_conf"]["group_whitelist"] == group_before
        )
        assert runtime.bot_map["other"] == other_before
        assert "私聊" in event.send.call_args.args[0].chain[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target", ["", "  ", "100 200", "adapter:FriendMessage:100", "@100"]
)
async def test_private_command_in_group_rejects_invalid_or_missing_target(
    runtime, event, target
):
    before = runtime.bot_config_manager.config_file.read_bytes()
    wrapper, _ = plugin_method("enable_private_session")
    _ = [c async for c in wrapper(runtime, event, target)]
    assert runtime.bot_config_manager.config_file.read_bytes() == before
    assert (
        "用法" in event.send.call_args.args[0].chain[0].text
        or "请填写" in event.send.call_args.args[0].chain[0].text
    )


@pytest.mark.asyncio
async def test_blacklist_mode_inverts_session_commands(runtime, event):
    bots = runtime.bot_config_manager.load_bots()
    bots[0]["decision_conf"]["group_whitelist_mode"] = "blacklist"
    bots[0]["decision_conf"]["group_whitelist"] = []
    assert runtime.bot_config_manager.save_bots(bots)
    runtime.sync_bot_maps()
    gate = runtime.chat_manager.decision_engine

    assert gate.check_whitelists(event)
    _ = [c async for c in runtime.cmd_handler.set_session_access(event, "", False)]
    assert runtime.bot_map["bot"]["decision_conf"]["group_whitelist"] == ["100"]
    assert not gate.check_whitelists(event)
    _ = [c async for c in runtime.cmd_handler.set_session_access(event, "", True)]
    assert runtime.bot_map["bot"]["decision_conf"]["group_whitelist"] == []
    assert gate.check_whitelists(event)


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_current_and_remote_membership_persist_and_last_removal_denies_all(
    runtime, event, private
):
    if private:
        event.get_group_id.return_value = ""
        event.unified_msg_origin = "adapter:FriendMessage:200"
    session = "200" if private else "100"
    list_key = "private_whitelist" if private else "group_whitelist"
    other_before = runtime.bot_map["other"]
    for existing in ("100", "101", "200"):
        _ = [
            c
            async for c in runtime.cmd_handler.set_session_access(
                event, existing, False
            )
        ]
    gate = runtime.chat_manager.decision_engine
    assert not gate.check_whitelists(event)
    assert runtime.bot_map["bot"]["decision_conf"][list_key] == []
    for _ in range(2):
        _ = [c async for c in runtime.cmd_handler.set_session_access(event, "", True)]
    assert runtime.bot_map["bot"]["decision_conf"][list_key] == [session]
    assert gate.check_whitelists(event)
    _ = [
        c async for c in runtime.cmd_handler.set_session_access(event, "1017789", True)
    ]
    runtime.sync_bot_maps()
    assert gate.is_session_allowed("bot", "1017789", is_private=private)
    _ = [
        c async for c in runtime.cmd_handler.set_session_access(event, "1017789", False)
    ]
    assert not gate.is_session_allowed("bot", "1017789", is_private=private)
    assert gate.check_whitelists(event)
    runtime.active_reply_counters[f"bot:{session}"] = 10
    _ = [c async for c in runtime.cmd_handler.set_session_access(event, "", False)]
    assert f"bot:{session}" not in runtime.active_reply_counters
    assert not gate.check_whitelists(event)
    assert runtime.bot_map["other"] == other_before


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_non_whitelisted_at_and_private_messages_are_rejected_before_parsing(
    runtime, event, private
):
    event.get_group_id.return_value = "" if private else "300"
    event.get_sender_id.return_value = "300"
    runtime.chat_manager.job = AsyncMock()
    await runtime.chat_manager.handle_message(event)
    runtime.chat_manager.job.assert_not_awaited()
    runtime.message_parser.parse_user_message.assert_not_awaited()
    runtime.data_cache.add_message.assert_not_awaited()
    event.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_blocking_is_persistent_and_scoped_to_bot_adapter_and_session(
    runtime, event
):
    for _ in range(2):
        _ = [c async for c in runtime.cmd_handler.block_user(event, "200")]
    runtime.sync_bot_maps()
    assert runtime.bot_map["bot"]["blocked_users"] == {
        "adapter:GroupMessage:100": ["200"]
    }
    gate = runtime.chat_manager.decision_engine
    assert not gate.check_whitelists(event)
    runtime.chat_manager.job = AsyncMock()
    await runtime.chat_manager.handle_message(event)
    runtime.chat_manager.job.assert_not_awaited()
    runtime.message_parser.parse_user_message.assert_not_awaited()
    runtime.data_cache.add_message.assert_not_awaited()
    for origin, adapter, group, user in (
        ("adapter:GroupMessage:101", "adapter", "101", "200"),
        ("adapter:FriendMessage:200", "adapter", "", "200"),
        ("other-adapter:GroupMessage:100", "other-adapter", "100", "200"),
        ("second-adapter:GroupMessage:100", "second-adapter", "100", "200"),
        ("adapter:GroupMessage:100", "adapter", "100", "201"),
    ):
        event.unified_msg_origin = origin
        event.platform_meta.id = adapter
        event.get_group_id.return_value = group
        event.get_sender_id.return_value = user
        assert gate.check_whitelists(event)


@pytest.mark.asyncio
async def test_dashboard_save_preserves_command_blocks_and_updates_runtime(
    runtime, event, monkeypatch
):
    _ = [c async for c in runtime.cmd_handler.block_user(event, "200")]
    body = {
        "name": "bot",
        "adapter_ids": ["adapter"],
        "decision_conf": {
            "group_whitelist_mode": "whitelist",
            "group_whitelist": ["1017789"],
        },
    }
    monkeypatch.setattr(
        "core.web.bot_api.request", SimpleNamespace(json=AsyncMock(return_value=body))
    )
    response = await BotApi(runtime).save_bot()
    assert json.loads(response.body)["status"] == "success"
    assert runtime.bot_map["bot"]["blocked_users"] == {
        event.unified_msg_origin: ["200"]
    }
    assert runtime.chat_manager.decision_engine.is_session_allowed(
        "bot", "1017789", is_private=False
    )
    assert not runtime.chat_manager.decision_engine.is_session_allowed(
        "bot", "100", is_private=False
    )
    assert runtime.bot_map["bot"]["decision_conf"]["private_whitelist"] == [
        "100",
        "101",
        "200",
    ]


@pytest.mark.asyncio
async def test_dashboard_can_save_opposite_modes_and_clear_private_list(
    runtime, event, monkeypatch
):
    body = {
        "name": "bot",
        "adapter_ids": ["adapter"],
        "decision_conf": {
            "group_whitelist_mode": "blacklist",
            "group_whitelist": ["100"],
            "private_whitelist_mode": "whitelist",
            "private_whitelist": ["100"],
        },
    }
    monkeypatch.setattr(
        "core.web.bot_api.request", SimpleNamespace(json=AsyncMock(return_value=body))
    )
    gate = runtime.chat_manager.decision_engine
    for private_list in (["100"], []):
        body["decision_conf"]["private_whitelist"] = private_list
        response = await BotApi(runtime).save_bot()
        assert json.loads(response.body)["status"] == "success"
        assert not gate.is_session_allowed("bot", "100", is_private=False)
        assert gate.is_session_allowed("bot", "100", is_private=True) is bool(
            private_list
        )
        assert (
            runtime.bot_config_manager.load_bots()[0]["decision_conf"][
                "private_whitelist"
            ]
            == private_list
        )


@pytest.mark.asyncio
async def test_block_command_resolves_structured_mention(runtime, event):
    event.get_messages.return_value = [Plain("/屏蔽 "), At(qq="201", name="目标用户")]
    wrapper, _ = plugin_method("block_user")
    command_filter = CommandFilter("屏蔽", handler_md=SimpleNamespace(handler=wrapper))
    event.get_message_str.return_value = "屏蔽"
    assert command_filter.filter(event, {})
    parsed = event.get_extra("parsed_params")
    _ = [c async for c in wrapper(runtime, event, **parsed)]
    runtime.sync_bot_maps()
    assert runtime.bot_map["bot"]["blocked_users"] == {
        "adapter:GroupMessage:100": ["201"]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "command", "enabled"),
    [
        ("enable_session", "说话", True),
        ("disable_session", "闭嘴", False),
        ("enable_private_session", "开启私聊", True),
        ("disable_private_session", "关闭私聊", False),
    ],
)
async def test_session_commands_resolve_structured_mention(
    runtime, event, method, command, enabled
):
    event.get_messages.return_value = [
        Plain(f"/{command} "),
        At(qq="1017789", name="目标会话"),
    ]
    wrapper, _ = plugin_method(method)
    command_filter = CommandFilter(command, handler_md=SimpleNamespace(handler=wrapper))
    event.get_message_str.return_value = command
    assert command_filter.filter(event, {})
    parsed = event.get_extra("parsed_params")
    _ = [c async for c in wrapper(runtime, event, **parsed)]
    runtime.sync_bot_maps()
    assert (
        runtime.chat_manager.decision_engine.is_session_allowed(
            "bot", "1017789", is_private=True
        )
        is enabled
    )


@pytest.mark.asyncio
async def test_unblock_command_removes_only_current_user_and_session(runtime, event):
    _ = [c async for c in runtime.cmd_handler.block_user(event, "200")]
    event.get_messages.return_value = [Plain("/取消屏蔽 "), At(qq="200", name="用户")]
    wrapper, _ = plugin_method("unblock_user")
    command_filter = CommandFilter(
        "取消屏蔽", handler_md=SimpleNamespace(handler=wrapper)
    )
    event.get_message_str.return_value = "取消屏蔽"
    assert command_filter.filter(event, {})
    parsed = event.get_extra("parsed_params")
    _ = [c async for c in wrapper(runtime, event, **parsed)]
    runtime.sync_bot_maps()
    assert runtime.bot_map["bot"]["blocked_users"] == {}
    assert runtime.chat_manager.decision_engine.check_whitelists(event)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,command,arg",
    [
        ("enable_session", "说话", ""),
        ("enable_session", "说话", "1017789"),
        ("disable_session", "闭嘴", ""),
        ("disable_session", "闭嘴", "1017789"),
        ("enable_private_session", "开启私聊", ""),
        ("enable_private_session", "开启私聊", "1017789"),
        ("disable_private_session", "关闭私聊", ""),
        ("disable_private_session", "关闭私聊", "1017789"),
        ("block_user", "屏蔽", "200"),
        ("unblock_user", "取消屏蔽", "200"),
    ],
)
async def test_command_registration_permissions_and_logging_exclusion(
    runtime, event, method, command, arg
):
    wrapper, decorators = plugin_method(method)
    assert decorators[0].func.attr == "permission_type"
    assert decorators[0].args[0].attr == "ADMIN"
    assert {
        keyword.arg: ast.literal_eval(keyword.value)
        for keyword in decorators[0].keywords
    } == {"raise_error": False}
    assert decorators[1].args[0].value == command
    permission = PermissionTypeFilter(PermissionType.ADMIN)
    assert permission.filter(event, {})
    event.is_admin.return_value = False
    assert not permission.filter(event, {})
    event.is_admin.return_value = True
    event.get_message_str.return_value = f"{command} {arg}".strip()
    command_filter = CommandFilter(command, handler_md=SimpleNamespace(handler=wrapper))
    assert command_filter.filter(event, {})
    event.set_extra(
        "activated_handlers",
        [SimpleNamespace(handler_name=method, event_filters=[command_filter])],
    )
    assert runtime.chat_manager._check_command_info(event) == (True, True)
    await runtime.chat_manager.handle_message(event)
    _ = [c async for c in wrapper(runtime, event, arg)]
    runtime.message_parser.parse_user_message.assert_not_awaited()
    runtime.message_parser.chain_to_result.assert_not_awaited()
    runtime.data_cache.add_message.assert_not_awaited()
    event._giftia_bypass_logging = False
    runtime.bot_map["bot"]["decision_conf"]["group_whitelist"] = []
    # The command remains callable outside the allowlist.
    _ = [c async for c in wrapper(runtime, event, arg)]
    assert event._giftia_bypass_logging is True


@pytest.mark.asyncio
@pytest.mark.parametrize("block", [False, True])
async def test_save_failure_preserves_persisted_and_live_access(
    runtime, event, monkeypatch, block
):
    before = runtime.bot_config_manager.config_file.read_bytes()
    monkeypatch.setattr(
        runtime.bot_config_manager, "save_bots", Mock(return_value=False)
    )
    operation = (
        runtime.cmd_handler.block_user(event, "200")
        if block
        else runtime.cmd_handler.set_session_access(event, "", False)
    )
    _ = [c async for c in operation]
    assert "失败" in event.send.call_args.args[0].chain[0].text
    assert runtime.bot_config_manager.config_file.read_bytes() == before
    assert runtime.chat_manager.decision_engine.check_whitelists(event)


@pytest.mark.asyncio
@pytest.mark.parametrize("block", [False, True])
@pytest.mark.parametrize("private", [False, True])
async def test_inflight_reply_is_discarded_after_access_changes(
    runtime, event, block, private
):
    if private:
        event.get_group_id.return_value = ""
        event.unified_msg_origin = "adapter:FriendMessage:200"
    runtime.message_parser.parse_user_message.return_value = (
        MessageData(content="hello"),
        [],
        [],
    )
    manager = runtime.chat_manager
    manager.decision_engine.evaluate_decision = AsyncMock(
        return_value=(True, None, None, None)
    )
    manager.action_dispatcher.dispatch_actions = AsyncMock()
    manager.reply_pipeline.commit_pending_session_recalled_memories = Mock()

    async def reply(**kwargs):
        operation = (
            runtime.cmd_handler.block_user(event, "200")
            if block
            else runtime.cmd_handler.set_session_access(event, "", False)
        )
        _ = [c async for c in operation]
        yield XmlLlmResult(msg_chains=[[Plain("Late reply")]])

    manager.reply_pipeline.dispatch_llm_reply_loop = reply
    await manager.job(event)
    manager.action_dispatcher.dispatch_actions.assert_not_awaited()
    assert runtime.replying_status["bot:200" if private else "bot:100"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", ["", "  ", "200 201", "@200"])
async def test_invalid_block_target_does_not_write(runtime, event, user_id):
    before = runtime.bot_config_manager.config_file.read_bytes()
    _ = [c async for c in runtime.cmd_handler.block_user(event, user_id)]
    assert "用法" in event.send.call_args.args[0].chain[0].text
    assert runtime.bot_config_manager.config_file.read_bytes() == before
