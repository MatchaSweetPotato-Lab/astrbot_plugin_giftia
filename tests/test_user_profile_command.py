import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from core.handlers.commands import CommandHandler
from core.reports.service import ReportService
from core.reports.user_profile import USER_PROFILE_FIELDS, build_user_profile_report

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Image, Plain
from astrbot.core.star.filter.command import CommandFilter, GreedyStr


@pytest.fixture
def command(tmp_path):
    plugin = SimpleNamespace(
        adapter_id_map={"adapter": "bot"},
        bot_map={"bot": {"nickname": "小吉"}},
        conf={},
        data_cache=SimpleNamespace(
            get_user_profile_record=AsyncMock(
                return_value={
                    "call_name": "小明",
                    "personality": "温和\n细心",
                    "relation": 0,
                    "title": "朋友",
                }
            )
        ),
        html_render=AsyncMock(return_value=str(tmp_path / "profile.jpg")),
    )
    plugin.reports = ReportService(plugin, tmp_path)
    event = SimpleNamespace(
        platform_meta=SimpleNamespace(id="adapter"),
        get_group_id=Mock(return_value="group"),
        get_sender_id=Mock(return_value="sender"),
        get_self_id=Mock(return_value="bot-id"),
        get_messages=Mock(return_value=[Plain("/画像 001234")]),
        send=AsyncMock(),
    )
    return CommandHandler(plugin), plugin, event


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", ["001234", "platform-user_A1"])
@pytest.mark.parametrize("group_id", ["group", ""])
async def test_profile_id_lookup_is_scoped_and_defaults_to_text(
    command, user_id, group_id
):
    handler, plugin, event = command
    event.get_group_id.return_value = group_id
    _ = [chunk async for chunk in handler.get_user_profile(event, user_id)]
    plugin.data_cache.get_user_profile_record.assert_awaited_once_with(
        "bot", group_id or "sender", user_id
    )
    text = event.send.call_args.args[0].chain[0].text
    assert f"用户 ID：{user_id}" in text
    assert "性格风格：温和\n细心" in text
    assert "好感度：0" in text
    assert "关系称谓：朋友" in text
    assert "兴趣话题：暂无记录" in text
    plugin.html_render.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["", "@小 明(123456)"])
async def test_mention_uses_real_id_and_ignores_bot_wake_mention(command, target):
    handler, plugin, event = command
    event.get_messages.return_value = [
        At(qq="bot-id"),
        Plain("/画像 "),
        At(qq=123456, name="小 明"),
    ]
    _ = [chunk async for chunk in handler.get_user_profile(event, target)]
    plugin.data_cache.get_user_profile_record.assert_awaited_once_with(
        "bot", "group", "123456"
    )


@pytest.mark.asyncio
async def test_bot_mention_after_command_is_a_target(command):
    handler, plugin, event = command
    event.get_messages.return_value = [
        At(qq="bot-id"),
        Plain("/画像 "),
        At(qq="bot-id"),
    ]
    _ = [chunk async for chunk in handler.get_user_profile(event)]
    plugin.data_cache.get_user_profile_record.assert_awaited_once_with(
        "bot", "group", "bot-id"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "mentions"),
    [
        ("", []),
        (" ", ["bot-id"]),
        ("123 456", []),
        ("@小明", []),
        ("", ["all"]),
        ("", ["123", "456"]),
    ],
)
async def test_invalid_target_returns_usage_without_lookup(command, target, mentions):
    handler, plugin, event = command
    event.get_messages.return_value = [At(qq=user_id) for user_id in mentions]
    _ = [chunk async for chunk in handler.get_user_profile(event, target)]
    assert "/画像" in event.send.call_args.args[0].chain[0].text
    plugin.data_cache.get_user_profile_record.assert_not_awaited()
    plugin.html_render.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_bot_and_missing_profile(command):
    handler, plugin, event = command
    event.platform_meta.id = "unconfigured"
    _ = [chunk async for chunk in handler.get_user_profile(event, "123")]
    assert "未找到对应的 Bot" in event.send.call_args.args[0].chain[0].text
    plugin.data_cache.get_user_profile_record.assert_not_awaited()
    event.platform_meta.id = "adapter"
    plugin.conf["report_config"] = {"render_mode": "图片响应"}
    plugin.data_cache.get_user_profile_record.return_value = None
    _ = [chunk async for chunk in handler.get_user_profile(event, "123")]
    assert (
        "当前会话中暂无用户 123 的画像记录"
        in event.send.call_args.args[0].chain[0].text
    )
    plugin.html_render.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("send_fails", [False, True])
async def test_profile_image_uses_saved_template_and_cleans_up(command, send_fails):
    handler, plugin, event = command
    plugin.conf["report_config"] = {"render_mode": "图片响应"}
    plugin.reports.save_template("status", "<h1>STATUS</h1>")
    plugin.reports.save_template(
        "user_profile", "<h1>{{ user_id }} / {{ call_name }} / {{ relation }}</h1>"
    )
    path = Path(plugin.html_render.return_value)
    path.write_bytes(b"rendered image")

    async def send(chain):
        assert path.exists()
        assert isinstance(chain.chain[0], Image)
        if send_fails:
            raise RuntimeError("send failed")

    event.send.side_effect = send
    if send_fails:
        with pytest.raises(RuntimeError, match="send failed"):
            _ = [chunk async for chunk in handler.get_user_profile(event, "123")]
    else:
        _ = [chunk async for chunk in handler.get_user_profile(event, "123")]
    event.send.assert_awaited_once()
    call = plugin.html_render.call_args
    assert "<h1>123 / 小明 / 0</h1>" in call.args[1]["report_html"]
    assert "STATUS" not in call.args[1]["report_html"]
    assert call.kwargs["return_url"] is False
    assert not path.exists()


@pytest.mark.asyncio
async def test_image_failure_returns_the_same_text(command):
    handler, plugin, event = command
    _ = [chunk async for chunk in handler.get_user_profile(event, "123")]
    text_response = event.send.call_args.args[0].chain[0].text
    plugin.conf["report_config"] = {"render_mode": "图片响应"}
    plugin.html_render.side_effect = RuntimeError("render service unavailable")
    _ = [chunk async for chunk in handler.get_user_profile(event, "123")]
    assert event.send.call_args.args[0].chain[0].text == text_response


@pytest.mark.parametrize("relation", [None, 0, -5, 68])
def test_report_normalizes_missing_fields_without_mutating_record(command, relation):
    _, plugin, _ = command
    record = {
        "call_name": "<小明>",
        "personality": "  ",
        "relation": relation,
        "title": "",
    }
    data = build_user_profile_report("bot", "小吉", "group", "00123", record)
    assert data["user_id"] == "00123"
    assert data["relation"] == ("暂无记录" if relation is None else relation)
    assert data["relation_title"] == "暂无记录"
    assert data["personality"] == "暂无记录"
    assert data["title"] == "用户画像"
    assert record["personality"] == "  "
    for field, label in USER_PROFILE_FIELDS.items():
        assert data[field] == data["profile_fields"][label]
    rendered = plugin.reports.render_html("user_profile", data)
    assert "&lt;小明&gt;" in rendered
    assert "<小明>" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["", "001234", "@小 明(123456)"])
async def test_actual_command_wrapper_accepts_empty_and_full_arguments(command, target):
    handler, plugin, event = command
    # Load the real wrapper without registering all plugin handlers or starting services.
    source = Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    wrapper_node = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "get_user_profile"
    )
    decorator = wrapper_node.decorator_list[0]
    assert decorator.func.attr == "command"
    assert decorator.args[0].value == "画像"
    wrapper_node.decorator_list = []
    namespace = {"AstrMessageEvent": AstrMessageEvent, "GreedyStr": GreedyStr}
    exec(
        compile(ast.Module(body=[wrapper_node], type_ignores=[]), str(source), "exec"),
        namespace,
    )
    wrapper = namespace["get_user_profile"]
    command_filter = CommandFilter("画像", handler_md=SimpleNamespace(handler=wrapper))
    parsed = command_filter.validate_and_convert_params(
        target.split(), command_filter.handler_params
    )
    assert parsed == {"target": target}
    plugin.cmd_handler = handler
    if target != "001234":
        event.get_messages.return_value = [
            Plain("/画像 "),
            At(qq="123456", name="小 明"),
        ]
    _ = [chunk async for chunk in wrapper(plugin, event, **parsed)]
    plugin.data_cache.get_user_profile_record.assert_awaited_once_with(
        "bot", "group", "001234" if target == "001234" else "123456"
    )
