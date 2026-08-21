import asyncio
import random
import time
from io import BytesIO
from pathlib import Path

from PIL import Image

from astrbot.api import logger
from astrbot.api.star import StarTools

from ..database.database import Database
from .schemas import BotSticker, Sticker


class EmojiManager:
    def __init__(self, db: Database, random_sticker_count: int = 50):
        self.db = db
        self.random_sticker_count = random_sticker_count

        # 表情包缓存，键是sticker_id，值是Sticker
        self.stickers: dict[str, Sticker] = {}
        self._stickers_loaded: bool = False
        # 机器人的表情包列表缓存，键是bot_name，值是BotSticker
        self.bot_stickers: dict[str, BotSticker] = {}
        # 表情包路径
        self.stickers_dir = StarTools.get_data_dir("astrbot_plugin_giftia") / "stickers"
        self.stickers_dir.mkdir(parents=True, exist_ok=True)

    async def save_sticker_image(self, image_bytes: bytes, sticker_id: str) -> Path:
        """
        处理并保存表情包图片到本地数据目录。
        返回保存后的本地绝对路径。
        """

        def _process_and_save() -> Path:
            ext = ""
            final_bytes = image_bytes

            if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                ext = ".png"
            elif image_bytes.startswith(b"\xff\xd8\xff"):
                ext = ".jpg"
            elif image_bytes.startswith(b"GIF8"):
                ext = ".gif"
            elif image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
                ext = ".webp"
            else:
                try:
                    with Image.open(BytesIO(image_bytes)) as img:
                        img_converted = img.convert("RGB")
                        buf = BytesIO()
                        img_converted.save(buf, format="JPEG", quality=90)
                        final_bytes = buf.getvalue()
                    ext = ".jpg"
                except Exception as e:
                    logger.warning(f"表情包图片格式转换失败，强制存为.jpg: {e}")
                    final_bytes = image_bytes
                    ext = ".jpg"

            local_path = self.stickers_dir / f"{sticker_id}{ext}"
            with open(local_path, "wb") as f:
                f.write(final_bytes)

            return local_path

        return await asyncio.to_thread(_process_and_save)

    def get_sticker_path(self, sticker_id: str) -> Path | None:
        """获取表情包文件的绝对路径"""
        sticker = self.stickers.get(sticker_id)
        if sticker and sticker.filename:
            local_path = self.stickers_dir / sticker.filename
            if local_path.exists():
                return local_path
        return None

    async def add_sticker(
        self, bot_name: str, media_id: str, sticker: Sticker | None = None
    ) -> None:
        """添加表情包"""
        if media_id not in self.stickers:
            if not sticker:
                return
            await self.db.insert_sticker(
                sticker_id=sticker.sticker_id,
                name=sticker.name,
                category=sticker.category,
                tags=sticker.tags,
                description=sticker.description,
                filename=sticker.filename,
            )
            self.stickers[media_id] = sticker

        await self.db.insert_sticker_bot(sticker_id=media_id, bot_name=bot_name)

        bot_sticker = await self.get_sticker(bot_name)
        if media_id not in bot_sticker.sticker_set:
            bot_sticker.sticker_list.append(media_id)
            bot_sticker.sticker_set.add(media_id)
            bot_sticker.timestamp = time.time()

    async def get_sticker(self, bot_name: str) -> BotSticker:
        """获取该机器人的所有表情包"""
        await self.ensure_loaded()

        sticker_data = self.bot_stickers.get(bot_name)
        if sticker_data:
            return sticker_data

        # 从数据库中获取
        sticker_ids = await self.db.get_sticker_bot(bot_name)
        bot_sticker = BotSticker(
            timestamp=time.time(),
            sticker_list=sticker_ids,
            sticker_set=set(sticker_ids),
        )
        self.bot_stickers[bot_name] = bot_sticker
        return bot_sticker

    async def has_sticker(self, bot_name: str, media_id: str) -> bool:
        """快速判断机器人是否已经收集了该表情包"""
        bot_sticker = await self.get_sticker(bot_name)
        return media_id in bot_sticker.sticker_set

    async def get_random_stickers(self, bot_name: str) -> str:
        """动态获取随机的表情包列表，格式化为易读的字符串"""
        bot_sticker = await self.get_sticker(bot_name)
        if not bot_sticker.sticker_list:
            return ""

        sampled_ids = random.sample(
            bot_sticker.sticker_list,
            min(len(bot_sticker.sticker_list), self.random_sticker_count),
        )

        result_lines = []
        for sid in sampled_ids:
            s = self.stickers.get(sid)
            if s:
                tags_str = ", ".join(s.tags) if s.tags else "无"
                line = f"[{s.name}](sticker_id: {s.sticker_id}) - 分类: {s.category}, 标签: {tags_str}"
                result_lines.append(line)

        return "\n".join(result_lines)

    # ── Web 管理面板写操作 ───────────────────────────────────────────────
    #
    # self.stickers 与 self.bot_stickers 是长驻内存缓存：get_random_stickers()
    # 注入提示词、_load_sticker_image() 发图时都直接读缓存。Web 端如果只改数据库
    # 不同步缓存，机器人会继续按旧数据挑图/发图，直到插件重载。
    # 因此所有写操作都必须走下面这些方法。

    async def ensure_loaded(self) -> None:
        """确保全局表情包缓存已从数据库加载"""
        if not self._stickers_loaded:
            all_stickers = await self.db.get_sticker()
            for s in all_stickers:
                self.stickers[s.sticker_id] = s
            self._stickers_loaded = True

    def invalidate(self) -> None:
        """让全部缓存失效，下次访问时重新从数据库加载。

        批量操作（批量改分类/加标签/删除等）影响面广，逐条同步容易漏，
        统一失效更稳妥。
        """
        self.stickers.clear()
        self._stickers_loaded = False
        self.bot_stickers.clear()

    def _resolve_sticker_file(self, filename: str) -> Path | None:
        """把 filename 解析为 stickers 目录内的绝对路径，越界返回 None。"""
        if not filename:
            return None
        try:
            base = self.stickers_dir.resolve()
            target = (base / filename).resolve()
            if target.is_relative_to(base):
                return target
        except (ValueError, OSError):
            return None
        return None

    async def update_sticker_meta(
        self,
        sticker_id: str,
        name: str,
        category: str,
        tags: list[str],
        description: str,
    ) -> bool:
        """更新表情包元数据并同步内存缓存。

        走 db.update_sticker（只改元数据），不用 insert_sticker——后者是全字段
        upsert 且 filename 默认空串，会把图片文件名清空导致发不出图。
        """
        await self.ensure_loaded()
        ok = await self.db.update_sticker(
            sticker_id=sticker_id,
            name=name,
            category=category,
            tags=tags,
            description=description,
        )
        if not ok:
            return False

        existing = self.stickers.get(sticker_id)
        if existing:
            existing.name = name
            existing.category = category
            existing.tags = list(tags)
            existing.description = description
        else:
            refreshed = await self.db.get_sticker_by_id(sticker_id)
            if refreshed:
                self.stickers[sticker_id] = refreshed
        return True

    async def remove_sticker(self, sticker_id: str) -> bool:
        """彻底删除表情包：数据库记录 + 所有机器人的归属引用 + 磁盘图片与缩略图。

        原有的 db.delete_sticker() 只删 stickers 一行，会在 stickers_bot 里
        残留失效 ID、在磁盘上残留孤儿文件。
        """
        await self.ensure_loaded()
        sticker = self.stickers.get(sticker_id) or await self.db.get_sticker_by_id(
            sticker_id
        )
        if not sticker:
            return False

        await self.db.remove_sticker_from_all_bots(sticker_id)
        await self.db.delete_sticker(sticker_id)

        self.stickers.pop(sticker_id, None)
        for bot_sticker in self.bot_stickers.values():
            if sticker_id in bot_sticker.sticker_set:
                bot_sticker.sticker_set.discard(sticker_id)
                bot_sticker.sticker_list[:] = [
                    sid for sid in bot_sticker.sticker_list if sid != sticker_id
                ]
                bot_sticker.timestamp = time.time()

        self._delete_sticker_files(sticker_id, sticker.filename)
        return True

    def _delete_sticker_files(self, sticker_id: str, filename: str) -> None:
        """删除表情包图片与其缩略图，失败只记日志不打断流程。

        缩略图由 Web 层以 sticker_id 命名（见 StickerApi.get_sticker_thumbnail_b64），
        与源文件扩展名无关，所以这里按 sticker_id 定位。
        """
        for candidate, is_thumb in ((filename, False), (sticker_id, True)):
            if not candidate:
                continue
            target = self._resolve_sticker_file(
                f"thumbnails/{candidate}" if is_thumb else candidate
            )
            if target and target.is_file():
                try:
                    target.unlink()
                except OSError as e:
                    logger.warning(
                        f"[Giftia EmojiManager] 删除表情包文件失败 {target.name}: {e}"
                    )

    async def link_bot(self, sticker_id: str, bot_name: str) -> bool:
        """把已有表情包加入某机器人的收藏（幂等）"""
        await self.ensure_loaded()
        if sticker_id not in self.stickers:
            if not await self.db.get_sticker_by_id(sticker_id):
                return False
        await self.add_sticker(bot_name=bot_name, media_id=sticker_id)
        return True

    async def unlink_bot(self, sticker_id: str, bot_name: str) -> bool:
        """把表情包从某机器人的收藏里移除并同步该机器人的缓存"""
        removed = await self.db.delete_sticker_bot(sticker_id, bot_name)

        bot_sticker = self.bot_stickers.get(bot_name)
        if bot_sticker and sticker_id in bot_sticker.sticker_set:
            bot_sticker.sticker_set.discard(sticker_id)
            bot_sticker.sticker_list[:] = [
                sid for sid in bot_sticker.sticker_list if sid != sticker_id
            ]
            bot_sticker.timestamp = time.time()
            return True
        return removed

    async def import_sticker(
        self,
        image_bytes: bytes,
        sticker_id: str,
        name: str,
        category: str,
        tags: list[str],
        description: str,
    ) -> Sticker:
        """手动导入一张表情包：落盘 + 入库 + 写缓存，返回入库后的 Sticker。

        调用方负责先算好 sticker_id（与自动收藏链路同源的 xxh3 图片哈希），
        以及判断是否已存在。
        """
        await self.ensure_loaded()
        local_path = await self.save_sticker_image(image_bytes, sticker_id)

        sticker = Sticker(
            sticker_id=sticker_id,
            name=name,
            category=category,
            tags=list(tags),
            description=description,
            filename=local_path.name,
        )
        await self.db.insert_sticker(
            sticker_id=sticker.sticker_id,
            name=sticker.name,
            category=sticker.category,
            tags=sticker.tags,
            description=sticker.description,
            filename=sticker.filename,
        )
        self.stickers[sticker_id] = sticker
        return sticker

