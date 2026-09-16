import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from core.conversation import media_captioner
from core.llm.llm_tools import InspectMediaTool
from core.utils.schemas import MediaCaption


@pytest.fixture
def video_runtime(tmp_path, monkeypatch):
    cache_dir = tmp_path / "media_cache"
    cache_dir.mkdir()
    video = MediaCaption(
        hash_val="1234567890abcdef",
        media_type="video",
        url="https://example.com/video.mp4",
        duration=90,
    )
    (cache_dir / f"{video.hash_val}.mp4").write_bytes(b"full video")
    plugin = MagicMock()
    plugin.adapter_id_map = {"platform": "bot"}
    plugin.conf = {"caption_config": {"video_clip_threshold_seconds": 30}}
    plugin.data_cache.get_caption_by_hash = AsyncMock(return_value=video)
    plugin.data_cache.update_caption = AsyncMock()
    plugin.call_llm.call_llm_video_caption = AsyncMock(
        return_value=MediaCaption(hash_val=video.hash_val, caption="New caption")
    )
    event = MagicMock()
    event.platform_meta.id = "platform"
    event.get_group_id.return_value = "group"
    event.get_sender_id.return_value = "user"
    context = SimpleNamespace(context=SimpleNamespace(event=event))

    async def clip_video(input_path, start_time, duration, output_path):
        Path(output_path).write_bytes(f"clip {start_time}:{duration}".encode())
        return True

    clip = AsyncMock(side_effect=clip_video)
    monkeypatch.setattr(media_captioner.StarTools, "get_data_dir", lambda _: tmp_path)
    monkeypatch.setattr(media_captioner, "clip_video_ffmpeg", clip)
    return SimpleNamespace(
        plugin=plugin,
        video=video,
        tool=InspectMediaTool(plugin=plugin),
        context=context,
        clip=clip,
        cache_dir=cache_dir,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options,video_length,start_time,expected_duration",
    [
        ({}, 90, 5, 30),
        ({}, 20, 0, None),
        ({"duration": None}, 90, 0, 30),
        ({"duration": 10}, 90, 5, 10),
        ({"duration": 10}, 20, 0, 10),
        ({"duration": 10}, 0, 0, 10),
        ({"duration": 30}, 90, 0, 30),
        ({"duration": 100}, 90, 5, 30),
        ({"duration": "10"}, 90, 5, 10),
        ({"duration": 0}, 90, 0, 1),
        ({"duration": -5}, 90, 0, 1),
    ],
)
async def test_video_duration_controls_tool_payload(
    video_runtime, options, video_length, start_time, expected_duration
):
    runtime = video_runtime
    runtime.video.duration = video_length
    output = await runtime.tool.call(
        runtime.context,
        media_id=runtime.video.hash_val,
        start_time=start_time,
        **options,
    )

    if expected_duration is None:
        runtime.clip.assert_not_awaited()
        expected_bytes = b"full video"
        assert "切片区间" not in output
    else:
        runtime.clip.assert_awaited_once()
        assert runtime.clip.call_args.kwargs["duration"] == expected_duration
        assert runtime.clip.call_args.kwargs["start_time"] == start_time
        expected_bytes = f"clip {start_time}:{expected_duration}".encode()
        assert f"{start_time}s ~ {start_time + expected_duration}s" in output

    runtime.plugin.call_llm.call_llm_video_caption.assert_awaited_once()
    video_url = runtime.plugin.call_llm.call_llm_video_caption.call_args.kwargs[
        "video_url"
    ]
    assert base64.b64decode(video_url.split(",", 1)[1]) == expected_bytes
    runtime.plugin.data_cache.update_caption.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("duration,expected_duration", [(10, 10), (100, 30)])
async def test_explicit_duration_uses_matching_clip_instead_of_old_caption(
    video_runtime, duration, expected_duration
):
    runtime = video_runtime
    runtime.video.caption = "Old caption"
    runtime.video.is_captioned = True
    clip_path = runtime.cache_dir / (
        f"{runtime.video.hash_val}_clip_0_{expected_duration}.mp4"
    )
    clip_path.write_bytes(b"cached clip")

    output = await runtime.tool.call(
        runtime.context, media_id=runtime.video.hash_val, duration=duration
    )

    assert "New caption" in output
    assert f"0s ~ {expected_duration}s" in output
    runtime.clip.assert_not_awaited()
    runtime.plugin.call_llm.call_llm_video_caption.assert_awaited_once()
    video_url = runtime.plugin.call_llm.call_llm_video_caption.call_args.kwargs[
        "video_url"
    ]
    assert base64.b64decode(video_url.split(",", 1)[1]) == b"cached clip"


@pytest.mark.asyncio
async def test_clip_failure_does_not_send_full_video(video_runtime):
    runtime = video_runtime
    runtime.clip.side_effect = None
    runtime.clip.return_value = False

    output = await runtime.tool.call(
        runtime.context, media_id=runtime.video.hash_val, duration=10
    )

    assert "视频切片失败" in output
    runtime.plugin.call_llm.call_llm_video_caption.assert_not_awaited()
    runtime.plugin.data_cache.update_caption.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("parameter", ["duration", "start_time"])
@pytest.mark.parametrize("value", ["", "abc", "1.5", [], {}, float("inf")])
async def test_invalid_video_parameters_return_clear_error(
    video_runtime, parameter, value
):
    runtime = video_runtime

    output = await runtime.tool.call(
        runtime.context, media_id=runtime.video.hash_val, **{parameter: value}
    )

    assert output == f"请求参数错误：{parameter} 必须是整数"
    runtime.plugin.data_cache.get_caption_by_hash.assert_not_awaited()
    runtime.clip.assert_not_awaited()
    runtime.plugin.call_llm.call_llm_video_caption.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cached_clip", [False, True])
@pytest.mark.parametrize(
    "video_length,start_time,expected_interval",
    [
        (20, 0, "切片区间: 0s ~ 20s"),
        (20, 15, "切片区间: 15s ~ 20s"),
        (20.5, 15, "切片区间: 15s ~ 20.5s"),
        (20, -5, "切片区间: 0s ~ 20s"),
        (0, 0, "请求切片区间: 0s ~ 30s"),
        (0, 5, "请求切片区间: 5s ~ 35s"),
    ],
)
async def test_video_interval_respects_known_duration(
    video_runtime, cached_clip, video_length, start_time, expected_interval
):
    runtime = video_runtime
    runtime.video.duration = video_length
    if cached_clip:
        clip_path = runtime.cache_dir / (
            f"{runtime.video.hash_val}_clip_{max(0, start_time)}_30.mp4"
        )
        clip_path.write_bytes(b"cached clip")

    output = await runtime.tool.call(
        runtime.context,
        media_id=runtime.video.hash_val,
        start_time=start_time,
        duration=30,
    )

    assert f" ({expected_interval})" in output
    assert runtime.video.caption == f"New caption ({expected_interval})"
    runtime.plugin.call_llm.call_llm_video_caption.assert_awaited_once()
    runtime.plugin.data_cache.update_caption.assert_awaited_once()
    if cached_clip:
        runtime.clip.assert_not_awaited()
    else:
        runtime.clip.assert_awaited_once()
        assert runtime.clip.call_args.kwargs["start_time"] == max(0, start_time)


@pytest.mark.asyncio
@pytest.mark.parametrize("start_time", [20, 25])
async def test_video_start_at_or_past_end_returns_error(video_runtime, start_time):
    runtime = video_runtime
    runtime.video.duration = 20

    output = await runtime.tool.call(
        runtime.context,
        media_id=runtime.video.hash_val,
        start_time=start_time,
        duration=10,
    )

    assert "视频切片起始时间超出视频时长" in output
    runtime.clip.assert_not_awaited()
    runtime.plugin.call_llm.call_llm_video_caption.assert_not_awaited()
    runtime.plugin.data_cache.update_caption.assert_not_awaited()
