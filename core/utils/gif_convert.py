"""表情包 GIF 转换：把表情包原图重编码为 GIF 并落盘缓存，供发送侧使用。

**为什么需要**：官方 QQ（qq_official）平台不支持 OneBot 的小图表情包外显
（subType=1），表情包会被当成普通大图发出，在会话窗口里占掉大量空间。统一转成
GIF 后，QQ 客户端会按动图/表情包的方式渲染，观感与体积都接近真表情包。

**为什么落盘而不是内存 base64**：官方 QQ 走的是 AstrBot 原生适配器把图片
*上传* 到富媒体接口，本地文件路径是所有适配器都吃得下的最稳形式。落盘还顺带
让缓存跨重启持久，且 OneBot 侧的 `_msg_chain_to_data` 不用改一行——它本来就
对 Image 调 `convert_to_base64()`，源文件换成 .gif 后自然编码出 GIF base64。

缓存目录是 `stickers/gif_cache/`，位于插件数据目录内，因此天然处于
`path_security.get_allowed_roots()` 的白名单里，不会触发安全拦截告警。
"""

import asyncio
import os
import urllib.parse
from io import BytesIO
from pathlib import Path

from PIL import Image as PILImage

from astrbot.api import logger
from astrbot.api.message_components import Image

from .emoji_manager import GIF_CACHE_SUBDIR, EmojiManager, resolve_sticker_path


class GifConverter:
    """把表情包原图转成 GIF 并缓存到 stickers/gif_cache/。

    所有方法都不抛异常：转换失败一律返回 None / 原消息链，由调用方按原文件发送。
    表情包发送属于对话热路径，绝不能因为一张图转换失败就中断回复。
    """

    # 动图最多保留的帧数（与 astrbot_plugin_stealer 的 MAX_FRAMES 一致）
    MAX_FRAMES = 30
    # 产物体积上限，与 web 层 MAX_STICKER_BYTES 对齐，避免撞平台上传限制
    MAX_OUTPUT_BYTES = 10 * 1024 * 1024

    def __init__(self, emoji_manager: EmojiManager):
        # 只在构造时取一次 stickers_dir——EmojiManager 建好之后它不会再变
        self.stickers_dir = Path(emoji_manager.stickers_dir)
        self.gif_cache_dir = self.stickers_dir / GIF_CACHE_SUBDIR
        try:
            self.gif_cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(f"[Giftia GifConverter] 创建 GIF 缓存目录失败: {e}")

    # ── 判定 ─────────────────────────────────────────────────────────────

    @staticmethod
    def _strip_file_scheme(raw: str) -> str:
        """剥掉 file:// 前缀，兼容 Windows 的 file:///C:/path 形式。

        与 path_security.get_safe_local_media_path 保持同一套解码规则。
        """
        if raw.startswith("file://"):
            raw = urllib.parse.unquote(raw[7:])
            if raw.startswith("/") and len(raw) > 2 and raw[2] == ":":
                raw = raw[1:]
        return raw

    def resolve_sticker_file(self, component) -> Path | None:
        """判断该 Image 组件是否指向 stickers 目录里的一张可转换表情包原图。

        返回原图绝对路径；不适用则返回 None。四种情况判为不适用：
        - 不是本地文件（网络 URL / base64，例如图片缺失时回退到媒体缓存 URL 的表情包）
        - 不在 stickers_dir 内（后台绘图结果、<image url=...> 网络图等，不能误伤）
        - 落在 gif_cache/ 或 thumbnails/ 子目录里（本身就是派生产物）
        - 源文件已经是 GIF（重编码只会走一遍量化并砍帧，纯损失）
        """
        raw = getattr(component, "file", None)
        if not raw:
            return None
        raw_str = self._strip_file_scheme(str(raw).strip())
        if not raw_str or "://" in raw_str:
            return None

        try:
            target = Path(raw_str).resolve()
            base = self.stickers_dir.resolve()
            if not target.is_relative_to(base):
                return None
            # 排除派生产物目录，只认 stickers_dir 根下的原图
            if target.parent != base:
                return None
            if not target.is_file():
                return None
        except (ValueError, OSError):
            return None

        try:
            with open(target, "rb") as f:
                if f.read(4).startswith(b"GIF8"):
                    return None
        except OSError:
            return None

        return target

    # ── 转换与缓存 ────────────────────────────────────────────────────────

    def _cache_path(self, src: Path) -> Path | None:
        """GIF 缓存文件路径，按源文件主名（即 sticker_id）命名。

        复用 resolve_sticker_path 的防穿越校验，避免自行拼路径引入越界风险。
        """
        return resolve_sticker_path(
            self.stickers_dir, f"{src.stem}.gif", subdir=GIF_CACHE_SUBDIR
        )

    async def get_gif_path(self, src: Path) -> Path | None:
        """返回 src 对应的 GIF 缓存文件，首次调用时生成。失败返回 None。"""
        cache_file = self._cache_path(src)
        if cache_file is None:
            return None

        try:
            src_mtime = src.stat().st_mtime
            if cache_file.is_file() and cache_file.stat().st_mtime >= src_mtime:
                return cache_file
        except OSError:
            pass

        try:
            return await asyncio.to_thread(self._convert_to_gif, src, cache_file)
        except Exception as e:
            logger.warning(f"[Giftia GifConverter] 转换 GIF 失败 {src.name}: {e}")
            return None

    def _convert_to_gif(self, src: Path, cache_file: Path) -> Path | None:
        """同步转换实现，只在 to_thread 里调用。

        动图按步长抽帧到 MAX_FRAMES 以内并保留每帧时长；静态图存单帧 GIF。
        一律不改尺寸、不 optimize，避免二次压缩糊掉表情包。
        """
        buf = BytesIO()
        with PILImage.open(src) as im:
            is_animated = bool(getattr(im, "is_animated", False))
            n_frames = int(getattr(im, "n_frames", 1) or 1)

            if is_animated and n_frames > 1:
                target_count = min(n_frames, self.MAX_FRAMES)
                frame_step = max(1, n_frames // target_count)
                frames = []
                durations = []
                for frame_idx in range(0, n_frames, frame_step):
                    if len(frames) >= self.MAX_FRAMES:
                        break
                    im.seek(frame_idx)
                    frames.append(im.convert("RGBA"))
                    durations.append(im.info.get("duration", 100))
                if not frames:
                    return None
                frames[0].save(
                    buf,
                    format="GIF",
                    save_all=True,
                    append_images=frames[1:],
                    duration=durations,
                    loop=0,
                    optimize=False,
                    disposal=2,
                )
            else:
                im.save(buf, format="GIF", optimize=False)

        payload = buf.getvalue()
        if not payload:
            return None
        if len(payload) > self.MAX_OUTPUT_BYTES:
            logger.warning(
                f"[Giftia GifConverter] {src.name} 转 GIF 后为 "
                f"{len(payload) / 1024 / 1024:.2f}MB，超过 "
                f"{self.MAX_OUTPUT_BYTES // (1024 * 1024)}MB 上限，回退原文件发送"
            )
            return None

        # 先写临时文件再原子替换：避免并发发送时读到半截文件
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        temp_path = cache_file.with_name(cache_file.name + ".tmp")
        try:
            with open(temp_path, "wb") as f:
                f.write(payload)
            os.replace(temp_path, cache_file)
        except OSError:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return cache_file

    # ── 发送侧入口 ────────────────────────────────────────────────────────

    async def build_send_chain(self, chain: list) -> list:
        """返回消息链的「发送副本」，其中的表情包原图换成 GIF 缓存文件。

        必须返回新列表而不是就地修改：调用方发完消息后还要用原链调
        message_parser.chain_to_result() 生成入库内容，若原链被换成 GIF，
        入库的 [图片:<sticker_id>] 会变成 GIF 的新哈希，破坏聊天记录与表情包复用。

        一个组件都没换时原样返回入参，避免无谓的列表拷贝。
        """
        if not chain:
            return chain

        replaced: dict[int, Image] = {}
        for index, component in enumerate(chain):
            if not isinstance(component, Image):
                continue
            src = self.resolve_sticker_file(component)
            if src is None:
                continue
            gif_path = await self.get_gif_path(src)
            if gif_path is not None:
                replaced[index] = Image.fromFileSystem(str(gif_path))

        if not replaced:
            return chain
        return [replaced.get(i, comp) for i, comp in enumerate(chain)]
