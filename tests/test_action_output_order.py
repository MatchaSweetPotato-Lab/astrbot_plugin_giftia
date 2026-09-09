from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, call

import mcp.types
import pytest
from core.conversation.action_dispatcher import ActionDispatcher
from core.conversation.chat_manager import ChatManager
from core.conversation.reply_pipeline import ReplyPipeline
from core.database.data_cache import DataCache
from core.llm.xml_parse import XmlParse
from core.tts.manager import TTSManager
from core.utils.anti_drool import filter_duplicate_replies
from core.utils.schemas import MessageData, XmlLlmResult

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image, Plain, Record, Reply
from astrbot.api.star import Context
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)


@pytest.fixture
def runtime(monkeypatch):
    calls = Mock()
    adapter = SimpleNamespace(
        send_message=AsyncMock(return_value=(True, "sent-id")),
        group_poke=AsyncMock(return_value=None),
        msg_emoji_like=AsyncMock(return_value=None),
        repeat_message=AsyncMock(return_value=(True, "repeated-id", None)),
    )
    for name, action in vars(adapter).items():
        calls.attach_mock(action, name)
    sleep = AsyncMock()
    monkeypatch.setattr("core.conversation.action_dispatcher.asyncio.sleep", sleep)
    plugin = SimpleNamespace(
        aiocqhttp=adapter,
        qq_official=adapter,
        get_bot_config=Mock(return_value={}),
        min_reply_interval=1,
        max_reply_interval=1,
        data_cache=SimpleNamespace(
            is_bot_muted=Mock(return_value=False),
            add_message=AsyncMock(),
            recent_messages={
                "bot:100": [MessageData(message_id="400", user_id="other")]
            },
        ),
        message_parser=SimpleNamespace(
            chain_to_result=AsyncMock(
                return_value=SimpleNamespace(
                    content="sent", media_id_list=[], forward_messages=[]
                )
            )
        ),
    )
    event = MagicMock(spec=AiocqhttpMessageEvent)
    event.get_platform_name.return_value = "aiocqhttp"
    event.get_group_id.return_value = "100"
    event.get_self_id.return_value = "self"
    dispatcher = ActionDispatcher(plugin)
    dispatcher.build_tts_message_chain = AsyncMock(
        return_value=([Plain("voice")], "voice")
    )
    parser = XmlParse(None, None)
    parser._load_sticker_image = AsyncMock(
        return_value=Image.fromURL("https://example.com/sticker.png")
    )
    return SimpleNamespace(
        parser=parser,
        dispatcher=dispatcher,
        plugin=plugin,
        event=event,
        calls=calls,
        sleep=sleep,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["aiocqhttp", "qq_official_webhook"])
@pytest.mark.parametrize(
    "xml,expected",
    [
        (
            '<message>before</message><poke user_id="200"/>'
            '<emoji_like message_id="300" emoji_id="76"/><message>after</message>',
            ["send_message", "group_poke", "msg_emoji_like", "send_message"],
        ),
        (
            '<poke user_id="200"/><tts>voice</tts>'
            '<emoji_like message_id="300" emoji_id="76"/>'
            '<image url="https://example.com/image.png"/>'
            '<poke user_id="201"/><repeat message_id="400"/>'
            '<emoji_like message_id="301" emoji_id="66"/>'
            '<sticker sticker_id="sticker"/><message>after</message>',
            [
                "group_poke",
                "send_message",
                "msg_emoji_like",
                "send_message",
                "group_poke",
                "repeat_message",
                "msg_emoji_like",
                "send_message",
                "send_message",
            ],
        ),
        (
            '<poke user_id="200"/><emoji_like message_id="300" emoji_id="76"/>'
            '<poke user_id="201"/>',
            ["group_poke", "msg_emoji_like", "group_poke"],
        ),
    ],
)
async def test_actions_follow_xml_order(runtime, platform, xml, expected):
    runtime.event.get_platform_name.return_value = platform
    result = await runtime.parser.decode_llm_xml(xml, "100")
    assert result is not None
    await runtime.dispatcher.dispatch_actions(
        runtime.event, "bot", "Bot", "100", result
    )
    assert [item[0] for item in runtime.calls.mock_calls] == expected
    assert runtime.sleep.await_count == len(expected) - 1
    official = platform != "aiocqhttp"
    runtime.plugin.aiocqhttp.group_poke.assert_any_await(
        event=runtime.event,
        group_id="100" if official else 100,
        user_id="200" if official else 200,
    )
    runtime.plugin.aiocqhttp.msg_emoji_like.assert_any_await(
        event=runtime.event,
        message_id="300" if official else 300,
        emoji_id=76,
    )
    logs = "\n".join(
        entry.args[2].content
        for entry in runtime.plugin.data_cache.add_message.await_args_list
        if entry.args[2].role == "operation_log"
    )
    assert logs.count("<poke ") == expected.count("group_poke")
    assert logs.count("<emoji_like ") == expected.count("msg_emoji_like")


@pytest.mark.asyncio
@pytest.mark.parametrize("partial_order", [False, True])
async def test_actions_missing_from_order_are_sent_once_after_messages(
    runtime, partial_order
):
    result = await runtime.parser.decode_llm_xml(
        '<message>before</message><repeat message_id="400"/>'
        '<emoji_like message_id="300" emoji_id="76"/><poke user_id="200"/>',
        "100",
    )
    result.output_order = [("message", 0), ("repeat", 0)] if partial_order else []
    await runtime.dispatcher.dispatch_actions(
        runtime.event, "bot", "Bot", "100", result
    )
    assert [item[0] for item in runtime.calls.mock_calls] == [
        "send_message",
        "repeat_message",
        "msg_emoji_like",
        "group_poke",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["aiocqhttp", "qq_official_webhook"])
async def test_invalid_actions_do_not_block_later_outputs(runtime, platform):
    runtime.event.get_platform_name.return_value = platform
    result = await runtime.parser.decode_llm_xml(
        '<poke/><emoji_like message_id="300"/>'
        '<emoji_like message_id="300" emoji_id="invalid"/>'
        '<message>after</message><poke user_id="200"/>',
        "100",
    )
    assert result is not None
    assert result.output_order == [("emoji_like", 0), ("message", 0), ("poke", 0)]
    await runtime.dispatcher.dispatch_actions(
        runtime.event, "bot", "Bot", "100", result
    )
    assert [item[0] for item in runtime.calls.mock_calls] == [
        "send_message",
        "group_poke",
    ]
    assert runtime.sleep.await_args_list == [call(1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["aiocqhttp", "qq_official_webhook"])
async def test_moderation_actions_follow_xml_order_and_leave_is_last(runtime, platform):
    runtime.event.get_platform_name.return_value = platform
    for name in ("delete_messages", "like", "group_ban", "group_kick", "group_leave"):
        action = AsyncMock(return_value=None)
        setattr(runtime.plugin.aiocqhttp, name, action)
        runtime.calls.attach_mock(action, name)
    runtime.plugin.data_cache.set_message_recalled = AsyncMock()
    result = await runtime.parser.decode_llm_xml(
        '<leave/><message>before</message><delete message_id="400"/>'
        '<message>middle</message><like user_id="200" count="2"/>'
        '<ban user_id="200" duration="60"/><poke user_id="200"/>'
        '<kick user_id="200"/><message>after</message><delete message_id="401"/>',
        "100",
    )
    await runtime.dispatcher.dispatch_actions(
        runtime.event, "bot", "Bot", "100", result
    )
    assert [item[0] for item in runtime.calls.mock_calls] == [
        "send_message",
        "delete_messages",
        "send_message",
        "like",
        "group_ban",
        "group_poke",
        "group_kick",
        "send_message",
        "delete_messages",
        "group_leave",
    ]
    official = platform != "aiocqhttp"
    runtime.plugin.aiocqhttp.delete_messages.assert_has_awaits(
        [
            call(event=runtime.event, message_ids=["400" if official else 400]),
            call(event=runtime.event, message_ids=["401" if official else 401]),
        ]
    )
    runtime.plugin.aiocqhttp.group_ban.assert_awaited_once_with(
        event=runtime.event,
        group_id="100" if official else 100,
        user_id="200" if official else 200,
        duration=60,
    )
    runtime.plugin.aiocqhttp.group_kick.assert_awaited_once_with(
        event=runtime.event,
        group_id="100" if official else 100,
        user_id="200" if official else 200,
    )
    runtime.plugin.aiocqhttp.like.assert_awaited_once_with(
        event=runtime.event,
        user_id="200" if official else 200,
        count=2,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "delete_first,error,repeat_count",
    [(False, None, 1), (True, None, 0), (True, "permission denied", 1)],
)
async def test_recall_only_affects_later_repeats_on_success(
    runtime, delete_first, error, repeat_count
):
    runtime.plugin.aiocqhttp.delete_messages = AsyncMock(return_value=error)
    cache = runtime.plugin.data_cache
    cache.db = SimpleNamespace(update_message_recall=AsyncMock())
    cache.set_message_recalled = DataCache.set_message_recalled.__get__(cache)
    tags = ['<delete message_id="400"/>', '<repeat message_id="400"/>']
    result = await runtime.parser.decode_llm_xml(
        "".join(tags if delete_first else reversed(tags)), "100"
    )
    await runtime.dispatcher.dispatch_actions(
        runtime.event, "bot", "Bot", "100", result
    )
    assert runtime.plugin.aiocqhttp.repeat_message.await_count == repeat_count
    assert cache.recent_messages["bot:100"][0].is_recalled == (error is None)


@pytest.mark.asyncio
async def test_history_timestamps_follow_completed_actions(runtime):
    result = await runtime.parser.decode_llm_xml(
        '<message>before</message><poke user_id="200"/><message>middle</message>'
        '<emoji_like message_id="300" emoji_id="76"/><message>after</message>',
        "100",
    )
    await runtime.dispatcher.dispatch_actions(
        runtime.event, "bot", "Bot", "100", result
    )
    rows = [
        entry.args[2] for entry in runtime.plugin.data_cache.add_message.await_args_list
    ]
    assert [row.role for row in rows] == [
        "message",
        "operation_log",
        "message",
        "operation_log",
        "message",
    ]
    assert "<poke " in rows[1].content
    assert "<emoji_like " in rows[3].content
    assert rows == sorted(rows, key=lambda row: row.time)


@pytest.fixture
def signature_manager():
    manager = TTSManager.__new__(TTSManager)
    manager.plugin = SimpleNamespace(
        get_bot_config=lambda _: {
            "tts_config": {
                "enabled": True,
                "replace_in_message": True,
                "signature_voices": [
                    {"audio": "/tmp/voice.wav", "matched_texts": ["hello"]}
                ],
            }
        }
    )
    manager.build_record = AsyncMock(
        return_value=Record.fromFileSystem("/tmp/voice.wav")
    )
    return manager


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["aiocqhttp", "generic"])
@pytest.mark.parametrize(
    "text,first_kind",
    [("hello", "tts"), ("hello world", "tts"), ("before hello", "message")],
)
async def test_signature_quote_attaches_to_first_content(
    runtime, signature_manager, platform, text, first_kind
):
    runtime.event.get_platform_name.return_value = platform
    runtime.plugin.tts_manager = signature_manager
    runtime.plugin.get_bot_config = signature_manager.plugin.get_bot_config
    runtime.dispatcher = ActionDispatcher(runtime.plugin)
    result = await runtime.parser.decode_llm_xml(
        f'<message quote="400">{text}</message><poke user_id="200"/>', "100"
    )
    signature_manager.preprocess_signatures(result)
    first_order = list(result.output_order)
    first_segments = list(result.tts_segments)
    signature_manager.preprocess_signatures(result)
    assert result.output_order == first_order
    assert result.tts_segments == first_segments
    assert result.output_order[0][0] == first_kind
    assert result.output_order[-1] == ("poke", 0)
    assert all(
        any(not isinstance(c, Reply) for c in chain) for chain in result.msg_chains
    )
    await runtime.dispatcher.dispatch_actions(
        runtime.event, "bot", "Bot", "100", result
    )
    if platform == "generic":
        chains = [entry.args[0].chain for entry in runtime.event.send.await_args_list]
    else:
        chains = [
            entry.args[1]
            for entry in runtime.plugin.aiocqhttp.send_message.await_args_list
        ]
    assert isinstance(chains[0][0], Reply)
    assert str(chains[0][0].id) == "400"
    assert isinstance(chains[0][1], Record if first_kind == "tts" else Plain)
    assert sum(isinstance(c, Reply) for chain in chains for c in chain) == 1
    if first_kind == "tts":
        assert (
            'quote="400"'
            in runtime.plugin.data_cache.add_message.await_args_list[0].args[2].content
        )


@pytest.mark.asyncio
async def test_dedup_and_signature_split_preserve_actions_and_tools(
    runtime, signature_manager
):
    result = await runtime.parser.decode_llm_xml(
        '<message>duplicate</message><poke user_id="200"/>'
        '<tts>hello world</tts><tool_call name="text_tool">{}</tool_call>'
        '<emoji_like message_id="300" emoji_id="76"/>'
        '<image url="https://example.com/image.png"/><kick user_id="200"/><message>last</message>',
        "100",
    )
    filter_duplicate_replies(result, ["duplicate"])
    signature_manager.preprocess_signatures(result)
    assert result.output_order == [
        ("poke", 0),
        ("tts", 0),
        ("tts", 1),
        ("tool_call", 0),
        ("emoji_like", 0),
        ("image", 0),
        ("kick", 0),
        ("message", 1),
    ]


@pytest.fixture
def tool_runtime(runtime, monkeypatch):
    plugin = runtime.plugin
    plugin.tools_config = {"max_loop": 3}
    plugin.context = Mock(
        spec=Context,
        get_llm_tool_manager=lambda: SimpleNamespace(
            get_func=lambda name: (
                None if name == "missing_tool" else SimpleNamespace(name=name)
            )
        ),
    )
    observed = []

    async def record_send(chain):
        first = chain.chain[0]
        observed.append("image" if isinstance(first, Image) else first.text)

    async def adapter_send(event, chain, **kwargs):
        await record_send(MessageChain(chain))
        return True, "sent-id"

    runtime.event._giftia_bypass_logging = False
    runtime.event.send = AsyncMock(side_effect=record_send)
    plugin.aiocqhttp.send_message.side_effect = adapter_send

    async def execute(tool, run_context, **kwargs):
        if tool.name == "image_tool":
            yield mcp.types.CallToolResult(
                content=[
                    mcp.types.ImageContent(
                        type="image", data="aW1n", mimeType="image/png"
                    ),
                    mcp.types.TextContent(type="text", text="image result"),
                ]
            )
            await run_context.context.event.send(
                MessageChain([Plain("image tool tail")])
            )
            yield "image done"
        elif tool.name == "error_tool":
            raise RuntimeError("tool failed")
        else:
            await run_context.context.event.send(MessageChain([Plain("tool text")]))
            yield "done"

    monkeypatch.setattr(
        "core.conversation.tool_executor.FunctionToolExecutor.execute", execute
    )
    runtime.observed = observed
    return runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["aiocqhttp", "qq_official_webhook", "generic"])
@pytest.mark.parametrize(
    "xml,expected",
    [
        (
            '<tool_call name="text_tool">{}</tool_call><message>after</message>',
            ["tool text", "after"],
        ),
        (
            '<message>before</message><tool_call name="image_tool">{}</tool_call>'
            '<message>between</message><tool_call name="text_tool">{}</tool_call><message>after</message>',
            ["before", "image", "image tool tail", "between", "tool text", "after"],
        ),
        (
            '<tool_call name="image_tool">{}</tool_call><tool_call name="text_tool">{}</tool_call>',
            ["image", "image tool tail", "tool text"],
        ),
    ],
)
async def test_xml_tool_outputs_and_results_follow_order(
    tool_runtime, monkeypatch, platform, xml, expected
):
    runtime = tool_runtime
    runtime.event.get_platform_name.return_value = platform
    result = await runtime.parser.decode_llm_xml(xml, "100")
    plugin = runtime.plugin
    plugin.bot_map = {"bot": {"llm_reply_conf": {"provider_id": "mock"}}}
    plugin.embedding_conf = {"enabled": False, "session_recall_enabled": False}
    plugin.msg_number = 10
    plugin.get_caption_config = lambda _: {}
    plugin.emoji_manager = SimpleNamespace(
        get_random_stickers=AsyncMock(return_value=[])
    )
    plugin.context.persona_manager = SimpleNamespace(
        get_persona_v3_by_id=lambda _: {"prompt": "test"}
    )
    plugin.db = SimpleNamespace(
        slang_repo=SimpleNamespace(get_entries=AsyncMock(return_value=[]))
    )
    plugin.call_llm = SimpleNamespace(
        call_llm_reply=AsyncMock(side_effect=[result, XmlLlmResult()])
    )
    for name in (
        "get_recent_message",
        "get_bot_status",
        "get_user_profile_record",
        "get_group_profile",
        "get_user_relation",
        "build_active_user_briefs",
        "set_bot_status",
    ):
        setattr(plugin.data_cache, name, AsyncMock(return_value=[]))
    prompt = Mock(return_value="prompt")
    monkeypatch.setattr("core.conversation.reply_pipeline.build_reply_prompt", prompt)
    pipeline = ReplyPipeline.__new__(ReplyPipeline)
    pipeline.plugin = plugin
    pipeline.media_captioner = SimpleNamespace(
        get_cached_media_captions=AsyncMock(return_value=[])
    )
    pipeline.tool_executor = runtime.dispatcher.tool_executor
    async for chunk in pipeline.dispatch_llm_reply_loop(
        runtime.event, "bot", "Bot", "100", remind_message="test"
    ):
        if isinstance(chunk, XmlLlmResult):
            await runtime.dispatcher.dispatch_actions(
                runtime.event, "bot", "Bot", "100", chunk
            )
    assert runtime.observed == expected
    assert plugin.call_llm.call_llm_reply.await_count == 2
    assert [item["name"] for item in result.xml_tool_results] == [
        name for name, _ in result.tools_to_call
    ]
    assert prompt.call_args.kwargs["tool_results"] == result.xml_tool_results
    assert all(item["results"] for item in result.xml_tool_results)
    assert runtime.event._giftia_bypass_logging is False
    rows = [entry.args[2] for entry in plugin.data_cache.add_message.await_args_list]
    tool_logs = [row for row in rows if "<tool_call " in row.content]
    assert len(tool_logs) == len(result.tools_to_call)
    assert rows == sorted(rows, key=lambda row: row.time)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["missing_tool", "error_tool"])
async def test_failed_tool_does_not_block_later_outputs(tool_runtime, name):
    result = await tool_runtime.parser.decode_llm_xml(
        f'<tool_call name="{name}">{{}}</tool_call><message>after</message>', "100"
    )
    await tool_runtime.dispatcher.dispatch_actions(
        tool_runtime.event, "bot", "Bot", "100", result
    )
    assert tool_runtime.observed == ["after"]
    assert result.xml_tool_results[0]["results"] == (
        "工具不存在" if name == "missing_tool" else "工具执行失败: tool failed"
    )


@pytest.mark.asyncio
async def test_official_fallback_restores_tool_message_logging(tool_runtime):
    runtime = tool_runtime
    runtime.event.get_platform_name.return_value = "qq_official_webhook"
    runtime.plugin.qq_official.send_message.side_effect = None
    runtime.plugin.qq_official.send_message.return_value = (False, None)
    bypass_flags = []

    async def send(chain):
        bypass_flags.append(runtime.event._giftia_bypass_logging)
        runtime.observed.append(chain.chain[0].text)

    runtime.event.send.side_effect = send
    result = await runtime.parser.decode_llm_xml(
        '<message>before</message><tool_call name="text_tool">{}</tool_call>', "100"
    )
    await runtime.dispatcher.dispatch_actions(
        runtime.event, "bot", "Bot", "100", result
    )
    assert runtime.observed == ["before", "tool text"]
    assert bypass_flags == [True, False]


@pytest.mark.asyncio
async def test_untracked_tools_still_execute_once(tool_runtime):
    result = await tool_runtime.parser.decode_llm_xml(
        '<tool_call name="text_tool">{}</tool_call>', "100"
    )
    result.output_order = []
    await tool_runtime.dispatcher.dispatch_actions(
        tool_runtime.event, "bot", "Bot", "100", result
    )
    assert tool_runtime.observed == ["tool text"]
    assert result.xml_tool_results == [{"name": "text_tool", "results": "done"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["aiocqhttp", "qq_official_webhook", "generic"])
async def test_reminders_dispatch_tools_and_images_in_order(tool_runtime, platform):
    runtime = tool_runtime
    runtime.event.get_platform_name.return_value = platform
    result = await runtime.parser.decode_llm_xml(
        '<tool_call name="image_tool">{}</tool_call><message>after</message>', "100"
    )
    plugin = runtime.plugin
    plugin.replying_status = {}
    plugin.active_reply_counters = {}
    plugin.bot_map = {"bot": {}}
    plugin.passive_memory_manager = SimpleNamespace(
        mark_silence_summary_armed=AsyncMock()
    )

    async def send_by_session(origin, chain):
        runtime.observed.append(
            "image" if isinstance(chain.chain[0], Image) else chain.chain[0].text
        )
        return True

    plugin.context.send_message = AsyncMock(side_effect=send_by_session)

    async def reply(**kwargs):
        yield result

    manager = ChatManager.__new__(ChatManager)
    manager.plugin = plugin
    manager.fake_event = Mock(return_value=runtime.event)
    manager.action_dispatcher = runtime.dispatcher
    manager.reply_pipeline = SimpleNamespace(
        dispatch_llm_reply_loop=reply, commit_pending_session_recalled_memories=Mock()
    )
    await manager.remind_task(
        unified_msg_origin="test:GroupMessage:100",
        adapter_id="test",
        bot_name="bot",
        nickname="Bot",
        self_id="self",
        platform_name=platform,
        user_id="200",
        user_name="User",
        group_id="100",
        group_or_user_id="100",
        remind_message="test",
    )
    assert runtime.observed == ["image", "image tool tail", "after"]
    assert len(result.xml_tool_results) == 1
    assert plugin.replying_status["bot:100"] == 0
