import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import pytest
import pytest_asyncio
from core.database.data_cache import DataCache
from core.database.profile_store import ProfileStoreMixin
from core.database.schema import initialize_database
from core.handlers.commands import CommandHandler
from core.llm.prompt import build_decision_prompt, build_reply_prompt
from core.memory.passive_tasks import PassiveSummaryTaskMixin
from core.utils.schemas import MessageData, Status
from core.web.profile_api import ProfileApi

from astrbot.api.event import AstrMessageEvent
from astrbot.core.star.filter.command import CommandFilter, GreedyStr
from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter


class ProfileDB(ProfileStoreMixin):
    def __init__(self, conn):
        self.conn = conn


@pytest_asyncio.fixture
async def runtime():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await initialize_database(conn)
        db = ProfileDB(conn)
        cache = DataCache(db, None, None)
        plugin = SimpleNamespace(
            db=db, data_cache=cache, adapter_id_map={"adapter": "bot"}
        )
        yield plugin


@pytest.mark.asyncio
async def test_alias_claims_are_atomic_scoped_and_manual_writes_do_not_increment(
    runtime,
):
    db = runtime.db
    claims = await asyncio.gather(
        db.upsert_user_aliases("bot", "group", "A", "草莓"),
        db.upsert_user_aliases("bot", "group", "B", "草莓"),
    )
    assert sorted(claims) == [[], ["草莓"]]
    async with db.conn.execute("SELECT user_id FROM user_aliases") as cursor:
        rows = await cursor.fetchall()
    assert len(rows) == 1
    owner = rows[0]["user_id"]
    other = "B" if owner == "A" else "A"
    assert await db.upsert_user_aliases("bot", "group", other, "草莓") == ["草莓"]
    assert await db.upsert_user_aliases("bot", "group", owner, "草莓") == []
    await db.upsert_user_aliases("bot", "group", owner, "草莓", increment_count=False)
    aliases = await db.get_user_aliases("bot", "group", owner, ignore_count_filter=True)
    assert aliases[0]["alias_count"] == 2
    assert await db.upsert_user_aliases("bot", "other-group", other, "草莓") == []
    assert await db.upsert_user_aliases("other-bot", "group", other, "草莓") == []
    await db.upsert_user_aliases("bot", "group", "A", "Alice")
    assert await db.upsert_user_aliases("bot", "group", "B", " alice ") == ["alice"]
    await db.delete_user_alias("bot", "group", owner, "草莓")
    assert await db.upsert_user_aliases("bot", "group", other, "草莓") == []


@pytest.mark.asyncio
async def test_existing_duplicate_owners_are_preserved_but_no_longer_increment(runtime):
    db = runtime.db
    for user_id in ("A", "B"):
        await db.conn.execute(
            "INSERT INTO user_aliases (bot_name, group_or_user_id, user_id, alias, alias_count) "
            "VALUES ('bot', 'group', ?, '草莓', 4)",
            (user_id,),
        )
    await db.conn.commit()
    for user_id in ("A", "B", "C"):
        assert await db.upsert_user_aliases("bot", "group", user_id, "草莓") == ["草莓"]
    async with db.conn.execute("SELECT alias_count FROM user_aliases") as cursor:
        assert [row[0] for row in await cursor.fetchall()] == [4, 4]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sender", "content", "role", "recalled", "completion", "expected_count"),
    [
        (
            "C",
            "今天买了草莓",
            "message",
            0,
            "<summary_user_profile>无</summary_user_profile>",
            1,
        ),
        (
            "C",
            "今天买了苹果",
            "message",
            0,
            '<summary_user_profile user_id="A"><aliases>草莓</aliases></summary_user_profile>',
            1,
        ),
        (
            "A",
            "我是草莓",
            "message",
            0,
            '<summary_user_profile user_id="A"><aliases>草莓</aliases></summary_user_profile>',
            1,
        ),
        (
            "bot-id",
            "草莓过来",
            "message",
            0,
            '<summary_user_profile user_id="A"><aliases>草莓</aliases></summary_user_profile>',
            1,
        ),
        (
            "C",
            "草莓过来",
            "operation_log",
            0,
            '<summary_user_profile user_id="A"><aliases>草莓</aliases></summary_user_profile>',
            1,
        ),
        (
            "C",
            "草莓过来",
            "message",
            1,
            '<summary_user_profile user_id="A"><aliases>草莓</aliases></summary_user_profile>',
            1,
        ),
        (
            "C",
            "草莓 @A 过来",
            "message",
            0,
            '<summary_user_profile user_id="A"><aliases>草莓，草莓</aliases></summary_user_profile>'
            * 2,
            2,
        ),
        (
            "C",
            "草莓 @A 过来",
            "message",
            0,
            '<summary_user_profile user_id="B"><aliases>草莓</aliases></summary_user_profile>',
            1,
        ),
    ],
)
async def test_only_current_model_confirmed_alias_observations_count_once(
    runtime, sender, content, role, recalled, completion, expected_count
):
    await runtime.db.upsert_user_aliases("bot", "group", "A", "草莓")
    await runtime.db.upsert_group_profile("group", "bot", "禁止刷屏")
    manager = PassiveSummaryTaskMixin()
    manager.plugin = runtime
    manager._call_summary_llm = AsyncMock(
        return_value=completion
        + "<summary_group_profile>允许刷屏</summary_group_profile>"
    )
    context = {
        "nickname_to_user_id": {},
        "active_users_in_range": {"A", "B", "C"},
        "user_profiles_text": "A 的外号：草莓",
        "media_captions_block": "",
        "chat_history_text": content,
        "alias_observation_messages": [
            MessageData(
                user_id=sender, content=content, role=role, is_recalled=recalled
            )
        ],
    }
    assert await manager._run_profile_summary_task(
        "bot", "group", "小吉", "bot-id", context
    )
    aliases = await runtime.db.get_user_aliases(
        "bot", "group", "A", ignore_count_filter=True
    )
    assert aliases[0]["alias_count"] == expected_count
    assert (
        await runtime.db.get_user_aliases("bot", "group", "B", ignore_count_filter=True)
        == []
    )
    assert await runtime.db.get_group_profile("group", "bot") == "禁止刷屏"
    user_prompt = manager._call_summary_llm.call_args.args[2]
    assert "current_group_profile" not in user_prompt
    assert "group_rules" not in user_prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("group_id", ["group", ""])
@pytest.mark.parametrize("rules", ["禁止刷屏", "1. 禁止刷屏\n2. 讨论时  尊重他人", ""])
async def test_rules_command_preserves_original_text_and_overwrites_only_current_session(
    runtime, group_id, rules
):
    await runtime.data_cache.set_group_profile("bot", group_id or "sender", "旧规则")
    await runtime.data_cache.set_group_profile("bot", "other-group", "其他群规则")
    await runtime.data_cache.set_group_profile(
        "other-bot", group_id or "sender", "其他机器人规则"
    )
    event = SimpleNamespace(
        platform_meta=SimpleNamespace(id="adapter"),
        get_group_id=Mock(return_value=group_id),
        get_sender_id=Mock(return_value="sender"),
        get_message_str=Mock(return_value=f"群规 {rules}"),
        is_at_or_wake_command=True,
        is_admin=Mock(return_value=True),
        set_extra=Mock(),
        send=AsyncMock(),
    )
    source = Path(__file__).resolve().parents[1] / "main.py"
    wrapper_node = next(
        node
        for node in ast.walk(ast.parse(source.read_text()))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "set_group_rules"
    )
    permission, command = wrapper_node.decorator_list
    assert permission.func.attr == "permission_type"
    assert permission.args[0].attr == "ADMIN"
    assert command.args[0].value == "群规"
    wrapper_node.decorator_list = []
    namespace = {"AstrMessageEvent": AstrMessageEvent, "GreedyStr": GreedyStr}
    exec(
        compile(ast.Module(body=[wrapper_node], type_ignores=[]), str(source), "exec"),
        namespace,
    )
    wrapper = namespace["set_group_rules"]
    permission_filter = PermissionTypeFilter(PermissionType.ADMIN)
    assert permission_filter.filter(event, {})
    event.is_admin.return_value = False
    assert not permission_filter.filter(event, {})
    command_filter = CommandFilter("群规", handler_md=SimpleNamespace(handler=wrapper))
    assert command_filter.filter(event, {})
    runtime.cmd_handler = CommandHandler(runtime)
    _ = [
        chunk
        async for chunk in wrapper(runtime, event, **event.set_extra.call_args.args[1])
    ]
    assert await runtime.data_cache.get_group_profile("bot", group_id or "sender") == (
        rules or "旧规则"
    )
    assert await runtime.db.get_group_profile(group_id or "sender", "bot") == (
        rules or "旧规则"
    )
    assert (
        await runtime.data_cache.get_group_profile("bot", "other-group") == "其他群规则"
    )
    assert (
        await runtime.data_cache.get_group_profile("other-bot", group_id or "sender")
        == "其他机器人规则"
    )
    response = event.send.call_args.args[0].chain[0].text
    assert ("已覆写" if rules else "用法") in response
    if not rules:
        assert "当前会话的群规：\n旧规则" in response
        assert "其他群规则" not in response
        assert "其他机器人规则" not in response


@pytest.mark.asyncio
@pytest.mark.parametrize("group_id", ["group", ""])
@pytest.mark.parametrize(
    "stored_rules", [None, "", " \n ", "禁止刷屏\n讨论时  尊重他人"]
)
async def test_empty_rules_command_displays_existing_content_without_writing(
    runtime, group_id, stored_rules
):
    session_id = group_id or "sender"
    if stored_rules is not None:
        await runtime.data_cache.set_group_profile("bot", session_id, stored_rules)
    runtime.db.upsert_group_profile = AsyncMock()
    event = SimpleNamespace(
        platform_meta=SimpleNamespace(id="adapter"),
        get_message_str=Mock(return_value="/群规 \n "),
        get_group_id=Mock(return_value=group_id),
        get_sender_id=Mock(return_value="sender"),
        send=AsyncMock(),
    )
    _ = [chunk async for chunk in CommandHandler(runtime).set_group_rules(event)]
    response = event.send.call_args.args[0].chain[0].text
    assert "用法：/群规 具体规则" in response
    if stored_rules and stored_rules.strip():
        assert f"当前会话的群规：\n{stored_rules}" in response
    else:
        assert "当前会话的群规：" not in response
    assert await runtime.db.get_group_profile(session_id, "bot") == stored_rules
    runtime.db.upsert_group_profile.assert_not_awaited()


def test_group_rules_are_injected_verbatim_into_both_prompts():
    rules = "禁止刷屏\n例外：无\n\n讨论时  尊重他人"
    status = Status()
    prompts = [
        build_decision_prompt(
            "A", "", [], MessageData(user_id="A"), status, group_profile=rules
        ),
        build_reply_prompt([], [], status, group_profile=rules),
    ]
    for prompt in prompts:
        assert f"<group_rules>\n{rules}\n</group_rules>" in prompt
        assert "<group_profile>" not in prompt


@pytest.mark.asyncio
async def test_manual_rules_api_refreshes_cache_and_reports_alias_conflicts(
    runtime, monkeypatch
):
    api = ProfileApi()
    api.giftia = runtime
    scope = {"bot_name": "bot", "group_or_user_id": "group"}
    request = SimpleNamespace(
        json=AsyncMock(return_value={**scope, "profile": "新规则"})
    )
    monkeypatch.setattr("core.web.profile_api.request", request)
    await runtime.data_cache.set_group_profile("bot", "group", "旧规则")
    assert json.loads((await api.update_group_profile()).body)["status"] == "success"
    assert await runtime.data_cache.get_group_profile("bot", "group") == "新规则"
    request.json.return_value = scope
    assert json.loads((await api.delete_group_profile()).body)["status"] == "success"
    assert await runtime.data_cache.get_group_profile("bot", "group") is None
    await runtime.db.upsert_user_aliases("bot", "group", "A", "草莓")
    request.json.return_value = {**scope, "user_id": "B", "alias": "草莓，蓝莓"}
    response = json.loads((await api.add_user_alias()).body)
    assert "归属冲突" in response["message"]
    aliases = await runtime.db.get_user_aliases(
        "bot", "group", "B", ignore_count_filter=True
    )
    assert [alias["alias"] for alias in aliases] == ["蓝莓"]


@pytest.mark.asyncio
async def test_failed_rules_write_does_not_change_cache_or_report_success(runtime):
    await runtime.data_cache.set_group_profile("bot", "group", "旧规则")
    runtime.db.upsert_group_profile = AsyncMock(
        side_effect=RuntimeError("write failed")
    )
    event = SimpleNamespace(
        platform_meta=SimpleNamespace(id="adapter"),
        get_message_str=Mock(return_value="/群规 新规则"),
        get_group_id=Mock(return_value="group"),
        get_sender_id=Mock(return_value="sender"),
        send=AsyncMock(),
    )
    with pytest.raises(RuntimeError, match="write failed"):
        _ = [
            chunk
            async for chunk in CommandHandler(runtime).set_group_rules(event, "新规则")
        ]
    assert await runtime.data_cache.get_group_profile("bot", "group") == "旧规则"
    event.send.assert_not_awaited()
    event.platform_meta.id = "missing"
    _ = [
        chunk
        async for chunk in CommandHandler(runtime).set_group_rules(event, "新规则")
    ]
    assert "未找到对应的 Bot" in event.send.call_args.args[0].chain[0].text
