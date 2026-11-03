import asyncio
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from aiocqhttp import Event
from core.conversation.chat_manager import ChatManager
from core.database.data_cache import DataCache
from core.database.database import Database
from core.utils.message_parse import MessageParser
from core.utils.notice_parse import NoticeParser
from core.utils.schemas import MessageData

from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
    AiocqhttpAdapter,
)


@pytest_asyncio.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "astrbot.api.star.StarTools.get_data_dir", lambda *args: tmp_path
    )
    db = await Database.connect()
    plugin = SimpleNamespace(
        _terminated=False,
        adapter_id_map={"adapter": "bot"},
        bot_map={"bot": {"nickname": "Bot"}},
        running_tasks={},
        active_reply_counters={},
        replying_status={},
        parse_locks=defaultdict(asyncio.Lock),
        block_command_messages=False,
        passive_memory_enabled=True,
        passive_memory_manager=SimpleNamespace(
            check_and_trigger_passive_memory=AsyncMock(),
        ),
        db=db,
    )
    cache = DataCache(db, Mock(), Mock(), plugin=plugin)
    cache.initialize_user_profile = AsyncMock()
    plugin.data_cache = cache
    parser = MessageParser(cache, Mock(), False, False, SimpleNamespace(plugin=plugin))
    resolver = AsyncMock(
        side_effect=lambda event, user_id, current_name, force_lookup=False: (
            f"User_{user_id}" if user_id else ""
        )
    )
    parser._resolve_user_name = resolver
    parser.notice_parser._resolve_user_name = resolver
    parser.parse_user_message = AsyncMock(wraps=parser.parse_user_message)
    plugin.message_parser = parser
    manager = ChatManager(plugin)
    manager._check_command_info = Mock(return_value=(False, False))
    manager.decision_engine.check_whitelists = Mock(return_value=True)
    manager._enqueue_message = AsyncMock()
    manager.report_ban_to_stronghold = AsyncMock()

    async def dispatch(extra, *, raw_format="onebot", direct=False):
        payload = {
            "time": 1793721600,
            "self_id": 999,
            "post_type": "notice",
            "group_id": 100,
            "user_id": 200,
        }
        payload.update(extra)
        payload = {key: value for key, value in payload.items() if value is not None}
        raw = Event.from_payload(payload)
        assert raw is not None
        if raw.type == "message":
            adapter = SimpleNamespace(bot=SimpleNamespace(call_action=AsyncMock()))
            message = await AiocqhttpAdapter._convert_handle_message_event(
                adapter, raw, get_reply=False
            )
        else:
            message = await AiocqhttpAdapter._convert_handle_notice_event(
                SimpleNamespace(), raw
            )
        if raw_format == "dict":
            message.raw_message = dict(raw)
        elif raw_format == "object":
            message.raw_message = SimpleNamespace(**raw)
        event = SimpleNamespace(
            platform_meta=SimpleNamespace(id="adapter"),
            message_obj=message,
            unified_msg_origin=f"adapter:{message.type.value}:{message.session_id}",
            get_self_id=lambda: message.self_id,
            get_sender_id=lambda: message.sender.user_id,
            get_sender_name=lambda: message.sender.nickname,
            get_group_id=lambda: message.group_id or "",
            get_messages=lambda: message.message,
            get_platform_name=lambda: "aiocqhttp",
            get_extra=lambda key, default=None: default,
            should_call_llm=Mock(),
            stop_event=Mock(),
            send=AsyncMock(),
        )
        original_send = event.send
        if direct:
            await parser.parse_user_message(event, "bot")
        else:
            await manager.handle_message(event)
            await asyncio.gather(*list(plugin.running_tasks.values()))
        return event, original_send

    try:
        yield SimpleNamespace(
            plugin=plugin,
            db=db,
            cache=cache,
            parser=parser,
            manager=manager,
            dispatch=dispatch,
            resolver=resolver,
        )
    finally:
        await asyncio.gather(
            *list(plugin.running_tasks.values()), return_exceptions=True
        )
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_format", ["onebot", "dict", "object"])
@pytest.mark.parametrize(
    "payload,expected",
    [
        (
            {
                "notice_type": "notify",
                "sub_type": "group_name",
                "name_new": "New group",
            },
            "New group",
        ),
        (
            {
                "notice_type": "essence",
                "sub_type": "add",
                "sender_id": 300,
                "operator_id": 400,
                "message_id": 42,
            },
            "设为精华",
        ),
        (
            {
                "notice_type": "essence",
                "sub_type": "delete",
                "sender_id": 300,
                "operator_id": 400,
                "message_id": 42,
            },
            "移出精华",
        ),
        (
            {
                "notice_type": "group_decrease",
                "sub_type": "disband",
                "operator_id": 200,
            },
            "User_200(200) 解散了群聊",
        ),
        ({"notice_type": "notify", "sub_type": "title", "title": ""}, "专属头衔已移除"),
        ({"notice_type": "group_admin", "sub_type": "set"}, "被设置为群管理员"),
        (
            {"notice_type": "group_increase", "sub_type": "invite", "operator_id": 400},
            "邀请加入群聊",
        ),
        (
            {"notice_type": "group_decrease", "sub_type": "kick", "operator_id": 400},
            "移出了群聊",
        ),
        (
            {"notice_type": "group_card", "card_old": "Before", "card_new": "After"},
            '改为 "After"',
        ),
        (
            {
                "notice_type": "group_ban",
                "sub_type": "ban",
                "operator_id": 400,
                "duration": 60,
            },
            "1分钟",
        ),
        (
            {"notice_type": "notify", "sub_type": "honor", "honor_type": "talkative"},
            "龙王",
        ),
        (
            {"notice_type": "notify", "sub_type": "lucky_king", "target_id": 400},
            "运气王",
        ),
    ],
)
async def test_group_state_events_store_system_messages(
    runtime, raw_format, payload, expected
):
    await runtime.dispatch(payload, raw_format=raw_format)

    rows = await runtime.db.get_messages("100", "bot")
    assert len(rows) == 1
    row = rows[0]
    assert (row.role, row.user_id, row.nickname) == ("system", "system", "系统")
    assert expected in row.content
    if payload["notice_type"] == "essence":
        assert "User_300(300)" in row.content
        assert "User_400(400)" in row.content
        assert "42" in row.content
    runtime.manager._enqueue_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "group_id,notice_type", [(100, "group_upload"), (None, "offline_file")]
)
async def test_file_delivery_belongs_to_uploader(runtime, group_id, notice_type):
    await runtime.dispatch(
        {
            "notice_type": notice_type,
            "group_id": group_id,
            "file": {"name": "report.pdf", "size": 12},
        }
    )

    rows = await runtime.db.get_messages(str(group_id or 200), "bot")
    assert len(rows) == 1
    assert (rows[0].role, rows[0].user_id) == ("message", "200")
    assert rows[0].content == "[文件:report.pdf]"
    runtime.manager._enqueue_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("group_id", [100, None])
async def test_poke_belongs_to_actual_user(runtime, group_id):
    await runtime.dispatch(
        {
            "notice_type": "notify",
            "sub_type": "poke",
            "target_id": 999,
            "group_id": group_id,
        }
    )

    rows = await runtime.db.get_messages(str(group_id or 200), "bot")
    assert len(rows) == 1
    assert (rows[0].role, rows[0].user_id) == ("message", "200")
    assert rows[0].content == "[戳一戳:User_999(999)]"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "is_add,label", [(True, "贴表情回应"), (False, "取消表情回应")]
)
@pytest.mark.parametrize("user_id", [200, None, 0, "None"])
async def test_reactions_keep_actions_counts_and_actual_actor(
    runtime, is_add, label, user_id
):
    await runtime.dispatch(
        {
            "notice_type": "group_msg_emoji_like",
            "user_id": user_id,
            "is_add": is_add,
            "message_id": 42,
            "likes": [{"emoji_id": "124", "count": 2}, {"emoji_id": "76", "count": 1}],
        }
    )

    rows = await runtime.db.get_messages("100", "bot")
    assert len(rows) == 1
    row = rows[0]
    assert (row.user_id, row.role) == (
        ("200", "message") if user_id == 200 else ("system", "system")
    )
    assert row.content.count(label) == 2
    assert 'emoji_id="124"' in row.content and 'emoji_id="76"' in row.content
    assert 'count="2"' in row.content and 'count="1"' in row.content
    assert f'action="{"add" if is_add else "remove"}"' in row.content
    assert "None" not in row.content


IGNORED_EVENTS = [
    {"notice_type": "notify", "sub_type": "input_status"},
    {"notice_type": "input_status"},
    {"notice_type": "friend_add"},
    {"notice_type": "notify", "sub_type": "profile_like", "operator_id": 300},
    {"notice_type": "notify", "sub_type": "gray_tip", "content": '{"text":"tip"}'},
    {"notice_type": "online_file_send", "sub_type": "receive", "peer_id": 300},
    {"notice_type": "online_file_send", "sub_type": "refuse", "peer_id": 300},
    {"notice_type": "online_file_receive", "sub_type": "cancel", "peer_id": 300},
    {"notice_type": "client_status"},
    {"notice_type": "bot_offline"},
    {"notice_type": "future_notice"},
    {"notice_type": "notify", "sub_type": "future_subtype"},
    {"post_type": "request", "request_type": "friend"},
    {"post_type": "request", "request_type": "group", "sub_type": "invite"},
    {"post_type": "meta_event", "meta_event_type": "heartbeat"},
    {"post_type": "meta_event", "meta_event_type": "lifecycle"},
]


@pytest.mark.asyncio
async def test_private_reaction_is_stored_in_the_peer_conversation(runtime):
    await runtime.dispatch(
        {
            "notice_type": "friend_msg_emoji_like",
            "group_id": None,
            "message_id": 42,
            "emoji_id": "124",
            "is_add": False,
        }
    )
    rows = await runtime.db.get_messages("200", "bot")
    assert len(rows) == 1
    assert (rows[0].user_id, rows[0].role) == ("200", "message")
    assert "取消表情回应" in rows[0].content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"notice_type": "friend_msg_emoji_like", "emoji_id": "124"},
        {"notice_type": "notify", "sub_type": "poke", "target_id": 999},
        {"notice_type": "offline_file", "file": {"name": "report.pdf", "size": 12}},
    ],
)
async def test_private_notices_without_a_peer_never_create_none_conversations(
    runtime, payload
):
    await runtime.dispatch({**payload, "group_id": None, "user_id": None})
    async with runtime.db.query("SELECT COUNT(*) FROM chat_history") as cursor:
        assert (await cursor.fetchone())[0] == 0
    runtime.parser.parse_user_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("group_id", [100, None])
@pytest.mark.parametrize("payload", IGNORED_EVENTS)
async def test_ignored_events_do_not_parse_store_or_schedule(
    runtime, group_id, payload
):
    event, original_send = await runtime.dispatch({**payload, "group_id": group_id})

    assert await runtime.db.get_messages(str(group_id or 200), "bot") == []
    runtime.parser.parse_user_message.assert_not_awaited()
    runtime.manager._enqueue_message.assert_not_awaited()
    runtime.plugin.passive_memory_manager.check_and_trigger_passive_memory.assert_not_awaited()
    assert event.send is original_send
    event.should_call_llm.assert_not_called()
    assert runtime.plugin.running_tasks == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", IGNORED_EVENTS)
async def test_direct_parser_also_discards_ignored_events(runtime, payload):
    await runtime.dispatch(payload, direct=True)
    assert await runtime.db.get_messages("100", "bot") == []
    runtime.resolver.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"notice_type": "notify", "sub_type": "group_name"},
        {"notice_type": "group_upload", "file": {}},
        {"notice_type": "group_msg_emoji_like", "likes": []},
        {"notice_type": "notify", "sub_type": "poke"},
        {"notice_type": "notify", "sub_type": "title"},
        {"notice_type": "group_card"},
    ],
)
async def test_incomplete_notices_do_not_write_empty_rows_or_trigger_memory(
    runtime, payload
):
    await runtime.dispatch(payload)
    assert await runtime.db.get_messages("100", "bot") == []
    runtime.manager._enqueue_message.assert_not_awaited()
    runtime.plugin.passive_memory_manager.check_and_trigger_passive_memory.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"notice_type": "group_increase", "sub_type": "invite", "operator_id": 400},
        {"notice_type": "group_card", "card_old": "Before", "card_new": "After"},
        {"notice_type": "notify", "sub_type": "title", "title": "Title"},
        {"notice_type": "notify", "sub_type": "group_name", "name_new": "New group"},
        {
            "notice_type": "essence",
            "sub_type": "add",
            "sender_id": 999,
            "operator_id": 400,
            "message_id": 42,
        },
    ],
)
async def test_bot_state_changes_survive_self_message_filter(runtime, payload):
    await runtime.dispatch({**payload, "user_id": 999})
    rows = await runtime.db.get_messages("100", "bot")
    assert len(rows) == 1
    assert rows[0].user_id == "system"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"notice_type": "notify", "sub_type": "poke", "target_id": 200},
        {"notice_type": "group_upload", "file": {"name": "report.pdf", "size": 12}},
        {"notice_type": "group_msg_emoji_like", "emoji_id": "124", "message_id": 42},
    ],
)
async def test_bot_initiated_interactions_remain_filtered(runtime, payload):
    await runtime.dispatch({**payload, "user_id": 999})
    assert await runtime.db.get_messages("100", "bot") == []
    runtime.parser.parse_user_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_format", ["onebot", "dict", "object"])
@pytest.mark.parametrize(
    "group_id,notice_type", [(100, "group_recall"), (None, "friend_recall")]
)
async def test_recall_only_marks_original_message(
    runtime, raw_format, group_id, notice_type
):
    session = str(group_id or 200)
    await runtime.db.insert_message(
        "bot",
        MessageData(
            group_or_user_id=session, user_id="200", message_id="42", content="original"
        ),
    )

    await runtime.dispatch(
        {"notice_type": notice_type, "group_id": group_id, "message_id": 42},
        raw_format=raw_format,
    )

    rows = await runtime.db.get_messages(session, "bot")
    assert len(rows) == 1
    assert rows[0].is_recalled == 1
    assert rows[0].content == "original"
    runtime.parser.parse_user_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("notice_first", [False, True])
async def test_file_message_and_notice_store_once_with_native_id(runtime, notice_first):
    notice = {"notice_type": "group_upload", "file": {"name": "report.pdf", "size": 12}}
    message = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 42,
        "sender": {"user_id": 200, "nickname": "User_200"},
        "message": [
            {
                "type": "file",
                "data": {
                    "file": "report.pdf",
                    "file_size": "12",
                    "url": "https://example.com/report.pdf",
                },
            }
        ],
    }
    order = [notice, message] if notice_first else [message, notice]
    for index, payload in enumerate(order):
        await runtime.dispatch(payload)
        # Reloading history must not leave a cached copy with the old notice ID.
        if index == 0:
            await runtime.cache.get_recent_message("bot", "100")
    await runtime.dispatch(message)

    rows = await runtime.db.get_messages("100", "bot")
    assert len(rows) == 1
    assert rows[0].message_id == "42"
    assert len(runtime.cache.recent_messages["bot:100"]) == 1
    assert runtime.cache.recent_messages["bot:100"][0].message_id == "42"
    runtime.manager._enqueue_message.assert_awaited_once()
    await runtime.dispatch({"notice_type": "group_recall", "message_id": 42})
    assert (await runtime.db.get_messages("100", "bot"))[0].is_recalled == 1


@pytest.mark.asyncio
async def test_repeated_file_uploads_are_not_merged(runtime):
    notice = {"notice_type": "group_upload", "file": {"name": "report.pdf", "size": 12}}
    await runtime.dispatch(notice)
    await runtime.dispatch(notice)
    assert len(await runtime.db.get_messages("100", "bot")) == 2


@pytest.mark.asyncio
async def test_each_file_notice_pairs_with_only_one_message(runtime):
    for message_id in (42, 43):
        await runtime.dispatch(
            {
                "notice_type": "group_upload",
                "file": {"name": "report.pdf", "size": 12},
            }
        )
        await runtime.dispatch(
            {
                "post_type": "message",
                "message_type": "group",
                "message_id": message_id,
                "sender": {"user_id": 200, "nickname": "User_200"},
                "message": [
                    {
                        "type": "file",
                        "data": {
                            "file": "report.pdf",
                            "file_size": "12",
                            "url": "https://example.com/report.pdf",
                        },
                    }
                ],
            }
        )

    rows = await runtime.db.get_messages("100", "bot")
    assert {row.message_id for row in rows} == {"42", "43"}
    assert len(rows) == 2
    assert runtime.manager._enqueue_message.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "different", [{"user_id": 300}, {"group_id": 101}, {"size": 13}]
)
async def test_unrelated_files_are_not_paired_by_name(runtime, different):
    await runtime.dispatch(
        {
            "notice_type": "group_upload",
            "file": {"name": "report.pdf", "size": 12},
        }
    )
    user_id = different.get("user_id", 200)
    group_id = different.get("group_id", 100)
    await runtime.dispatch(
        {
            "post_type": "message",
            "message_type": "group",
            "message_id": 42,
            "user_id": user_id,
            "group_id": group_id,
            "sender": {"user_id": user_id, "nickname": f"User_{user_id}"},
            "message": [
                {
                    "type": "file",
                    "data": {
                        "file": "report.pdf",
                        "file_size": different.get("size", 12),
                        "url": "https://example.com/report.pdf",
                    },
                }
            ],
        }
    )
    async with runtime.db.query("SELECT COUNT(*) FROM chat_history") as cursor:
        assert (await cursor.fetchone())[0] == 2


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            SimpleNamespace(name="notice.notify.group_name"),
            ("notice", "notify", "group_name"),
        ),
        ({"name": "notice.group_recall"}, ("notice", "group_recall", "")),
        ({"name": "request.friend"}, ("request", "", "")),
        (None, ("", "", "")),
    ],
)
def test_qualified_event_names_are_normalized(raw, expected):
    assert NoticeParser.get_event_types(raw) == expected
