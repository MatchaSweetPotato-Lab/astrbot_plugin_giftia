from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass

from astrbot.api import logger
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import StarTools
from astrbot.core.provider.provider import TTSProvider

from ..utils.audio_norm import describe_audio, normalize_wav, read_wav_info
from ..utils.event_utils import resolve_bot_name
from ..utils.qq_official_action import is_qq_official
from ..utils.schemas import TTSRequest, XmlLlmResult
from .constants import (
    LANGUAGE_LABELS,
    LANGUAGE_NAMES,
    MINIMAX_EMOTIONS,
    SUPPORTED_PROVIDER_TYPES,
)


@dataclass(slots=True)
class ResolvedTTSRequest:
    request: TTSRequest
    provider: TTSProvider
    provider_id: str
    lang: str
    text: str
    emotion: str


class TTSManager:
    def __init__(self, plugin):
        self.plugin = plugin
        self._provider_locks: dict[str, asyncio.Lock] = {}
        try:
            self.data_dir = StarTools.get_data_dir("astrbot_plugin_giftia")
        except Exception as e:
            logger.warning(f"[Giftia TTS] 获取插件数据目录失败: {e}")
            self.data_dir = None

    def get_config(self, bot_conf: dict | str = None) -> dict:
        if hasattr(self.plugin, "get_bot_config"):
            conf = self.plugin.get_bot_config(bot_conf)
            if isinstance(conf, dict):
                return conf.get("tts_config", {}) or {}
        return {}

    def enabled(self, bot_conf: dict | str = None) -> bool:
        return bool(self.get_config(bot_conf).get("enabled", False))

    def provider_type(self, bot_conf: dict | str = None) -> str:
        provider_type = (
            str(self.get_config(bot_conf).get("provider_type", "minimax"))
            .strip()
            .lower()
        )
        return provider_type if provider_type in SUPPORTED_PROVIDER_TYPES else "minimax"

    def _language_items(self, bot_conf: dict | str = None) -> list[tuple[str, str]]:
        tts_conf = self.get_config(bot_conf)
        result = []

        # 1. 优先使用按顺序配对的 language_provider_map 模式（首个配置项即为默认语言）
        items = tts_conf.get("language_provider_map") or []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                original_lang = (
                    item.get("language") or item.get("lang") or item.get("语言") or ""
                )
                lang = self.normalize_language(original_lang)
                provider_id = str(
                    item.get("provider_id")
                    or item.get("provider")
                    or item.get("tts_provider_id")
                    or item.get("供应商")
                    or ""
                ).strip()
                if lang and provider_id:
                    result.append((lang, provider_id))

        if result:
            return result

        # 2. 兼容回退旧版静态多语言字段
        default_lang_label = str(tts_conf.get("default_language", "中文")).strip()
        default_lang = self.normalize_language(default_lang_label) or "zh-CN"

        lang_fields = [
            ("zh-CN", tts_conf.get("zh_provider_id")),
            ("en-US", tts_conf.get("en_provider_id")),
            ("ja-JP", tts_conf.get("ja_provider_id")),
        ]

        for lang_code, provider_id in lang_fields:
            p_id = str(provider_id or "").strip()
            if p_id:
                result.append((lang_code, p_id))

        if result:
            result.sort(key=lambda x: 0 if x[0] == default_lang else 1)

        return result

    @staticmethod
    def normalize_language(value: str) -> str:
        key = str(value or "").strip()
        if not key:
            return ""
        if key in LANGUAGE_LABELS:
            return LANGUAGE_LABELS[key]
        if key.lower() in LANGUAGE_LABELS:
            return LANGUAGE_LABELS[key.lower()]
        return key

    def default_language(self, bot_conf: dict | str = None) -> str:
        items = self._language_items(bot_conf)
        return items[0][0] if items else "zh-CN"

    def language_options(self, bot_conf: dict | str = None) -> list[tuple[str, str]]:
        options = []
        seen = set()
        for lang, _provider_id in self._language_items(bot_conf):
            if lang in seen:
                continue
            seen.add(lang)
            options.append((lang, LANGUAGE_NAMES.get(lang, lang)))
        return options

    def _provider_id_for_language(
        self, lang: str, bot_conf: dict | str = None
    ) -> tuple[str, str]:
        items = self._language_items(bot_conf)
        if not items:
            return "", lang or "zh-CN"

        normalized_lang = self.normalize_language(lang) or lang
        for item_lang, provider_id in items:
            if item_lang == normalized_lang:
                return provider_id, item_lang
        return items[0][1], items[0][0]

    def _lock_for_provider(self, provider_id: str) -> asyncio.Lock:
        if provider_id not in self._provider_locks:
            self._provider_locks[provider_id] = asyncio.Lock()
        return self._provider_locks[provider_id]

    def _warn_provider_type_mismatch(
        self, provider: TTSProvider, provider_id: str, bot_conf: dict | str = None
    ) -> None:
        expected = {
            "minimax": "minimax_tts_api",
            "fishaudio": "fishaudio_tts_api",
            "gsvtts": "gsv_tts_selfhost",
        }.get(self.provider_type(bot_conf))
        actual = ""
        try:
            actual = provider.meta().type
        except Exception:
            actual = getattr(provider, "provider_config", {}).get("type", "")
        if expected and actual and actual != expected:
            logger.warning(
                f"[Giftia TTS] 配置的 TTS 供应商类型为 {self.provider_type()}，"
                f"但 provider_id={provider_id} 的实际类型是 {actual}。"
            )

    def resolve(
        self, segment: TTSRequest, bot_conf: dict | str = None
    ) -> ResolvedTTSRequest | None:
        if not self.enabled(bot_conf):
            return None

        text = str(segment.text or "").strip()
        if not text:
            return None

        lang = self.normalize_language(segment.lang) or self.default_language(bot_conf)
        provider_id, resolved_lang = self._provider_id_for_language(lang, bot_conf)
        if not provider_id:
            logger.warning(
                f"[Giftia TTS] 未配置 {LANGUAGE_NAMES.get(resolved_lang, resolved_lang)} 的 AstrBot TTS 供应商，跳过语音合成。"
            )
            return None

        provider = self.plugin.context.get_provider_by_id(provider_id)
        if not isinstance(provider, TTSProvider):
            logger.warning(
                f"[Giftia TTS] provider_id={provider_id} 不是可用的 AstrBot TTS 供应商，跳过语音合成。"
            )
            return None

        self._warn_provider_type_mismatch(provider, provider_id, bot_conf)

        emotion = str(segment.emotion or "").strip()
        return ResolvedTTSRequest(
            request=segment,
            provider=provider,
            provider_id=provider_id,
            lang=resolved_lang,
            text=self._adapt_text(text, emotion),
            emotion=emotion,
        )

    def _adapt_text(self, text: str, emotion: str) -> str:
        """
        对语音文本进行预处理或适配。

        注：目前虽然保留了 `emotion` 参数，但实际上并未在任何现有的供应商中被使用。
        此处保留该参数及签名，是为未来可能需要根据 emotion 进行文本适配/拼接的其他供应商提供预留与前向兼容性。
        """
        return text

    async def get_audio_path(
        self, resolved: ResolvedTTSRequest, event=None, bot_conf: dict | str = None
    ) -> str:
        lock = self._lock_for_provider(resolved.provider_id)
        async with lock:
            audio_path = ""
            if self.provider_type(bot_conf) != "minimax":
                audio_path = await resolved.provider.get_audio(resolved.text)
            else:
                emotion = resolved.emotion.strip().lower()
                if emotion not in MINIMAX_EMOTIONS or not hasattr(
                    resolved.provider, "voice_setting"
                ):
                    audio_path = await resolved.provider.get_audio(resolved.text)
                else:
                    voice_setting = resolved.provider.voice_setting
                    marker = object()
                    old_emotion = voice_setting.get("emotion", marker)
                    voice_setting["emotion"] = emotion
                    try:
                        audio_path = await resolved.provider.get_audio(resolved.text)
                    finally:
                        if old_emotion is marker:
                            voice_setting.pop("emotion", None)
                        else:
                            voice_setting["emotion"] = old_emotion

            if audio_path and self.plugin:
                bot_name = ""
                group_or_user_id = ""
                if event:
                    bot_name = resolve_bot_name(self.plugin, event)
                    group_or_user_id = (
                        event.get_group_id() or event.get_sender_id() or ""
                    )
                if not bot_name and isinstance(bot_conf, dict):
                    bot_name = bot_conf.get("name", "")
                char_count = len(resolved.text)
                await self.plugin.db.log_token_usage(
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                    type="tts",
                    provider_id=resolved.provider_id,
                    model_name=self.provider_type(bot_conf),
                    prompt_tokens=char_count,
                    completion_tokens=0,
                    total_tokens=char_count,
                    extra_info={"text_len": char_count},
                )
            return audio_path

    async def build_record(
        self, event, segment: TTSRequest, bot_conf: dict | str = None
    ) -> Record | None:
        if segment.pre_recorded_path:
            resolved_path = self.resolve_audio_path(segment.pre_recorded_path)
            if not os.path.exists(resolved_path):
                logger.error(f"[Giftia TTS] 标志性语音文件不存在: {resolved_path}")
                return None
            logger.info(f"[Giftia TTS] 使用标志性语音文件: {resolved_path}")
            send_path = await self.prepare_audio_for_platform(event, resolved_path)
            if not send_path:
                return None
            return Record.fromFileSystem(send_path, text=segment.text)

        resolved = self.resolve(segment, bot_conf)
        if not resolved:
            return None

        max_attempts = 3
        last_exception = None

        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(
                    f"[Giftia TTS] 请求语音合成 (第 {attempt}/{max_attempts} 次尝试): lang={resolved.lang}, "
                    f"provider={resolved.provider_id}, text={resolved.text}"
                )
                audio_path = await self.get_audio_path(resolved, event, bot_conf)
                if not audio_path:
                    logger.warning(
                        f"[Giftia TTS] 第 {attempt}/{max_attempts} 次请求未返回音频文件路径。"
                    )
                else:
                    if hasattr(event, "track_temporary_local_file"):
                        event.track_temporary_local_file(audio_path)

                    logger.info(f"[Giftia TTS] 语音合成完成: {audio_path}")
                    send_path = await self.prepare_audio_for_platform(event, audio_path)
                    if not send_path:
                        # 平台侧转码不可行时重试合成也是白费（同一提供商同一参数），
                        # 直接放弃该段语音，由调用方按「未合成」处理。
                        return None
                    return Record.fromFileSystem(send_path, text=segment.text)
            except Exception as e:
                last_exception = e
                logger.warning(
                    f"[Giftia TTS] 第 {attempt}/{max_attempts} 次语音合成失败: {e}"
                )

            if attempt < max_attempts:
                await asyncio.sleep(1)

        if last_exception:
            logger.error(
                f"[Giftia TTS] 语音合成连续 {max_attempts} 次失败，终止重试: {last_exception}",
                exc_info=True,
            )
        else:
            logger.error(
                f"[Giftia TTS] 语音合成连续 {max_attempts} 次失败（未获取到音频路径），终止重试。"
            )
        return None

    async def prepare_audio_for_platform(self, event, audio_path: str) -> str:
        """按平台把音频调整成可发送的形态，返回可发送路径（空串表示发不了）。

        目前只有官方 QQ 需要特殊处理：那边不收 wav，AstrBot 核心发送前会转腾讯
        silk（`MediaResolver.to_path(target_format="tencent_silk")`）。核心那条链路
        转换失败时只打一句 `处理语音时出错: {e}`——不打异常类型也不打 traceback，
        于是最典型的故障（流式 TTS 的 wav 头声明约 2GiB，核心照此分配内存抛
        `MemoryError`，而该异常不带消息）在日志里只剩孤零零一句话，语音被静默丢弃。

        所以这里先把音频修成头长度正确、24kHz/单声道/16bit 的 PCM wav，再用核心同一套
        转换预检一次：失败就带真实异常与 traceback 记日志并放弃该段语音，
        不再出现「语音无声消失、日志什么都没说」。其他平台原样返回。
        """
        if not audio_path or not is_qq_official(event):
            return audio_path

        if self.data_dir:
            norm_path = os.path.join(
                str(self.data_dir),
                "tts_qq_official",
                f"{os.path.splitext(os.path.basename(audio_path))[0]}_24k.wav",
            )
        else:
            norm_path = f"{os.path.splitext(audio_path)[0]}_24k.wav"

        send_path, note = normalize_wav(audio_path, norm_path)
        logger.info(
            f"[Giftia TTS] 官方 QQ 语音规范化: {note} | 待发送: {describe_audio(send_path)}"
        )
        if send_path != audio_path and hasattr(event, "track_temporary_local_file"):
            # 规范化产物同样交给核心的事件级临时文件清理，避免堆积
            event.track_temporary_local_file(send_path)

        info = read_wav_info(send_path)
        if info and info.header_lies:
            # 规范化没成功（非 PCM / audioop 不可用等），此时连预检都不能做：
            # 核心与预检都会照着头里声明的长度去分配内存，小内存机器上可能
            # 不是干净地抛 MemoryError，而是直接触发 OOM。
            logger.error(
                "[Giftia TTS] 官方 QQ 语音的 wav 头长度不可信且未能修正，放弃发送该段语音"
                f"（核心会照头声明的 {info.declared_frames} 帧分配内存）: {send_path}"
            )
            return ""

        if not await self.probe_tencent_silk(send_path):
            return ""
        return send_path

    async def probe_tencent_silk(self, audio_path: str) -> bool:
        """用核心同一套转换预检音频能否转成腾讯 silk，失败时打出真实异常。

        转换代码与核心发送时走的完全是同一条（`MediaResolver` → `wav_to_tencent_silk`
        → `pysilk.encode`），所以这里失败就意味着核心也必然失败。核心不可用（版本较老、
        模块改名）时一律放行，不因为预检本身的问题拦住语音。
        """
        try:
            from astrbot.core.utils.media_utils import MediaResolver
        except Exception as e:
            logger.debug(
                f"[Giftia TTS] 核心媒体转换模块不可用，跳过官方 QQ 语音预检: {e}"
            )
            return True

        silk_path = ""
        try:
            silk_path = await MediaResolver(
                audio_path, media_type="audio", default_suffix=".wav"
            ).to_path(target_format="tencent_silk")
            return True
        except Exception as e:
            logger.error(
                "[Giftia TTS] 官方 QQ 语音转 silk 失败，放弃发送该段语音"
                f"（核心同一处只会打一句空消息）: {type(e).__module__}.{type(e).__name__}: "
                f"{e or '(该异常不带消息)'} | 文件: {audio_path} | {describe_audio(audio_path)}",
                exc_info=True,
            )
            return False
        finally:
            # 预检产物没人要，核心发送时会自己再转一份
            if silk_path:
                try:
                    os.remove(silk_path)
                except OSError as e:
                    logger.debug(f"[Giftia TTS] 清理预检 silk 文件失败: {e}")

    def resolve_audio_path(self, path: str) -> str:
        path = path.strip()
        if not path:
            return ""
        if os.path.isabs(path):
            return path

        # 1. Try relative to plugin data directory (where uploaded files are saved)
        if self.data_dir:
            data_path = os.path.abspath(os.path.join(str(self.data_dir), path))
            if os.path.exists(data_path):
                return data_path

        # 2. Try relative to Cwd (project root)
        cwd_path = os.path.abspath(os.path.join(os.getcwd(), path))
        if os.path.exists(cwd_path):
            return cwd_path

        # 3. Try relative to plugin root (3 levels up from core/tts/manager.py)
        plugin_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        plugin_path = os.path.abspath(os.path.join(plugin_root, path))
        if os.path.exists(plugin_path):
            return plugin_path

        # Fallback to plugin data directory path
        if self.data_dir:
            return os.path.abspath(os.path.join(str(self.data_dir), path))
        return cwd_path

    @staticmethod
    def _resolve_voices_conf(voices_conf: list) -> list[dict]:
        import random
        import re

        resolved_voices = []
        for item in voices_conf:
            audio_path = ""
            matched_texts = []
            if isinstance(item, str):
                item_str = item.strip()
                if not item_str:
                    continue
                if ":" in item_str or "：" in item_str:
                    delim = ":" if ":" in item_str else "："
                    parts = item_str.split(delim, 1)
                    raw_audios = parts[0].strip()
                    raw_texts = parts[1].strip()

                    audio_list = [
                        a.strip() for a in re.split(r"[,，|;]", raw_audios) if a.strip()
                    ]
                    audio_path = random.choice(audio_list) if audio_list else ""

                    text_list = [
                        t.strip() for t in re.split(r"[,，|;]", raw_texts) if t.strip()
                    ]
                    matched_texts = text_list
                else:
                    audio_list = [
                        a.strip() for a in re.split(r"[,，|;]", item_str) if a.strip()
                    ]
                    audio_path = random.choice(audio_list) if audio_list else ""
                    matched_texts = []
            elif isinstance(item, dict):
                audio_val = item.get("audio")
                if isinstance(audio_val, list):
                    valid_audios = [str(a).strip() for a in audio_val if a]
                    audio_path = random.choice(valid_audios) if valid_audios else ""
                elif isinstance(audio_val, str):
                    audio_list = [
                        a.strip() for a in re.split(r"[,，|;]", audio_val) if a.strip()
                    ]
                    audio_path = random.choice(audio_list) if audio_list else ""
                else:
                    audio_path = ""

                matched_val = item.get("matched_texts") or []
                if isinstance(matched_val, list):
                    matched_texts = [str(t).strip() for t in matched_val if t]
                elif isinstance(matched_val, str):
                    matched_texts = [
                        t.strip()
                        for t in re.split(r"[,，|;]", matched_val)
                        if t.strip()
                    ]
            else:
                continue

            if audio_path:
                resolved_voices.append(
                    {"audio": audio_path, "matched_texts": matched_texts}
                )
        return resolved_voices

    def split_text_by_signatures(
        self, text: str, resolved_voices: list[dict] = None, bot_conf: dict | str = None
    ) -> list[dict]:
        voices_conf = self.get_config(bot_conf).get("signature_voices") or []
        if not voices_conf:
            return [{"type": "tts", "text": text}]

        text = text.strip()
        if not text:
            return []

        if resolved_voices is None:
            resolved_voices = self._resolve_voices_conf(voices_conf)

        # Regex for leading emotion tags like [元気に] or (laughs)
        LEADING_TAGS_RE = re.compile(r"^(\s*(?:\[[^\]]+\]|\([^)]+\))\s*)+")

        leading_tags = ""
        content_text = text

        match = LEADING_TAGS_RE.match(text)
        if match:
            leading_tags = match.group(0)
            content_text = text[match.end() :]

        def find_exact_match(t: str) -> tuple[str, str] | None:
            t_clean = t.strip(" ,，。！!?？、;；:：.）)]｝}")
            for item in resolved_voices:
                audio_path = item.get("audio") or ""
                matched_texts = item.get("matched_texts") or []
                if not audio_path or not matched_texts:
                    continue
                for kw in matched_texts:
                    kw = kw.strip()
                    if not kw:
                        continue
                    if t_clean == kw:
                        return audio_path, kw
            return None

        exact = find_exact_match(content_text)
        if exact:
            audio_path, kw = exact
            return [{"type": "signature", "path": audio_path, "text": kw}]

        head_match = None
        longest_head_len = 0
        for item in resolved_voices:
            audio_path = item.get("audio") or ""
            matched_texts = item.get("matched_texts") or []
            if not audio_path or not matched_texts:
                continue
            for kw in matched_texts:
                kw = kw.strip()
                if not kw:
                    continue
                if content_text.startswith(kw) and len(kw) > longest_head_len:
                    head_match = (audio_path, kw)
                    longest_head_len = len(kw)

        remaining_content = content_text
        segments = []

        if head_match:
            audio_path, kw = head_match
            segments.append({"type": "signature", "path": audio_path, "text": kw})
            remaining_content = content_text[len(kw) :]

        remaining_content_clean = remaining_content.strip(
            " ,，。！!?？、;；:：.）)]｝}"
        )

        tail_match = None
        longest_tail_len = 0
        for item in resolved_voices:
            audio_path = item.get("audio") or ""
            matched_texts = item.get("matched_texts") or []
            if not audio_path or not matched_texts:
                continue
            for kw in matched_texts:
                kw = kw.strip()
                if not kw:
                    continue
                if remaining_content_clean.endswith(kw) and len(kw) > longest_tail_len:
                    tail_match = (audio_path, kw)
                    longest_tail_len = len(kw)

        if tail_match:
            audio_path, kw = tail_match
            idx = remaining_content.rfind(kw)
            if idx != -1:
                middle = remaining_content[:idx].strip(" ,，。！!?？、;；:：.）)]｝}")
                tail = remaining_content[idx:]
            else:
                middle = remaining_content_clean[: -len(kw)].strip(
                    " ,，。！!?？、;；:：.）)]｝}"
                )
                tail = kw

            if middle:
                segments.append({"type": "tts", "text": leading_tags + middle})
            segments.append({"type": "signature", "path": audio_path, "text": tail})
        else:
            final_remaining = remaining_content.strip(" ,，。！!?？、;；:：.）)]｝}")
            if final_remaining:
                segments.append({"type": "tts", "text": leading_tags + final_remaining})
            elif leading_tags and not segments:
                segments.append({"type": "tts", "text": text})

        return segments

    def preprocess_signatures(
        self, llm_result: XmlLlmResult, bot_conf: dict | str = None
    ) -> None:
        if not self.enabled(bot_conf):
            return

        voices_conf = self.get_config(bot_conf).get("signature_voices") or []
        if not voices_conf:
            return

        resolved_voices = self._resolve_voices_conf(voices_conf)
        if not resolved_voices:
            return

        # 1. Process TTS segments
        self._preprocess_tts_segments(llm_result, resolved_voices, bot_conf)

        # 2. Process message chains (if enabled)
        if self.get_config(bot_conf).get("replace_in_message", False):
            self._preprocess_msg_chains(llm_result, resolved_voices, bot_conf)

    def _preprocess_tts_segments(
        self,
        llm_result: XmlLlmResult,
        resolved_voices: list[dict],
        bot_conf: dict | str = None,
    ) -> None:
        new_tts_segments: list[TTSRequest] = []
        index_mapping: dict[int, list[int]] = {}

        for i, segment in enumerate(llm_result.tts_segments):
            split_parts = self.split_text_by_signatures(
                segment.text, resolved_voices, bot_conf
            )
            new_indices = []
            for part in split_parts:
                if part["type"] == "signature":
                    new_seg = TTSRequest(
                        text=part["text"],
                        lang=segment.lang,
                        emotion=segment.emotion,
                        pre_recorded_path=part["path"],
                    )
                else:
                    new_seg = TTSRequest(
                        text=part["text"],
                        lang=segment.lang,
                        emotion=segment.emotion,
                    )
                new_indices.append(len(new_tts_segments))
                new_tts_segments.append(new_seg)
            index_mapping[i] = new_indices

        new_output_order: list[tuple[str, int]] = []
        output_order = llm_result.output_order
        if not output_order:
            output_order = [
                ("message", index) for index in range(len(llm_result.msg_chains))
            ]
            output_order.extend(
                ("tts", index) for index in range(len(llm_result.tts_segments))
            )

        for item_type, index in output_order:
            if item_type == "tts":
                if index in index_mapping:
                    for new_idx in index_mapping[index]:
                        new_output_order.append(("tts", new_idx))
            else:
                new_output_order.append((item_type, index))

        llm_result.tts_segments = new_tts_segments
        llm_result.output_order = new_output_order

    def _preprocess_msg_chains(
        self,
        llm_result: XmlLlmResult,
        resolved_voices: list[dict],
        bot_conf: dict | str = None,
    ) -> None:
        new_msg_chains = []
        new_tts_segments = list(llm_result.tts_segments)
        msg_index_mapping: dict[int, list[tuple[str, int]]] = {}

        for msg_idx, chain in enumerate(llm_result.msg_chains):
            new_chain_items = []
            order_mapping = []

            for component in chain:
                if isinstance(component, Plain):
                    split_parts = self.split_text_by_signatures(
                        component.text, resolved_voices, bot_conf
                    )
                    for part in split_parts:
                        if part["type"] == "signature":
                            new_seg = TTSRequest(
                                text=part["text"],
                                pre_recorded_path=part["path"],
                            )
                            tts_idx = len(new_tts_segments)
                            new_tts_segments.append(new_seg)

                            if new_chain_items:
                                msg_chain_idx = len(new_msg_chains)
                                new_msg_chains.append(new_chain_items)
                                order_mapping.append(("message", msg_chain_idx))
                                new_chain_items = []

                            order_mapping.append(("tts", tts_idx))
                        else:
                            new_chain_items.append(Plain(text=part["text"]))
                else:
                    new_chain_items.append(component)

            if new_chain_items:
                msg_chain_idx = len(new_msg_chains)
                new_msg_chains.append(new_chain_items)
                order_mapping.append(("message", msg_chain_idx))

            msg_index_mapping[msg_idx] = order_mapping

        new_output_order: list[tuple[str, int]] = []
        output_order = llm_result.output_order
        if not output_order:
            output_order = [
                ("message", index) for index in range(len(llm_result.msg_chains))
            ]
            output_order.extend(
                ("tts", index) for index in range(len(llm_result.tts_segments))
            )

        for item_type, index in output_order:
            if item_type == "message":
                if index in msg_index_mapping:
                    new_output_order.extend(msg_index_mapping[index])
            else:
                new_output_order.append((item_type, index))

        new_msg_texts = []
        new_msg_logs = []
        for chain in new_msg_chains:
            text_parts = []
            log_parts = []
            for comp in chain:
                if isinstance(comp, Plain):
                    text_parts.append(comp.text)
                    log_parts.append(comp.text)
                elif hasattr(comp, "qq"):
                    log_parts.append(f" <@{comp.qq}>")
                elif hasattr(comp, "path") or hasattr(comp, "url"):
                    log_parts.append(" [图片]")
            new_msg_texts.append("".join(text_parts))
            new_msg_logs.append("".join(log_parts))

        llm_result.msg_chains = new_msg_chains
        llm_result.msg_texts = new_msg_texts
        llm_result.msg_logs = new_msg_logs
        llm_result.tts_segments = new_tts_segments
        llm_result.output_order = new_output_order
