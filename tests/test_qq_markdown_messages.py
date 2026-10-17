from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiocqhttp import Event
from core.utils.message_parse import MessageParser

from astrbot.api.message_components import At, Node, Plain, Reply
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
    AiocqhttpAdapter,
)

MENTION = r"[@e\.e\.](mqqapi://markdown/mention?at_type=1&at_tinyid=1017789696)"
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


@pytest.mark.asyncio
@pytest.mark.parametrize("markdown_first", [True, False])
@pytest.mark.parametrize("markdown_key", ["content", "markdown"])
async def test_official_bot_markdown_and_fallback_are_stored_once(
    parser, markdown_first, markdown_key
):
    markdown = {"type": "markdown", "data": {markdown_key: MENTION + BODY}}
    fallback = [
        {"type": "at", "data": {"qq": "1017789696"}},
        {"type": "text", "data": {"text": BODY}},
    ]
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
async def test_markdown_only_keeps_a_readable_mention(parser):
    result = await parser.chain_to_result([Plain(MENTION + BODY)])
    assert result.content == READABLE


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
async def test_reply_and_forwarded_component_chains_use_the_same_normalization(parser):
    chain = [Plain(MENTION + BODY), At(qq="1017789696", name="e.e."), Plain(BODY)]
    reply = await parser.chain_to_result([Reply(id="original", chain=chain)])
    assert reply.content.startswith('<quote message_id="original"')
    assert reply.content.endswith(f">{READABLE}</quote>")

    forward = await parser.chain_to_result(
        [Node(uin="10001", name="Official bot", content=chain)]
    )
    assert forward.forward_messages[0]["nodes"][0]["content"] == READABLE
