"""官方 QQ 语音发送前的音频规范化。

**为什么需要**：官方 QQ 不收 wav。AstrBot 核心在发送 `Record` 前会把音频转成
腾讯 silk（`MediaResolver.to_path(target_format="tencent_silk")` →
`wav_to_tencent_silk` → `pysilk.encode`），这条链路有三个坑：

1. **wav 头声明的长度可能是假的**。流式返回的 TTS（如 fishaudio 的
   `Transfer-Encoding: chunked` 响应）在生成时不知道总长度，头里的 data chunk
   长度会写成占位最大值（实测 0x7FFFFF80，约 2GiB / 21 亿帧），AstrBot 原样落盘。
   核心用标准库 `wave` 读，`wav.readframes(wav.getnframes())` 会照着这个假长度分配
   内存，直接 `MemoryError`——而 `MemoryError` **不带任何消息**。
2. 核心只在采样率不属于编码器白名单（8/12/16/24/32/48kHz）时才重采样，且重采样
   用的是**原始位宽**；24bit / 32bit 的 wav 会带着异常位宽一路进 `pysilk.encode`。
3. 转换失败时核心只打 `logger.error(f"处理语音时出错: {e}")`，既不打异常类型也不打
   traceback。于是坑 1 的表现就是日志里孤零零一句 `处理语音时出错:`，随后
   `record_file_path = None` 静默丢弃语音、只发文本——线上完全无从排查。
   （aiocqhttp 不解析 wav 头，同一个文件在 OneBot 上照发无事，所以只有官方 QQ 中招。）

所以交给核心之前先自己把音频修成编码器最保险的形式：**用文件实际可读字节校验头里的
声明**（声明值多出来或少报的部分无法解释成后续 chunk 时一律按实际字节），再统一成
24kHz / 单声道 / 16bit PCM 重新写一份头正确的 wav。

本模块只依赖标准库（手写 RIFF 解析 + `audioop`），不引入 AstrBot，便于单测覆盖。
`audioop` 在 Python 3.13 已从标准库移除，缺失时本模块整体降级为「原样返回」，
绝不因为规范化失败而拦住语音发送。
"""

from __future__ import annotations

import os
import struct
import wave
from dataclasses import dataclass

try:  # Python 3.13 起 audioop 已从标准库移除
    import audioop
except ImportError:  # pragma: no cover - 取决于运行时 Python 版本
    audioop = None  # type: ignore[assignment]

TARGET_RATE = 24000
TARGET_CHANNELS = 1
TARGET_SAMPWIDTH = 2

_WAVE_FORMAT_PCM = 1
# 正常 TTS 语音只有几百 KB；上限纯粹是防御坏头/异常大文件把内存吃穿。
# 超过上限一律**拒绝规范化并说明原因**，绝不截断——截断后的音频看不出异常，
# 会被当成完整语音发出去。
_MAX_PCM_BYTES = 64 * 1024 * 1024
# audioop.tomono 只处理双声道；lin2lin 支持 1/2/3/4 字节位宽
_MIXABLE_CHANNELS = (1, 2)
_CONVERTIBLE_SAMPWIDTHS = (1, 2, 3, 4)


@dataclass(slots=True)
class WavInfo:
    """wav 参数（`frames` 已按文件实际可读字节校验过，不是头里声明的值）"""

    channels: int
    sampwidth: int
    rate: int
    frames: int
    declared_frames: int
    data_offset: int
    data_bytes: int

    @property
    def is_target(self) -> bool:
        return (
            self.channels == TARGET_CHANNELS
            and self.sampwidth == TARGET_SAMPWIDTH
            and self.rate == TARGET_RATE
        )

    @property
    def header_lies(self) -> bool:
        """头里声明的帧数与实际数据不符（流式 TTS 的占位长度就属于这种）。

        这种文件必须重写：核心用标准库 `wave` 读会照着声明长度分配内存，
        轻则读到一堆空数据，重则直接 `MemoryError`；声明值**少报**时则相反，
        核心只会读出前面一小截，把语音悄悄截短。
        """
        return self.declared_frames != self.frames

    def describe(self) -> str:
        return f"{self.rate}Hz/{self.channels}ch/{self.sampwidth * 8}bit"


def _looks_like_chunk_header(raw: bytes, remaining: int) -> bool:
    """这 8 字节是否像一个 RIFF chunk 头（4 个可打印 ASCII 的 id + 落在文件内的长度）。

    只看 id 会有约 2% 的误判率（PCM 采样点恰好四个字节都落在可打印区间），
    所以把声明长度一并校验：真 chunk 的长度不会超出文件剩余部分。
    """
    if len(raw) < 8 or not all(0x20 <= b < 0x7F for b in raw[:4]):
        return False
    return int.from_bytes(raw[4:8], "little") <= remaining


def _resolve_data_bytes(f, declared_bytes: int, data_offset: int, size: int) -> int:
    """确定 data chunk 真正可读的字节数。

    头里声明的长度有两种失真，都不能信：
    - **多报**：流式 TTS 写的占位最大值（实测 0x7FFFFF80），照此分配内存会 `MemoryError`；
    - **少报**：写头时长度算错或写入中断，照此读会把语音尾巴悄悄丢掉。

    但「声明值小于剩余字节」还有一种完全合法的情况：data 之后跟着别的 chunk
    （LIST/INFO/id3 之类的元数据）。这时声明值才是对的，多出来的字节是元数据，
    当成 PCM 读进去会在音频末尾拼上一段噪音。所以只在「紧随其后的字节确实像下一个
    chunk 头」时才承认声明值，其余一律以文件实际可读字节为准。
    """
    available = max(0, size - data_offset)
    if declared_bytes <= 0 or declared_bytes >= available:
        return available
    tail = data_offset + declared_bytes + (declared_bytes & 1)
    if tail + 8 > size:
        # 余下的字节连一个 chunk 头都放不下，说明声明值是少报的
        return available
    try:
        f.seek(tail)
        if _looks_like_chunk_header(f.read(8), size - tail - 8):
            return declared_bytes
    except OSError:  # pragma: no cover - 读尾部失败时按实际字节处理
        pass
    return available


def _parse_riff(path: str) -> WavInfo | None:
    """手写 RIFF/WAVE 解析，只认未压缩 PCM，解析不了返回 None。

    刻意不用标准库 `wave`：它无条件信任 data chunk 里声明的长度，而流式生成的 wav
    这个值是占位最大值，`readframes` 会直接 `MemoryError`。这里用文件实际可读字节
    校验声明值（见 `_resolve_data_bytes`），并把声明值一并带出来供日志说明。
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            header = f.read(12)
            if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                return None

            fmt: tuple[int, int, int] | None = None
            data_offset = 0
            declared_bytes = 0
            pos = 12
            while pos + 8 <= size:
                f.seek(pos)
                chunk = f.read(8)
                if len(chunk) < 8:
                    break
                chunk_id = chunk[:4]
                chunk_size = int.from_bytes(chunk[4:8], "little")
                body = pos + 8
                if chunk_id == b"fmt ":
                    raw = f.read(16)
                    if len(raw) < 16:
                        return None
                    audio_format, channels, rate, _byte_rate, _align, bits = (
                        struct.unpack("<HHIIHH", raw)
                    )
                    if audio_format != _WAVE_FORMAT_PCM:
                        # 浮点 / 压缩 / extensible 一律交给核心的 ffmpeg 分支
                        return None
                    fmt = (channels, bits // 8, rate)
                elif chunk_id == b"data":
                    data_offset = body
                    declared_bytes = chunk_size
                    break
                pos = body + chunk_size + (chunk_size & 1)

            if not fmt or not data_offset:
                return None

            channels, sampwidth, rate = fmt
            if channels <= 0 or sampwidth <= 0 or rate <= 0:
                return None

            block = channels * sampwidth
            data_bytes = _resolve_data_bytes(f, declared_bytes, data_offset, size)

        data_bytes -= data_bytes % block
        declared_frames = declared_bytes // block if declared_bytes > 0 else 0
        return WavInfo(
            channels=channels,
            sampwidth=sampwidth,
            rate=rate,
            frames=data_bytes // block,
            declared_frames=declared_frames,
            data_offset=data_offset,
            data_bytes=data_bytes,
        )
    except Exception:
        return None


def read_wav_info(path: str) -> WavInfo | None:
    """读 wav 参数，读不动（不存在 / 不是未压缩 PCM wav）就返回 None，不抛异常。"""
    if not path:
        return None
    return _parse_riff(path)


def describe_audio(path: str) -> str:
    """给日志用的音频描述：能解析就报参数（坏头会额外标注），不能就报文件头字节。"""
    info = read_wav_info(path)
    if info:
        text = f"wav {info.describe()}/{info.frames}frames"
        if info.header_lies:
            text += f"（头声明 {info.declared_frames}frames，已按实际字节修正）"
        return text
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return f"无法读取（{type(e).__name__}）"
    head = b""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError:
        pass
    return f"非标准 PCM wav（{size} bytes, head={head!r}）"


def _read_pcm(info: WavInfo, path: str) -> bytes:
    """读出整段 PCM。超过内存上限时抛异常而不是截断——截半的音频发出去看不出异常。"""
    if info.data_bytes > _MAX_PCM_BYTES:
        raise ValueError(
            f"PCM 数据 {info.data_bytes} 字节超过上限 {_MAX_PCM_BYTES}，拒绝截断"
        )
    with open(path, "rb") as f:
        f.seek(info.data_offset)
        return f.read(info.data_bytes)


def normalize_wav(src: str, dst: str) -> tuple[str, str]:
    """把 wav 修成 24kHz / 单声道 / 16bit PCM 且头长度正确的文件。

    返回 `(可用路径, 说明)`：
    - 参数已达标**且**头长度可信时返回 `(src, 原因)`，不做多余拷贝；
    - 参数不达标或头长度是假的（流式 TTS 的占位长度）时重写并返回 `(dst, 变换说明)`；
    - 源文件不是未压缩 PCM wav、或规范化条件不满足时返回 `(src, 原因)`。

    规范化只是保险手段，任何失败都退回原文件，不抛异常。
    """
    info = read_wav_info(src)
    if info is None:
        return src, "非标准 PCM wav，跳过规范化"
    if info.frames <= 0:
        return src, "wav 不含音频帧，跳过规范化"
    if info.is_target and not info.header_lies:
        return src, f"已是目标格式（{info.describe()}）"
    if audioop is None:
        return src, "audioop 不可用（Python 3.13+ 已移除），跳过规范化"
    if info.channels not in _MIXABLE_CHANNELS:
        return src, f"声道数 {info.channels} 无法混音，跳过规范化"
    if info.sampwidth not in _CONVERTIBLE_SAMPWIDTHS:
        return src, f"位宽 {info.sampwidth * 8}bit 无法转换，跳过规范化"

    try:
        pcm = _read_pcm(info, src)
        block = info.channels * info.sampwidth
        pcm = pcm[: len(pcm) - len(pcm) % block]
        if not pcm:
            return src, "读到的 PCM 为空，跳过规范化"

        # 先统一位宽再混音/重采样：audioop 的混音与重采样都按位宽解释样本，
        # 位宽异常时先转 16bit 才能保证后续两步拿到的是正确的采样点。
        width = info.sampwidth
        if width != TARGET_SAMPWIDTH:
            pcm = audioop.lin2lin(pcm, width, TARGET_SAMPWIDTH)
            width = TARGET_SAMPWIDTH
        if info.channels == 2:
            pcm = audioop.tomono(pcm, width, 0.5, 0.5)
        if info.rate != TARGET_RATE:
            pcm, _ = audioop.ratecv(
                pcm, width, TARGET_CHANNELS, info.rate, TARGET_RATE, None
            )
        if not pcm:
            return src, "规范化后 PCM 为空，回退原文件"

        parent = os.path.dirname(dst)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # 用 wave 写出：写入侧会按实际数据量落正确的 chunk 长度
        with wave.open(dst, "wb") as out:
            out.setnchannels(TARGET_CHANNELS)
            out.setsampwidth(TARGET_SAMPWIDTH)
            out.setframerate(TARGET_RATE)
            out.writeframes(pcm)

        note = f"{info.describe()} → {TARGET_RATE}Hz/1ch/16bit"
        if info.header_lies:
            note = (
                f"修正坏 wav 头（头声明 {info.declared_frames}frames，"
                f"实际 {info.frames}frames）并转 {note}"
            )
        return dst, note
    except Exception as e:
        return src, f"规范化失败（{type(e).__name__}: {e}），按原文件发送"
