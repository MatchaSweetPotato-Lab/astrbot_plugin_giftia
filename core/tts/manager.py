import asyncio
import os
import re
from dataclasses import dataclass

from astrbot.api import logger
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import StarTools
from astrbot.core.provider.provider import TTSProvider

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

    @property
    def config(self) -> dict:
        return getattr(self.plugin, "tts_config", {}) or {}

    def enabled(self) -> bool:
        return bool(self.config.get("enabled", False))

    def provider_type(self) -> str:
        provider_type = str(self.config.get("provider_type", "minimax")).strip().lower()
        return provider_type if provider_type in SUPPORTED_PROVIDER_TYPES else "minimax"

    def _language_items(self) -> list[tuple[str, str]]:
        items = self.config.get("language_provider_map") or []
        if not isinstance(items, list):
            return []

        result = []
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
            if lang:
                result.append((lang, provider_id))
            elif original_lang:
                supported_langs = "/".join(LANGUAGE_NAMES.values())
                logger.warning(
                    f"[Giftia TTS] 语言配置错误: 无法识别语言标签 '{original_lang}'，该配置条目已忽略。目前仅支持: {supported_langs}。"
                )
        return result

    @staticmethod
    def normalize_language(value: str) -> str:
        key = str(value or "").strip()
        return LANGUAGE_LABELS.get(key) or LANGUAGE_LABELS.get(key.lower(), "")

    def default_language(self) -> str:
        items = self._language_items()
        return items[0][0] if items else "zh-CN"

    def language_options(self) -> list[tuple[str, str]]:
        options = []
        seen = set()
        for lang, _provider_id in self._language_items():
            if lang in seen:
                continue
            seen.add(lang)
            options.append((lang, LANGUAGE_NAMES.get(lang, lang)))
        return options

    def _provider_id_for_language(self, lang: str) -> tuple[str, str]:
        items = self._language_items()
        if not items:
            return "", lang or "zh-CN"

        normalized_lang = lang if lang in LANGUAGE_NAMES else ""
        for item_lang, provider_id in items:
            if item_lang == normalized_lang:
                return provider_id, item_lang
        return items[0][1], items[0][0]

    def _lock_for_provider(self, provider_id: str) -> asyncio.Lock:
        if provider_id not in self._provider_locks:
            self._provider_locks[provider_id] = asyncio.Lock()
        return self._provider_locks[provider_id]

    def _warn_provider_type_mismatch(self, provider: TTSProvider, provider_id: str) -> None:
        expected = {
            "minimax": "minimax_tts_api",
            "fishaudio": "fishaudio_tts_api",
            "gsvtts": "gsv_tts_selfhost",
        }.get(self.provider_type())
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

    def resolve(self, segment: TTSRequest) -> ResolvedTTSRequest | None:
        if not self.enabled():
            return None

        text = str(segment.text or "").strip()
        if not text:
            return None

        lang = self.normalize_language(segment.lang) or self.default_language()
        provider_id, resolved_lang = self._provider_id_for_language(lang)
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

        self._warn_provider_type_mismatch(provider, provider_id)

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

    async def get_audio_path(self, resolved: ResolvedTTSRequest, event=None) -> str:
        lock = self._lock_for_provider(resolved.provider_id)
        async with lock:
            audio_path = ""
            if self.provider_type() != "minimax":
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
                    bot_name = self.plugin.adapter_id_map.get(event.platform_meta.id) or ""
                    group_or_user_id = event.get_group_id() or event.get_sender_id() or ""
                char_count = len(resolved.text)
                await self.plugin.db.log_token_usage(
                    bot_name=bot_name,
                    group_or_user_id=group_or_user_id,
                    type="tts",
                    provider_id=resolved.provider_id,
                    model_name=self.provider_type(),
                    prompt_tokens=char_count,
                    completion_tokens=0,
                    total_tokens=char_count,
                    extra_info={"text_len": char_count},
                )
            return audio_path

    async def build_record(self, event, segment: TTSRequest) -> Record | None:
        if segment.pre_recorded_path:
            resolved_path = self.resolve_audio_path(segment.pre_recorded_path)
            if not os.path.exists(resolved_path):
                logger.error(f"[Giftia TTS] 标志性语音文件不存在: {resolved_path}")
                return None
            logger.info(f"[Giftia TTS] 使用标志性语音文件: {resolved_path}")
            return Record.fromFileSystem(resolved_path, text=segment.text)

        resolved = self.resolve(segment)
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
                audio_path = await self.get_audio_path(resolved, event)
                if not audio_path:
                    logger.warning(
                        f"[Giftia TTS] 第 {attempt}/{max_attempts} 次请求未返回音频文件路径。"
                    )
                else:
                    if hasattr(event, "track_temporary_local_file"):
                        event.track_temporary_local_file(audio_path)

                    logger.info(f"[Giftia TTS] 语音合成完成: {audio_path}")
                    return Record.fromFileSystem(audio_path, text=segment.text)
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
        plugin_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        plugin_path = os.path.abspath(os.path.join(plugin_root, path))
        if os.path.exists(plugin_path):
            return plugin_path

        # Fallback to plugin data directory path
        if self.data_dir:
            return os.path.abspath(os.path.join(str(self.data_dir), path))
        return cwd_path

    def split_text_by_signatures(self, text: str, resolved_voices: list[dict] = None) -> list[dict]:
        voices_conf = self.config.get("signature_voices") or []
        if not voices_conf:
            return [{"type": "tts", "text": text}]
            
        text = text.strip()
        if not text:
            return []

        if resolved_voices is None:
            # Resolve and cache the selected audio path for each voice item during this split
            # This handles list of files (picking one randomly) and safeguards against list type stripping errors
            import random
            resolved_voices = []
            for item in voices_conf:
                audio_val = item.get("audio")
                if isinstance(audio_val, list):
                    valid_audios = [str(a).strip() for a in audio_val if a]
                    audio_path = random.choice(valid_audios) if valid_audios else ""
                else:
                    audio_path = str(audio_val or "").strip()
                
                if audio_path:
                    resolved_voices.append({
                        "audio": audio_path,
                        "matched_texts": item.get("matched_texts") or []
                    })

        # Regex for leading emotion tags like [元気に] or (laughs)
        LEADING_TAGS_RE = re.compile(r'^(\s*(?:\[[^\]]+\]|\([^)]+\))\s*)+')
        
        leading_tags = ""
        content_text = text
        
        match = LEADING_TAGS_RE.match(text)
        if match:
            leading_tags = match.group(0)
            content_text = text[match.end():]
            
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
            remaining_content = content_text[len(kw):]
            
        remaining_content_clean = remaining_content.strip(" ,，。！!?？、;；:：.）)]｝}")
        
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
                middle = remaining_content_clean[:-len(kw)].strip(" ,，。！!?？、;；:：.）)]｝}")
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

    def preprocess_signatures(self, llm_result: XmlLlmResult) -> None:
        if not self.enabled():
            return
            
        voices_conf = self.config.get("signature_voices") or []
        if not voices_conf:
            return

        # Resolve once for this entire response turn to ensure random consistency across all segments
        import random
        resolved_voices = []
        for item in voices_conf:
            audio_val = item.get("audio")
            if isinstance(audio_val, list):
                valid_audios = [str(a).strip() for a in audio_val if a]
                audio_path = random.choice(valid_audios) if valid_audios else ""
            else:
                audio_path = str(audio_val or "").strip()
            
            if audio_path:
                resolved_voices.append({
                    "audio": audio_path,
                    "matched_texts": item.get("matched_texts") or []
                })

        if not resolved_voices:
            return

        # 1. Process TTS segments
        self._preprocess_tts_segments(llm_result, resolved_voices)
        
        # 2. Process message chains (if enabled)
        if self.config.get("replace_in_message", False):
            self._preprocess_msg_chains(llm_result, resolved_voices)

    def _preprocess_tts_segments(self, llm_result: XmlLlmResult, resolved_voices: list[dict]) -> None:
        new_tts_segments: list[TTSRequest] = []
        index_mapping: dict[int, list[int]] = {}
        
        for i, segment in enumerate(llm_result.tts_segments):
            split_parts = self.split_text_by_signatures(segment.text, resolved_voices)
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
            output_order = [("message", index) for index in range(len(llm_result.msg_chains))]
            output_order.extend(("tts", index) for index in range(len(llm_result.tts_segments)))
            
        for item_type, index in output_order:
            if item_type == "tts":
                if index in index_mapping:
                    for new_idx in index_mapping[index]:
                        new_output_order.append(("tts", new_idx))
            else:
                new_output_order.append((item_type, index))
                
        llm_result.tts_segments = new_tts_segments
        llm_result.output_order = new_output_order

    def _preprocess_msg_chains(self, llm_result: XmlLlmResult, resolved_voices: list[dict]) -> None:
        new_msg_chains = []
        new_tts_segments = list(llm_result.tts_segments)
        msg_index_mapping: dict[int, list[tuple[str, int]]] = {}

        for msg_idx, chain in enumerate(llm_result.msg_chains):
            new_chain_items = []
            order_mapping = []
            
            for component in chain:
                if isinstance(component, Plain):
                    split_parts = self.split_text_by_signatures(component.text, resolved_voices)
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
            output_order = [("message", index) for index in range(len(llm_result.msg_chains))]
            output_order.extend(("tts", index) for index in range(len(llm_result.tts_segments)))
            
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
