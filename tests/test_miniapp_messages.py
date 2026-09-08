import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
from core.database.data_cache import DataCache
from core.database.database import Database
from core.database.schema import initialize_database
from core.utils.message_card import json_card_to_components
from core.utils.message_parse import MessageParser
from core.utils.message_parse_types import ChainParseResult
from xxhash import xxh3_64_hexdigest

from astrbot.api.message_components import Image, Json, Plain, Reply
from astrbot.api.star import StarTools

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j5xkAAAAASUVORK5CYII="
)
IMAGE_ID = xxh3_64_hexdigest(PNG_BYTES)
PREVIEW = "https://pubminishare.example.com/cover.png"
LINK = "https://example.com/article?id=42&from=qq"


@pytest.fixture
def miniapp():
    return {
        "app": "com.tencent.miniapp_01",
        "desc": "示例小程序",
        "prompt": "[QQ小程序]示例小程序",
        "view": "view_example",
        "meta": {
            "detail_1": {
                "appid": "123456",
                "title": "一篇图文分享",
                "desc": "第一段说明\n第二段说明",
                "preview": "pubminishare.example.com/cover.png",
                "icon": "https://example.com/app-icon.png",
                "qqdocurl": "https://example.com/article?id=42&amp;from=qq",
                "url": "m.q.qq.com/a/s/example",
            }
        },
    }


@pytest.fixture
def parser(tmp_path, monkeypatch):
    monkeypatch.setattr(StarTools, "get_data_dir", lambda *args: tmp_path)
    db = MagicMock()
    db.get_media_caption_by_hash = AsyncMock(return_value=None)
    db.get_media_caption_by_filename = AsyncMock(return_value=None)
    db.insert_media_caption = AsyncMock()
    db.insert_message = AsyncMock()
    http = MagicMock()
    http.download_media = AsyncMock(return_value=PNG_BYTES)
    cache = DataCache(db=db, http_manager=http, ltm=MagicMock())
    return MessageParser(
        data_cache=cache,
        http_manager=http,
        image_caption_enabled=False,
        audio_caption_enabled=False,
        call_llm=SimpleNamespace(plugin=None),
    )


@pytest.mark.asyncio
async def test_miniapp_persists_readable_content_and_image_hash(
    parser, miniapp, tmp_path
):
    chain = [Plain("前文"), Json(miniapp), Plain("后文")]
    event = MagicMock()
    event.message_obj = SimpleNamespace(
        timestamp=1700000000, message_id="miniapp-1", raw_message=None
    )
    event.get_messages.return_value = chain
    event.get_sender_id.return_value = "10001"
    event.get_sender_name.return_value = "测试用户"
    event.get_group_id.return_value = "20001"

    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await initialize_database(conn)
        parser.data_cache.db = Database(conn)
        message, images, audio = await parser.parse_user_message(event, "bot")

        assert message.content == (
            "前文 [小程序]\n示例小程序\n一篇图文分享\n第一段说明\n第二段说明 "
            f"[图片:{IMAGE_ID}] \n链接: {LINK} 后文"
        )
        assert message.media_id_list == [IMAGE_ID]
        assert message.forward_messages == []
        assert images == [PREVIEW]
        assert audio == []
        saved = await parser.data_cache.db.get_messages("20001", "bot")
        assert saved[0].content == message.content
        assert saved[0].media_id_list == [IMAGE_ID]
        caption = await parser.data_cache.db.get_media_caption_by_hash(IMAGE_ID)
        assert caption.media_type == "image"
        assert caption.url == PREVIEW

        # The same picture sent normally must resolve to the same cached ID.
        normal = await parser.chain_to_result([Image.fromURL(PREVIEW)])
        assert normal.content == f"[图片:{IMAGE_ID}]"
        parser.http_manager.download_media.assert_awaited_with(PREVIEW)
        async with conn.execute("SELECT COUNT(*) FROM media_caption") as cursor:
            assert (await cursor.fetchone())[0] == 1
        assert (tmp_path / "media_cache" / IMAGE_ID).read_bytes() == PNG_BYTES
        assert len(chain) == 3 and isinstance(chain[1], Json)


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("segment_type", ["json", "miniapp"])
async def test_miniapp_inside_forward(parser, miniapp, wrapped, segment_type):
    payload = json.dumps(miniapp, ensure_ascii=False).replace(",", "&#44;")
    if wrapped:
        payload = {"data": payload}
    forward = Json(
        {
            "app": "com.tencent.multimsg",
            "meta": {
                "detail": {
                    "nodes": [
                        {
                            "sender": {"user_id": "10001", "nickname": "转发者"},
                            "message": [
                                {"type": "text", "data": {"text": "分享"}},
                                {"type": segment_type, "data": {"data": payload}},
                            ],
                        }
                    ]
                }
            },
        }
    )
    result = await parser.chain_to_result([forward])
    assert result.content.startswith("[合并转发:fwd_")
    assert len(result.forward_messages) == 1
    node = result.forward_messages[0]["nodes"][0]
    assert node["content"].startswith("分享 [小程序]")
    assert f"[图片:{IMAGE_ID}]" in node["content"]
    assert LINK in node["content"]
    assert node["sender_name"] == "转发者"
    assert (await parser.data_cache.get_caption_by_hash(IMAGE_ID)).url == PREVIEW


@pytest.mark.asyncio
async def test_miniapp_inside_reply(parser, miniapp):
    result = await parser.chain_to_result(
        [Reply(id="original", chain=[Json(miniapp)]), Plain("回复")],
        defer_caption=True,
    )
    assert result.content.startswith('<quote message_id="original"')
    assert ">[小程序]" in result.content
    assert result.content.endswith("</quote> 回复")
    assert result.media_id_list == [IMAGE_ID]
    assert result.image_urls == [PREVIEW]


@pytest.mark.asyncio
@pytest.mark.parametrize("defer_caption", [False, True])
async def test_card_images_share_the_normal_caption_budget(
    parser, miniapp, defer_caption
):
    parser._format_image_ref = AsyncMock(
        return_value=(f"[图片:{IMAGE_ID}]", ChainParseResult(media_id_list=[IMAGE_ID]))
    )
    chain = [Json(miniapp), Image.fromURL(PREVIEW), Json(miniapp)]
    await parser.chain_to_result(chain, defer_caption=defer_caption)
    assert [call.args[2] for call in parser._format_image_ref.await_args_list] == [
        defer_caption,
        True,
        True,
    ]
    assert len(chain) == 3 and isinstance(chain[0], Json)


@pytest.mark.asyncio
async def test_unavailable_preview_keeps_card_text(parser, miniapp):
    parser.http_manager.download_media.return_value = b""
    result = await parser.chain_to_result([Json(miniapp)])
    assert "一篇图文分享" in result.content
    assert "[图片]" in result.content
    assert LINK in result.content
    assert "合并转发" not in result.content
    assert result.media_id_list == []


@pytest.mark.parametrize("detail_key", ["detail_1", "detail", "detail_2"])
def test_miniapp_fields_and_scheme_less_urls(miniapp, detail_key):
    detail = miniapp["meta"].pop("detail_1")
    miniapp["meta"][detail_key] = detail
    detail["title"] = miniapp["desc"]
    detail["preview"] = "//pubminishare.example.com/cover.png"
    detail.pop("qqdocurl")
    components = json_card_to_components({"data": json.dumps(miniapp)})
    assert components[0].text.count("示例小程序") == 1
    assert components[1].file == PREVIEW
    assert components[2].text == "\n链接: https://m.q.qq.com/a/s/example"


@pytest.mark.parametrize(
    "preview", [None, {}, "/tmp/cover.png", "file:///tmp/cover.png", "https://[invalid"]
)
def test_invalid_preview_is_not_treated_as_a_local_image(miniapp, preview):
    miniapp["meta"]["detail_1"]["preview"] = preview
    components = json_card_to_components(miniapp)
    assert all(isinstance(component, Plain) for component in components)
    assert "一篇图文分享" in components[0].text
    assert LINK in components[-1].text


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("not JSON", "[JSON卡片]"),
        ([], "[JSON卡片]"),
        ({"data": None}, "[JSON卡片]"),
        ({"app": "other", "prompt": "卡片摘要"}, "[JSON卡片]\n卡片摘要"),
        ({"app": "com.tencent.miniapp_01", "meta": None}, "[小程序]"),
        ({"app": "com.tencent.miniapp_01", "prompt": "卡片摘要"}, "[小程序]\n卡片摘要"),
        ({"app": "com.tencent.multimsg"}, "[合并转发消息]"),
    ],
)
def test_card_fallbacks(payload, expected):
    assert json_card_to_components(payload)[0].text == expected
