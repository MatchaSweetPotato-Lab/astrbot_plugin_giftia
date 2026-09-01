"""官方 QQ 语音发送链路的单元测试。

覆盖两处线上故障：

1. **语音被静默丢弃**：官方 QQ 不收 wav，AstrBot 核心发送前要转腾讯 silk
   （`MediaResolver` → `wav_to_tencent_silk` → `pysilk.encode`）。流式合成的 wav
   （fishaudio 的 chunked 响应）头里 data chunk 长度是占位最大值 0x7FFFFF80（约 2GiB），
   核心用标准库 `wave` 照此 `readframes` 直接 `MemoryError`——而 `MemoryError` 不带消息，
   核心那句 `处理语音时出错: {e}` 于是啥也说不出来，语音消失而文本照发。
   核心另外只在采样率不在编码器白名单时才重采样，且用的是原始位宽。
   所以发送前必须按**实际字节数**重算长度并统一成 24kHz/单声道/16bit PCM，
   且规范化本身绝不能抛异常打断回复。
2. **webhook 平台名漏判**：AstrBot 的 Webhook 适配器注册名是 `qq_official_webhook`，
   而旧名单里只有 `qqofficial_webhook`（下划线位置不同），导致所有按平台名字符串
   判定的分支把 webhook 机器人当成通用平台，平台侧动作全部失效。

与 test_gif_convert.py 一致：先把 astrbot 相关模块 stub 进 sys.modules 再导入被测模块。
"""

import ast
import audioop
import logging
import pathlib
import struct
import sys
import tempfile
import types
import unittest
import wave


def _stub_module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = []
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


if "astrbot.api" not in sys.modules:
    _stub_module("astrbot")
    _stub_module("astrbot.api", logger=logging.getLogger("astrbot"))
    _stub_module("astrbot.api.star", StarTools=object)
    _stub_module("astrbot.api.event", AstrMessageEvent=object, MessageChain=object)
    _stub_module(
        "astrbot.api.message_components",
        At=object,
        File=object,
        Image=object,
        Plain=object,
        Record=object,
        Reply=object,
        Video=object,
    )
    _stub_module("astrbot.core")
    _stub_module("astrbot.core.message")
    _stub_module("astrbot.core.message.components", BaseMessageComponent=object)
if "aiohttp" not in sys.modules:
    _stub_module("aiohttp", ClientError=Exception)

from core.utils.audio_norm import (  # noqa: E402
    TARGET_RATE,
    describe_audio,
    normalize_wav,
    read_wav_info,
)
from core.utils.qq_official_action import is_qq_official  # noqa: E402


def write_wav(
    path: str, *, rate: int, channels: int, sampwidth: int, seconds: float = 0.5
) -> str:
    """造一个指定参数的 PCM wav；frames=0 时写出只有头没有数据的空 wav。"""
    frames = int(rate * seconds)
    samples = []
    for i in range(frames):
        value = int(0.4 * (2 ** (sampwidth * 8 - 1) - 1) * ((i % 40) / 40 - 0.5) * 2)
        samples.append(value)
    pcm = b"".join(
        value.to_bytes(sampwidth, "little", signed=True) for value in samples
    )
    if sampwidth == 1:
        # 8bit wav 是无符号的，按 audioop 的约定转一下
        pcm = audioop.lin2lin(b"".join(struct.pack("<h", v) for v in samples), 2, 1)
    if channels == 2:
        pcm = audioop.tostereo(pcm, sampwidth, 1.0, 1.0)
    with wave.open(path, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(sampwidth)
        out.setframerate(rate)
        out.writeframes(pcm)
    return path


# 流式 TTS（fishaudio 的 chunked 响应）实测写下来的占位长度：0x7FFFFF80 字节 ≈ 2GiB
STREAMED_PLACEHOLDER_BYTES = 0x7FFFFF80


def write_lying_header_wav(
    path: str,
    *,
    rate: int = 44100,
    channels: int = 1,
    sampwidth: int = 2,
    seconds: float = 0.5,
    declared_bytes: int = STREAMED_PLACEHOLDER_BYTES,
) -> str:
    """复刻线上故障文件：参数正常，但 data chunk 长度是占位最大值。

    标准库 `wave` 会照着这个假长度去分配内存（`readframes` 直接 MemoryError），
    所以测试里**绝不能**对这种文件调 readframes。
    """
    write_wav(path, rate=rate, channels=channels, sampwidth=sampwidth, seconds=seconds)
    raw = bytearray(pathlib.Path(path).read_bytes())
    marker = raw.index(b"data")
    raw[marker + 4 : marker + 8] = declared_bytes.to_bytes(4, "little")
    pathlib.Path(path).write_bytes(bytes(raw))
    return path


class NormalizeWavTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())

    def _dst(self, name: str = "out_24k.wav") -> str:
        # 刻意放进不存在的子目录，验证会自动建目录
        return str(self.tmp / "nested" / name)

    def _assert_is_target(self, path: str):
        info = read_wav_info(path)
        self.assertIsNotNone(info, f"{path} 不是可解析的 wav")
        self.assertEqual(
            (info.rate, info.channels, info.sampwidth), (TARGET_RATE, 1, 2)
        )
        self.assertGreater(info.frames, 0, "规范化后不能是空音频")

    def test_stereo_44100_downmixed_and_resampled(self):
        src = write_wav(str(self.tmp / "s.wav"), rate=44100, channels=2, sampwidth=2)
        dst = self._dst()
        got, note = normalize_wav(src, dst)
        self.assertEqual(got, dst, note)
        self._assert_is_target(dst)

    def test_32bit_source_is_converted(self):
        """32bit wav 是核心会带着异常位宽进编码器的场景，必须先转成 16bit"""
        src = write_wav(str(self.tmp / "f32.wav"), rate=44100, channels=1, sampwidth=4)
        dst = self._dst("f32_24k.wav")
        got, note = normalize_wav(src, dst)
        self.assertEqual(got, dst, note)
        self._assert_is_target(dst)

    def test_already_target_format_returns_source_untouched(self):
        src = write_wav(
            str(self.tmp / "ok.wav"), rate=TARGET_RATE, channels=1, sampwidth=2
        )
        dst = self._dst("ok_24k.wav")
        got, note = normalize_wav(src, dst)
        self.assertEqual(got, src)
        self.assertFalse(pathlib.Path(dst).exists(), "已达标时不该多写一份文件")
        self.assertIn("已是目标格式", note)

    def test_non_wav_falls_back_without_raising(self):
        """mp3 改名成 .wav 这类文件交给核心的 ffmpeg 分支，这里只能原样放行"""
        src = self.tmp / "fake.wav"
        src.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00fake mp3 payload")
        got, note = normalize_wav(str(src), self._dst("fake_24k.wav"))
        self.assertEqual(got, str(src))
        self.assertIn("非标准 PCM wav", note)

    def test_lying_header_is_detected_and_rewritten(self):
        """线上故障本体：流式 TTS 的 wav 头声明约 2GiB，核心照此分配内存直接 MemoryError"""
        src = write_lying_header_wav(str(self.tmp / "streamed.wav"), seconds=0.4)

        # 标准库会被假头骗到（这就是核心 readframes 炸掉的原因），故不能碰 readframes
        with wave.open(src, "rb") as fooled:
            self.assertEqual(
                fooled.getnframes(), STREAMED_PLACEHOLDER_BYTES // 2, "假头未生效"
            )

        info = read_wav_info(src)
        self.assertEqual(info.frames, int(44100 * 0.4), "应按实际字节数算帧数")
        self.assertTrue(info.header_lies)
        self.assertIn("已按实际字节修正", describe_audio(src))

        dst = self._dst("streamed_24k.wav")
        got, note = normalize_wav(src, dst)
        self.assertEqual(got, dst, note)
        self.assertIn("修正坏 wav 头", note)
        self._assert_is_target(dst)
        # 产物必须能被标准库正常读全（核心走的就是这条路）
        with wave.open(dst, "rb") as fixed:
            self.assertEqual(
                len(fixed.readframes(fixed.getnframes())), fixed.getnframes() * 2
            )

    def test_target_format_with_lying_header_is_still_rewritten(self):
        """参数已达标也不能放过：假头本身就会让核心炸在 readframes 上"""
        src = write_lying_header_wav(
            str(self.tmp / "ok_but_lying.wav"), rate=TARGET_RATE, seconds=0.3
        )
        dst = self._dst("ok_but_lying_24k.wav")
        got, note = normalize_wav(src, dst)
        self.assertEqual(got, dst, note)
        self.assertIn("修正坏 wav 头", note)
        self._assert_is_target(dst)

    def test_zero_frame_wav_falls_back(self):
        src = write_wav(
            str(self.tmp / "empty.wav"), rate=44100, channels=1, sampwidth=2, seconds=0
        )
        got, note = normalize_wav(src, self._dst("empty_24k.wav"))
        self.assertEqual(got, src)
        self.assertIn("不含音频帧", note)

    def test_missing_source_is_tolerated(self):
        missing = str(self.tmp / "nope.wav")
        self.assertIsNone(read_wav_info(missing))
        got, note = normalize_wav(missing, self._dst())
        self.assertEqual(got, missing)
        self.assertIn("非标准 PCM wav", note)

    def test_describe_audio_reports_params_or_head(self):
        src = write_wav(str(self.tmp / "d.wav"), rate=16000, channels=1, sampwidth=2)
        self.assertIn("16000Hz/1ch/16bit", describe_audio(src))
        bogus = self.tmp / "bogus.bin"
        bogus.write_bytes(b"\x00\x01\x02\x03")
        self.assertIn("非标准 PCM wav", describe_audio(str(bogus)))
        self.assertIn("无法读取", describe_audio(str(self.tmp / "missing.bin")))


class StubEvent:
    def __init__(self, platform_name: str):
        self._platform_name = platform_name

    def get_platform_name(self) -> str:
        return self._platform_name


class QQOfficialWebhookMessageEvent:
    """类名带 QQOfficial 的事件替身（真实 webhook 事件类同名前缀）"""

    def get_platform_name(self) -> str:
        return "qq_official_webhook"


class PlatformNameMatchTests(unittest.TestCase):
    def test_all_official_qq_names_recognized(self):
        for name in (
            "qq_official",
            "qq_official_webhook",  # 现行 Webhook 注册名，历史上漏在名单外
            "qqofficial",
            "qqofficial_webhook",
            "qqchannel",
            "QQ_Official_Webhook",
            " qq_official ",
        ):
            with self.subTest(name=name):
                self.assertTrue(is_qq_official(name), f"{name} 应判为官方 QQ")
                self.assertTrue(is_qq_official(StubEvent(name)))

    def test_other_platforms_not_recognized(self):
        for name in ("aiocqhttp", "telegram", "wecom", "", "qq"):
            with self.subTest(name=name):
                self.assertFalse(is_qq_official(name))

    def test_event_class_name_fallback(self):
        """平台名取不到时仍要靠事件类名兜底命中"""

        class QQOfficialSilentEvent(QQOfficialWebhookMessageEvent):
            def get_platform_name(self) -> str:
                return ""

        class SomeOtherEvent:
            def get_platform_name(self) -> str:
                return ""

        self.assertTrue(is_qq_official(QQOfficialWebhookMessageEvent()))
        self.assertTrue(is_qq_official(QQOfficialSilentEvent()))
        self.assertFalse(is_qq_official(SomeOtherEvent()))
        self.assertFalse(is_qq_official(None))


class BuildRecordWiringTests(unittest.TestCase):
    """所有 Record 出口都必须先过平台预处理，漏一条就会重演静默丢语音"""

    def test_build_record_routes_every_return_through_prepare(self):
        source = (
            pathlib.Path(__file__).resolve().parents[1] / "core" / "tts" / "manager.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        build_record = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "build_record"
        )
        record_calls = [
            node
            for node in ast.walk(build_record)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "fromFileSystem"
        ]
        self.assertTrue(record_calls, "build_record 里找不到 Record 构造")
        prepared_vars = {
            target.id
            for node in ast.walk(build_record)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Await)
            and isinstance(node.value.value, ast.Call)
            and isinstance(node.value.value.func, ast.Attribute)
            and node.value.value.func.attr == "prepare_audio_for_platform"
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        self.assertTrue(prepared_vars, "build_record 未调用 prepare_audio_for_platform")
        for call in record_calls:
            self.assertTrue(
                call.args and isinstance(call.args[0], ast.Name),
                "Record 的音频路径应来自变量",
            )
            self.assertIn(
                call.args[0].id,
                prepared_vars,
                "Record 用的路径没经过 prepare_audio_for_platform",
            )


if __name__ == "__main__":
    unittest.main()
