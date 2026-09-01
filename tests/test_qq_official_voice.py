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

本模块**不导入 `audioop`**（Python 3.13 已移除）：被测模块在 `audioop` 缺失时整体降级
为「原样返回」，测试也必须能在这种环境下收集并跑完解析相关的用例，只跳过真正需要
重采样的那几条。造 wav 的辅助函数因此手写 PCM，不借 `audioop`。
"""

import ast
import logging
import pathlib
import sys
import tempfile
import types
import unittest
import wave
from unittest import mock


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

from core.utils import audio_norm  # noqa: E402
from core.utils.audio_norm import (  # noqa: E402
    TARGET_RATE,
    describe_audio,
    normalize_wav,
    read_wav_info,
)
from core.utils.qq_official_action import is_qq_official  # noqa: E402

# audioop 缺失（Python 3.13+）时规范化整体降级为原样返回，重写类用例不适用
requires_audioop = unittest.skipUnless(
    audio_norm.audioop is not None,
    "audioop 已从 Python 3.13 标准库移除，规范化降级为原样返回",
)


def write_wav(
    path: str, *, rate: int, channels: int, sampwidth: int, seconds: float = 0.5
) -> str:
    """造一个指定参数的 PCM wav；frames=0 时写出只有头没有数据的空 wav。"""
    frames = int(rate * seconds)
    peak = 2 ** (sampwidth * 8 - 1) - 1
    pcm = bytearray()
    for i in range(frames):
        value = int(0.4 * peak * ((i % 40) / 40 - 0.5) * 2)
        if sampwidth == 1:
            # 8bit wav 按 RIFF 约定是无符号的，0x80 为静音中点
            sample = (value + 128).to_bytes(1, "little")
        else:
            sample = value.to_bytes(sampwidth, "little", signed=True)
        pcm += sample * channels  # 左右声道同信号
    with wave.open(path, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(sampwidth)
        out.setframerate(rate)
        out.writeframes(bytes(pcm))
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
    """改写 data chunk 的声明长度，默认复刻线上故障文件（占位最大值 ≈ 2GiB）。

    标准库 `wave` 会照着这个假长度去分配内存（`readframes` 直接 MemoryError），
    所以测试里**绝不能**对这种文件调 readframes。传入较小的 `declared_bytes`
    即可复刻另一个方向的失真：声明值少报，照此读会把语音尾巴悄悄丢掉。
    """
    write_wav(path, rate=rate, channels=channels, sampwidth=sampwidth, seconds=seconds)
    raw = bytearray(pathlib.Path(path).read_bytes())
    marker = raw.index(b"data")
    raw[marker + 4 : marker + 8] = declared_bytes.to_bytes(4, "little")
    pathlib.Path(path).write_bytes(bytes(raw))
    return path


def append_chunk(path: str, chunk_id: bytes = b"LIST", body: bytes = b"INFOhi") -> str:
    """在 data chunk 之后追加一个合法的元数据 chunk。

    RIFF 允许 data 后面还有 chunk，此时「声明长度 < 剩余字节」是完全正常的，
    多出来的字节是元数据而不是音频——当成 PCM 读进去会在末尾拼上一段噪音。
    """
    raw = bytearray(pathlib.Path(path).read_bytes())
    raw += chunk_id + len(body).to_bytes(4, "little") + body
    if len(body) & 1:
        raw += b"\x00"  # chunk 按偶数字节对齐
    raw[4:8] = (len(raw) - 8).to_bytes(4, "little")  # RIFF 总长度要跟着涨
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

    @requires_audioop
    def test_stereo_44100_downmixed_and_resampled(self):
        src = write_wav(str(self.tmp / "s.wav"), rate=44100, channels=2, sampwidth=2)
        dst = self._dst()
        got, note = normalize_wav(src, dst)
        self.assertEqual(got, dst, note)
        self._assert_is_target(dst)

    @requires_audioop
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

    def test_lying_header_is_detected(self):
        """线上故障本体：流式 TTS 的 wav 头声明约 2GiB，核心照此分配内存直接 MemoryError

        检测这一步不依赖 audioop：manager 靠 `header_lies` 决定「修不了就别发」，
        所以 Python 3.13 上这条也必须成立。
        """
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

    @requires_audioop
    def test_lying_header_is_rewritten(self):
        src = write_lying_header_wav(str(self.tmp / "streamed.wav"), seconds=0.4)
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

    def test_underreported_header_is_not_trusted(self):
        """另一个方向的失真：声明长度少报，照此读会把语音尾巴悄悄丢掉

        标准库 `wave` 只会读出声明的那一小截，所以少报同样必须按实际字节修正。
        """
        seconds, rate = 0.4, 44100
        actual_bytes = int(rate * seconds) * 2
        src = write_lying_header_wav(
            str(self.tmp / "short_claim.wav"),
            rate=rate,
            seconds=seconds,
            declared_bytes=actual_bytes // 2,
        )

        with wave.open(src, "rb") as fooled:
            self.assertEqual(fooled.getnframes(), actual_bytes // 4, "少报头未生效")

        info = read_wav_info(src)
        boundary = pathlib.Path(src).read_bytes()[
            info.data_offset + actual_bytes // 2 :
        ][:4]
        self.assertFalse(
            all(0x20 <= b < 0x7F for b in boundary),
            "用例前提：声明长度之后的字节不能恰好像一个 chunk id",
        )
        self.assertEqual(info.frames, actual_bytes // 2, "少报时也要按实际字节数算帧数")
        self.assertTrue(info.header_lies, "少报同样是坏头，必须重写")

    def test_trailing_metadata_chunk_keeps_declared_length(self):
        """data 之后跟元数据 chunk 是合法的，那些字节不能当音频读进来"""
        seconds, rate = 0.3, TARGET_RATE
        src = append_chunk(
            write_wav(
                str(self.tmp / "with_meta.wav"),
                rate=rate,
                channels=1,
                sampwidth=2,
                seconds=seconds,
            )
        )

        info = read_wav_info(src)
        self.assertEqual(info.frames, int(rate * seconds), "元数据被当成 PCM 读了")
        self.assertFalse(info.header_lies, "声明值与音频一致，不该判成坏头")

        got, note = normalize_wav(src, self._dst("with_meta_24k.wav"))
        self.assertEqual(got, src, note)
        self.assertIn("已是目标格式", note)

    @requires_audioop
    def test_oversized_pcm_is_refused_instead_of_truncated(self):
        """超过内存上限时只能拒绝并说明原因——截半的音频发出去看不出异常"""
        src = write_wav(str(self.tmp / "big.wav"), rate=44100, channels=1, sampwidth=2)
        dst = self._dst("big_24k.wav")
        with mock.patch.object(audio_norm, "_MAX_PCM_BYTES", 1024):
            got, note = normalize_wav(src, dst)
        self.assertEqual(got, src, note)
        self.assertIn("超过上限", note)
        self.assertFalse(pathlib.Path(dst).exists(), "拒绝规范化时不该留下半截产物")

    def test_missing_audioop_falls_back_to_source(self):
        """Python 3.13 起 audioop 不在标准库里，此时只能原样放行，不能拦住语音"""
        src = write_wav(str(self.tmp / "s313.wav"), rate=44100, channels=2, sampwidth=2)
        dst = self._dst("s313_24k.wav")
        with mock.patch.object(audio_norm, "audioop", None):
            got, note = normalize_wav(src, dst)
        self.assertEqual(got, src, note)
        self.assertIn("audioop 不可用", note)
        self.assertFalse(pathlib.Path(dst).exists())

    @requires_audioop
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
