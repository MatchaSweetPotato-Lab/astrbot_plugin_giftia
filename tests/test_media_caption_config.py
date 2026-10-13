import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from core.conversation.media_captioner import MediaCaptioner
from core.llm.call_llm import CallLLM
from core.utils.schemas import MediaCaption, XmlLlmResult
from core.web import sticker_api

MEDIA_REQUESTS = [
    ("call_llm_image_caption", "image", {"image_urls": ["base64://aGVsbG8="]}),
    ("call_llm_audio_caption", "audio", {"audio_urls": ["audio.wav"]}),
    ("call_llm_video_caption", "video", {"video_url": "data:video/mp4;base64,aGVsbG8="}),
    (
        "call_llm_sticker_analysis",
        "image",
        {"image_urls": ["base64://aGVsbG8="], "categories": [], "media_id": "sticker"},
    ),
]


@pytest.fixture
def caption_caller(request):
    retry_limit = getattr(request, "param", 2)
    context = SimpleNamespace(
        llm_generate=AsyncMock(
            return_value=SimpleNamespace(
                completion_text=json.dumps(
                    {"caption": "Shared caption", "isUseful": True, "name": "Sticker"}
                )
            )
        )
    )
    network_config = (
        {} if retry_limit is None else {"media_caption_retry_times": retry_limit}
    )
    return CallLLM(
        context=context,
        xml_parse=SimpleNamespace(parse_str_json=json.loads),
        network_config=network_config,
        caption_config={"media_caption_provider_ids": ["primary", "", "fallback"]},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("method,media_type,kwargs", MEDIA_REQUESTS)
@pytest.mark.parametrize("caption_caller", [None, 1, 3], indirect=True)
async def test_media_requests_share_retries_and_provider_order(
    caption_caller, method, media_type, kwargs
):
    caller = caption_caller
    generate = caller.context.llm_generate
    expected_attempts = caller.network_conf.get("media_caption_retry_times", 2)
    generate.side_effect = [
        RuntimeError("Provider rejected the payload")
    ] * expected_attempts + [generate.return_value]

    result = await getattr(caller, method)(**kwargs)

    assert [call.kwargs["chat_provider_id"] for call in generate.await_args_list] == [
        "primary"
    ] * expected_attempts + ["fallback"]
    if method == "call_llm_sticker_analysis":
        assert result[0] is True and result[1].sticker_id == "sticker"
    else:
        assert result.caption == "Shared caption"
        assert result.media_type == media_type
    if media_type == "audio":
        assert generate.call_args.kwargs["audio_urls"] == kwargs["audio_urls"]
    else:
        assert generate.call_args.kwargs["image_urls"] == kwargs.get(
            "image_urls", [kwargs.get("video_url")]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("method,media_type,kwargs", MEDIA_REQUESTS)
async def test_disabled_media_never_reaches_a_provider(
    caption_caller, method, media_type, kwargs
):
    caption_caller.caption_config[f"{media_type}_caption_enabled"] = False

    result = await getattr(caption_caller, method)(**kwargs)

    assert result == ((False, None) if method == "call_llm_sticker_analysis" else None)
    caption_caller.context.llm_generate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("method,media_type,kwargs", MEDIA_REQUESTS)
async def test_unconfigured_media_never_reaches_a_provider(
    caption_caller, method, media_type, kwargs
):
    caller = CallLLM(caption_caller.context, caption_caller.xml_parse, {}, {})

    result = await getattr(caller, method)(**kwargs)

    assert result == ((False, None) if method == "call_llm_sticker_analysis" else None)
    caller.context.llm_generate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("media_type", ["image", "audio", "video"])
@pytest.mark.parametrize(
    "has_cache,question,can_reuse", [(False, "", False), (True, "", True), (True, "Focus", False)]
)
async def test_inspect_respects_switches_and_reuses_existing_captions(
    media_type, has_cache, question, can_reuse
):
    media = MediaCaption(
        hash_val="1234567890abcdef",
        media_type=media_type,
        caption="Cached caption" if has_cache else "",
        is_captioned=has_cache,
    )
    plugin = SimpleNamespace(
        conf={"caption_config": {f"{media_type}_caption_enabled": False}},
        data_cache=SimpleNamespace(get_caption_by_hash=AsyncMock(return_value=media)),
        http_manager=SimpleNamespace(download_media=AsyncMock()),
        call_llm=SimpleNamespace(
            media_caption_provider_ids=["shared"],
            call_llm_image_caption=AsyncMock(),
            call_llm_audio_caption=AsyncMock(),
            call_llm_video_caption=AsyncMock(),
        ),
    )

    record, text = await MediaCaptioner(plugin).inspect_media(media.hash_val, question)

    assert record is media
    assert ("Cached caption" if can_reuse else "转述已关闭") in text
    plugin.http_manager.download_media.assert_not_awaited()
    for kind in ("image", "audio", "video"):
        getattr(plugin.call_llm, f"call_llm_{kind}_caption").assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "enabled,providers,available", [(False, ["shared"], False), (True, [], False), (True, ["shared"], True)]
)
async def test_sticker_ai_availability_matches_image_settings(
    monkeypatch, enabled, providers, available
):
    plugin = SimpleNamespace(
        conf={"caption_config": {"image_caption_enabled": enabled}},
        call_llm=SimpleNamespace(media_caption_provider_ids=providers),
        db=SimpleNamespace(
            get_sticker_categories=AsyncMock(return_value=[]),
            get_all_sticker_tags=AsyncMock(return_value=[]),
        ),
        http_manager=SimpleNamespace(handle_image=Mock()),
    )
    api = sticker_api.StickerApi()
    api.giftia = plugin
    api._get_bot_names = lambda: []
    monkeypatch.setattr(sticker_api, "json_response", lambda data: data)

    response = await api.get_sticker_filter_options()

    assert response["data"]["ai_available"] is available
    if not available:
        useful, sticker, error = await api._analyze_sticker_bytes(b"image", "sticker")
        assert useful is False and sticker is None and error
        plugin.http_manager.handle_image.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_image_skips_background_sticker_analysis():
    plugin = SimpleNamespace(
        conf={"caption_config": {"image_caption_enabled": False}},
        db=SimpleNamespace(get_sticker_categories=AsyncMock()),
    )

    await MediaCaptioner(plugin).analyze_and_add_stickers(
        None, "bot", "Bot", "group", XmlLlmResult(add_stickers=["sticker"])
    )

    plugin.db.get_sticker_categories.assert_not_awaited()
