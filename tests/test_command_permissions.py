import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from core.handlers.permissions import admin_permission

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.core.pipeline.waking_check import stage as waking
from astrbot.core.platform.message_type import MessageType
from astrbot.core.star import command_management, star_handler
from astrbot.core.star.filter.command import CommandFilter, GreedyStr
from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter
from astrbot.core.star.register import star_handler as registration
from astrbot.core.star.star import StarMetadata

SOURCE = Path(__file__).resolve().parents[1] / "main.py"
PLUGIN = next(
    node
    for node in ast.parse(SOURCE.read_text()).body
    if isinstance(node, ast.ClassDef)
)
COMMAND_NAMES = [
    name
    for node in PLUGIN.body
    if isinstance(node, ast.AsyncFunctionDef)
    for decorator in node.decorator_list
    if isinstance(decorator, ast.Call) and decorator.func.attr == "command"
    for name in [
        ast.literal_eval(decorator.args[0]),
        *(
            alias
            for keyword in decorator.keywords
            if keyword.arg == "alias"
            for alias in ast.literal_eval(keyword.value)
        ),
    ]
]


@pytest.fixture
def pipeline(monkeypatch):
    registry = star_handler.StarHandlerRegistry()
    metadata = {
        name: StarMetadata(name=name, module_path=name, activated=True)
        for name in ("giftia_permission_probe", __name__)
    }
    for module in (registration, waking, command_management):
        monkeypatch.setattr(module, "star_handlers_registry", registry)
    for module in (star_handler, waking, command_management):
        monkeypatch.setattr(module, "star_map", metadata)
    namespace = {
        "__name__": "giftia_permission_probe",
        "AstrMessageEvent": AstrMessageEvent,
        "GreedyStr": GreedyStr,
        "admin_permission": admin_permission,
        "filter": filter,
    }
    # Compile the real registrations without starting database and background services.
    exec(
        compile(
            ast.Module(
                body=[
                    node
                    for node in PLUGIN.body
                    if isinstance(node, ast.AsyncFunctionDef) and node.decorator_list
                ],
                type_ignores=[],
            ),
            str(SOURCE),
            "exec",
        ),
        namespace,
    )
    downstream = AsyncMock()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def downstream_handler(_plugin, event):
        await downstream(event)

    monkeypatch.setattr(
        waking.SessionPluginManager,
        "filter_handlers_by_session",
        AsyncMock(side_effect=lambda _event, handlers: handlers),
    )
    stage = waking.WakingCheckStage()
    stage.ctx = SimpleNamespace(
        astrbot_config={"admins_id": ["admin"], "wake_prefix": ["/"]}
    )
    stage.ignore_bot_self_message = False
    stage.friend_message_needs_wake_prefix = False
    stage.ignore_at_all = False
    stage.disable_builtin_commands = False
    stage.no_permission_reply = True
    stage._umo_auto_name_recorder = Mock()
    return stage, registry, downstream


@pytest.fixture(params=[False, True], ids=["group", "private"])
def event(request):
    event = Mock(spec=AstrMessageEvent)
    event.message_str = ""
    event.message_obj = SimpleNamespace(raw_message={})
    event.role = "member"
    event.is_wake = False
    event.is_at_or_wake_command = False
    event._extras = {}
    event.get_extra.side_effect = lambda key=None, default=None: (
        event._extras if key is None else event._extras.get(key, default)
    )
    event.set_extra.side_effect = event._extras.__setitem__
    event.get_message_str.side_effect = lambda: event.message_str
    event.get_messages.return_value = [Plain("/command")]
    event.get_sender_id.return_value = "member"
    event.get_self_id.return_value = "bot"
    event.get_group_id.return_value = "" if request.param else "group"
    event.get_platform_name.return_value = "aiocqhttp"
    event.get_message_type.return_value = (
        MessageType.FRIEND_MESSAGE if request.param else MessageType.GROUP_MESSAGE
    )
    event.is_private_chat.return_value = request.param
    event.is_admin.side_effect = lambda: event.role == "admin"
    event.send = AsyncMock()
    return event


@pytest.mark.asyncio
@pytest.mark.parametrize("name", COMMAND_NAMES)
@pytest.mark.parametrize("arguments", ["", "invalid extra arguments"])
async def test_denied_commands_are_silent_and_continue_to_listeners(
    pipeline, event, name, arguments
):
    stage, _, downstream = pipeline
    event.message_str = f"/{name} {arguments}".strip()
    await stage.process(event)

    event.send.assert_not_awaited()
    event.stop_event.assert_not_called()
    assert event.get_extra("handlers_parsed_params") == {}
    assert "parsed_params" not in event._extras
    handlers = event.get_extra("activated_handlers")
    assert {handler.handler_name for handler in handlers} == {
        "on_message",
        "downstream_handler",
    }
    plugin = SimpleNamespace(chat_manager=SimpleNamespace(handle_message=AsyncMock()))
    for handler in handlers:
        await handler.handler(plugin, event)
    plugin.chat_manager.handle_message.assert_awaited_once_with(event)
    downstream.assert_awaited_once_with(event)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message,method,params",
    [
        ("工具列表 2", "tool_list", {"index": 2}),
        ("获取定时任务 bot:123", "get_task_by_group", {"prefix": "bot:123"}),
        ("状态", "get_bot_status", {}),
        ("媒体转述", "get_media_caption", {}),
        ("用户画像 @小 明", "get_user_profile", {"target": "@小 明"}),
        ("休眠", "silence", {}),
    ],
)
async def test_administrators_keep_argument_parsing_and_aliases(
    pipeline, event, message, method, params
):
    stage, registry, _ = pipeline
    event.get_sender_id.return_value = "admin"
    event.message_str = f"/{message}"
    await stage.process(event)
    handler = next(handler for handler in registry if handler.handler_name == method)
    permission = next(
        item for item in handler.event_filters if isinstance(item, PermissionTypeFilter)
    )
    assert permission.permission_type == PermissionType.ADMIN
    assert permission.raise_error is False
    assert handler in event.get_extra("activated_handlers")
    assert (
        event.get_extra("handlers_parsed_params")[handler.handler_full_name] == params
    )
    event.send.assert_not_awaited()
    event.stop_event.assert_not_called()


@pytest.mark.asyncio
async def test_dashboard_permission_changes_apply_to_the_early_check(
    pipeline, event, monkeypatch
):
    stage, registry, _ = pipeline
    handler = next(item for item in registry if item.handler_name == "get_bot_status")
    monkeypatch.setattr(command_management.sp, "global_get", AsyncMock(return_value={}))
    monkeypatch.setattr(command_management.sp, "global_put", AsyncMock())
    descriptor = await command_management.update_command_permission(
        handler.handler_full_name, "member"
    )
    assert descriptor.permission == "member"
    event.message_str = "/状态"
    await stage.process(event)
    assert handler in event.get_extra("activated_handlers")

    await command_management.update_command_permission(
        handler.handler_full_name, "admin"
    )
    event.message_str = "/状态"
    await stage.process(event)
    assert handler not in event.get_extra("activated_handlers")
    event.send.assert_not_awaited()
    event.stop_event.assert_not_called()


def test_every_command_keeps_a_native_silent_admin_default(pipeline):
    _, registry, _ = pipeline
    for handler in registry:
        if not any(isinstance(item, CommandFilter) for item in handler.event_filters):
            continue
        permissions = [
            item
            for item in handler.event_filters
            if isinstance(item, PermissionTypeFilter)
        ]
        assert len(permissions) == 1
        assert permissions[0].permission_type == PermissionType.ADMIN
        assert permissions[0].raise_error is False
