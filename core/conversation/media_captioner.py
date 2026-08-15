import os
import base64
from pathlib import Path
import asyncio
import copy
import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image, Reply
from astrbot.core.star.star_tools import StarTools

from ..utils.schemas import MediaCaption, XmlLlmResult, extract_media_ids
from ..utils.video_utils import (
    VIDEO_AUTO_COMPRESS_THRESHOLD_BYTES,
    VIDEO_MAX_PAYLOAD_BYTES,
    check_ffmpeg_available,
    clip_video_ffmpeg,
    compress_video_ffmpeg,
    format_duration,
    format_file_size,
)


class MediaCaptioner:
    def __init__(self, plugin):
        self.plugin = plugin

    @staticmethod
    def _caption_enabled(media_caption: MediaCaption, caption_config: dict) -> bool:
        media_type = str(getattr(media_caption, "media_type", "") or "").lower()
        if media_type == "audio":
            return bool(caption_config.get("audio_caption_enabled", True))
        elif media_type == "video":
            # 设计用意：视频不触发全局被动监听/回复前自动转述，且不自动注入到回复提示词的媒体注脚区。
            # 避免长视频或多切片内容占用大量对话上下文窗口（防止 Token 消耗过大与上下文膨胀）。
            # 视频内容仅在 Bot 明确需要时，通过 inspect_media 工具按需切片与定向提问查看。
            return False
        return bool(caption_config.get("image_caption_enabled", True))

    async def get_cached_media_captions(
        self, bot_name: str, recent_messages: list, caption_config: dict, group_or_user_id: str = ""
    ) -> list[MediaCaption]:
        """
        根据近期消息中的媒体ID，仅读取本地已有缓存的转述结果并添加表情包标记，不触发任何阻塞的延迟转述。
        """
        hash_vals = []
        seen_media = set()
        for msg in reversed(recent_messages):
            content_media_ids = extract_media_ids(getattr(msg, "content", "") or "")
            for media_id in reversed(content_media_ids):
                if media_id not in seen_media:
                    seen_media.add(media_id)
                    hash_vals.append(media_id)

        media_captions: list[MediaCaption] = []
        for hash_val in hash_vals:
            media_caption = await self.plugin.data_cache.get_caption_by_hash(hash_val)
            if media_caption:
                if not self._caption_enabled(media_caption, caption_config):
                    continue
                # 仅添加已经成功转述完成的记录
                if getattr(media_caption, "is_captioned", False):
                    if await self.plugin.emoji_manager.has_sticker(bot_name, hash_val):
                        media_caption = copy.copy(media_caption)
                        media_caption.caption = (media_caption.caption or "") + " (你已收藏此表情包)"
                    media_captions.append(media_caption)

        return media_captions

    # 兼容旧调用名
    transcribe_media_if_deferred = get_cached_media_captions

    async def analyze_and_add_stickers(
        self,
        event: AstrMessageEvent,
        bot_name: str,
        nickname: str,
        group_or_user_id: str,
        llm_result: XmlLlmResult,
    ):
        """
        分析并后台添加表情包
        """
        if not llm_result.add_stickers:
            return

        categories = await self.plugin.db.get_sticker_categories()
        for sticker_id in llm_result.add_stickers:
            async with self.plugin.sticker_locks[sticker_id]:
                # 先检查有没有添加过，如果全局有过，就直接关联而无需再次消耗Token分析
                if sticker_id in self.plugin.emoji_manager.stickers:
                    await self.plugin.emoji_manager.add_sticker(
                        bot_name=bot_name, media_id=sticker_id
                    )
                    continue

                caption = await self.plugin.data_cache.get_caption_by_hash(sticker_id)
                is_useful, sticker = False, None

                target_url = None
                for comp in event.get_messages():
                    if isinstance(comp, Reply) and comp.chain:
                        for quote in comp.chain:
                            if isinstance(quote, Image) and quote.url:
                                if quote.file and sticker_id in quote.file.lower():
                                    target_url = quote.url
                                    break
                                elif quote.file:
                                    (
                                        quote_hash,
                                        _,
                                    ) = await self.plugin.data_cache.get_caption_by_filename(
                                        quote.file
                                    )
                                    if quote_hash == sticker_id:
                                        target_url = quote.url
                                        break
                        if target_url:
                            break
                if not target_url and caption and caption.url:
                    target_url = caption.url

                if target_url:
                    # 先将图片下载并转为 base64，防止大模型无法访问本地/内网 URL
                    image_bytes = await self.plugin.http_manager.download_media(
                        target_url
                    )
                    if image_bytes:
                        base64s, _ = await asyncio.to_thread(
                            self.plugin.http_manager.handle_image, image_bytes
                        )
                        if base64s:
                            (
                                is_useful,
                                sticker,
                            ) = await self.plugin.call_llm.call_llm_sticker_analysis(
                                image_urls=base64s,
                                categories=categories,
                                media_id=sticker_id,
                                bot_name=bot_name,
                                group_or_user_id=group_or_user_id,
                            )
                        # 如果判定为有用，则下载保存到本地
                        if is_useful and sticker:
                            local_path = (
                                await self.plugin.emoji_manager.save_sticker_image(
                                    image_bytes, sticker_id
                                )
                            )
                            sticker.filename = local_path.name

                if is_useful and sticker:
                    await self.plugin.emoji_manager.add_sticker(
                        bot_name=bot_name, media_id=sticker_id, sticker=sticker
                    )

    async def inspect_media(
        self,
        hash_val: str,
        question: str = "",
        start_time: int = 0,
        bot_name: str = "",
        group_or_user_id: str = "",
    ) -> tuple[MediaCaption | None, str]:
        """
        统一查看/重新转述媒体（图片/语音/视频）。
        返回 (MediaCaption 对象, 格式化的文字结论)。
        """
        media_caption = await self.plugin.data_cache.get_caption_by_hash(hash_val)
        if not media_caption:
            logger.warning(f"[Giftia] inspect_media 失败：未找到对应的媒体缓存 hash={hash_val}")
            return None, f"未找到 media_id 为 [{hash_val}] 的媒体数据记录"

        media_type = str(getattr(media_caption, "media_type", "") or "image").lower()
        logger.info(
            f"[Giftia] inspect_media 处理: bot_name={bot_name}, hash={hash_val}, type={media_type}, question={question}, start_time={start_time}"
        )

        # 性能与开销优化：若没有特定关注问题 (question 为空) 且为默认起始时间 (start_time == 0)，
        # 且该媒体此前已经完成过转述，则直接复用已有的转述结果，避免重复触发视觉/音频大模型。
        if (
            not question
            and start_time == 0
            and getattr(media_caption, "is_captioned", False)
            and getattr(media_caption, "caption", "")
        ):
            logger.info(
                f"[Giftia] inspect_media 命中已有转述缓存，直接复用: hash={hash_val}, type={media_type}"
            )
            parts = []
            if media_type == "video":
                return media_caption, media_caption.caption
            elif media_type == "audio":
                if media_caption.caption:
                    parts.append(f"描述: {media_caption.caption}")
                if media_caption.text:
                    parts.append(f"语音文字: {media_caption.text}")
                if media_caption.genre:
                    parts.append(f"类型: {media_caption.genre}")
                return media_caption, "；".join(parts) if parts else media_caption.caption
            else:  # image
                if media_caption.caption:
                    parts.append(f"画面描述: {media_caption.caption}")
                if media_caption.text:
                    parts.append(f"文字: {media_caption.text}")
                if media_caption.genre:
                    parts.append(f"类型: {media_caption.genre}")
                if media_caption.character:
                    parts.append(f"人物: {media_caption.character}")
                if media_caption.source:
                    parts.append(f"来源: {media_caption.source}")
                return media_caption, "；".join(parts) if parts else media_caption.caption

        try:
            if media_type == "video":
                caption_text = await self.transcribe_video_media(
                    media_caption=media_caption,
                    start_time=start_time,
                    question=question,
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                )
                return media_caption, caption_text

            cache_file = (
                StarTools.get_data_dir("astrbot_plugin_giftia")
                / "media_cache"
                / hash_val
            )

            if media_type == "audio":
                audio_urls = (
                    [str(cache_file)]
                    if cache_file.exists()
                    else [media_caption.url]
                )
                if audio_urls and audio_urls[0]:
                    transcribed = await self.plugin.call_llm.call_llm_audio_caption(
                        audio_urls,
                        question=question,
                        bot_name=bot_name,
                        group_or_user_id=group_or_user_id,
                    )
                    if transcribed:
                        media_caption.genre = transcribed.genre
                        media_caption.character = transcribed.character
                        media_caption.source = transcribed.source
                        media_caption.text = transcribed.text
                        media_caption.caption = transcribed.caption
                        media_caption.is_captioned = True
                        await self.plugin.data_cache.update_caption(media_caption)

                        parts = []
                        if media_caption.caption:
                            parts.append(f"描述: {media_caption.caption}")
                        if media_caption.text:
                            parts.append(f"语音文字: {media_caption.text}")
                        if media_caption.genre:
                            parts.append(f"类型: {media_caption.genre}")
                        return media_caption, "；".join(parts) if parts else "语音转述完成但无有效文本"
                return media_caption, "语音文件缺失或转述失败"

            else:  # image media
                image_bytes = None
                if cache_file.exists():
                    try:
                        image_bytes = cache_file.read_bytes()
                    except Exception as e:
                        logger.error(f"[Giftia] 读取图片缓存失败: {e}")
                if not image_bytes and media_caption.url:
                    image_bytes = await self.plugin.http_manager.download_media(
                        media_caption.url
                    )
                if image_bytes:
                    base64s, _is_animated = await asyncio.to_thread(
                        self.plugin.http_manager.handle_image,
                        image_bytes,
                    )
                    if base64s:
                        transcribed = await self.plugin.call_llm.call_llm_image_caption(
                            base64s,
                            question=question,
                            bot_name=bot_name,
                            group_or_user_id=group_or_user_id,
                        )
                        if transcribed:
                            media_caption.genre = transcribed.genre
                            media_caption.character = transcribed.character
                            media_caption.source = transcribed.source
                            media_caption.text = transcribed.text
                            media_caption.caption = transcribed.caption
                            media_caption.is_captioned = True
                            await self.plugin.data_cache.update_caption(media_caption)

                            parts = []
                            if media_caption.caption:
                                parts.append(f"画面描述: {media_caption.caption}")
                            if media_caption.text:
                                parts.append(f"文字: {media_caption.text}")
                            if media_caption.genre:
                                parts.append(f"类型: {media_caption.genre}")
                            if media_caption.character:
                                parts.append(f"人物: {media_caption.character}")
                            if media_caption.source:
                                parts.append(f"来源: {media_caption.source}")
                            return media_caption, "；".join(parts) if parts else "图片转述完成但无有效信息"
                return media_caption, "图片文件获取失败或无法解析"

        except Exception as e:
            logger.error(f"[Giftia] inspect_media 处理失败: {e}", exc_info=True)
            return media_caption, f"媒体分析失败: {e}"

    async def retranscribe_media_with_question(
        self, bot_name: str, hash_val: str, question: str, group_or_user_id: str = ""
    ) -> MediaCaption | None:
        """
        兼容旧接口：强制针对给定的 media_id (hash_val) 和额外关注的问题重新转述。
        """
        media_caption, _ = await self.inspect_media(
            hash_val=hash_val,
            question=question,
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
        )
        return media_caption

    async def transcribe_video_media(
        self,
        media_caption: MediaCaption,
        start_time: int = 0,
        question: str = "",
        bot_name: str = "",
        group_or_user_id: str = "",
    ) -> str:
        """
        对视频进行缓存、切片并调用 LLM 进行转述理解。

        【设计意图与说明】：
        1. 视频转述结果不会自动注入到 Bot 回复提示词的媒体注脚区，避免长视频或多次切片的详尽转述占用宝贵的对话上下文窗口。
        2. 视频转述通过 inspect_media 工具按需实时调用，允许 Bot 在多轮交互中针对不同时间区间（start_time）或具体观察重点（question）深入查看。
        3. 若没有指定 question 且为默认起始时间 0，若已存在转述结论则直接秒级复用，避免重复触发视觉模型。
        """
        # 缓存命中复用：若无 question 且 start_time == 0，若已转述过则直接返回已有转述结论
        if not question and start_time == 0 and getattr(media_caption, "is_captioned", False) and getattr(media_caption, "caption", ""):
            return media_caption.caption

        caption_config = getattr(self.plugin, "conf", {}).get("caption_config", {})
        threshold = int(caption_config.get("video_clip_threshold_seconds", 30))

        url = media_caption.url or media_caption.file_name
        if not url:
            return "[视频文件路径或URL无效]"

        cache_dir = StarTools.get_data_dir("astrbot_plugin_giftia") / "media_cache"
        os.makedirs(cache_dir, exist_ok=True)
        local_video_path = cache_dir / f"{media_caption.hash_val}.mp4"

        if not local_video_path.exists():
            if url.startswith("file://"):
                local_video_path = Path(url.replace("file://", ""))
            elif url.startswith("http://") or url.startswith("https://"):
                logger.info(f"[Giftia] 下载视频文件进行分析: hash={media_caption.hash_val}")
                video_bytes = await self.plugin.http_manager.download_media(url)
                if not video_bytes:
                    return "[视频下载失败]"
                local_video_path.write_bytes(video_bytes)
            elif os.path.exists(url):
                local_video_path = Path(url)

        if not local_video_path.exists():
            return "[本地视频文件不存在]"

        duration = media_caption.duration or 0.0
        target_video_file = str(local_video_path)
        clip_info_str = ""

        if duration > threshold:
            clip_output = cache_dir / f"{media_caption.hash_val}_clip_{start_time}_{threshold}.mp4"
            if not clip_output.exists():
                success = await clip_video_ffmpeg(
                    str(local_video_path),
                    start_time=start_time,
                    duration=threshold,
                    output_path=str(clip_output)
                )
                if success:
                    target_video_file = str(clip_output)
                    clip_info_str = f" (切片区间: {start_time}s ~ {start_time + threshold}s)"
            else:
                target_video_file = str(clip_output)
                clip_info_str = f" (切片区间: {start_time}s ~ {start_time + threshold}s)"

        # 1. 主动智能压制：如果目标视频/切片体积大于 5MB，主动调用 ffmpeg 压制为 720p/CRF 28，确保数据轻量快速
        if os.path.exists(target_video_file):
            actual_size = os.path.getsize(target_video_file)
            if actual_size > VIDEO_AUTO_COMPRESS_THRESHOLD_BYTES:
                compressed_output = cache_dir / f"{Path(target_video_file).stem}_compressed.mp4"
                logger.info(
                    f"[Giftia] 视频文件体积 ({format_file_size(actual_size)}) 超过 5MB 主动压制阈值，触发 ffmpeg 智能压制 (720p/CRF 28)"
                )
                if not compressed_output.exists():
                    compressed_ok = await compress_video_ffmpeg(
                        input_path=target_video_file,
                        output_path=str(compressed_output),
                    )
                    if compressed_ok:
                        target_video_file = str(compressed_output)
                        actual_size = os.path.getsize(target_video_file)
                        logger.info(
                            f"[Giftia] 视频智能压制完成，压制后体积: {format_file_size(actual_size)}"
                        )
                else:
                    target_video_file = str(compressed_output)
                    actual_size = os.path.getsize(target_video_file)

        # 2. 强抓原生视频全量字节包转 Base64 (data:video/mp4;base64,...)
        try:
            video_raw_bytes = Path(target_video_file).read_bytes()
            video_b64_str = base64.b64encode(video_raw_bytes).decode("utf-8")
            # 3. 发送前严格校验：最终 Base64 编码数据量不得超过 20MB 网关上限
            if len(video_b64_str) > VIDEO_MAX_PAYLOAD_BYTES:
                return f"视频 [{media_caption.hash_val}] 数据量过大 ({format_file_size(len(video_b64_str))})，超出视觉模型单次接收上限 (20MB)，无法直接查看。请提示用户发送更短的视频片段。"
            video_data_url = f"data:video/mp4;base64,{video_b64_str}"
        except Exception as e:
            logger.error(f"[Giftia] 视频文件读取或编码 Base64 失败: {e}")
            return f"[视频编码失败: {e}]"

        try:
            transcribed = await self.plugin.call_llm.call_llm_video_caption(
                video_url=video_data_url,
                question=question,
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
            )
            if transcribed:
                caption_text = f"{transcribed.caption}{clip_info_str}"
                media_caption.genre = transcribed.genre
                media_caption.character = transcribed.character
                media_caption.source = transcribed.source
                media_caption.text = transcribed.text
                media_caption.caption = caption_text
                media_caption.is_captioned = True
                await self.plugin.data_cache.update_caption(media_caption)
                return caption_text
        except Exception as e:
            logger.error(f"[Giftia] 原生视频转述 LLM 调用失败: {e}", exc_info=True)
            return f"[视频转述失败: {e}]"

        return "[视频解析未生成有效结论]"
