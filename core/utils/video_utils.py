import os
import re
import shutil
import asyncio
import logging
import aiohttp

logger = logging.getLogger(__name__)

# 视频转述核心体积阈值常量
VIDEO_AUTO_COMPRESS_THRESHOLD_BYTES = 5 * 1024 * 1024   # 5 MB：超过此大小即主动触发 ffmpeg 720p 智能压制，防止高码率膨胀
VIDEO_MAX_PAYLOAD_BYTES = 20 * 1024 * 1024             # 20 MB：发往视觉模型的最终 Base64 请求体安全上限


def check_ffmpeg_available() -> bool:
    """检查 ffmpeg 是否可用"""
    if shutil.which("ffmpeg"):
        return True
    try:
        import static_ffmpeg
        static_ffmpeg.add_paths()
        return shutil.which("ffmpeg") is not None
    except ImportError:
        pass
    return False


def format_file_size(size_bytes: int) -> str:
    """格式化字节大小为人类可读格式 (e.g., 12.5MB)"""
    if not size_bytes or size_bytes <= 0:
        return ""
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f}MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f}GB"


def format_duration(seconds: float) -> str:
    """格式化秒数为人类可读格式 (e.g., 45s 或 01:15)"""
    if not seconds or seconds <= 0:
        return ""
    sec = int(seconds)
    if sec < 60:
        return f"{sec}s"
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _scan_media_info_dict(data: dict) -> tuple[int, float, str]:
    """深度扫描字典查找 file_size、duration 以及 url"""
    f_size = 0
    dur = 0.0
    found_url = ""
    stack = [data]
    while stack:
        curr = stack.pop()
        if isinstance(curr, dict):
            # 匹配 URL
            if not found_url:
                for k in ("url", "file_url", "src"):
                    v = curr.get(k)
                    if isinstance(v, str) and (v.startswith("http://") or v.startswith("https://") or v.startswith("file://")):
                        found_url = v
                        break
            # 匹配体积
            if not f_size:
                for k in ("file_size", "size", "file_bytes", "fileSize", "file_size_bytes"):
                    v = curr.get(k)
                    if v and str(v).isdigit() and int(v) > 0:
                        f_size = int(v)
                        break
            # 匹配时长
            if not dur:
                for k in ("duration", "length", "seconds", "video_duration"):
                    v = curr.get(k)
                    if v is not None:
                        try:
                            val = float(v)
                            if val > 0:
                                dur = val
                                break
                        except (ValueError, TypeError):
                            pass
            for v in curr.values():
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(curr, list):
            for item in curr:
                if isinstance(item, (dict, list)):
                    stack.append(item)
    return f_size, dur, found_url


def _parse_cq_code_video(raw_str: str) -> tuple[int, float, str]:
    """从 OneBot CQ 码字符串提取 [CQ:video,...] 参数"""
    f_size = 0
    dur = 0.0
    url = ""
    if not raw_str or "[CQ:video" not in raw_str:
        return f_size, dur, url

    match = re.search(r"\[CQ:video,([^\]]+)\]", raw_str)
    if match:
        params_str = match.group(1)
        params = dict(re.findall(r"([a-zA-Z0-9_]+)=([^,\s\]]+)", params_str))
        if "url" in params and (params["url"].startswith("http") or params["url"].startswith("file")):
            url = params["url"]
        if "file_size" in params and params["file_size"].isdigit():
            f_size = int(params["file_size"])
        elif "size" in params and params["size"].isdigit():
            f_size = int(params["size"])
        if "duration" in params:
            try:
                dur = float(params["duration"])
            except ValueError:
                pass
    return f_size, dur, url


async def _fetch_onebot_file_api(event, file_id: str) -> tuple[str, int, float]:
    """通过 OneBot v11 API (get_file) 主动查询视频信息"""
    if not event or not hasattr(event, "bot") or not event.bot:
        return "", 0, 0.0

    url = ""
    file_size = 0
    duration = 0.0

    clean_id = file_id.strip()
    if not clean_id:
        return url, file_size, duration

    try:
        for key in ("file_id", "file"):
            try:
                res = await event.bot.call_action("get_file", **{key: clean_id})
                if res and isinstance(res, dict):
                    data = res.get("data") if "data" in res else res
                    if isinstance(data, dict):
                        file_path = data.get("file") or data.get("path") or ""
                        u = data.get("url") or ""
                        sz = int(data.get("file_size") or data.get("size") or 0)
                        
                        if file_path and os.path.isfile(file_path):
                            file_size = os.path.getsize(file_path)
                            url = file_path
                        elif u:
                            url = u
                        if sz > 0 and not file_size:
                            file_size = sz
                        
                        if file_size > 0 or url:
                            break
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"[VideoUtils] OneBot get_file API 调用跳过: {e}")

    return url, file_size, duration


async def _fetch_http_video_size(url: str) -> tuple[int, float]:
    """通过 HTTP HEAD 及 Range 请求高效获取远程视频大小"""
    file_size = 0
    duration = 0.0
    try:
        async with aiohttp.ClientSession() as session:
            # 1. 尝试 HEAD 请求
            try:
                async with session.head(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                    if resp.status == 200:
                        cl = resp.headers.get("Content-Length")
                        if cl and cl.isdigit():
                            file_size = int(cl)
            except Exception:
                pass

            # 2. 如果 HEAD 拿不到 Content-Length，发送 Range: bytes=0-0
            if not file_size:
                headers = {"Range": "bytes=0-0"}
                async with session.get(url, headers=headers, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                    if resp.status in (200, 206):
                        cr = resp.headers.get("Content-Range")
                        if cr and "/" in cr:
                            total_str = cr.split("/")[-1].strip()
                            if total_str.isdigit():
                                file_size = int(total_str)
                        if not file_size:
                            cl = resp.headers.get("Content-Length")
                            if cl and cl.isdigit() and resp.status == 200:
                                file_size = int(cl)
    except Exception as e:
        logger.debug(f"[VideoUtils] 获取远程 HTTP 视频大小失败: {e}")

    return file_size, duration


async def _probe_duration_fast(url_or_path: str) -> float:
    """
    使用 ffprobe / ffmpeg 在不下载视频全量文件的情况下探针解析视频 Header 中的时长 (秒)。
    """
    if not url_or_path or not check_ffmpeg_available():
        return 0.0

    # 1. 优先尝试 ffprobe
    ffprobe_bin = shutil.which("ffprobe")
    if ffprobe_bin:
        cmd = [
            ffprobe_bin,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            url_or_path
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
            if proc.returncode == 0 and stdout:
                val = stdout.decode().strip()
                return float(val)
        except Exception as e:
            logger.debug(f"[VideoUtils] ffprobe 探针时长异常: {e}")

    # 2. 回退使用 ffmpeg -i 解析 stderr 中的 Duration
    ffmpeg_bin = shutil.which("ffmpeg")
    if ffmpeg_bin:
        cmd = [ffmpeg_bin, "-i", url_or_path]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=3.0)
            err_text = stderr.decode(errors="ignore")
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err_text)
            if m:
                hours, minutes, seconds = m.groups()
                total_sec = float(hours) * 3600 + float(minutes) * 60 + float(seconds)
                return total_sec
        except Exception as e:
            logger.debug(f"[VideoUtils] ffmpeg 探针时长异常: {e}")

    return 0.0


async def get_remote_video_info(
    url: str | None = None,
    file_name: str | None = None,
    path: str | None = None,
    event=None,
) -> tuple[int, float]:
    """
    针对 OneBot 及主流平台强化的视频元数据 (file_size, duration) 提取。
    顺序: 本地文件解析 -> OneBot API (get_file) -> CQ码/Raw Message 深度解析 -> HTTP Range 请求 -> FFprobe/FFmpeg Header探针
    """
    file_size = 0
    duration = 0.0

    candidates = []
    for c in (path, url, file_name):
        if isinstance(c, str) and c.strip():
            candidates.append(c.strip())

    # 1. 本地文件直接解析
    local_target_path = ""
    for cand in candidates:
        clean_path = cand.replace("file://", "") if cand.startswith("file://") else cand
        try:
            if os.path.isfile(clean_path):
                file_size = os.path.getsize(clean_path)
                local_target_path = clean_path
                break
        except Exception:
            pass

    # 2. OneBot API 主动查询 (针对 NapCat / Lagrange / Go-CQHTTP)
    if (not file_size or not duration) and event and hasattr(event, "bot") and event.bot:
        file_id_candidate = file_name or url or ""
        if file_id_candidate and not file_id_candidate.startswith("http"):
            api_url, api_size, api_dur = await _fetch_onebot_file_api(event, file_id_candidate)
            if api_size:
                file_size = api_size
            if api_dur:
                duration = api_dur
            if api_url and api_url not in candidates:
                candidates.append(api_url)

    # 3. 深度从 raw_message / CQ 码提取
    if event and hasattr(event, "message_obj") and event.message_obj:
        raw_msg = getattr(event.message_obj, "raw_message", None)
        if raw_msg:
            # CQ 码解析
            if isinstance(raw_msg, str):
                cq_size, cq_dur, cq_url = _parse_cq_code_video(raw_msg)
                if not file_size and cq_size:
                    file_size = cq_size
                if not duration and cq_dur:
                    duration = cq_dur
                if cq_url and cq_url not in candidates:
                    candidates.append(cq_url)
            # 字典格式解析
            elif isinstance(raw_msg, dict):
                raw_size, raw_dur, raw_url = _scan_media_info_dict(raw_msg)
                if not file_size and raw_size:
                    file_size = raw_size
                if not duration and raw_dur:
                    duration = raw_dur
                if raw_url and raw_url not in candidates:
                    candidates.append(raw_url)

    # 4. 通过 HTTP HEAD/Range 请求获取文件大小
    target_http_url = ""
    for cand in candidates:
        if isinstance(cand, str) and (cand.startswith("http://") or cand.startswith("https://")):
            target_http_url = cand
            break

    if not file_size and target_http_url:
        http_size, http_dur = await _fetch_http_video_size(target_http_url)
        if not file_size and http_size:
            file_size = http_size
        if not duration and http_dur:
            duration = http_dur

    # 5. 如果没有拿到时长，使用 ffprobe/ffmpeg Header 探针快速抓取时长
    probe_target = local_target_path or target_http_url
    if not duration and probe_target:
        duration = await _probe_duration_fast(probe_target)

    return int(file_size), float(duration)


async def clip_video_ffmpeg(
    input_path: str,
    start_time: int,
    duration: int,
    output_path: str,
) -> bool:
    """
    使用 ffmpeg 流拷贝 (-c copy) 秒级截取视频片段。
    """
    if not check_ffmpeg_available():
        logger.warning("[VideoUtils] ffmpeg 未找到，无法执行视频切片。")
        return False

    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-ss", str(max(0, start_time)),
        "-i", input_path,
        "-t", str(max(1, duration)),
        "-c", "copy",
        "-y",
        output_path,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return True
        else:
            logger.error(f"[VideoUtils] ffmpeg 切片失败, exit_code={proc.returncode}, err={stderr.decode(errors='ignore')}")
            return False
    except Exception as e:
        logger.error(f"[VideoUtils] 执行 ffmpeg 异常: {e}")
        return False


async def compress_video_ffmpeg(
    input_path: str,
    output_path: str,
    max_width: int = 720,
    crf: int = 28,
    preset: str = "fast",
) -> bool:
    """
    使用 ffmpeg 将高码率/大体积视频智能压缩（等比缩小至 720p 并使用 H.264 CRF 28 与 AAC 64k 音频压制），
    防止 Base64 数据流过大引发 HTTP 413 Payload Too Large。
    """
    if not check_ffmpeg_available():
        logger.warning("[VideoUtils] ffmpeg 未找到，无法执行视频压缩。")
        return False

    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-i", input_path,
        "-vf", f"scale='min({max_width},iw)':-2",
        "-c:v", "libx264",
        "-crf", str(crf),
        "-preset", preset,
        "-c:a", "aac",
        "-b:a", "64k",
        "-movflags", "+faststart",
        "-y",
        output_path,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return True
        else:
            logger.error(f"[VideoUtils] ffmpeg 压缩失败, exit_code={proc.returncode}, err={stderr.decode(errors='ignore')}")
            return False
    except Exception as e:
        logger.error(f"[VideoUtils] 执行 ffmpeg 压缩异常: {e}")
        return False
