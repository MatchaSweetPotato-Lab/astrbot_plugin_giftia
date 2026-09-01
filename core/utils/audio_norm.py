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

所以交给核心之前先自己把音频修成编码器最保险的形式：**按文件实际字节数**重算 PCM
长度（绝不相信头里的声明），再统一成 24kHz / 单声道 / 16bit PCM 重新写一份头正确的 wav。

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
# 正常 TTS 语音只有几百 KB；上限纯粹是防御坏头/异常大文件把内存吃穿
_MAX_PCM_BYTES = 64 * 1024 * 1024
# audioop.tomono 只处理双声道；lin2lin 支持 1/2/3/4 字节位宽
_MIXABLE_CHANNELS = (1, 2)
_CONVERTIBLE_SAMPWIDTHS = (1, 2, 3, 4)


@dataclass(slots=True)
class WavInfo:
    """wav 参数（`frames` 已按文件实际字节修正，不是头里声明的值）"""

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
        轻则读到一堆空数据，重则直接 `MemoryError`。
        """
        return self.declared_frames != self.frames

    def describe(self) -> str:
        return f"{self.rate}Hz/{self.channels}ch/{self.sampwidth * 8}bit"


def _parse_riff(path: str) -> WavInfo | None:
    """手写 RIFF/WAVE 解析，只认未压缩 PCM，解析不了返回 None。

    刻意不用标准库 `wave`：它信任 data chunk 里声明的长度，而流式生成的 wav
    这个值是占位最大值，`readframes` 会直接 `MemoryError`。这里一律以
    「文件实际剩余字节」为准，并把头里的声明值一并带出来供日志说明。
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
        available = max(0, size - data_offset)
        # 声明值只在「不超过实际可读字节」时才可信
        data_bytes = declared_bytes if 0 < declared_bytes <= available else available
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
    with open(path, "rb") as f:
        f.seek(info.data_offset)
        return f.read(min(info.data_bytes, _MAX_PCM_BYTES))


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
