import os
import asyncio
import base64 as b64_module
import contextlib
import re
import urllib.parse
from collections import defaultdict
from pathlib import Path

from xxhash import xxh3_64_hexdigest

from astrbot.api import logger

from ..database.data_cache import DataCache, is_temp_or_local_path
from ..llm.call_llm import CallLLM
from .http_manager import HttpManager
from .message_parse_types import ChainParseResult
from .schemas import MediaCaption
from .video_utils import get_remote_video_info, format_file_size, format_duration

# 支持的图片文件格式
SUPPORTED_FILE_FORMATS_WITH_DOT = (
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".mpo",
)


class LockManager:
    def __init__(self):
        self._locks = {}
        self._global_lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def lock(self, key: str):
        async with self._global_lock:
            if key not in self._locks:
                self._locks[key] = [asyncio.Lock(), 0]
            lock_info = self._locks[key]
            lock_info[1] += 1
        
        try:
            async with lock_info[0]:
                yield
        finally:
            async with self._global_lock:
                lock_info[1] -= 1
                if lock_info[1] <= 0:
                    self._locks.pop(key, None)


class MessageMediaFormatter:
    def __init__(
        self,
        data_cache: DataCache,
        http_manager: HttpManager,
        image_caption_enabled: bool,
        audio_caption_enabled: bool,
        call_llm: CallLLM,
        url_locks=None,
        hash_locks=None,
    ):
        self.data_cache = data_cache
        self.http_manager = http_manager
        self.image_caption_enabled = image_caption_enabled
        self.audio_caption_enabled = audio_caption_enabled
        self.call_llm = call_llm
        self.url_locks = (
            url_locks if url_locks is not None else LockManager()
        )
        self.hash_locks = (
            hash_locks if hash_locks is not None else LockManager()
        )

    @staticmethod
    def first_media_url(url: str | None, file_name: str | None) -> str:
        for candidate in (url, file_name):
            if not isinstance(candidate, str):
                continue
            candidate = candidate.strip()
            if candidate.startswith(("http://", "https://", "file://")):
                return candidate
        return ""

    @staticmethod
    def filename_stable_hash(file_name: str | None) -> str | None:
        if not file_name or is_temp_or_local_path(file_name):
            return None
        stem = re.sub(r"\.[^.]+$", "", os.path.basename(file_name))
        if re.fullmatch(r"[a-fA-F0-9]{16,64}", stem):
            return stem.lower()[:16]
        return None

    @staticmethod
    def is_filename_stable_hash(hash_val: str | None) -> bool:
        return bool(hash_val and re.fullmatch(r"[a-fA-F0-9]{16,64}", str(hash_val)))

    @staticmethod
    def get_stable_hash(file_name: str | None, url: str | None) -> str:
        """
        获取稳定的 16 位媒体哈希值。
        1. 优先提取 16~64 位 Hex 文件名标识。
        2. 如果为网络 URL，自动剥离 ?rkey=.../token 等动态 Query 鉴权参数，确保相同视频哈希完全一致。
        """
        for cand in (file_name, url):
            if not cand or not isinstance(cand, str):
                continue
            cand_str = cand.strip()
            if is_temp_or_local_path(cand_str):
                continue

            clean_path = urllib.parse.urlparse(cand_str).path if cand_str.startswith("http") else cand_str
            basename = os.path.basename(clean_path)
            stem = re.sub(r"\.[^.]+$", "", basename)
            if re.fullmatch(r"[a-fA-F0-9]{16,64}", stem):
                return stem.lower()[:16]

        target_str = url or file_name or ""
        if target_str.startswith("http://") or target_str.startswith("https://"):
            try:
                parsed = urllib.parse.urlparse(target_str)
                target_str = parsed.netloc + parsed.path
            except Exception:
                pass

        return xxh3_64_hexdigest(target_str.encode("utf-8"))[:16]

    async def _update_existing_media_caption(
        self,
        existing_caption: MediaCaption,
        url: str | None = None,
        file_name: str | None = None,
        duration: float = 0.0,
        file_size: int = 0,
    ) -> None:
        """统一更新已存在媒体的 URL、文件名与元数据，并同步更新数据缓存"""
        if url:
            existing_caption.url = url
        if file_name:
            existing_caption.file_name = file_name
        if duration > 0 and getattr(existing_caption, "duration", 0) <= 0:
            existing_caption.duration = duration
        if file_size > 0 and getattr(existing_caption, "file_size", 0) <= 0:
            existing_caption.file_size = file_size
        await self.data_cache.set_caption(existing_caption)

    async def format_image_ref(
        self,
        url: str,
        file_name: str | None,
        defer_caption: bool,
        custom_desc: str | None = None,
        event = None,
    ) -> tuple[str, ChainParseResult]:
        result = ChainParseResult()
        decision_url = self.first_media_url(url, file_name)
        if decision_url:
            result.image_urls.append(decision_url)

        media_caption = None
        hash_val = None
        legacy_caption = None
        if file_name and not is_temp_or_local_path(file_name):
            hash_val, media_caption = await self.data_cache.get_caption_by_filename(
                file_name
            )
            if self.is_filename_stable_hash(hash_val):
                legacy_caption = media_caption
                media_caption = None

        should_try_caption = bool(
            url
            or (
                file_name
                and (
                    file_name.startswith("http")
                    or file_name.startswith("file://")
                    or file_name.startswith("base64://")
                )
            )
        )
        # Resolve bot_name and group_or_user_id from event
        bot_name = ""
        group_or_user_id = ""
        if event and self.call_llm.plugin:
            bot_name = self.call_llm.plugin.adapter_id_map.get(event.platform_meta.id) or ""
            group_or_user_id = event.get_group_id() or event.get_sender_id() or ""

        if not media_caption and should_try_caption:
            hash_val, media_caption = await self.get_image_caption(
                url or "", file_name, defer_caption, custom_desc=custom_desc,
                bot_name=bot_name, group_or_user_id=group_or_user_id
            )
        if hash_val and media_caption:
            result.media_id_list.append(hash_val)
            await self.data_cache.set_caption(media_caption)
            return f"[图片:{hash_val}]", result
        if hash_val and legacy_caption:
            result.media_id_list.append(hash_val)
            return f"[图片:{hash_val}]", result
        return "[图片]", result

    async def format_audio_ref(
        self, url: str, file_name: str | None, defer_caption: bool, event = None
    ) -> tuple[str, ChainParseResult]:
        result = ChainParseResult()
        decision_url = self.first_media_url(url, file_name)
        if decision_url:
            result.audio_urls.append(decision_url)

        media_caption = None
        hash_val = None
        if (
            file_name
            and self.audio_caption_enabled
            and not is_temp_or_local_path(file_name)
        ):
            hash_val, media_caption = await self.data_cache.get_caption_by_filename(
                file_name
            )

        should_try_caption = bool(
            self.audio_caption_enabled
            and (
                url
                or (
                    file_name
                    and (
                        file_name.startswith("http")
                        or file_name.startswith("file://")
                    )
                )
            )
        )
        # Resolve bot_name and group_or_user_id from event
        bot_name = ""
        group_or_user_id = ""
        if event and self.call_llm.plugin:
            bot_name = self.call_llm.plugin.adapter_id_map.get(event.platform_meta.id) or ""
            group_or_user_id = event.get_group_id() or event.get_sender_id() or ""

        if not media_caption and should_try_caption:
            hash_val, media_caption = await self.get_audio_caption(
                url or "", file_name, defer_caption,
                bot_name=bot_name, group_or_user_id=group_or_user_id
            )
        if hash_val and media_caption:
            result.media_id_list.append(hash_val)
            await self.data_cache.set_caption(media_caption)
            return f"[语音:{hash_val}]", result
        return "[语音]", result

    async def format_video_ref(
        self, url: str, file_name: str | None, defer_caption: bool, event = None, path: str | None = None
    ) -> tuple[str, ChainParseResult]:
        result = ChainParseResult()
        decision_url = self.first_media_url(url, file_name)

        # 1. 尝试多维获取远程/本地视频元数据 (file_size, duration)
        file_size, duration = await get_remote_video_info(
            url=decision_url or url or "",
            file_name=file_name or "",
            path=path or "",
            event=event,
        )

        # 2. 从配置获取限制
        caption_config = {}
        if self.call_llm and hasattr(self.call_llm, "plugin") and self.call_llm.plugin:
            caption_config = getattr(self.call_llm.plugin, "conf", {}).get("caption_config", {})
        
        max_size_mb = caption_config.get("video_max_file_size_mb", 50)
        max_size_bytes = max_size_mb * 1024 * 1024

        # 3. 计算 16 位稳定哈希
        hash_val = self.get_stable_hash(file_name, decision_url or url or path or "")

        size_str = format_file_size(file_size)
        dur_str = format_duration(duration)

        # 4. 检查大小超限
        is_over_limit = (file_size > 0) and (file_size > max_size_bytes)
        
        existing_caption = await self.data_cache.get_caption_by_hash(hash_val) if hash_val else None
        if existing_caption:
            await self._update_existing_media_caption(
                existing_caption,
                url=decision_url or url or "",
                file_name=file_name,
                duration=duration,
                file_size=file_size,
            )

        if is_over_limit:
            tag = f"[视频:{hash_val}][文件过大:{size_str}]"
            if not existing_caption:
                media_caption = MediaCaption(
                    hash_val=hash_val,
                    file_name=file_name or "",
                    url=decision_url or url or "",
                    media_type="video",
                    duration=duration,
                    file_size=file_size,
                    is_captioned=True,
                    text=f"[视频体积过大已限制转述: {size_str}]",
                    caption=f"[视频体积过大已限制转述: {size_str}]",
                )
                await self.data_cache.set_caption(media_caption)
            result.media_id_list.append(hash_val)
            return tag, result

        # 5. 未超限逻辑：构建灵活、防御性的 Tag
        if dur_str and size_str:
            tag = f"[视频:{hash_val}][时长:{dur_str}, 大小:{size_str}]"
        elif size_str:
            tag = f"[视频:{hash_val}][大小:{size_str}]"
        elif dur_str:
            tag = f"[视频:{hash_val}][时长:{dur_str}]"
        else:
            tag = f"[视频:{hash_val}]"
        
        if not existing_caption:
            media_caption = MediaCaption(
                hash_val=hash_val,
                file_name=file_name or "",
                url=decision_url or url or "",
                media_type="video",
                duration=duration,
                file_size=file_size,
                is_captioned=False,
            )
            await self.data_cache.set_caption(media_caption)
        result.media_id_list.append(hash_val)
        return tag, result

    async def get_image_caption(
        self,
        url: str,
        file_name: str | None = None,
        defer_caption: bool = False,
        custom_desc: str | None = None,
        bot_name: str = "",
        group_or_user_id: str = "",
    ) -> tuple[str | None, MediaCaption | None]:
        """获取图片描述"""
        if not url and file_name:
            url = file_name
        async with self.url_locks.lock(url):
            legacy_hash = self.filename_stable_hash(file_name)
            legacy_caption = None
            if file_name and not is_temp_or_local_path(file_name):
                # 检查缓存
                hash_val, media_caption = await self.data_cache.get_caption_by_filename(
                    file_name
                )
                if (
                    hash_val
                    and media_caption
                    and not self.is_filename_stable_hash(hash_val)
                ):
                    if getattr(media_caption, "is_captioned", True) or defer_caption:
                        return hash_val, media_caption
                if hash_val and media_caption:
                    legacy_caption = media_caption

            if legacy_hash and not legacy_caption:
                legacy_caption = await self.data_cache.get_caption_by_hash(legacy_hash)

            # 下载图片
            image_bytes = None
            if file_name and file_name.startswith("file://"):
                clean_path = urllib.parse.unquote(file_name[7:])
                local_path = Path(clean_path)
                if local_path.is_file():
                    try:
                        image_bytes = local_path.read_bytes()
                    except Exception as e:
                        file_name_disp = (
                            (file_name[:100] + "...")
                            if file_name and len(file_name) > 100
                            else file_name
                        )
                        logger.error(f"[Giftia] 读取本地图片失败 {file_name_disp}: {e}")
            elif file_name and file_name.startswith("base64://"):
                try:
                    b64_data = file_name[9:]
                    if "," in b64_data:
                        b64_data = b64_data.split(",", 1)[1]
                    image_bytes = b64_module.b64decode(b64_data)
                except Exception as e:
                    logger.error(f"[Giftia] 解码 base64 图片失败: {e}")

            if not image_bytes:
                image_bytes = await self.http_manager.download_media(url)

            if not image_bytes:
                if legacy_hash and legacy_caption:
                    return legacy_hash, legacy_caption
                return None, None
            # 新的 canonical media_id 一律使用图片内容哈希；文件名里的 32 位 MD5 只做旧缓存别名。
            hash_val = xxh3_64_hexdigest(image_bytes)

            # If the URL/file_name is a base64 string, replace it with a clean placeholder to avoid bloated DB columns
            db_url = url
            if db_url and db_url.startswith("base64://"):
                db_url = f"base64://{hash_val}"

            db_file_name = file_name
            if db_file_name and db_file_name.startswith("base64://"):
                db_file_name = f"base64://{hash_val}"

            # 保存到本地持久缓存目录，以便网页端可以永久预览
            try:
                from astrbot.core.star.star_tools import StarTools

                cache_dir = (
                    StarTools.get_data_dir("astrbot_plugin_giftia") / "media_cache"
                )
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache_file = cache_dir / hash_val
                if not cache_file.exists():
                    cache_file.write_bytes(image_bytes)
            except Exception as e:
                logger.error(f"[Giftia] 保存媒体缓存失败: {e}")

        async with self.hash_locks.lock(hash_val):
            if custom_desc:
                media_caption = MediaCaption(
                    hash_val=hash_val,
                    url=db_url,
                    media_type="image",
                    caption=custom_desc,
                    genre="表情包",
                    is_captioned=True,
                )
                if file_name:
                    media_caption.file_name = db_file_name
                await self.data_cache.set_caption(media_caption)
                return hash_val, media_caption

            # 检查缓存
            media_caption = await self.data_cache.get_caption_by_hash(hash_val)
            if media_caption:
                await self._update_existing_media_caption(
                    media_caption, url=db_url, file_name=db_file_name if file_name else None
                )
                if getattr(media_caption, "is_captioned", True) or defer_caption:
                    return hash_val, media_caption

            if legacy_caption:
                canonical_caption = MediaCaption(
                    hash_val=hash_val,
                    file_name=db_file_name if file_name else legacy_caption.file_name,
                    url=db_url,
                    media_type="image",
                    genre=legacy_caption.genre,
                    character=legacy_caption.character,
                    source=legacy_caption.source,
                    text=legacy_caption.text,
                    caption=legacy_caption.caption,
                    is_captioned=legacy_caption.is_captioned,
                )
                await self.data_cache.set_caption(canonical_caption)
                if getattr(canonical_caption, "is_captioned", True) or defer_caption:
                    return hash_val, canonical_caption

            if not self.image_caption_enabled:
                media_caption = MediaCaption(
                    hash_val=hash_val,
                    url=db_url,
                    media_type="image",
                    is_captioned=True,
                )
                if file_name:
                    media_caption.file_name = db_file_name
                await self.data_cache.set_caption(media_caption)
                return hash_val, media_caption

            # 如果开启了延迟，直接返回一个仅包含url和hash的基础对象
            if defer_caption:
                media_caption = MediaCaption(
                    hash_val=hash_val,
                    url=db_url,
                    media_type="image",
                    is_captioned=False,
                )
                if file_name:
                    media_caption.file_name = db_file_name
                await self.data_cache.set_caption(media_caption)
                return hash_val, media_caption

            # 处理图片
            base64s, _is_animated = await asyncio.to_thread(
                self.http_manager.handle_image, image_bytes
            )
            if not base64s:
                media_caption = MediaCaption(
                    hash_val=hash_val,
                    url=db_url,
                    media_type="image",
                    is_captioned=True,
                )
                if file_name:
                    media_caption.file_name = db_file_name
                await self.data_cache.set_caption(media_caption)
                return hash_val, media_caption
            # Log key identifiers so we can detect if two different URLs produce the
            # same image content (which would indicate a stale-temp-file read).
            url_disp = (url[:100] + "...") if url and len(url) > 100 else url
            logger.info(
                f"[Giftia] 调用LLM转述图片: hash={hash_val} "
                f"size={len(image_bytes)}B "
                f"head={image_bytes[:8].hex()} "
                f"url={url_disp!r}"
            )
            # 调用LLM生成图片描述
            media_caption = await self.call_llm.call_llm_image_caption(
                base64s, bot_name=bot_name, group_or_user_id=group_or_user_id
            )
            if not media_caption:
                media_caption = MediaCaption(
                    hash_val=hash_val,
                    url=db_url,
                    media_type="image",
                    is_captioned=True,
                )
                if file_name:
                    media_caption.file_name = db_file_name
                await self.data_cache.set_caption(media_caption)
                return hash_val, media_caption
            media_caption.hash_val = hash_val
            media_caption.url = db_url
            if file_name:
                media_caption.file_name = db_file_name
            media_caption.media_type = "image"
            media_caption.is_captioned = True
            # 缓存
            await self.data_cache.set_caption(media_caption)
            return hash_val, media_caption

    async def get_audio_caption(
        self,
        url: str,
        file_name: str | None = None,
        defer_caption: bool = False,
        bot_name: str = "",
        group_or_user_id: str = "",
    ) -> tuple[str | None, MediaCaption | None]:
        """获取语音描述"""
        if not url and file_name:
            url = file_name
        async with self.url_locks.lock(url):
            if file_name and not is_temp_or_local_path(file_name):
                # 检查缓存
                hash_val, media_caption = await self.data_cache.get_caption_by_filename(
                    file_name
                )
                if hash_val and media_caption:
                    if getattr(media_caption, "is_captioned", True) or defer_caption:
                        return hash_val, media_caption

            # 语音的hash_val用url生成，如果是本地文件，使用本地内容生成hash
            audio_bytes = None
            if file_name and file_name.startswith("file://"):
                clean_path = urllib.parse.unquote(file_name[7:])
                local_path = Path(clean_path)
                if local_path.is_file():
                    try:
                        audio_bytes = local_path.read_bytes()
                    except Exception as e:
                        file_name_disp = (
                            (file_name[:100] + "...")
                            if file_name and len(file_name) > 100
                            else file_name
                        )
                        logger.error(f"[Giftia] 读取本地音频失败 {file_name_disp}: {e}")

            if audio_bytes:
                hash_val = xxh3_64_hexdigest(audio_bytes)
            else:
                hash_val = xxh3_64_hexdigest(url.encode())

            # 检查缓存
            media_caption = await self.data_cache.get_caption_by_hash(hash_val)
            if media_caption:
                await self._update_existing_media_caption(
                    media_caption, url=url, file_name=file_name
                )
                if getattr(media_caption, "is_captioned", True) or defer_caption:
                    return hash_val, media_caption

            # 下载并保存语音文件，以便永久播放
            if not audio_bytes:
                try:
                    audio_bytes = await self.http_manager.download_media(url)
                except Exception as e:
                    url_disp = (url[:100] + "...") if url and len(url) > 100 else url
                    logger.error(f"[Giftia] 下载音频失败 {url_disp}: {e}")

            if audio_bytes:
                try:
                    from astrbot.core.star.star_tools import StarTools

                    cache_dir = (
                        StarTools.get_data_dir("astrbot_plugin_giftia") / "media_cache"
                    )
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    cache_file = cache_dir / hash_val
                    if not cache_file.exists():
                        cache_file.write_bytes(audio_bytes)
                except Exception as e:
                    logger.error(f"[Giftia] 保存音频缓存失败: {e}")

            if defer_caption:
                media_caption = MediaCaption(
                    hash_val=hash_val,
                    url=url,
                    media_type="audio",
                    is_captioned=False,
                )
                if file_name:
                    media_caption.file_name = file_name
                await self.data_cache.set_caption(media_caption)
                return hash_val, media_caption

            # 调用LLM生成语音描述
            media_caption = await self.call_llm.call_llm_audio_caption(
                [url], bot_name=bot_name, group_or_user_id=group_or_user_id
            )
            if not media_caption:
                # 即使LLM失败，也需要保存一个未转述的或者空的对象，但标记为 captioned=False 方便后续重试
                media_caption = MediaCaption(
                    hash_val=hash_val,
                    url=url,
                    media_type="audio",
                    is_captioned=False,
                )
                if file_name:
                    media_caption.file_name = file_name
                await self.data_cache.set_caption(media_caption)
                return hash_val, media_caption

            media_caption.hash_val = hash_val
            media_caption.url = url
            if file_name:
                media_caption.file_name = file_name
            media_caption.media_type = "audio"
            media_caption.is_captioned = True
            # 缓存
            await self.data_cache.set_caption(media_caption)
            return hash_val, media_caption
