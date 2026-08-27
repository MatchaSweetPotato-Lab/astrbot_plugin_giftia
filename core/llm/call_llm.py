from contextlib import contextmanager
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context
from astrbot.core.exceptions import EmptyModelOutputError
from astrbot.core.utils import media_utils
from xxhash import xxh3_64_hexdigest

from ..utils.event_utils import resolve_bot_name
from ..utils.qq_official_action import is_qq_official as is_qq_official_platform
from ..utils.schemas import (
    Decision,
    MediaCaption,
    Sticker,
    XmlLlmResult,
)
from ..utils.token_utils import extract_tokens_robust
from .json_parse import decode_media_audio_json, decode_media_caption_json
from .preset_prompts import (
    DEFAULT_AUDIO_CAPTION_PROMPT,
    DEFAULT_DECISION_RULES,
    DEFAULT_IMAGE_CAPTION_PROMPT,
    DEFAULT_STICKER_ANALYSIS_PROMPT,
    DEFAULT_VIDEO_CAPTION_PROMPT,
    build_xml_instructions,
    get_audio_caption_prompt,
    get_image_caption_prompt,
    get_sticker_analysis_prompt,
    get_video_caption_prompt,
)
from .xml_parse import XmlParse

_orig_detect_image_mime_type = media_utils.detect_image_mime_type


def _smart_detect_media_mime_type(
    image_source: bytes | str | Path,
    *,
    default_mime_type: str | None = "image/jpeg",
) -> str | None:
    res = _orig_detect_image_mime_type(image_source, default_mime_type=None)
    if res:
        return res

    try:
        header = b""
        if isinstance(image_source, bytes):
            header = image_source[:64]
        elif isinstance(image_source, (str, Path)):
            clean = str(image_source).removeprefix("file://")
            if clean.startswith("data:"):
                mime = clean.split(";")[0].removeprefix("data:")
                if mime.startswith("video/"):
                    return mime
            elif len(clean) < 4096:
                p = Path(clean)
                if p.exists() and p.is_file():
                    with open(p, "rb") as f:
                        header = f.read(64)

        if b"ftyp" in header or header.startswith(b"\x00\x00\x00"):
            return "video/mp4"
        elif header.startswith(b"\x1a\x45\xdf\xa3"):
            return "video/webm"
        elif b"moov" in header or b"free" in header or b"qt  " in header:
            return "video/quicktime"
        elif header.startswith(b"RIFF") and b"AVI " in header:
            return "video/x-msvideo"
    except Exception:
        pass

    return default_mime_type


@contextmanager
def _scoped_video_mime_detection():
    """按需在作用域内临时对 media_utils 附加视频 MIME 类型探测能力，规避全局副作用"""
    orig_fn = media_utils.detect_image_mime_type
    media_utils.detect_image_mime_type = _smart_detect_media_mime_type
    try:
        yield
    finally:
        media_utils.detect_image_mime_type = orig_fn


class CallLLM:
    def __init__(
        self,
        context: Context,
        xml_parse: XmlParse,
        network_config: dict,
        caption_config: dict,
        plugin=None,
    ):
        self.context = context
        self.xml_parse = xml_parse
        self.network_conf = network_config
        self.plugin = plugin
        self.sticker_analysis_prompt = DEFAULT_STICKER_ANALYSIS_PROMPT
        # 图片转述配置
        image_caption_provider_ids = (
            caption_config.get("image_caption_provider_ids") or []
        )
        self.image_caption_provider_ids = [p for p in image_caption_provider_ids if p]
        self.image_caption_prompt = DEFAULT_IMAGE_CAPTION_PROMPT
        # 音频转述配置
        audio_caption_provider_ids = (
            caption_config.get("audio_caption_provider_ids") or []
        )
        self.audio_caption_provider_ids = [p for p in audio_caption_provider_ids if p]
        self.audio_caption_prompt = DEFAULT_AUDIO_CAPTION_PROMPT
        # 视频转述配置
        video_caption_provider_ids = (
            caption_config.get("video_caption_provider_ids") or []
        )
        self.video_caption_provider_ids = [p for p in video_caption_provider_ids if p]
        self.video_caption_prompt = DEFAULT_VIDEO_CAPTION_PROMPT

    async def call_llm_decision(
        self,
        provider_ids: list[str],
        system_prompt: str,
        user_prompt: str,
        bot_name: str = "",
        group_or_user_id: str = "",
    ) -> Decision | None:
        """调用LLM进行决策"""
        if system_prompt:
            actual_system_prompt = (
                system_prompt.strip() + "\n\n" + DEFAULT_DECISION_RULES
            )
        else:
            actual_system_prompt = DEFAULT_DECISION_RULES

        logger.debug(f"\n<system_prompt>{actual_system_prompt}</system_prompt>")
        logger.debug(f"\n<user_prompt>{user_prompt}</user_prompt>")

        for provider_id in provider_ids:
            for i in range(self.network_conf["decision_retry_times"]):
                if i > 0:
                    logger.warning(f"LLM决策失败，{provider_id} 重试第 {i} 次")
                try:
                    llm_resp = await self.context.llm_generate(
                        chat_provider_id=provider_id,
                        system_prompt=actual_system_prompt,
                        prompt=user_prompt,
                    )
                    parsed_result = None
                    is_parsed = False
                    if llm_resp and llm_resp.completion_text:
                        logger.info(
                            f"\n<completion>\n{llm_resp.completion_text}\n</completion>"
                        )
                        parsed_result = self.xml_parse.decode_decision_xml(
                            llm_resp.completion_text
                        )
                        if parsed_result is not None:
                            is_parsed = True

                    await self._log_token_usage_safely(
                        bot_name=bot_name,
                        group_or_user_id=group_or_user_id,
                        type_name="decision",
                        provider_id=provider_id,
                        llm_resp=llm_resp,
                        extra_info={
                            "status": "success" if is_parsed else "parse_failed"
                        },
                    )
                    if parsed_result is not None:
                        return parsed_result
                    logger.warning(
                        f"LLM 决策 XML 解析失败，准备重试。provider_id: {provider_id}"
                    )
                    continue
                except Exception as e:
                    logger.error(f"LLM回复失败: {e!s}")
                    await self._log_token_usage_safely(
                        bot_name=bot_name,
                        group_or_user_id=group_or_user_id,
                        type_name="decision",
                        provider_id=provider_id,
                        llm_resp=None,
                        extra_info={"status": "api_failed", "error": str(e)},
                    )
                    continue
        return None

    async def call_llm_reply(
        self,
        event: AstrMessageEvent,
        group_or_user_id: str,
        provider_ids: list[str],
        system_prompt: str,
        user_prompt: str,
        timeout: int = 120,
        use_source_tools: bool = False,
        force_xml_tools: bool = False,
        enabled_features: list[str] | None = None,
        tts_instruction: str = "",
        is_qq_official: bool = False,
        persona_tools: list[str] | None = None,
    ) -> XmlLlmResult | None:
        """调用LLM进行回复"""
        is_qq_official_flag = is_qq_official or is_qq_official_platform(event)

        for provider_id in provider_ids:
            for i in range(self.network_conf["reply_retry_times"]):
                if i > 0:
                    logger.warning(f"LLM回复失败，{provider_id} 重试第 {i} 次")
                try:
                    xml_inst = build_xml_instructions(
                        enabled_features,
                        tts_instruction,
                        is_qq_official=is_qq_official_flag,
                    )
                    actual_system_prompt = (system_prompt or "") + "\n\n" + xml_inst
                    tools_set = None
                    if use_source_tools or force_xml_tools:
                        tool_manager = self.context.get_llm_tool_manager()
                        tools_set = tool_manager.get_full_tool_set()
                        # AstrBot 内置的 Tavily 工具（web_search_tavily /
                        # tavily_extract_web_page）默认不会进入
                        # get_full_tool_set()，这里按白名单手动追加，
                        # 前提是 AstrBot 配置里开启了网页搜索且提供商为 tavily。
                        merged_builtin_tools: list[str] = []
                        try:
                            provider_settings = (
                                self.context.astrbot_config_mgr.default_conf.get(
                                    "provider_settings", {}
                                )
                            )
                        except Exception:
                            provider_settings = {}
                        if bool(provider_settings.get("web_search")) and (
                            provider_settings.get("websearch_provider") == "tavily"
                        ):
                            existing_names = {t.name for t in tools_set.tools}
                            for builtin_name in (
                                "web_search_tavily",
                                "tavily_extract_web_page",
                            ):
                                if (
                                    builtin_name in existing_names
                                    or not tool_manager.is_builtin_tool(builtin_name)
                                ):
                                    continue
                                builtin_tool = tool_manager.get_builtin_tool(
                                    builtin_name
                                )
                                tools_set.add_tool(builtin_tool)
                                existing_names.add(builtin_name)
                                merged_builtin_tools.append(builtin_name)
                        if merged_builtin_tools:
                            logger.debug(
                                f"<native_tool_merge>\n"
                                f"  added: {merged_builtin_tools}\n"
                                f"  reason: provider_settings.web_search=True, websearch_provider=tavily\n"
                                f"</native_tool_merge>"
                            )
                        for tool in tools_set.tools[:]:
                            if not tool.active:
                                tools_set.remove_tool(tool.name)

                        if persona_tools is not None:
                            if isinstance(persona_tools, (list, tuple, set)):
                                allowed_names = {
                                    str(name).strip() for name in persona_tools if name
                                }
                                for tool in tools_set.tools[:]:
                                    if tool.name not in allowed_names:
                                        tools_set.remove_tool(tool.name)
                                logger.info(
                                    f"[Giftia] 经过人格工具限制过滤 ({len(allowed_names)} 个在白名单)，剩余 {len(tools_set.tools)} 个可用工具"
                                )

                        logger.debug(
                            f"\n<native_tools count={len(tools_set.tools)}>\n"
                            + "\n".join(
                                f"- name: {t.name}\n  description: {t.description}"
                                for t in tools_set.tools
                            )
                            + "\n</native_tools>"
                        )
                        target_tool_name = "web_search_tavily"
                        target_tool = next(
                            (t for t in tools_set.tools if t.name == target_tool_name),
                            None,
                        )
                        if target_tool is None:
                            is_builtin = tool_manager.is_builtin_tool(target_tool_name)
                            logger.debug(
                                f"<native_tool_probe>\n"
                                f"  target: {target_tool_name}\n"
                                f"  in_tools_set: False\n"
                                f"  is_builtin_tool: {is_builtin}\n"
                                f"  hint: {'内置工具未进入 tools_set（get_full_tool_set 仅遍历 func_list）' if is_builtin else '该工具未注册到当前工具管理器'}\n"
                                f"</native_tool_probe>"
                            )
                        else:
                            logger.debug(
                                f"<native_tool_probe>\n"
                                f"  target: {target_tool_name}\n"
                                f"  in_tools_set: True\n"
                                f"  active: {target_tool.active}\n"
                                f"</native_tool_probe>"
                            )

                        if force_xml_tools and tools_set and tools_set.tools:
                            import json

                            xml_tools_str = "\n".join(
                                f'  - <tool_call name="{t.name}" description="{t.description}">{json.dumps(t.parameters, ensure_ascii=False)}</tool_call>'
                                for t in tools_set.tools
                            )
                            xml_tools_instruction = (
                                "\n\n# 可用工具 (强制使用 XML 标签调用)\n"
                                '如果你需要使用工具，必须通过输出并列的 <tool_call name="工具名">参数JSON</tool_call> 标签来调用。不要使用原生的 function calling 功能。若没有需要调用的工具，则不要输出任何 tool_call 标签。\n'
                                "注意：参数部分必须是正确的 JSON 格式对象，例如：\n"
                                "<status>...</status>\n"
                                '<tool_call name="search_chat_history">{"keyword": "查询词"}</tool_call>\n\n'
                                "当前可用的工具列表：\n"
                            ) + xml_tools_str
                            actual_system_prompt = (
                                actual_system_prompt + xml_tools_instruction
                            )

                    logger.debug(
                        f"\n<system_prompt>\n{actual_system_prompt}\n</system_prompt>"
                    )
                    logger.debug(f"\n<user_prompt>\n{user_prompt}\n</user_prompt>")

                    if use_source_tools and not force_xml_tools:
                        llm_resp = await self.context.tool_loop_agent(
                            event=event,
                            chat_provider_id=provider_id,
                            system_prompt=actual_system_prompt,
                            prompt=user_prompt,
                            tools=tools_set,
                            max_steps=10,
                            tool_call_timeout=timeout,
                            stream=True,
                        )
                    else:
                        llm_resp = await self.context.tool_loop_agent(
                            event=event,
                            chat_provider_id=provider_id,
                            system_prompt=actual_system_prompt,
                            prompt=user_prompt,
                            tool_call_timeout=timeout,
                            stream=True,
                        )
                    parsed_result = None
                    is_parsed = False
                    status_val = "parse_failed"

                    if llm_resp.completion_text:
                        parsed_result = await self.xml_parse.decode_llm_xml(
                            llm_resp.completion_text, group_or_user_id
                        )
                        if parsed_result is not None:
                            is_parsed = True
                            status_val = "success"
                            parsed_result.native_tools_called = list(
                                llm_resp.tools_call_name or []
                            )
                    elif llm_resp.reasoning_content:
                        # LLM generated reasoning but empty text completion; likely safety blocked or cut off.
                        status_val = "parse_failed"
                    else:
                        # Succeeded but both completion and reasoning are empty.
                        is_parsed = True
                        status_val = "success"
                        parsed_result = XmlLlmResult(
                            native_tools_called=list(llm_resp.tools_call_name or [])
                        )

                    bot_name = resolve_bot_name(self.plugin, event)
                    await self._log_token_usage_safely(
                        bot_name=bot_name,
                        group_or_user_id=group_or_user_id,
                        type_name="reply",
                        provider_id=provider_id,
                        llm_resp=llm_resp,
                        extra_info={"status": status_val},
                    )

                    if llm_resp.tools_call_name:
                        logger.info(
                            f"\n<tools_call>\n{llm_resp.tools_call_name}\n</tools_call>"
                        )
                    if llm_resp.reasoning_content:
                        logger.info(
                            f"\n<reasoning>\n{llm_resp.reasoning_content}\n</reasoning>"
                        )

                    if parsed_result is not None:
                        if llm_resp.completion_text:
                            logger.info(
                                f"\n<completion>\n{llm_resp.completion_text}\n</completion>"
                            )
                        return parsed_result

                    if llm_resp.reasoning_content:
                        logger.warning(
                            f"LLM generated reasoning but empty completion, treating as failure. provider_id: {provider_id}"
                        )
                        continue
                    else:
                        logger.warning(
                            f"LLM回复 XML 解析失败且无法补救，准备重试。provider_id: {provider_id}"
                        )
                        continue
                except EmptyModelOutputError:
                    # Gemini empty output error; treat as no reply needed.
                    logger.info(
                        f"LLM generated empty output error, treating as no reply. provider_id: {provider_id}"
                    )
                    bot_name = resolve_bot_name(self.plugin, event)
                    await self._log_token_usage_safely(
                        bot_name=bot_name,
                        group_or_user_id=group_or_user_id,
                        type_name="reply",
                        provider_id=provider_id,
                        llm_resp=None,
                        extra_info={
                            "status": "api_failed",
                            "error": "EmptyModelOutputError",
                        },
                    )
                    return XmlLlmResult()
                except Exception as e:
                    logger.error(f"LLM回复失败: {e!s}，provider_id: {provider_id}")
                    bot_name = resolve_bot_name(self.plugin, event)
                    await self._log_token_usage_safely(
                        bot_name=bot_name,
                        group_or_user_id=group_or_user_id,
                        type_name="reply",
                        provider_id=provider_id,
                        llm_resp=None,
                        extra_info={"status": "api_failed", "error": str(e)},
                    )
                    continue
        return None

    async def call_llm_image_caption(
        self,
        image_urls: list[str],
        question: str | None = None,
        bot_name: str = "",
        group_or_user_id: str = "",
    ) -> MediaCaption | None:
        """调用LLM生成图片描述"""
        logger.info(f"调用LLM生成图片描述，共{len(image_urls)}张图片")
        for provider_id in self.image_caption_provider_ids:
            for i in range(self.network_conf["image_caption_retry_times"]):
                if i > 0:
                    logger.warning(f"LLM生成图片描述失败，{provider_id} 重试第 {i} 次")
                try:
                    # Hash a 128-char window starting at offset 200 (past the ~150-char
                    # JPEG JFIF header in base64), so different images produce different
                    # fingerprints. Also include the payload length as a discriminator.
                    def _b64_sig(u: str) -> str:
                        payload = u.removeprefix("base64://")
                        return f"{len(payload)}:{xxh3_64_hexdigest(payload[200:328].encode())}"

                    b64_hashes = [_b64_sig(u) for u in image_urls]
                    logger.debug(
                        f"[Giftia] 发送给LLM的图片内容hash: {b64_hashes} "
                        f"provider={provider_id}"
                    )
                    unique_prompt = get_image_caption_prompt(
                        question=question,
                        fingerprint=",".join(b64_hashes),
                    )
                    llm_resp = await self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=unique_prompt,
                        image_urls=image_urls,
                    )
                    is_parsed = False
                    parsed = None
                    if llm_resp and llm_resp.completion_text:
                        parsed = decode_media_caption_json(llm_resp.completion_text)
                        if parsed:
                            is_parsed = True

                    await self._log_token_usage_safely(
                        bot_name=bot_name,
                        group_or_user_id=group_or_user_id,
                        type_name="image_caption",
                        provider_id=provider_id,
                        llm_resp=llm_resp,
                        extra_info={
                            "status": "success" if is_parsed else "parse_failed"
                        },
                    )
                    if parsed:
                        logger.info(
                            f"[Giftia] LLM转述响应片段: "
                            f"{llm_resp.completion_text[:120]!r}"
                        )
                        return parsed
                    logger.warning("解析图片转述 JSON 失败，准备重试或降级...")
                    continue
                except Exception as e:
                    logger.error(f"LLM回复失败: {e!s}")
                    await self._log_token_usage_safely(
                        bot_name=bot_name,
                        group_or_user_id=group_or_user_id,
                        type_name="image_caption",
                        provider_id=provider_id,
                        llm_resp=None,
                        extra_info={"status": "api_failed", "error": str(e)},
                    )
                    continue
        return None

    async def call_llm_audio_caption(
        self,
        audio_urls: list[str],
        question: str | None = None,
        bot_name: str = "",
        group_or_user_id: str = "",
    ) -> MediaCaption | None:
        """调用LLM生成音频描述"""
        logger.info(f"调用LLM生成音频描述，共{len(audio_urls)}个音频")
        for provider_id in self.audio_caption_provider_ids:
            for i in range(self.network_conf["audio_caption_retry_times"]):
                if i > 0:
                    logger.warning(f"LLM生成音频描述失败，{provider_id} 重试第 {i} 次")
                try:
                    # Generate a unique fingerprint of the audio URLs.
                    audio_fingerprints = [
                        xxh3_64_hexdigest(u.encode()) for u in audio_urls
                    ]
                    unique_prompt = get_audio_caption_prompt(
                        question=question,
                        fingerprint=",".join(audio_fingerprints),
                    )
                    llm_resp = await self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=unique_prompt,
                        audio_urls=audio_urls,
                    )
                    is_parsed = False
                    parsed = None
                    if llm_resp and llm_resp.completion_text:
                        parsed = decode_media_audio_json(llm_resp.completion_text)
                        if parsed:
                            is_parsed = True

                    await self._log_token_usage_safely(
                        bot_name=bot_name,
                        group_or_user_id=group_or_user_id,
                        type_name="audio_caption",
                        provider_id=provider_id,
                        llm_resp=llm_resp,
                        extra_info={
                            "status": "success" if is_parsed else "parse_failed"
                        },
                    )
                    if parsed:
                        return parsed
                    logger.warning("解析音频转述 JSON 失败，准备重试或降级...")
                    continue
                except Exception as e:
                    logger.error(f"LLM回复失败: {e!s}")
                    await self._log_token_usage_safely(
                        bot_name=bot_name,
                        group_or_user_id=group_or_user_id,
                        type_name="audio_caption",
                        provider_id=provider_id,
                        llm_resp=None,
                        extra_info={"status": "api_failed", "error": str(e)},
                    )
                    continue
        return None

    async def call_llm_sticker_analysis(
        self,
        image_urls: list[str],
        categories: list[str],
        media_id: str,
        bot_name: str = "",
        group_or_user_id: str = "",
    ) -> tuple[bool, Sticker | None]:
        """调用LLM生成表情包分析结果"""
        logger.info(f"调用LLM生成表情包分析结果，共{len(image_urls)}张图片")
        for provider_id in self.image_caption_provider_ids:
            for i in range(self.network_conf.get("image_caption_retry_times", 1)):
                if i > 0:
                    logger.warning(f"LLM表情包分析失败，{provider_id} 重试第 {i} 次")
                try:
                    unique_prompt = get_sticker_analysis_prompt(
                        categories=categories,
                        fingerprint=",".join(image_urls),
                    )
                    llm_resp = await self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=unique_prompt,
                        image_urls=image_urls,
                    )
                    is_parsed = False
                    result_dict = None
                    if llm_resp and llm_resp.completion_text:
                        result_dict = self.xml_parse.parse_str_json(
                            llm_resp.completion_text
                        )
                        if result_dict:
                            is_parsed = True

                    await self._log_token_usage_safely(
                        bot_name=bot_name,
                        group_or_user_id=group_or_user_id,
                        type_name="sticker_analysis",
                        provider_id=provider_id,
                        llm_resp=llm_resp,
                        extra_info={
                            "status": "success" if is_parsed else "parse_failed"
                        },
                    )
                    if result_dict:
                        is_useful = result_dict.get("isUseful", False)
                        if not is_useful:
                            return False, None

                        tags = result_dict.get("tags", [])
                        if isinstance(tags, str):
                            tags = [tags]

                        sticker = Sticker(
                            sticker_id=media_id,
                            name=result_dict.get("name", "未知表情"),
                            category=result_dict.get("category", "默认分类"),
                            tags=tags,
                            description=result_dict.get("description", ""),
                        )
                        return True, sticker
                    logger.warning("解析表情包分析 JSON 失败，准备重试或降级...")
                    continue
                except Exception as e:
                    logger.error(f"LLM表情包分析失败: {e!s}")
                    await self._log_token_usage_safely(
                        bot_name=bot_name,
                        group_or_user_id=group_or_user_id,
                        type_name="sticker_analysis",
                        provider_id=provider_id,
                        llm_resp=None,
                        extra_info={"status": "api_failed", "error": str(e)},
                    )
                    continue
        return False, None

    async def _log_token_usage_safely(
        self,
        bot_name: str,
        group_or_user_id: str,
        type_name: str,
        provider_id: str,
        llm_resp=None,
        extra_info: dict | None = None,
    ):
        if not self.plugin:
            return

        if extra_info is None:
            extra_info = {}
        else:
            extra_info = dict(extra_info)

        prompt_tokens, completion_tokens, total_tokens = extract_tokens_robust(llm_resp)
        model_name = provider_id

        if llm_resp:
            if prompt_tokens == 0 and completion_tokens == 0:
                extra_info["usage_missing"] = True
        else:
            extra_info["usage_missing"] = True

        await self.plugin.db.log_token_usage(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            type=type_name,
            provider_id=provider_id,
            model_name=model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            extra_info=extra_info,
        )

    async def call_llm_video_caption(
        self,
        video_url: str,
        question: str | None = None,
        bot_name: str = "",
        group_or_user_id: str = "",
    ) -> MediaCaption | None:
        """调用支持原生视频理解的大语言模型 (如 Gemini 3.6 Flash) 进行视频与语音全量转述"""
        logger.info(f"调用LLM生成原生视频描述(音频与画面同频)，带问题={bool(question)}")
        for provider_id in self.video_caption_provider_ids:
            for i in range(self.network_conf.get("image_caption_retry_times", 2)):
                if i > 0:
                    logger.warning(f"LLM生成视频描述失败，{provider_id} 重试第 {i} 次")
                try:
                    payload = video_url.removeprefix("base64://")
                    video_sig = f"{len(payload)}:{xxh3_64_hexdigest(payload[200:328].encode()) if len(payload) > 328 else 'sig'}"

                    unique_prompt = get_video_caption_prompt(
                        question=question,
                        fingerprint=video_sig,
                    )

                    with _scoped_video_mime_detection():
                        llm_resp = await self.context.llm_generate(
                            chat_provider_id=provider_id,
                            prompt=unique_prompt,
                            image_urls=[video_url],
                        )
                    is_parsed = False
                    parsed = None
                    if llm_resp and llm_resp.completion_text:
                        parsed = decode_media_caption_json(
                            llm_resp.completion_text,
                            media_type="video",
                        )
                        if parsed:
                            is_parsed = True

                    await self._log_token_usage_safely(
                        bot_name=bot_name,
                        group_or_user_id=group_or_user_id,
                        type_name="video_caption",
                        provider_id=provider_id,
                        llm_resp=llm_resp,
                        extra_info={
                            "status": "success" if is_parsed else "parse_failed"
                        },
                    )
                    if parsed:
                        logger.info(
                            f"[Giftia] LLM原生视频转述响应片段: "
                            f"{llm_resp.completion_text[:120]!r}"
                        )
                        return parsed
                except Exception as e:
                    logger.error(f"LLM原生视频转述异常: {e}")
                    await self._log_token_usage_safely(
                        bot_name=bot_name,
                        group_or_user_id=group_or_user_id,
                        type_name="video_caption",
                        provider_id=provider_id,
                        llm_resp=None,
                        extra_info={"status": "api_failed", "error": str(e)},
                    )
                    continue
        return None
