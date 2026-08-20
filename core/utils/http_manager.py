import base64
import ssl
import urllib.parse
from datetime import datetime
from io import BytesIO
from pathlib import Path

from aiohttp import (
    ClientConnectorCertificateError,
    ClientConnectorSSLError,
    ClientSession,
    ClientTimeout,
)
from PIL import Image

from astrbot.api import logger
from astrbot.core import AstrBotConfig


from .path_security import get_safe_local_media_path


class HttpManager:
    def __init__(self, config: AstrBotConfig):
        self.session = ClientSession(timeout=ClientTimeout(connect=30, total=60))
        self.config = config

    async def download_media(self, url: str) -> bytes:
        """下载媒体文件"""
        if not url or not isinstance(url, str):
            return b""

        # 如果是本地文件路径或 file://，必须通过安全沙箱校验后方可从本地读取
        if not url.startswith("http://") and not url.startswith("https://"):
            safe_path = get_safe_local_media_path(url)
            if not safe_path:
                logger.warning(f"[Giftia Security] 拦截不安全的媒体路径读取请求: {url}")
                return b""
            try:
                return safe_path.read_bytes()
            except Exception as e:
                logger.error(f"从本地读取媒体文件失败: {e}, Path: {safe_path}")
                return b""

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        }
        try:
            parsed = urllib.parse.urlparse(url)
            host = parsed.netloc.lower()
            if any(d in host for d in ("qpic.cn", "qq.com", "gtimg.cn", "qlogo.cn")):
                headers["Referer"] = "https://im.qq.com/"
            elif "pximg.net" in host or "pixiv.net" in host:
                headers["Referer"] = "https://www.pixiv.net/"
            elif "bilibili.com" in host or "hdslb.com" in host:
                headers["Referer"] = "https://www.bilibili.com/"
            elif "sinaimg.cn" in host or "weibo.com" in host:
                headers["Referer"] = "https://weibo.com/"
            elif parsed.scheme and parsed.netloc:
                headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
        except Exception:
            pass

        for attempt in range(3):
            try:
                async with self.session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        return await resp.read()

                    # 识别 Cloudflare 人机验证挑战（5秒盾/Turnstile），优雅跳过缓存并避免重复重试刷屏
                    if (
                        resp.headers.get("Cf-Mitigated") == "challenge"
                        or (
                            resp.status == 403
                            and resp.headers.get("Server", "").lower() == "cloudflare"
                        )
                    ):
                        logger.warning(
                            f"[Giftia] 目标媒体站点开启了 Cloudflare 5秒盾/人机验证防护，跳过本地缓存: URL={url}"
                        )
                        return b""

                    logger.error(
                        f"下载媒体文件失败: {resp.status}，retry: {attempt + 1} times, URL: {url}"
                    )
            except (
                ClientConnectorSSLError,
                ClientConnectorCertificateError,
            ) as ssl_err:
                logger.warning(
                    f"SSL 证书验证失败 ({ssl_err})，将尝试临时关闭 SSL 验证重新下载: {url}"
                )
                ssl_context = ssl.create_default_context()
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
                try:
                    async with self.session.get(
                        url, ssl=ssl_context, headers=headers
                    ) as resp:
                        if resp.status == 200:
                            return await resp.read()
                        else:
                            logger.error(
                                f"下载媒体文件失败(无SSL): {resp.status}，retry: {attempt + 1} times, URL: {url}"
                            )
                except Exception as inner_e:
                    logger.error(f"下载媒体文件失败(无SSL重试): {inner_e}, URL={url}")
            except Exception as e:
                logger.error(f"下载媒体文件失败: {e}，retry: {attempt + 1} times, URL={url}")
        return b""

    @staticmethod
    def handle_image(image_bytes: bytes, max_frames: int = 8) -> tuple[list[str], bool]:
        try:
            results = []
            with Image.open(BytesIO(image_bytes)) as img:
                is_animated = getattr(img, "is_animated", False)
                if is_animated:
                    total_frames = getattr(img, "n_frames", 1)
                    if total_frames <= max_frames:
                        frame_indices = list(range(total_frames))
                    else:
                        frame_indices = [
                            int(i * (total_frames - 1) / (max_frames - 1))
                            for i in range(max_frames)
                        ]
                    for idx in frame_indices:
                        img.seek(idx)
                        # 使用副本进行转换，不破坏原 img 对象的帧索引
                        frame = img.convert("RGB")
                        buf = BytesIO()
                        frame.save(buf, format="JPEG", quality=90)
                        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
                        results.append("base64://" + b64)
                else:
                    frame = img.convert("RGB")
                    buf = BytesIO()
                    frame.save(buf, format="JPEG", quality=90)
                    results.append(
                        "base64://" + base64.b64encode(buf.getvalue()).decode("utf-8")
                    )

                return results, is_animated
        except Exception as e:
            logger.warning(f"图片处理失败: {e}")
            return [], False

    @staticmethod
    def handle_audio(audio_bytes: bytes) -> list[str]:
        """处理语音"""
        try:
            results = []
            with open("temp.silk", "wb") as f:
                f.write(audio_bytes)
            return results
        except Exception as e:
            logger.warning(f"语音处理失败: {e}")
            return []

    async def upload_file(self, file_path: Path) -> bool:
        """上传文件到R2"""
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            remote_file_name = f"{timestamp}_giftia.sqlite"
            with open(file_path, "rb") as f:
                async with self.session.put(
                    f"{self.config.get('r2_config', {}).get('r2_base_url', '')}/{remote_file_name}",
                    data=f,
                    headers={
                        "X-Auth-Token": self.config.get("r2_config", {}).get(
                            "r2_auth_token", ""
                        ),
                        "Content-Type": "application/x-sqlite3",
                    },
                ) as resp:
                    if resp.status == 200:
                        logger.info(f"文件成功备份至 R2: {remote_file_name}")
                        return True
                    else:
                        body = await resp.text()
                        logger.error(f"R2 响应错误 (状态码 {resp.status}): {body}")
                        return False
        except Exception as e:
            logger.error(f"备份文件上传到R2失败: {e}")
            return False

    async def close_session(self) -> None:
        """关闭客户端会话"""
        if self.session and not self.session.closed:
            await self.session.close()
