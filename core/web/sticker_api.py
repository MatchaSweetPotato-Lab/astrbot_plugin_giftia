import base64
import json
import re
from pathlib import Path

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from .web_helpers import optional_int


# 单张表情包体积上限（字节）
MAX_STICKER_BYTES = 10 * 1024 * 1024
# 单次上传张数上限
MAX_UPLOAD_BATCH = 50
# 缩略图尺寸
THUMBNAIL_SIZE = (150, 150)


class StickerApi:
    """表情包管理 APIs：列表筛选、元数据编辑、删除、手动上传、AI 分析、
    机器人归属增删、批量操作、分类与标签管理。

    所有写操作都经 EmojiManager 的写方法，以保证 self.stickers /
    self.bot_stickers 内存缓存与数据库一致——否则机器人会继续按旧数据挑图发图。
    """

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _is_valid_sticker_id(sticker_id: str | None) -> bool:
        """校验 sticker_id 为安全标识符，禁止斜杠、点号与控制字符。"""
        if not sticker_id or not isinstance(sticker_id, str):
            return False
        return bool(re.fullmatch(r"^[a-zA-Z0-9_\-]{1,64}$", sticker_id))

    def _get_stickers_dir(self) -> Path:
        """返回表情包存储目录（与 EmojiManager 同一个目录）。"""
        emoji_manager = getattr(self.giftia, "emoji_manager", None)
        if emoji_manager is not None and getattr(emoji_manager, "stickers_dir", None):
            return Path(emoji_manager.stickers_dir)

        from astrbot.core.star.star_tools import StarTools

        return StarTools.get_data_dir("astrbot_plugin_giftia") / "stickers"

    def _resolve_sticker_file(
        self, filename: str, is_thumbnail: bool = False
    ) -> Path | None:
        """安全解析表情包文件路径，确保严格落在 stickers 目录内。"""
        if not filename:
            return None

        # 只取文件名部分，杜绝 ../ 与绝对路径
        clean_name = Path(str(filename).replace("\\", "/")).name
        if not clean_name or clean_name in (".", ".."):
            return None

        try:
            base = self._get_stickers_dir().resolve()
            target_dir = (base / "thumbnails").resolve() if is_thumbnail else base
            # 目录自身也不能越界（防 symlink 逃逸）
            if not target_dir.is_relative_to(base):
                return None
            target = (target_dir / clean_name).resolve()
            if target.is_relative_to(target_dir):
                return target
        except (ValueError, OSError):
            return None
        return None

    def _find_sticker_image(self, sticker_id: str, filename: str = "") -> Path | None:
        """定位表情包图片：先查 stickers 目录，再回退 media_cache。

        回退逻辑与 xml_parse._load_sticker_image 一致——历史上下载失败但有
        媒体缓存的记录也能正常显示。
        """
        if filename:
            local = self._resolve_sticker_file(filename)
            if local and local.is_file():
                return local

        # 按 sticker_id 猜常见扩展名
        for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ""):
            candidate = self._resolve_sticker_file(f"{sticker_id}{ext}")
            if candidate and candidate.is_file():
                return candidate

        # 回退到媒体缓存
        try:
            from astrbot.core.star.star_tools import StarTools

            cache_dir = (
                StarTools.get_data_dir("astrbot_plugin_giftia") / "media_cache"
            ).resolve()
            cache_file = (cache_dir / sticker_id).resolve()
            if cache_file.is_relative_to(cache_dir) and cache_file.is_file():
                return cache_file
        except (ValueError, OSError, ImportError):
            pass

        return None

    @staticmethod
    def _detect_image_content_type(
        file_path: Path, fallback: str = "application/octet-stream"
    ) -> str:
        """按 magic bytes 判断图片 MIME 类型（不信任扩展名）。"""
        try:
            with open(file_path, "rb") as f:
                header = f.read(16)
        except OSError:
            return fallback

        if header.startswith(b"\x89PNG"):
            return "image/png"
        if header.startswith(b"\xff\xd8"):
            return "image/jpeg"
        if header.startswith(b"GIF8"):
            return "image/gif"
        if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
            return "image/webp"
        if header.startswith(b"BM"):
            return "image/bmp"
        return fallback

    @staticmethod
    def _is_supported_image(image_bytes: bytes) -> bool:
        """按 magic bytes 判断上传内容是否为受支持的图片。"""
        if not image_bytes or len(image_bytes) < 12:
            return False
        return (
            image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
            or image_bytes.startswith(b"\xff\xd8\xff")
            or image_bytes.startswith(b"GIF8")
            or (image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP")
            or image_bytes.startswith(b"BM")
        )

    @staticmethod
    def _normalize_tags(raw) -> list[str]:
        """把前端传来的标签规整为去重、去空、保序的字符串列表。"""
        if raw is None:
            return []
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                raw = parsed if isinstance(parsed, list) else raw.split(",")
            except json.JSONDecodeError:
                raw = raw.split(",")
        if not isinstance(raw, (list, tuple, set)):
            return []

        result: list[str] = []
        for tag in raw:
            if tag is None:
                continue
            text = str(tag).strip()
            if text and text not in result:
                result.append(text)
        return result

    @staticmethod
    def _normalize_ids(raw) -> list[str]:
        """规整 sticker_id 列表：去重保序。"""
        if not isinstance(raw, (list, tuple, set)):
            return []
        result: list[str] = []
        for item in raw:
            if item is None:
                continue
            text = str(item).strip()
            if text and text not in result:
                result.append(text)
        return result

    def _valid_ids_or_none(self, raw) -> list[str] | None:
        """规整并校验一批 sticker_id，任一非法则返回 None。"""
        ids = self._normalize_ids(raw)
        if not ids:
            return None
        if any(not self._is_valid_sticker_id(sid) for sid in ids):
            return None
        return ids

    def _get_bot_names(self) -> list[str]:
        """获取已配置的机器人名称列表。"""
        try:
            manager = getattr(self.giftia, "bot_config_manager", None)
            if manager is None:
                return []
            return [
                str(bot.get("name") or "").strip()
                for bot in manager.load_bots()
                if isinstance(bot, dict) and str(bot.get("name") or "").strip()
            ]
        except Exception as e:
            logger.warning(f"[Giftia API] 获取机器人列表失败: {e}")
            return []

    def _sticker_locks(self):
        """返回按 sticker_id 分片的锁字典，与自动收藏链路共用以避免并发写冲突。"""
        return getattr(self.giftia, "sticker_locks", None)

    # ── 列表与筛选项 ──────────────────────────────────────────────────────

    async def get_stickers(self):
        """分页获取表情包列表，支持分类/标签/关键词/机器人筛选。"""
        try:
            # 非法的分页参数回落到默认值，而不是让整个列表请求失败
            page = optional_int(request.query.get("page"), default=1, min_value=1) or 1
            limit = optional_int(request.query.get("limit"), default=12, min_value=1) or 12
            limit = min(100, limit)
            category = (request.query.get("category") or "").strip()
            tag = (request.query.get("tag") or "").strip()
            search = (request.query.get("search") or "").strip()
            bot_name = (request.query.get("bot_name") or "").strip()
            sort = (request.query.get("sort") or "created_desc").strip()

            db = self.giftia.db

            # 按机器人筛选：先取该 bot 收藏的 ID 集合，再在其中查询
            scoped_ids = None
            if bot_name:
                scoped_ids = await db.get_sticker_bot(bot_name)

            items, total = await db.query_stickers(
                page=page,
                limit=limit,
                category=category,
                tag=tag,
                search=search,
                sticker_ids=scoped_ids,
                sort=sort,
            )

            # 一次性反查归属，避免逐条查询
            bot_map = await db.get_all_bot_sticker_map()

            for item in items:
                sticker_id = item["sticker_id"]
                item["bot_names"] = bot_map.get(sticker_id, [])
                image_path = self._find_sticker_image(sticker_id, item.get("filename", ""))
                item["has_file"] = image_path is not None
                try:
                    item["file_size"] = image_path.stat().st_size if image_path else 0
                except OSError:
                    item["file_size"] = 0

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
            logger.error(f"[Giftia API] get_stickers error: {e}", exc_info=True)
            return error_response(f"获取表情包列表失败: {str(e)}")

    async def get_sticker_filter_options(self):
        """获取筛选项：分类、标签、机器人列表。"""
        try:
            db = self.giftia.db
            categories = await db.get_sticker_categories()
            tags = await db.get_all_sticker_tags()
            bots = self._get_bot_names()

            has_ai = bool(
                getattr(
                    getattr(self.giftia, "call_llm", None),
                    "image_caption_provider_ids",
                    None,
                )
            )

            return json_response(
                {
                    "status": "success",
                    "data": {
                        "categories": sorted(c for c in categories if c),
                        "tags": tags,
                        "bots": bots,
                        "ai_available": has_ai,
                    },
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] get_sticker_filter_options error: {e}")
            return error_response(f"获取表情包筛选项失败: {str(e)}")

    # ── 文件服务 ─────────────────────────────────────────────────────────

    async def get_sticker_file(self, sticker_id: str):
        """获取表情包原图。"""
        try:
            from astrbot.api.web import file_response

            if not self._is_valid_sticker_id(sticker_id):
                return error_response("无效的 sticker_id 参数", status_code=400)

            sticker = await self.giftia.db.get_sticker_by_id(sticker_id)
            filename = sticker.filename if sticker else ""

            image_path = self._find_sticker_image(sticker_id, filename)
            if not image_path:
                return error_response("表情包文件不存在或已被删除", status_code=404)

            content_type = self._detect_image_content_type(
                image_path, fallback="image/jpeg"
            )
            return file_response(image_path, content_type=content_type)
        except Exception as e:
            logger.error(f"[Giftia API] get_sticker_file error: {e}")
            return error_response(f"获取表情包文件失败: {str(e)}")

    async def get_sticker_file_b64(self, sticker_id: str):
        """获取表情包原图的 base64。"""
        try:
            if not self._is_valid_sticker_id(sticker_id):
                return error_response("无效的 sticker_id 参数", status_code=400)

            sticker = await self.giftia.db.get_sticker_by_id(sticker_id)
            filename = sticker.filename if sticker else ""

            image_path = self._find_sticker_image(sticker_id, filename)
            if not image_path:
                return error_response("表情包文件不存在或已被删除", status_code=404)

            content_type = self._detect_image_content_type(
                image_path, fallback="image/jpeg"
            )
            with open(image_path, "rb") as f:
                file_bytes = f.read()

            return json_response(
                {
                    "status": "success",
                    "base64": base64.b64encode(file_bytes).decode("utf-8"),
                    "content_type": content_type,
                    "file_size": len(file_bytes),
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] get_sticker_file_b64 error: {e}")
            return error_response(f"获取表情包 Base64 失败: {str(e)}")

    async def get_sticker_thumbnail_b64(self, sticker_id: str):
        """获取表情包缩略图 base64，生成后落盘缓存到 stickers/thumbnails/。"""
        try:
            if not self._is_valid_sticker_id(sticker_id):
                return error_response("无效的 sticker_id 参数", status_code=400)

            sticker = await self.giftia.db.get_sticker_by_id(sticker_id)
            filename = sticker.filename if sticker else ""

            image_path = self._find_sticker_image(sticker_id, filename)
            if not image_path:
                return error_response("表情包文件不存在或已被删除", status_code=404)

            content_type = self._detect_image_content_type(
                image_path, fallback="image/jpeg"
            )
            target_file = image_path

            # 缩略图统一以 sticker_id 命名：与源文件扩展名无关，删除时好定位
            thumb_file = self._resolve_sticker_file(sticker_id, is_thumbnail=True)
            if thumb_file:
                try:
                    thumb_file.parent.mkdir(parents=True, exist_ok=True)
                    need_generate = True

                    if thumb_file.exists():
                        # 源文件比缩略图旧则复用缓存
                        if image_path.stat().st_mtime <= thumb_file.stat().st_mtime:
                            need_generate = False
                            target_file = thumb_file
                            with open(thumb_file, "rb") as f:
                                header = f.read(12)
                            if b"WEBP" in header:
                                content_type = "image/webp"
                            elif header.startswith(b"\xff\xd8"):
                                content_type = "image/jpeg"
                            elif header.startswith(b"\x89PNG"):
                                content_type = "image/png"

                    if need_generate:
                        import os

                        from PIL import Image as PILImage

                        with PILImage.open(image_path) as img:
                            # 动图取首帧，避免缩略图过大
                            if getattr(img, "is_animated", False):
                                img.seek(0)
                                img = img.copy()
                            img.thumbnail(THUMBNAIL_SIZE)

                            temp_path = thumb_file.with_name(thumb_file.name + ".tmp")
                            try:
                                img.save(temp_path, format="WEBP")
                                content_type = "image/webp"
                            except Exception:
                                try:
                                    img.save(temp_path, format="PNG")
                                    content_type = "image/png"
                                except Exception:
                                    img.convert("RGB").save(temp_path, format="JPEG")
                                    content_type = "image/jpeg"

                            os.replace(temp_path, thumb_file)
                            target_file = thumb_file
                except Exception as thumb_err:
                    logger.warning(
                        f"[Giftia API] 生成表情包缩略图失败，回退原图 {sticker_id}: {thumb_err}"
                    )
                    target_file = image_path
                    content_type = self._detect_image_content_type(
                        image_path, fallback="image/jpeg"
                    )

            with open(target_file, "rb") as f:
                file_bytes = f.read()

            return json_response(
                {
                    "status": "success",
                    "base64": base64.b64encode(file_bytes).decode("utf-8"),
                    "content_type": content_type,
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] get_sticker_thumbnail_b64 error: {e}")
            return error_response(f"获取表情包缩略图失败: {str(e)}")

    # ── 元数据编辑与删除 ──────────────────────────────────────────────────

    async def update_sticker(self):
        """更新表情包元数据（名称/分类/标签/描述），并同步机器人归属。"""
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return error_response("请求体格式错误，预期 JSON 对象")

            sticker_id = str(body.get("sticker_id") or "").strip()
            if not sticker_id:
                return error_response("缺少 sticker_id 参数")
            if not self._is_valid_sticker_id(sticker_id):
                return error_response("无效的 sticker_id 参数", status_code=400)

            name = str(body.get("name") or "").strip()
            if not name:
                return error_response("表情包名称不能为空")

            category = str(body.get("category") or "").strip()
            description = str(body.get("description") or "").strip()
            tags = self._normalize_tags(body.get("tags"))

            emoji_manager = self.giftia.emoji_manager
            ok = await emoji_manager.update_sticker_meta(
                sticker_id=sticker_id,
                name=name,
                category=category,
                tags=tags,
                description=description,
            )
            if not ok:
                return error_response("表情包记录不存在", status_code=404)

            # 可选：同步归属（bot_names 为期望的完整归属列表）
            bot_names = body.get("bot_names")
            if isinstance(bot_names, list):
                desired = {
                    str(b).strip() for b in bot_names if str(b).strip()
                }
                current_map = await self.giftia.db.get_all_bot_sticker_map()
                existing = set(current_map.get(sticker_id, []))

                for bot in desired - existing:
                    await emoji_manager.link_bot(sticker_id, bot)
                for bot in existing - desired:
                    await emoji_manager.unlink_bot(sticker_id, bot)

            return json_response({"status": "success", "message": "保存表情包成功"})
        except Exception as e:
            logger.error(f"[Giftia API] update_sticker error: {e}", exc_info=True)
            return error_response(f"保存表情包失败: {str(e)}")

    async def delete_sticker(self):
        """删除表情包：数据库记录 + 所有机器人归属 + 磁盘图片与缩略图。"""
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return error_response("请求体格式错误，预期 JSON 对象")

            sticker_id = str(body.get("sticker_id") or "").strip()
            if not sticker_id:
                return error_response("缺少 sticker_id 参数")
            if not self._is_valid_sticker_id(sticker_id):
                return error_response("无效的 sticker_id 参数", status_code=400)

            ok = await self.giftia.emoji_manager.remove_sticker(sticker_id)
            if not ok:
                return error_response("表情包记录不存在", status_code=404)

            return json_response({"status": "success", "message": "删除表情包成功"})
        except Exception as e:
            logger.error(f"[Giftia API] delete_sticker error: {e}", exc_info=True)
            return error_response(f"删除表情包失败: {str(e)}")

    # ── 上传与 AI 分析 ────────────────────────────────────────────────────

    @staticmethod
    async def _collect_upload_files(body: dict | None) -> list[tuple[str, bytes]]:
        """从请求中收集上传的文件，返回 [(filename, bytes), ...]。

        双路径（与 bot_api.upload_signature_voice 一致）：
        优先 multipart 的 request.files，回退 JSON 里的 base64。
        """
        collected: list[tuple[str, bytes]] = []

        # 路径 1: multipart
        if hasattr(request, "files"):
            req_files = getattr(request, "files")
            files_dict = None
            if callable(req_files):
                try:
                    files_dict = req_files()
                except Exception:
                    files_dict = None
            elif isinstance(req_files, dict) or hasattr(req_files, "items"):
                files_dict = req_files

            if files_dict and hasattr(files_dict, "items"):
                for _key, file_item in files_dict.items():
                    candidates = (
                        file_item if isinstance(file_item, (list, tuple)) else [file_item]
                    )
                    for item in candidates:
                        filename = (
                            getattr(item, "filename", "")
                            or getattr(item, "name", "")
                            or "sticker.png"
                        )
                        raw = getattr(item, "body", None) or getattr(
                            item, "content", None
                        )
                        if raw is None:
                            reader = getattr(item, "read", None)
                            if callable(reader):
                                try:
                                    raw = reader()
                                except Exception:
                                    raw = None
                        if raw:
                            collected.append((str(filename), bytes(raw)))

        if collected:
            return collected

        # 路径 2: JSON base64
        if isinstance(body, dict):
            entries = body.get("files")
            if not isinstance(entries, list):
                # 兼容单文件字段
                single = body.get("content")
                entries = (
                    [{"filename": body.get("filename"), "content": single}]
                    if single
                    else []
                )

            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                content = str(entry.get("content") or "")
                if not content:
                    continue
                if "," in content and content.strip().startswith("data:"):
                    content = content.split(",", 1)[1]
                try:
                    raw = base64.b64decode(content, validate=False)
                except Exception:
                    continue
                if raw:
                    collected.append((str(entry.get("filename") or "sticker.png"), raw))

        return collected

    async def _analyze_sticker_bytes(self, image_bytes: bytes, sticker_id: str):
        """调用视觉模型分析表情包，返回 (is_useful, Sticker|None, 错误信息)。"""
        call_llm = getattr(self.giftia, "call_llm", None)
        if call_llm is None or not getattr(call_llm, "image_caption_provider_ids", None):
            return False, None, "未配置图片转述模型 (image_caption_provider_ids)，无法使用 AI 分析"

        try:
            import asyncio

            base64s, _is_animated = await asyncio.to_thread(
                self.giftia.http_manager.handle_image, image_bytes
            )
            if not base64s:
                return False, None, "图片解析失败，无法进行 AI 分析"

            categories = await self.giftia.db.get_sticker_categories()
            is_useful, sticker = await call_llm.call_llm_sticker_analysis(
                image_urls=base64s,
                categories=categories,
                media_id=sticker_id,
                bot_name="",
                group_or_user_id="",
            )
            return is_useful, sticker, ""
        except Exception as e:
            logger.error(f"[Giftia API] AI 分析表情包失败: {e}", exc_info=True)
            return False, None, f"AI 分析失败: {e}"

    async def upload_stickers(self):
        """手动上传表情包（支持批量），可选调用 AI 自动填写元信息。"""
        try:
            body = None
            try:
                body = await request.json()
            except Exception:
                body = None

            files = await self._collect_upload_files(body)
            if not files:
                return error_response("未接收到有效的图片文件")
            if len(files) > MAX_UPLOAD_BATCH:
                return error_response(
                    f"单次最多上传 {MAX_UPLOAD_BATCH} 张，当前 {len(files)} 张"
                )

            body = body if isinstance(body, dict) else {}
            base_name = str(body.get("name") or "").strip()
            category = str(body.get("category") or "").strip()
            description = str(body.get("description") or "").strip()
            tags = self._normalize_tags(body.get("tags"))
            use_ai = bool(body.get("ai_analysis"))
            bind_bots = [
                str(b).strip()
                for b in (body.get("bind_bots") or [])
                if str(b).strip()
            ]

            try:
                from xxhash import xxh3_64_hexdigest
            except ImportError:
                return error_response("缺少 xxhash 依赖，无法计算表情包标识")

            emoji_manager = self.giftia.emoji_manager
            locks = self._sticker_locks()

            results: list[dict] = []
            added = skipped = failed = 0

            for index, (filename, image_bytes) in enumerate(files):
                display_name = filename or f"sticker_{index + 1}"

                if len(image_bytes) > MAX_STICKER_BYTES:
                    failed += 1
                    results.append(
                        {
                            "filename": display_name,
                            "status": "failed",
                            "message": f"文件超过 {MAX_STICKER_BYTES // (1024 * 1024)}MB 上限",
                        }
                    )
                    continue

                if not self._is_supported_image(image_bytes):
                    failed += 1
                    results.append(
                        {
                            "filename": display_name,
                            "status": "failed",
                            "message": "不是受支持的图片格式（仅 PNG/JPEG/GIF/WEBP/BMP）",
                        }
                    )
                    continue

                # 与自动收藏链路同源的哈希，天然去重
                sticker_id = xxh3_64_hexdigest(image_bytes)

                lock = locks[sticker_id] if locks is not None else None
                try:
                    if lock is not None:
                        await lock.acquire()

                    existing = await self.giftia.db.get_sticker_by_id(sticker_id)
                    if existing:
                        # 已存在则不覆盖元数据，只按需补归属
                        for bot in bind_bots:
                            await emoji_manager.link_bot(sticker_id, bot)
                        skipped += 1
                        results.append(
                            {
                                "filename": display_name,
                                "sticker_id": sticker_id,
                                "status": "exists",
                                "name": existing.name,
                                "message": f"已存在同图表情包「{existing.name}」"
                                + ("，已补充机器人归属" if bind_bots else ""),
                            }
                        )
                        continue

                    final_name = base_name
                    final_category = category
                    final_tags = list(tags)
                    final_description = description
                    ai_note = ""

                    if use_ai:
                        is_useful, ai_sticker, err = await self._analyze_sticker_bytes(
                            image_bytes, sticker_id
                        )
                        if err:
                            ai_note = err
                        elif is_useful and ai_sticker:
                            final_name = ai_sticker.name or final_name
                            final_category = ai_sticker.category or final_category
                            final_tags = self._normalize_tags(
                                list(final_tags) + list(ai_sticker.tags or [])
                            )
                            final_description = (
                                ai_sticker.description or final_description
                            )
                            ai_note = "AI 分析完成"
                        else:
                            ai_note = "AI 判定该图不适合作为表情包，已按手填信息保存"

                    if not final_name:
                        stem = Path(str(display_name)).stem
                        final_name = stem or sticker_id[:8]
                    if not final_category:
                        final_category = "未分类"

                    # 批量上传时给个后缀避免同名难以分辨
                    if len(files) > 1 and base_name and final_name == base_name:
                        final_name = f"{base_name}_{index + 1}"

                    await emoji_manager.import_sticker(
                        image_bytes=image_bytes,
                        sticker_id=sticker_id,
                        name=final_name,
                        category=final_category,
                        tags=final_tags,
                        description=final_description,
                    )

                    for bot in bind_bots:
                        await emoji_manager.link_bot(sticker_id, bot)

                    added += 1
                    results.append(
                        {
                            "filename": display_name,
                            "sticker_id": sticker_id,
                            "status": "added",
                            "name": final_name,
                            "category": final_category,
                            "tags": final_tags,
                            "message": ai_note or "上传成功",
                        }
                    )
                except Exception as item_err:
                    logger.error(
                        f"[Giftia API] 上传表情包失败 {display_name}: {item_err}",
                        exc_info=True,
                    )
                    failed += 1
                    results.append(
                        {
                            "filename": display_name,
                            "status": "failed",
                            "message": f"处理失败: {item_err}",
                        }
                    )
                finally:
                    if lock is not None and lock.locked():
                        lock.release()

            summary = f"新增 {added} 张"
            if skipped:
                summary += f"，跳过重复 {skipped} 张"
            if failed:
                summary += f"，失败 {failed} 张"

            return json_response(
                {
                    "status": "success",
                    "message": summary,
                    "data": {
                        "added": added,
                        "skipped": skipped,
                        "failed": failed,
                        "results": results,
                    },
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] upload_stickers error: {e}", exc_info=True)
            return error_response(f"上传表情包失败: {str(e)}")

    async def analyze_sticker(self):
        """对已有表情包重新执行 AI 分析并写回元数据。"""
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return error_response("请求体格式错误，预期 JSON 对象")

            sticker_id = str(body.get("sticker_id") or "").strip()
            if not sticker_id:
                return error_response("缺少 sticker_id 参数")
            if not self._is_valid_sticker_id(sticker_id):
                return error_response("无效的 sticker_id 参数", status_code=400)

            sticker = await self.giftia.db.get_sticker_by_id(sticker_id)
            if not sticker:
                return error_response("表情包记录不存在", status_code=404)

            image_path = self._find_sticker_image(sticker_id, sticker.filename)
            if not image_path:
                return error_response("表情包图片文件缺失，无法分析", status_code=404)

            with open(image_path, "rb") as f:
                image_bytes = f.read()

            is_useful, ai_sticker, err = await self._analyze_sticker_bytes(
                image_bytes, sticker_id
            )
            if err:
                return error_response(err)
            if not (is_useful and ai_sticker):
                return json_response(
                    {
                        "status": "success",
                        "changed": False,
                        "message": "AI 判定该图不适合作为表情包，已保留原有信息",
                    }
                )

            tags = self._normalize_tags(ai_sticker.tags)
            await self.giftia.emoji_manager.update_sticker_meta(
                sticker_id=sticker_id,
                name=ai_sticker.name or sticker.name,
                category=ai_sticker.category or sticker.category,
                tags=tags or sticker.tags,
                description=ai_sticker.description or sticker.description,
            )

            return json_response(
                {
                    "status": "success",
                    "changed": True,
                    "message": "AI 分析完成，已更新表情包信息",
                    "data": {
                        "sticker_id": sticker_id,
                        "name": ai_sticker.name or sticker.name,
                        "category": ai_sticker.category or sticker.category,
                        "tags": tags or sticker.tags,
                        "description": ai_sticker.description or sticker.description,
                    },
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] analyze_sticker error: {e}", exc_info=True)
            return error_response(f"AI 分析表情包失败: {str(e)}")

    # ── 机器人归属 ───────────────────────────────────────────────────────

    async def link_sticker_bot(self):
        """把表情包加入某机器人的收藏。"""
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return error_response("请求体格式错误，预期 JSON 对象")

            sticker_id = str(body.get("sticker_id") or "").strip()
            bot_name = str(body.get("bot_name") or "").strip()
            if not sticker_id or not bot_name:
                return error_response("缺少 sticker_id 或 bot_name 参数")
            if not self._is_valid_sticker_id(sticker_id):
                return error_response("无效的 sticker_id 参数", status_code=400)

            ok = await self.giftia.emoji_manager.link_bot(sticker_id, bot_name)
            if not ok:
                return error_response("表情包记录不存在", status_code=404)

            return json_response(
                {"status": "success", "message": f"已将表情包加入 {bot_name} 的收藏"}
            )
        except Exception as e:
            logger.error(f"[Giftia API] link_sticker_bot error: {e}", exc_info=True)
            return error_response(f"添加归属失败: {str(e)}")

    async def unlink_sticker_bot(self):
        """把表情包从某机器人的收藏里移除。"""
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return error_response("请求体格式错误，预期 JSON 对象")

            sticker_id = str(body.get("sticker_id") or "").strip()
            bot_name = str(body.get("bot_name") or "").strip()
            if not sticker_id or not bot_name:
                return error_response("缺少 sticker_id 或 bot_name 参数")
            if not self._is_valid_sticker_id(sticker_id):
                return error_response("无效的 sticker_id 参数", status_code=400)

            await self.giftia.emoji_manager.unlink_bot(sticker_id, bot_name)
            return json_response(
                {"status": "success", "message": f"已从 {bot_name} 的收藏移除"}
            )
        except Exception as e:
            logger.error(f"[Giftia API] unlink_sticker_bot error: {e}", exc_info=True)
            return error_response(f"移除归属失败: {str(e)}")

    # ── 批量操作 ─────────────────────────────────────────────────────────

    async def batch_stickers(self):
        """批量操作：改分类 / 加标签 / 移除标签 / 删除 / 绑定或解绑机器人。"""
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return error_response("请求体格式错误，预期 JSON 对象")

            action = str(body.get("action") or "").strip()
            sticker_ids = self._valid_ids_or_none(body.get("sticker_ids"))
            if sticker_ids is None:
                return error_response("sticker_ids 为空或包含无效标识", status_code=400)

            db = self.giftia.db
            emoji_manager = self.giftia.emoji_manager

            if action == "set_category":
                category = str(body.get("category") or "").strip()
                if not category:
                    return error_response("分类名称不能为空")
                affected = await db.batch_update_sticker_category(sticker_ids, category)
                emoji_manager.invalidate()
                return json_response(
                    {
                        "status": "success",
                        "affected": affected,
                        "message": f"已把 {affected} 张表情包移到分类「{category}」",
                    }
                )

            if action in ("add_tags", "remove_tags"):
                tags = self._normalize_tags(body.get("tags"))
                if not tags:
                    return error_response("标签不能为空")
                if action == "add_tags":
                    affected = await db.batch_add_sticker_tags(sticker_ids, tags)
                    verb = "追加"
                else:
                    affected = await db.batch_remove_sticker_tags(sticker_ids, tags)
                    verb = "移除"
                emoji_manager.invalidate()
                return json_response(
                    {
                        "status": "success",
                        "affected": affected,
                        "message": f"已为 {affected} 张表情包{verb}标签",
                    }
                )

            if action == "delete":
                deleted = 0
                for sticker_id in sticker_ids:
                    if await emoji_manager.remove_sticker(sticker_id):
                        deleted += 1
                emoji_manager.invalidate()
                return json_response(
                    {
                        "status": "success",
                        "affected": deleted,
                        "message": f"已删除 {deleted} 张表情包",
                    }
                )

            if action in ("link_bot", "unlink_bot"):
                bot_name = str(body.get("bot_name") or "").strip()
                if not bot_name:
                    return error_response("缺少 bot_name 参数")

                affected = 0
                for sticker_id in sticker_ids:
                    if action == "link_bot":
                        if await emoji_manager.link_bot(sticker_id, bot_name):
                            affected += 1
                    else:
                        if await emoji_manager.unlink_bot(sticker_id, bot_name):
                            affected += 1

                verb = "加入" if action == "link_bot" else "移出"
                return json_response(
                    {
                        "status": "success",
                        "affected": affected,
                        "message": f"已将 {affected} 张表情包{verb} {bot_name} 的收藏",
                    }
                )

            return error_response(f"不支持的批量操作: {action}")
        except Exception as e:
            logger.error(f"[Giftia API] batch_stickers error: {e}", exc_info=True)
            return error_response(f"批量操作失败: {str(e)}")

    # ── 分类与标签管理 ────────────────────────────────────────────────────

    async def get_sticker_categories(self):
        """获取分类及其表情包数量。"""
        try:
            stats = await self.giftia.db.get_sticker_category_stats()
            return json_response({"status": "success", "data": {"categories": stats}})
        except Exception as e:
            logger.error(f"[Giftia API] get_sticker_categories error: {e}")
            return error_response(f"获取分类统计失败: {str(e)}")

    async def get_sticker_tags(self):
        """获取标签及其表情包数量。"""
        try:
            stats = await self.giftia.db.get_sticker_tag_stats()
            return json_response({"status": "success", "data": {"tags": stats}})
        except Exception as e:
            logger.error(f"[Giftia API] get_sticker_tags error: {e}")
            return error_response(f"获取标签统计失败: {str(e)}")

    async def rename_sticker_category(self):
        """重命名分类；目标已存在则等价于合并。"""
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return error_response("请求体格式错误，预期 JSON 对象")

            old_name = str(body.get("old_name") or "").strip()
            new_name = str(body.get("new_name") or "").strip()
            if not new_name:
                return error_response("新分类名称不能为空")
            if old_name == new_name:
                return error_response("新旧分类名称相同，无需修改")

            affected = await self.giftia.db.rename_sticker_category(old_name, new_name)
            self.giftia.emoji_manager.invalidate()

            label = old_name or "未分类"
            return json_response(
                {
                    "status": "success",
                    "affected": affected,
                    "message": f"已把「{label}」下 {affected} 张表情包改为「{new_name}」",
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] rename_sticker_category error: {e}", exc_info=True)
            return error_response(f"重命名分类失败: {str(e)}")

    async def rename_sticker_tag(self):
        """重命名标签；目标已存在则在同一张表情包上自动去重（合并）。"""
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return error_response("请求体格式错误，预期 JSON 对象")

            old_tag = str(body.get("old_tag") or "").strip()
            new_tag = str(body.get("new_tag") or "").strip()
            if not old_tag or not new_tag:
                return error_response("标签名称不能为空")
            if old_tag == new_tag:
                return error_response("新旧标签名称相同，无需修改")

            affected = await self.giftia.db.rename_sticker_tag(old_tag, new_tag)
            self.giftia.emoji_manager.invalidate()

            return json_response(
                {
                    "status": "success",
                    "affected": affected,
                    "message": f"已把 {affected} 张表情包的标签「{old_tag}」改为「{new_tag}」",
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] rename_sticker_tag error: {e}", exc_info=True)
            return error_response(f"重命名标签失败: {str(e)}")

    async def delete_sticker_tag(self):
        """从所有表情包上移除某个标签。"""
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return error_response("请求体格式错误，预期 JSON 对象")

            tag = str(body.get("tag") or "").strip()
            if not tag:
                return error_response("标签名称不能为空")

            affected = await self.giftia.db.delete_sticker_tag(tag)
            self.giftia.emoji_manager.invalidate()

            return json_response(
                {
                    "status": "success",
                    "affected": affected,
                    "message": f"已从 {affected} 张表情包上移除标签「{tag}」",
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] delete_sticker_tag error: {e}", exc_info=True)
            return error_response(f"删除标签失败: {str(e)}")
