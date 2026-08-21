import os
import tempfile
import urllib.parse
from pathlib import Path

from astrbot.api import logger

_CACHED_ALLOWED_ROOTS: list[Path] | None = None


def get_allowed_roots() -> list[Path]:
    """获取并缓存允许的安全媒体目录根节点列表。

    避免在媒体解析热点路径上重复执行 import、文件系统 stat 与 resolve 开销。
    """
    global _CACHED_ALLOWED_ROOTS
    if _CACHED_ALLOWED_ROOTS is not None:
        return _CACHED_ALLOWED_ROOTS

    roots: list[Path] = []

    # 1. 系统临时目录
    try:
        tmp = Path(tempfile.gettempdir()).resolve()
        if tmp.exists() and tmp.is_dir():
            roots.append(tmp)
    except Exception as e:
        logger.warning(f"[Giftia Security] 解析系统临时目录失败: {e}")

    for sys_tmp in ("/tmp", "/var/tmp"):
        try:
            p = Path(sys_tmp).resolve()
            if p.exists() and p.is_dir() and p not in roots:
                roots.append(p)
        except Exception as e:
            logger.debug(f"[Giftia Security] 检查系统目录 {sys_tmp} 失败: {e}")

    # 2. AstrBot 插件数据目录 (包含 Giftia 自身 media_cache 以及其它插件如 meme_manager、生图、TTS 生成的媒体)
    try:
        p_data = Path("data/plugin_data").resolve()
        p_data.mkdir(parents=True, exist_ok=True)
        if p_data.is_dir() and p_data not in roots:
            roots.append(p_data)
    except Exception as e:
        logger.warning(f"[Giftia Security] 初始化 AstrBot 插件数据目录失败: {e}")

    # 3. AstrBot 官方临时目录 (data/temp，显式确保目录存在)
    try:
        temp_dir = Path("data/temp").resolve()
        temp_dir.mkdir(parents=True, exist_ok=True)
        if temp_dir.is_dir() and temp_dir not in roots:
            roots.append(temp_dir)
    except Exception as e:
        logger.warning(f"[Giftia Security] 初始化 AstrBot 临时目录失败: {e}")

    _CACHED_ALLOWED_ROOTS = roots
    return roots


def reset_allowed_roots_cache() -> None:
    """重置缓存（主要供单元测试或动态环境变动时使用）。"""
    global _CACHED_ALLOWED_ROOTS
    _CACHED_ALLOWED_ROOTS = None


def get_safe_local_media_path(raw_path: str | Path | None) -> Path | None:
    """安全解析并校验本地媒体文件路径，阻断任意本地文件读取（LFD/SSRF）。

    仅允许读取位于系统临时目录、AstrBot 官方临时目录或插件专属 media_cache 目录中的合法文件。

    :param raw_path: 文件路径字符串或 Path 对象 (可包含 file:// 协议前缀)
    :return: 规范化后的 Path 对象（如安全且文件存在），否则返回 None
    """
    if not raw_path:
        return None

    raw_str = str(raw_path).strip()
    if not raw_str:
        return None

    if raw_str.startswith("file://"):
        raw_str = urllib.parse.unquote(raw_str[7:])
        # 处理 Windows 系统路径: file:///C:/path -> C:/path
        if raw_str.startswith("/") and len(raw_str) > 2 and raw_str[2] == ":":
            raw_str = raw_str[1:]

    try:
        target_path = Path(raw_str).resolve()
    except Exception as e:
        logger.debug(f"[Giftia Security] 路径解析失败: {raw_str}, error: {e}")
        return None

    allowed_roots = get_allowed_roots()

    # 1. 优先校验沙箱边界：不在允许白名单目录内的，立即拦截，绝不探测沙箱外部磁盘
    is_inside_sandbox = False
    for root in allowed_roots:
        try:
            if target_path.is_relative_to(root):
                is_inside_sandbox = True
                break
        except (ValueError, AttributeError):
            pass

    if not is_inside_sandbox:
        logger.warning(f"[Giftia Security] 拦截非安全目录文件读取请求: {target_path}")
        return None

    # 2. 确认在安全沙箱内部后，再检查文件是否存在且为普通文件
    if not target_path.exists() or not target_path.is_file():
        logger.debug(f"[Giftia Security] 目标文件不存在或不是普通文件: {target_path}")
        return None

    return target_path
