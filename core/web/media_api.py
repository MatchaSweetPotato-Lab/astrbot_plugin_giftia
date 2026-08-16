import mimetypes
import re
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request


class MediaApi:
    """Media management APIs: captions CRUD, file serving, genres, cache cleaning."""

    def __init__(self, giftia):
        self.giftia = giftia

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _detect_content_type(
        file_path: Path, media_caption=None, fallback: str = "application/octet-stream"
    ) -> str:
        """Detect content type from DB record or magic bytes.

        Args:
            file_path: Path to the media file on disk.
            media_caption: Optional DB record with media_type / file_name / url.
            fallback: Content type to return if detection fails.

        Returns:
            The detected MIME content type string.
        """
        magic_type = None
        name_type = None

        # 1. Magic bytes - 优先于扩展名（QQ/微信语音经常改后缀为 .wav，
        #    但实际是 AMR/Silk，扩展名猜出来的类型完全是误导）
        try:
            with open(file_path, "rb") as f:
                header = f.read(16)
            if header.startswith(b"\x89PNG"):
                magic_type = "image/png"
            elif header.startswith(b"\xff\xd8"):
                magic_type = "image/jpeg"
            elif header.startswith(b"GIF8"):
                magic_type = "image/gif"
            elif header.startswith(b"RIFF") and header[8:12] == b"WEBP":
                magic_type = "image/webp"
            elif header.startswith(b"RIFF") and header[8:12] == b"WAVE":
                magic_type = "audio/wav"
            elif (
                header.startswith(b"ID3")
                or header.startswith(b"\xff\xfb")
                or header.startswith(b"\xff\xf3")
                or header.startswith(b"\xff\xf2")
            ):
                magic_type = "audio/mpeg"
            elif header.startswith(b"OggS"):
                magic_type = "audio/ogg"
            elif header.startswith(b"fLaC"):
                magic_type = "audio/flac"
            elif header[4:8] == b"ftyp":
                magic_type = "audio/mp4"
            elif header.startswith(b"#!AMR\n") or header.startswith(b"#!AMR-WB\n"):
                magic_type = "audio/amr"
            elif header.startswith(b"#!SILK_V3\n"):
                magic_type = "audio/silk"
        except Exception:
            pass

        # 2. file_name / url 后缀（仅作为 magic bytes 不可用时的回退）
        if media_caption:
            file_name = getattr(media_caption, "file_name", None) or getattr(
                media_caption, "url", None
            )
            if file_name:
                name_type, _ = mimetypes.guess_type(file_name)
            if not name_type:
                mt = getattr(media_caption, "media_type", None)
                if mt == "image":
                    name_type = "image/jpeg"
                elif mt in ("audio", "voice"):
                    name_type = "audio/mpeg"

        return magic_type or name_type or fallback

    @staticmethod
    def _get_cache_dir() -> Path:
        """Return the media_cache directory path."""
        from astrbot.core.star.star_tools import StarTools

        return StarTools.get_data_dir("astrbot_plugin_giftia") / "media_cache"

    @classmethod
    def _is_valid_hash(cls, hash_val: str | None) -> bool:
        """Validate that hash_val is a safe, alphanumeric identifier without path traversal characters."""
        if not hash_val or not isinstance(hash_val, str):
            return False
        # Allow alphanumeric, underscore, and hyphen (1-64 chars), strictly forbidding slashes, dots, or control chars
        return bool(re.fullmatch(r"^[a-zA-Z0-9_\-]{1,64}$", hash_val))

    def _resolve_cache_file(
        self, hash_val: str, is_thumbnail: bool = False
    ) -> Path | None:
        """Safely resolve media cache file path ensuring it stays strictly inside the designated directory."""
        if not self._is_valid_hash(hash_val):
            return None

        cache_dir = self._get_cache_dir().resolve()
        target_dir = (cache_dir / "thumbnails").resolve() if is_thumbnail else cache_dir

        try:
            target_file = (target_dir / hash_val).resolve()
            if target_file.is_relative_to(target_dir):
                return target_file
        except (ValueError, OSError):
            return None
        return None

    # ── Caption CRUD ────────────────────────────────────────────────────

    async def get_media(self):
        """Get media captions with pagination and filters."""
        try:
            page = int(request.query.get("page", 1))
            limit = int(request.query.get("limit", 20))
            media_type = request.query.get("media_type")
            search = request.query.get("search")

            offset = (page - 1) * limit
            conditions = []
            params = []

            if media_type:
                conditions.append("media_type = ?")
                params.append(media_type)
            if search:
                conditions.append(
                    "(caption LIKE ? OR file_name LIKE ? OR hash_val LIKE ?)"
                )
                params.append(f"%{search}%")
                params.append(f"%{search}%")
                params.append(f"%{search}%")

            where_clause = ""
            if conditions:
                where_clause = "WHERE " + " AND ".join(conditions)

            # Query count
            count_sql = f"SELECT COUNT(*) as total FROM media_caption {where_clause}"
            async with self.giftia.db.conn.execute(count_sql, params) as cursor:
                row = await cursor.fetchone()
                total = row["total"] if row else 0

            # Query data
            data_sql = f"""
                SELECT id, hash_val, file_name, url, media_type, genre, character, source, text, caption, is_captioned, duration, file_size, query_times, created_at
                FROM media_caption
                {where_clause}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            """
            data_params = params + [limit, offset]
            items = []
            async with self.giftia.db.conn.execute(data_sql, data_params) as cursor:
                rows = await cursor.fetchall()
                for r in rows:
                    r_keys = r.keys() if hasattr(r, "keys") else []
                    items.append(
                        {
                            "id": r["id"],
                            "hash_val": r["hash_val"],
                            "file_name": r["file_name"],
                            "url": r["url"],
                            "media_type": r["media_type"],
                            "genre": r["genre"],
                            "character": r["character"],
                            "source": r["source"],
                            "text": r["text"],
                            "caption": r["caption"],
                            "is_captioned": bool(r["is_captioned"]),
                            "duration": float(r["duration"]) if "duration" in r_keys and r["duration"] is not None else 0.0,
                            "file_size": int(r["file_size"]) if "file_size" in r_keys and r["file_size"] is not None else 0,
                            "query_times": r["query_times"],
                            "created_at": r["created_at"],
                        }
                    )

            return json_response(
                {
                    "status": "success",
                    "data": {
                        "items": items,
                        "total": total,
                        "page": page,
                        "limit": limit,
                    },
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] get_media error: {e}")
            return error_response(f"获取媒体转述列表失败: {str(e)}")

    async def update_media(self):
        """Update media caption text."""
        try:
            body = await request.json()
            hash_val = body.get("hash_val")
            caption = body.get("caption")
            text = body.get("text")
            genre = body.get("genre")
            character = body.get("character")
            source = body.get("source")

            if not hash_val:
                return error_response("缺少 hash_val 参数")
            if not self._is_valid_hash(hash_val):
                return error_response("无效的 hash_val 参数", status_code=400)

            # Fetch existing cache to verify and update
            media_caption = await self.giftia.data_cache.get_caption_by_hash(hash_val)
            if not media_caption:
                return error_response("媒体记录不存在")

            media_caption.caption = caption
            if text is not None:
                media_caption.text = text
            if genre is not None:
                media_caption.genre = genre
            if character is not None:
                media_caption.character = character
            if source is not None:
                media_caption.source = source

            # Update DB
            await self.giftia.db.conn.execute(
                """
                UPDATE media_caption
                SET caption = ?, text = ?, genre = ?, character = ?, source = ?, updated_at = ?
                WHERE hash_val = ?
                """,
                (
                    caption,
                    media_caption.text,
                    media_caption.genre,
                    media_caption.character,
                    media_caption.source,
                    datetime.now().isoformat(),
                    hash_val,
                ),
            )
            await self.giftia.db.conn.commit()

            # Update cache
            self.giftia.data_cache.caption[hash_val] = media_caption

            return json_response({"status": "success", "message": "保存媒体描述成功"})
        except Exception as e:
            logger.error(f"[Giftia API] update_media error: {e}")
            return error_response(f"修改媒体描述失败: {str(e)}")

    async def delete_media(self):
        """Delete media caption cache."""
        try:
            body = await request.json()
            hash_val = body.get("hash_val")

            if not hash_val:
                return error_response("缺少 hash_val 参数")
            if not self._is_valid_hash(hash_val):
                return error_response("无效的 hash_val 参数", status_code=400)

            # Delete from DB
            await self.giftia.db.conn.execute(
                "DELETE FROM media_caption WHERE hash_val = ?", (hash_val,)
            )
            await self.giftia.db.conn.commit()

            # Remove from cache
            self.giftia.data_cache.caption.pop(hash_val, None)

            # Remove from local persistent disk cache
            try:
                cache_file = self._resolve_cache_file(hash_val)
                if cache_file and cache_file.is_file():
                    cache_file.unlink()
                # Also delete thumbnail if exists
                thumb_file = self._resolve_cache_file(hash_val, is_thumbnail=True)
                if thumb_file and thumb_file.is_file():
                    thumb_file.unlink()
            except Exception as e:
                logger.error(f"[Giftia API] delete_media file error: {e}")

            return json_response({"status": "success", "message": "删除媒体描述成功"})
        except Exception as e:
            logger.error(f"[Giftia API] delete_media error: {e}")
            return error_response(f"删除媒体描述失败: {str(e)}")

    # ── File Serving ────────────────────────────────────────────────────

    async def get_media_file(self, hash_val: str):
        """Get cached media file by hash value."""
        try:
            from astrbot.api.web import file_response

            cache_file = self._resolve_cache_file(hash_val)
            if not cache_file or not cache_file.is_file():
                return error_response("文件不存在或已被删除", status_code=404)

            media_caption = None
            try:
                media_caption = await self.giftia.db.get_media_caption_by_hash(hash_val)
            except Exception as e:
                logger.warning(f"[Giftia API] 无法从数据库获取媒体类型: {e}")

            content_type = self._detect_content_type(cache_file, media_caption)

            return file_response(cache_file, content_type=content_type)
        except Exception as e:
            logger.error(f"[Giftia API] get_media_file error: {e}")
            return error_response(f"获取媒体文件失败: {str(e)}")

    async def get_media_file_b64(self, hash_val: str):
        """Get cached media file as base64 string (JSON response)."""
        try:
            import base64

            cache_file = self._resolve_cache_file(hash_val)
            if not cache_file or not cache_file.is_file():
                return error_response("文件不存在或已被删除", status_code=404)

            media_caption = None
            try:
                media_caption = await self.giftia.db.get_media_caption_by_hash(hash_val)
            except Exception as e:
                logger.warning(f"[Giftia API] 无法从数据库获取媒体类型: {e}")

            # 根据 media_type 推断 fallback：
            #  - 音频类 → audio/mpeg（即使检测失败，PC 浏览器至少能正确处理错误）
            #  - 其它   → application/octet-stream
            media_type = getattr(media_caption, "media_type", None)
            if media_type in ("audio", "voice"):
                fallback = "audio/mpeg"
            else:
                fallback = "application/octet-stream"

            content_type = self._detect_content_type(
                cache_file, media_caption, fallback=fallback
            )

            file_size = cache_file.stat().st_size

            # 音频文件 < 1KB 视为不完整（基本只剩头部）
            is_too_small = media_type in ("audio", "voice") and file_size < 1024

            with open(cache_file, "rb") as f:
                file_bytes = f.read()

            b64_str = base64.b64encode(file_bytes).decode("utf-8")

            return json_response(
                {
                    "status": "success",
                    "base64": b64_str,
                    "content_type": content_type,
                    "file_size": file_size,
                    "warning": "audio_too_small" if is_too_small else None,
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] get_media_file_b64 error: {e}")
            return error_response(f"获取媒体 Base64 失败: {str(e)}")

    async def get_media_file_thumbnail_b64(self, hash_val: str):
        """Get cached media thumbnail as base64 string (JSON response).

        Args:
            hash_val: The hash of the media file.

        Returns:
            A dict containing the response status, base64 string, and content type.
        """
        try:
            import base64

            cache_file = self._resolve_cache_file(hash_val)
            if not cache_file or not cache_file.is_file():
                return error_response("文件不存在或已被删除", status_code=404)

            media_caption = None
            try:
                media_caption = await self.giftia.db.get_media_caption_by_hash(hash_val)
            except Exception as e:
                logger.warning(f"[Giftia API] 无法从数据库获取媒体类型: {e}")

            content_type = self._detect_content_type(
                cache_file, media_caption, fallback="image/jpeg"
            )

            target_file = cache_file

            # If it's an image, try to load/generate thumbnail
            if content_type and content_type.startswith("image/"):
                thumb_file = self._resolve_cache_file(hash_val, is_thumbnail=True)
                if not thumb_file:
                    return error_response("无效的媒体标识", status_code=400)
                thumb_dir = thumb_file.parent
                use_thumbnail = False

                try:
                    thumb_dir.mkdir(parents=True, exist_ok=True)
                    need_generate = True
                    if thumb_file.exists():
                        try:
                            if cache_file.stat().st_mtime <= thumb_file.stat().st_mtime:
                                need_generate = False
                                use_thumbnail = True
                                # Read magic bytes from cached thumbnail to determine correct content type
                                with open(thumb_file, "rb") as f:
                                    header = f.read(12)
                                if b"WEBP" in header:
                                    content_type = "image/webp"
                                elif header.startswith(b"\xff\xd8"):
                                    content_type = "image/jpeg"
                                elif header.startswith(b"\x89PNG"):
                                    content_type = "image/png"
                        except Exception as mtime_err:
                            logger.warning(
                                f"[Giftia API] Error checking cached thumbnail {hash_val}: {mtime_err}"
                            )

                    if need_generate:
                        from PIL import Image as PILImage

                        with PILImage.open(cache_file) as img:
                            # If animated (GIF, animated WebP, etc.), extract first frame
                            if getattr(img, "is_animated", False):
                                img.seek(0)
                                img = img.copy()

                            img.thumbnail((150, 150))

                            temp_thumb_path = thumb_file.with_name(
                                thumb_file.name + ".tmp"
                            )
                            try:
                                img.save(temp_thumb_path, format="WEBP")
                                content_type = "image/webp"
                            except Exception:
                                try:
                                    img.save(temp_thumb_path, format="PNG")
                                    content_type = "image/png"
                                except Exception:
                                    # Fallback to JPEG requires converting to RGB mode to support RGBA/P formats
                                    rgb_img = img.convert("RGB")
                                    rgb_img.save(temp_thumb_path, format="JPEG")
                                    content_type = "image/jpeg"

                            import os

                            os.replace(temp_thumb_path, thumb_file)
                            use_thumbnail = True
                except Exception as img_err:
                    logger.warning(
                        f"[Giftia API] Failed to generate/load thumbnail for {hash_val}, falling back to original: {img_err}"
                    )

                target_file = thumb_file if use_thumbnail else cache_file

            # Read target file bytes and encode to base64
            with open(target_file, "rb") as f:
                file_bytes = f.read()

            b64_str = base64.b64encode(file_bytes).decode("utf-8")

            return json_response(
                {"status": "success", "base64": b64_str, "content_type": content_type}
            )
        except Exception as e:
            logger.error(f"[Giftia API] get_media_file_thumbnail_b64 error: {e}")
            return error_response(f"获取媒体缩略图 Base64 失败: {str(e)}")

    # ── Genres & Cache Cleaning ─────────────────────────────────────────

    async def get_media_genres(self) -> dict:
        """Get distinct genres list from media_caption table.

        Returns:
            A dict containing the response status and the list of genres.
        """
        try:
            genres = []
            async with self.giftia.db.conn.execute(
                "SELECT DISTINCT genre FROM media_caption WHERE genre IS NOT NULL AND genre != ''"
            ) as cursor:
                rows = await cursor.fetchall()
                genres = [r["genre"] for r in rows if r["genre"]]
            return json_response({"status": "success", "genres": genres})
        except Exception as e:
            logger.error(f"[Giftia API] get_media_genres error: {e}")
            return error_response(f"获取风格列表失败: {str(e)}")

    async def clean_media_cache(self) -> dict:
        """Clean media file cache by criteria (dry_run or actual).

        Returns:
            A dict containing the status, matching file count, total size freed in bytes,
            dry_run flag, and a message.
        """
        try:
            body = await request.json()
            media_type = body.get("media_type", "all")
            max_query_times = body.get("max_query_times")
            dry_run = body.get("dry_run", False)

            conditions = []
            params = []

            if media_type == "image":
                conditions.append("media_type = 'image'")
            elif media_type == "audio":
                conditions.append("media_type IN ('audio', 'voice')")
            elif media_type == "video":
                conditions.append("media_type = 'video'")

            genres = body.get("genres")
            exclude_genres = body.get("exclude_genres", False)

            if genres is not None:
                if not exclude_genres and not genres:
                    conditions.append("1 = 0")
                elif genres:
                    has_unspecified = "" in genres
                    specified_genres = [g for g in genres if g != ""]

                    if not exclude_genres:
                        if specified_genres:
                            placeholders = ",".join(["?"] * len(specified_genres))
                            if has_unspecified:
                                conditions.append(
                                    f"(genre IN ({placeholders}) OR genre IS NULL OR genre = '')"
                                )
                            else:
                                conditions.append(f"genre IN ({placeholders})")
                            params.extend(specified_genres)
                        else:
                            conditions.append("(genre IS NULL OR genre = '')")
                    else:
                        if specified_genres:
                            placeholders = ",".join(["?"] * len(specified_genres))
                            if has_unspecified:
                                conditions.append(
                                    f"(genre NOT IN ({placeholders}) AND genre IS NOT NULL AND genre != '')"
                                )
                            else:
                                conditions.append(
                                    f"(genre NOT IN ({placeholders}) OR genre IS NULL OR genre = '')"
                                )
                            params.extend(specified_genres)
                        else:
                            conditions.append("genre IS NOT NULL AND genre != ''")

            if max_query_times is not None:
                try:
                    max_query_times = int(max_query_times)
                    conditions.append("query_times <= ?")
                    params.append(max_query_times)
                except ValueError:
                    pass

            where_clause = ""
            if conditions:
                where_clause = "WHERE " + " AND ".join(conditions)

            sql = f"SELECT hash_val FROM media_caption {where_clause}"

            matching_hashes = []
            async with self.giftia.db.conn.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                matching_hashes = [r["hash_val"] for r in rows if r["hash_val"]]

            cleaned_count = 0
            freed_bytes = 0

            for hash_val in matching_hashes:
                cache_file = self._resolve_cache_file(hash_val)
                if cache_file and cache_file.is_file():
                    file_size = cache_file.stat().st_size
                    cleaned_count += 1
                    freed_bytes += file_size
                    if not dry_run:
                        try:
                            cache_file.unlink()
                        except Exception as file_err:
                            logger.error(
                                f"[Giftia API] Failed to delete cache file {hash_val}: {file_err}"
                            )
                        # Also delete thumbnail if exists
                        try:
                            thumb_file = self._resolve_cache_file(hash_val, is_thumbnail=True)
                            if thumb_file and thumb_file.is_file():
                                thumb_file.unlink()
                        except Exception as thumb_err:
                            logger.error(
                                f"[Giftia API] Failed to delete thumbnail file {hash_val}: {thumb_err}"
                            )

            action_msg = "预估" if dry_run else "成功"
            return json_response(
                {
                    "status": "success",
                    "count": cleaned_count,
                    "size_bytes": freed_bytes,
                    "dry_run": dry_run,
                    "message": f"{action_msg}清理了 {cleaned_count} 个媒体文件，释放空间 {freed_bytes} 字节",
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] clean_media_cache error: {e}")
            return error_response(f"清理媒体文件缓存失败: {str(e)}")

    async def get_auto_clean_config(self) -> dict:
        """获取自动清理缓存的配置"""
        try:
            import json

            raw_cfg = await self.giftia.db.get_kv_data("auto_clean_media_config")
            cfg = (
                json.loads(raw_cfg)
                if raw_cfg
                else {"enabled": False, "keep_genres": ["表情包", "sticker"]}
            )
            return json_response({"status": "success", "config": cfg})
        except Exception as e:
            logger.error(f"[Giftia API] get_auto_clean_config error: {e}")
            return error_response(f"获取自动清理配置失败: {str(e)}")

    async def set_auto_clean_config(self) -> dict:
        """更新并保存自动清理缓存的配置"""
        try:
            import json

            body = await request.json()
            enabled = bool(body.get("enabled", False))
            keep_genres = list(body.get("keep_genres", ["表情包", "sticker"]))

            cfg = {"enabled": enabled, "keep_genres": keep_genres}
            await self.giftia.db.upsert_kv_data(
                "auto_clean_media_config", json.dumps(cfg)
            )

            # 动态更新调度器任务
            self.giftia.tools_func.update_auto_clean_media_job()

            return json_response({"status": "success", "message": "配置保存成功"})
        except Exception as e:
            logger.error(f"[Giftia API] set_auto_clean_config error: {e}")
            return error_response(f"保存自动清理配置失败: {str(e)}")

    async def trigger_auto_clean(self) -> dict:
        """手动触发执行自动清理"""
        try:
            res = await self.giftia.tools_func.auto_clean_media_cache()
            return json_response(res)
        except Exception as e:
            logger.error(f"[Giftia API] trigger_auto_clean error: {e}")
            return error_response(f"执行自动清理失败: {str(e)}")
