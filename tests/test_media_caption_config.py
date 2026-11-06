import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from core.conversation import media_captioner
from core.conversation.media_captioner import MediaCaptioner
from core.llm.call_llm import CallLLM
from core.llm.llm_tools import InspectForwardMessageTool, InspectMediaTool
from core.utils.schemas import MediaCaption, MessageData, XmlLlmResult
from core.web import sticker_api

from astrbot.api.event import AstrMessageEvent

MEDIA_REQUESTS = [
    ("call_llm_image_caption", "image", {"image_urls": ["base64://aGVsbG8="]}),
    ("call_llm_audio_caption", "audio", {"audio_urls": ["audio.wav"]}),
    (
        "call_llm_video_caption",
        "video",
        {"video_url": "data:video/mp4;base64,aGVsbG8="},
    ),
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
        caption_config={
            "media_caption_provider_ids": ["primary", "", "fallback"],
            "inspect_media_provider_ids": ["inspect-primary", "", "inspect-fallback"],
        },
    )


@pytest.fixture
def inspect_runtime(caption_caller, request, tmp_path, monkeypatch):
    media_type = getattr(request, "param", "image")
    media = MediaCaption(
        hash_val="1234567890abcdef", media_type=media_type, is_captioned=False
    )
    cache_dir = tmp_path / "media_cache"
    cache_dir.mkdir()
    filename = f"{media.hash_val}.mp4" if media_type == "video" else media.hash_val
    media_file = cache_dir / filename
    media_file.write_bytes(b"media payload")
    media.url = str(media_file)
    monkeypatch.setattr(media_captioner.StarTools, "get_data_dir", lambda _: tmp_path)
    plugin = SimpleNamespace(
        conf={"caption_config": caption_caller.caption_config},
        adapter_id_map={"adapter": "bot"},
        data_cache=SimpleNamespace(
            get_caption_by_hash=AsyncMock(return_value=media),
            update_caption=AsyncMock(),
        ),
        http_manager=SimpleNamespace(
            download_media=AsyncMock(),
            handle_image=Mock(return_value=(["base64://aGVsbG8="], False)),
        ),
        call_llm=caption_caller,
    )
    event = Mock(spec=AstrMessageEvent)
    event.platform_meta = SimpleNamespace(id="adapter")
    event.get_group_id.return_value = "group"
    event.get_sender_id.return_value = "user"
    return SimpleNamespace(
        plugin=plugin,
        media=media,
        tool=InspectMediaTool(plugin=plugin),
        event=event,
        generate=caption_caller.context.llm_generate,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("inspect_runtime", ["image", "audio", "video"], indirect=True)
@pytest.mark.parametrize("refresh", [False, True])
async def test_inspect_uses_dedicated_provider_order_and_updates_cache(
    inspect_runtime, refresh
):
    runtime = inspect_runtime
    runtime.media.is_captioned = refresh
    runtime.media.caption = "Ambiguous caption" if refresh else ""
    question = "What does the label say?" if refresh else ""
    runtime.generate.side_effect = [RuntimeError("Unavailable")] * 2 + [
        runtime.generate.return_value
    ]

    output = await runtime.tool.call(
        runtime.event, media_id=runtime.media.hash_val, question=question
    )

    assert "Shared caption" in output
    assert [
        call.kwargs["chat_provider_id"] for call in runtime.generate.await_args_list
    ] == ["inspect-primary", "inspect-primary", "inspect-fallback"]
    if question:
        assert question in runtime.generate.call_args.kwargs["prompt"]
    assert runtime.media.is_captioned and runtime.media.caption == "Shared caption"
    runtime.plugin.data_cache.update_caption.assert_awaited_once_with(runtime.media)
    assert runtime.plugin.call_llm.media_caption_provider_ids == ["primary", "fallback"]


@pytest.mark.asyncio
@pytest.mark.parametrize("inspect_runtime", ["image", "audio", "video"], indirect=True)
@pytest.mark.parametrize(
    "caption_config,expected_provider",
    [
        ({"media_caption_provider_ids": ["regular"]}, "regular"),
        (
            {
                "media_caption_provider_ids": ["regular"],
                "inspect_media_provider_ids": [],
            },
            "regular",
        ),
        (
            {
                "media_caption_provider_ids": ["regular"],
                "inspect_media_provider_ids": [""],
            },
            "regular",
        ),
        ({"inspect_media_provider_ids": ["", "strong"]}, "strong"),
        ({}, None),
    ],
)
async def test_inspect_provider_defaults_and_tool_only_configuration(
    inspect_runtime, caption_config, expected_provider
):
    runtime = inspect_runtime
    runtime.plugin.conf["caption_config"] = caption_config
    runtime.plugin.call_llm = CallLLM(
        runtime.plugin.call_llm.context, None, {}, caption_config
    )

    output = await runtime.tool.call(runtime.event, media_id=runtime.media.hash_val)

    if expected_provider is None:
        assert "未配置" in output
        runtime.generate.assert_not_awaited()
        runtime.plugin.data_cache.update_caption.assert_not_awaited()
        runtime.plugin.http_manager.handle_image.assert_not_called()
        runtime.plugin.http_manager.download_media.assert_not_awaited()
    else:
        assert "Shared caption" in output
        runtime.generate.assert_awaited_once()
        assert (
            runtime.generate.call_args.kwargs["chat_provider_id"] == expected_provider
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("inspect_runtime", ["image", "audio", "video"], indirect=True)
async def test_inspect_failure_does_not_retry_regular_providers(inspect_runtime):
    runtime = inspect_runtime
    runtime.generate.side_effect = RuntimeError("Unavailable")

    output = await runtime.tool.call(runtime.event, media_id=runtime.media.hash_val)

    assert "Shared caption" not in output
    assert [
        call.kwargs["chat_provider_id"] for call in runtime.generate.await_args_list
    ] == ["inspect-primary", "inspect-primary", "inspect-fallback", "inspect-fallback"]
    runtime.plugin.data_cache.update_caption.assert_not_awaited()
    assert not runtime.media.is_captioned


@pytest.mark.asyncio
@pytest.mark.parametrize("inspect_runtime", ["image", "audio", "video"], indirect=True)
async def test_inspect_reuses_cache_with_dedicated_providers(inspect_runtime):
    runtime = inspect_runtime
    runtime.media.is_captioned = True
    runtime.media.caption = "Cached caption"

    output = await runtime.tool.call(runtime.event, media_id=runtime.media.hash_val)

    assert "Cached caption" in output
    runtime.generate.assert_not_awaited()
    runtime.plugin.data_cache.update_caption.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("inspect_runtime", ["image", "audio"], indirect=True)
async def test_forward_media_use_inspection_providers(inspect_runtime):
    runtime = inspect_runtime

    captions = await InspectForwardMessageTool()._load_media_captions(
        runtime.plugin, "bot", [runtime.media.hash_val]
    )

    assert captions == [runtime.media]
    assert runtime.generate.call_args.kwargs["chat_provider_id"] == "inspect-primary"


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
@pytest.mark.parametrize("method,media_type,kwargs", MEDIA_REQUESTS)
@pytest.mark.parametrize("global_enabled", [False, True])
async def test_media_requests_ignore_legacy_bot_caption_settings(
    caption_caller, method, media_type, kwargs, global_enabled
):
    caption_caller.caption_config[f"{media_type}_caption_enabled"] = global_enabled
    caption_caller.plugin = SimpleNamespace(
        bot_map={
            "bot": {
                "caption_config": {f"{media_type}_caption_enabled": not global_enabled}
            }
        },
        db=SimpleNamespace(log_token_usage=AsyncMock()),
    )

    await getattr(caption_caller, method)(bot_name="bot", **kwargs)

    assert caption_caller.context.llm_generate.await_count == int(global_enabled)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "media_type,media_label", [("image", "图片"), ("audio", "语音"), ("video", "视频")]
)
@pytest.mark.parametrize("global_enabled", [False, True])
async def test_cached_captions_use_global_settings_for_all_bots(
    media_type, media_label, global_enabled
):
    media = MediaCaption(
        hash_val="1234567890abcdef",
        media_type=media_type,
        caption="Cached caption",
        is_captioned=True,
    )
    plugin = SimpleNamespace(
        conf={"caption_config": {f"{media_type}_caption_enabled": global_enabled}},
        bot_map={
            "first": {"caption_config": {f"{media_type}_caption_enabled": False}},
            "second": {"caption_config": {f"{media_type}_caption_enabled": True}},
        },
        data_cache=SimpleNamespace(get_caption_by_hash=AsyncMock(return_value=media)),
        emoji_manager=SimpleNamespace(has_sticker=AsyncMock(return_value=False)),
    )
    messages = [MessageData(content=f"[{media_label}:{media.hash_val}]")]
    captioner = MediaCaptioner(plugin)

    for bot_name in ("first", "second"):
        captions = await captioner.get_cached_media_captions(bot_name, messages)
        assert captions == ([media] if global_enabled and media_type != "video" else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("media_type", ["image", "audio", "video"])
@pytest.mark.parametrize(
    "has_cache,question,can_reuse",
    [(False, "", False), (True, "", True), (True, "Focus", False)],
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
        bot_map={"bot": {"caption_config": {f"{media_type}_caption_enabled": True}}},
        data_cache=SimpleNamespace(get_caption_by_hash=AsyncMock(return_value=media)),
        http_manager=SimpleNamespace(download_media=AsyncMock()),
        call_llm=SimpleNamespace(
            media_caption_provider_ids=["shared"],
            inspect_media_provider_ids=["strong"],
            call_llm_image_caption=AsyncMock(),
            call_llm_audio_caption=AsyncMock(),
            call_llm_video_caption=AsyncMock(),
        ),
    )

    record, text = await MediaCaptioner(plugin).inspect_media(
        media.hash_val, question, bot_name="bot"
    )

    assert record is media
    assert ("Cached caption" if can_reuse else "转述已关闭") in text
    plugin.http_manager.download_media.assert_not_awaited()
    for kind in ("image", "audio", "video"):
        getattr(plugin.call_llm, f"call_llm_{kind}_caption").assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "enabled,providers,available",
    [(False, ["shared"], False), (True, [], False), (True, ["shared"], True)],
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
