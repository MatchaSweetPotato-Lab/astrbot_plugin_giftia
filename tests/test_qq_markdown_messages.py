from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiocqhttp import Event
from core.utils.message_parse import MessageParser
from core.utils.qq_official_action import QQOfficialAction

from astrbot.api.message_components import At, Node, Plain, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
    AiocqhttpAdapter,
)

MENTION = r"[@e\.e\.](mqqapi://markdown/mention?at_type=1&at_tinyid=1017789696)"
ESCAPED_MENTION = MENTION.replace("](", r"]\(").replace(")", r"\)")
BODY = "这次看到了嘛"
READABLE = f"@e.e.(1017789696) {BODY}"


@pytest.fixture
def parser():
    return MessageParser(
        data_cache=SimpleNamespace(add_message=AsyncMock()),
        http_manager=MagicMock(),
        image_caption_enabled=False,
        audio_caption_enabled=False,
        call_llm=SimpleNamespace(plugin=None),
    )


@pytest.fixture
def event_from_segments():
    async def create(segments):
        raw = Event.from_payload(
            {
                "post_type": "message",
                "message_type": "group",
                "self_id": 3970706156,
                "group_id": 1102207288,
                "user_id": 4018683728,
                "sender": {"user_id": 4018683728, "nickname": "王小桃"},
                "message_id": -1958290251,
                "message": segments,
            }
        )
        adapter = SimpleNamespace(
            bot=SimpleNamespace(call_action=AsyncMock(return_value={"card": "e.e."}))
        )
        message_obj = await AiocqhttpAdapter._convert_handle_message_event(
            adapter, raw, get_reply=False
        )
        return SimpleNamespace(
            message_obj=message_obj,
            get_self_id=lambda: "3970706156",
            get_messages=lambda: message_obj.message,
            get_group_id=lambda: "1102207288",
            get_sender_id=lambda: "4018683728",
            get_sender_name=lambda: "王小桃",
        )

    return create


@pytest.mark.asyncio
@pytest.mark.parametrize("markdown_first", [True, False])
@pytest.mark.parametrize("markdown_key", ["content", "markdown"])
@pytest.mark.parametrize("body", ["sensei 在这里哦", "**收到**\n\n指令已执行"])
async def test_markdown_without_mentions_drops_only_its_text_fallback(
    parser, event_from_segments, markdown_first, markdown_key, body
):
    markdown = {"type": "markdown", "data": {markdown_key: body}}
    fallback = {"type": "text", "data": {"text": body}}
    segments = [markdown, fallback] if markdown_first else [fallback, markdown]
    event = await event_from_segments(segments)
    original = [component.toDict() for component in event.get_messages()]

    message, _, _ = await parser.parse_user_message(event, "bot")

    assert message.content == body
    parser.data_cache.add_message.assert_awaited_once_with("bot", "1102207288", message)
    assert [component.toDict() for component in event.get_messages()] == original
    assert event.message_obj.raw_message["message"] == segments


@pytest.mark.asyncio
@pytest.mark.parametrize("segment_type", ["markdown", "text"])
async def test_identical_segments_of_the_same_type_are_preserved(
    parser, event_from_segments, segment_type
):
    text = MENTION + BODY
    key = "content" if segment_type == "markdown" else "text"
    segment = {"type": segment_type, "data": {key: text}}
    event = await event_from_segments([segment, segment])

    message, _, _ = await parser.parse_user_message(event, "bot")

    assert message.content.count(BODY) == 2
    assert message.content.count("@e.e.(1017789696)") == 2


@pytest.mark.asyncio
async def test_reported_raw_markdown_with_trailing_mention_is_stored_once(
    parser, event_from_segments
):
    text = "sensei 在这里哦" + MENTION
    event = await event_from_segments(
        [
            {"type": "reply", "data": {"id": "1367513543"}},
            {"type": "markdown", "data": {"content": text}},
            {"type": "text", "data": {"text": text}},
        ]
    )
    quote = event.get_messages()[0]
    quote.chain = [At(qq="4018683728", name="王小桃"), Plain("艾特我并引用这条消息")]
    quote.sender_id = "1017789696"
    quote.sender_nickname = "e.e."
    original = [component.toDict() for component in event.get_messages()]

    message, _, _ = await parser.parse_user_message(event, "bot")

    assert message.content == (
        '<quote message_id="1367513543" sender_id="1017789696" sender_name="e.e.">'
        "@王小桃(4018683728) 艾特我并引用这条消息</quote> "
        "sensei 在这里哦 @e.e.(1017789696)"
    )
    assert message.message_id == "-1958290251"
    parser.data_cache.add_message.assert_awaited_once_with("bot", "1102207288", message)
    assert [component.toDict() for component in event.get_messages()] == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fallback", [MENTION + BODY + "。", MENTION.replace("1017789696", "10002") + BODY]
)
async def test_different_raw_markdown_and_text_are_preserved(
    parser, event_from_segments, fallback
):
    event = await event_from_segments(
        [
            {"type": "markdown", "data": {"content": MENTION + BODY}},
            {"type": "text", "data": {"text": fallback}},
        ]
    )

    message, _, _ = await parser.parse_user_message(event, "bot")

    assert message.content.count(BODY) == 2


@pytest.mark.asyncio
async def test_grouped_text_fallback_matches_adapter_grouping(
    parser, event_from_segments
):
    event = await event_from_segments(
        [
            {"type": "markdown", "data": {"content": BODY}},
            {"type": "text", "data": {"text": BODY[:3]}},
            {"type": "text", "data": {"text": BODY[3:]}},
        ]
    )

    message, _, _ = await parser.parse_user_message(event, "bot")

    assert message.content == BODY


@pytest.mark.asyncio
@pytest.mark.parametrize("context", ["outgoing", "quote", "modified"])
async def test_raw_source_does_not_apply_to_other_or_modified_chains(
    parser, event_from_segments, context
):
    event = await event_from_segments(
        [
            {"type": "markdown", "data": {"content": BODY}},
            {"type": "text", "data": {"text": BODY}},
        ]
    )
    body = BODY
    chain = [Plain(body), Plain(body)]
    if context == "quote":
        chain = [Reply(id="other", chain=chain)]
    elif context == "modified":
        body = "其他插件改写的正文"
        chain = event.get_messages()
        for comp in chain:
            comp.text = body

    result = await parser.chain_to_result(chain, event=event)

    assert result.content.count(body) == 2


@pytest.mark.asyncio
async def test_raw_fallback_separated_by_quote_is_preserved(
    parser, event_from_segments
):
    event = await event_from_segments(
        [
            {"type": "markdown", "data": {"content": BODY}},
            {"type": "reply", "data": {"id": "original"}},
            {"type": "text", "data": {"text": BODY}},
        ]
    )

    message, _, _ = await parser.parse_user_message(event, "bot")

    assert message.content.count(BODY) == 2


@pytest.mark.asyncio
async def test_adjacent_markdown_without_source_types_is_preserved(parser):
    result = await parser.chain_to_result(
        [Plain(MENTION + BODY), Plain(MENTION + BODY)]
    )

    assert result.content == f"{READABLE} {READABLE}"


@pytest.mark.asyncio
@pytest.mark.parametrize("markdown_first", [True, False])
@pytest.mark.parametrize("markdown_key", ["content", "markdown"])
@pytest.mark.parametrize("mention", [MENTION, ESCAPED_MENTION])
@pytest.mark.parametrize("fallback_kind", ["native", "markdown_text"])
async def test_official_bot_markdown_and_fallback_are_stored_once(
    parser, markdown_first, markdown_key, mention, fallback_kind
):
    markdown = {"type": "markdown", "data": {markdown_key: mention + BODY}}
    fallback = [
        {"type": "at", "data": {"qq": "1017789696"}},
        {"type": "text", "data": {"text": BODY}},
    ]
    if fallback_kind == "markdown_text":
        fallback = [{"type": "text", "data": {"text": mention + BODY}}]
    segments = [markdown, *fallback] if markdown_first else [*fallback, markdown]
    raw = Event.from_payload(
        {
            "post_type": "message",
            "message_type": "group",
            "self_id": 99999,
            "group_id": 20001,
            "user_id": 10001,
            "sender": {"user_id": 10001, "nickname": "Official bot"},
            "message_id": 12345,
            "message": segments,
        }
    )
    adapter = SimpleNamespace(
        bot=SimpleNamespace(call_action=AsyncMock(return_value={"card": "e.e."}))
    )
    message_obj = await AiocqhttpAdapter._convert_handle_message_event(adapter, raw)
    chain = message_obj.message
    original = [component.toDict() for component in chain]
    event = SimpleNamespace(
        message_obj=message_obj,
        get_self_id=lambda: "99999",
        get_messages=lambda: chain,
        get_group_id=lambda: "20001",
        get_sender_id=lambda: "10001",
        get_sender_name=lambda: "Official bot",
    )

    message, images, audio = await parser.parse_user_message(event, "bot")

    assert message.content == READABLE
    assert images == audio == []
    parser.data_cache.add_message.assert_awaited_once_with("bot", "20001", message)
    assert [component.toDict() for component in chain] == original


@pytest.mark.asyncio
@pytest.mark.parametrize("mention", [MENTION, ESCAPED_MENTION])
async def test_markdown_only_keeps_a_readable_mention(parser, mention):
    result = await parser.chain_to_result([Plain(mention + BODY)])
    assert result.content == READABLE


@pytest.mark.asyncio
@pytest.mark.parametrize("markdown_first", [True, False])
@pytest.mark.parametrize("mention", [MENTION, ESCAPED_MENTION])
async def test_duplicate_markdown_with_quote_matches_reported_outline(
    parser, event_from_segments, markdown_first, mention
):
    body = "收到，sensei 的指令已执行"
    quote = Reply(
        id="-419929186",
        sender_id="1017789696",
        sender_nickname="e.e.",
        message_str=" @王小桃(4018683728) 艾特我并引用这条消息",
        chain=[At(qq="4018683728", name="王小桃"), Plain("艾特我并引用这条消息")],
    )
    markdown = {"type": "markdown", "data": {"content": mention + body}}
    fallback = {"type": "text", "data": {"text": mention + body}}
    segments = [markdown, fallback] if markdown_first else [fallback, markdown]
    event = await event_from_segments(
        [{"type": "reply", "data": {"id": "-419929186"}}, *segments]
    )
    chain = event.get_messages()
    chain[0] = quote
    original = [component.toDict() for component in chain]
    assert AstrMessageEvent._outline_chain(None, chain) == (
        "[引用消息(e.e.:  @王小桃(4018683728) 艾特我并引用这条消息)] "
        + " ".join([mention + body] * 2)
    )

    result = await parser.chain_to_result(chain, event=event)

    assert result.content == (
        '<quote message_id="-419929186" sender_id="1017789696" sender_name="e.e.">'
        "@王小桃(4018683728) 艾特我并引用这条消息</quote> "
        f"@e.e.(1017789696) {body}"
    )
    assert [component.toDict() for component in chain] == original


@pytest.mark.asyncio
async def test_duplicate_markdown_and_native_fallback_are_stored_once(
    parser, event_from_segments
):
    event = await event_from_segments(
        [
            {"type": "markdown", "data": {"content": ESCAPED_MENTION + BODY}},
            {"type": "text", "data": {"text": ESCAPED_MENTION + BODY}},
            {"type": "at", "data": {"qq": "1017789696"}},
            {"type": "text", "data": {"text": BODY}},
        ]
    )
    event.get_messages()[2].name = "群名片"
    result = await parser.chain_to_result(event.get_messages(), event=event)
    assert result.content == f"@群名片(1017789696) {BODY}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "other",
    [
        MENTION.replace("1017789696", "10002") + BODY,
        MENTION + BODY + "。",
        MENTION + "另一句话",
    ],
)
async def test_distinct_markdown_components_are_preserved(parser, other):
    separate = await parser.chain_to_result([Plain(other)])
    result = await parser.chain_to_result([Plain(ESCAPED_MENTION + BODY), Plain(other)])
    assert result.content == f"{READABLE} {separate.content}"


@pytest.mark.asyncio
async def test_full_markdown_repetition_inside_one_component_is_preserved(parser):
    result = await parser.chain_to_result([Plain(f"{MENTION}{BODY} {MENTION}{BODY}")])
    assert result.content == f"{READABLE}  {READABLE}"


@pytest.mark.asyncio
async def test_markdown_copies_separated_by_quote_are_preserved(parser):
    result = await parser.chain_to_result(
        [
            Plain(MENTION + BODY),
            Reply(
                id="original",
                sender_id=None,
                sender_nickname=None,
                chain=[Plain("引用内容")],
            ),
            Plain(MENTION + BODY),
        ]
    )
    assert result.content == (
        f'{READABLE} <quote message_id="original">引用内容</quote> {READABLE}'
    )


@pytest.mark.asyncio
async def test_extra_plain_fallback_before_native_mention_is_stored_once(parser):
    body = "收到，测试指令已执行捏"
    mention = "[@影](mqqapi://markdown/mention?at_type=1&at_tinyid=1614170952)"
    quote = Reply(
        id="original",
        sender_nickname="影",
        message_str="引用这条消息再艾特我一下 @小祥²(3889698602) ",
        chain=[Plain("引用这条消息再艾特我一下"), At(qq="3889698602", name="小祥²")],
    )
    chain = [
        quote,
        Plain(mention + body),
        Plain(body),
        At(qq="1614170952", name="影"),
        Plain(body),
    ]
    outline = AstrMessageEvent._outline_chain(None, chain)
    assert outline == (
        "[引用消息(影: 引用这条消息再艾特我一下 @小祥²(3889698602) )] "
        f"{mention}{body} {body} [At:1614170952] {body}"
    )

    result = await parser.chain_to_result(chain)

    assert result.content.startswith('<quote message_id="original"')
    assert "引用这条消息再艾特我一下 @小祥²(3889698602)</quote>" in result.content
    assert result.content.endswith(f"</quote> @影(1614170952) {body}")
    assert result.content.count(body) == 1
    assert len(chain) == 5


@pytest.mark.asyncio
async def test_plain_fallback_requires_a_complete_native_copy(parser):
    result = await parser.chain_to_result(
        [
            Plain(MENTION + BODY),
            Plain(BODY),
            At(qq="10002", name="另一位"),
            Plain(BODY),
        ]
    )
    assert result.content == f"{READABLE} {BODY} @另一位(10002) {BODY}"


@pytest.mark.asyncio
async def test_repetition_inside_markdown_is_preserved(parser):
    result = await parser.chain_to_result(
        [
            Plain(f"{MENTION}{BODY} {BODY}"),
            At(qq="1017789696", name="e.e."),
            Plain(BODY),
        ]
    )
    assert result.content == f"{READABLE} {BODY} {READABLE}"


@pytest.mark.asyncio
async def test_fallback_mention_is_matched_by_id_not_display_name(parser):
    result = await parser.chain_to_result(
        [Plain(MENTION + BODY), At(qq="1017789696", name="群名片"), Plain(BODY)]
    )
    assert result.content == f"@群名片(1017789696) {BODY}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "body", "expected"),
    [
        ("10002", BODY, f"@e.e.(10002) {BODY}"),
        ("1017789696", "另一句话", "@e.e.(1017789696) 另一句话"),
        (None, BODY, BODY),
    ],
)
async def test_distinct_mentions_or_text_are_preserved(parser, target, body, expected):
    fallback = [Plain(body)]
    if target:
        fallback.insert(0, At(qq=target, name="e.e."))
    result = await parser.chain_to_result([Plain(MENTION + BODY), *fallback])
    assert result.content == f"{READABLE} {expected}"


@pytest.mark.asyncio
async def test_real_repeated_text_and_mentions_are_preserved(parser):
    result = await parser.chain_to_result(
        [
            At(qq="1017789696", name="e.e."),
            Plain(BODY),
            At(qq="1017789696", name="e.e."),
            Plain(BODY),
            Plain("哈哈 哈哈"),
        ]
    )
    assert result.content == f"{READABLE} {READABLE} 哈哈 哈哈"


@pytest.mark.asyncio
async def test_extra_markdown_text_and_internal_word_spaces_are_preserved(parser):
    for markdown_body, plain_body in [(BODY + "，还有后文", BODY), ("a b", "ab")]:
        result = await parser.chain_to_result(
            [
                Plain(MENTION + markdown_body),
                At(qq="1017789696", name="e.e."),
                Plain(plain_body),
            ]
        )
        assert result.content == (
            f"@e.e.(1017789696) {markdown_body} @e.e.(1017789696) {plain_body}"
        )


@pytest.mark.asyncio
async def test_multiple_mentions_with_prefix_and_segment_whitespace(parser):
    other = "[@另一位](mqqapi://markdown/mention?at_tinyid=10002&amp;at_type=1)"
    result = await parser.chain_to_result(
        [
            Plain(f"前文 {MENTION} 和 {other} {BODY}"),
            Plain("前文"),
            At(qq="1017789696", name="e.e."),
            Plain(" 和 "),
            At(qq="10002", name="另一位"),
            Plain(f" {BODY}"),
        ]
    )
    assert (
        result.content.split()
        == f"前文 @e.e.(1017789696) 和 @另一位(10002) {BODY}".split()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "[普通链接](https://example.com)",
        "[@未知](mqqapi://markdown/mention?at_type=1&at_tinyid=not-an-id)",
        "[@未知](mqqapi://markdown/mention?at_type=9&at_tinyid=123)",
        "[@不完整](mqqapi://markdown/mention?at_type=1)",
    ],
)
async def test_unrelated_and_unknown_links_are_preserved(parser, text):
    assert (await parser.chain_to_result([Plain(text)])).content == text


@pytest.mark.asyncio
@pytest.mark.parametrize("mention", [MENTION, ESCAPED_MENTION])
async def test_reply_and_forwarded_component_chains_use_the_same_normalization(
    parser, mention
):
    chain = [
        Plain(mention + BODY),
        At(qq="1017789696", name="e.e."),
        Plain(BODY),
    ]
    reply = await parser.chain_to_result([Reply(id="original", chain=chain)])
    assert reply.content.startswith('<quote message_id="original"')
    assert reply.content.endswith(f">{READABLE}</quote>")

    forward = await parser.chain_to_result(
        [Node(uin="10001", name="Official bot", content=chain)]
    )
    assert forward.forward_messages[0]["nodes"][0]["content"] == READABLE


@pytest.mark.asyncio
@pytest.mark.parametrize("group_id", ["group-openid", ""])
@pytest.mark.parametrize("mode", ["plain", "markdown", "mention"])
async def test_official_send_uses_one_content_representation(group_id, mode):
    api = SimpleNamespace(
        post_group_message=AsyncMock(return_value={"id": "sent"}),
        post_c2c_message=AsyncMock(return_value={"id": "sent"}),
    )
    event = SimpleNamespace(
        bot=SimpleNamespace(api=api),
        message_obj=SimpleNamespace(message_id="trigger"),
        get_platform_name=lambda: "qq_official",
        get_group_id=lambda: group_id,
        get_sender_id=lambda: "sender-openid",
    )
    chain = [Plain(BODY)]
    if mode == "mention":
        chain.insert(0, At(qq="recipient-openid"))

    success, message_id = await QQOfficialAction().send_message(
        event, chain, quote_message_id="reference", use_markdown=mode == "markdown"
    )

    assert (success, message_id) == (True, "sent")
    send = api.post_group_message if group_id else api.post_c2c_message
    send.assert_awaited_once()
    payload = send.await_args.kwargs
    assert payload["msg_id"] == "trigger"
    assert payload["message_reference"]["message_id"] == "reference"
    if mode == "plain":
        assert payload["msg_type"] == 0
        assert payload["content"] == BODY
        assert "markdown" not in payload
    else:
        expected = (
            f'<qqbot-at-user id="recipient-openid" />{BODY}'
            if mode == "mention"
            else BODY
        )
        assert payload["msg_type"] == 2
        assert payload["markdown"] == {"content": expected}
        assert "content" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("group_id", ["group-openid", ""])
async def test_rejected_markdown_retries_with_plain_content(group_id):
    send = AsyncMock(side_effect=[RuntimeError("Markdown rejected"), {"id": "sent"}])
    event = SimpleNamespace(
        bot=SimpleNamespace(
            api=SimpleNamespace(post_group_message=send, post_c2c_message=send)
        ),
        message_obj=SimpleNamespace(message_id="trigger"),
        get_platform_name=lambda: "qq_official",
        get_group_id=lambda: group_id,
        get_sender_id=lambda: "sender-openid",
    )

    result = await QQOfficialAction().send_message(
        event, [Plain(BODY)], quote_message_id="reference", use_markdown=True
    )

    assert result == (True, "sent")
    assert send.await_count == 2
    markdown, fallback = [call.kwargs for call in send.await_args_list]
    assert "content" not in markdown
    assert fallback["msg_type"] == 0
    assert fallback["content"] == BODY
    assert "markdown" not in fallback
    assert fallback["msg_seq"] == markdown["msg_seq"]
    assert fallback["message_reference"] == markdown["message_reference"]
