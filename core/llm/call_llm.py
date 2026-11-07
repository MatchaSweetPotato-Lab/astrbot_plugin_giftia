import json
from contextlib import contextmanager
from pathlib import Path
from xml.etree.ElementTree import fromstring

from xxhash import xxh3_64_hexdigest

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.tool import ToolSet
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.exceptions import EmptyModelOutputError
from astrbot.core.utils import media_utils

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
from .native_tools import (
    NATIVE_CONTEXT_KEY,
    NATIVE_TOOL_CONFIG_KEYS,
)
from .preset_prompts import (
    RELATION_SCORE_GUIDE,
    REPLY_CONTEXT_GUIDE,
    REPLY_REVIEW_PROMPT,
    build_xml_instructions,
    get_audio_caption_prompt,
    get_decision_rules,
    get_image_caption_prompt,
    get_sticker_analysis_prompt,
    get_video_caption_prompt,
)
from .provider_health import ProviderUnavailableError, get_provider_health
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


class _ReplyToolHooks(BaseAgentRunHooks[AstrAgentContext]):
    """Track tool execution across all steps of one reply attempt."""

    def __init__(self):
        self.called_tools: list[str] = []

    async def on_tool_start(self, run_context, tool, tool_args) -> None:
        """Record a tool before it can produce side effects.

        Args:
            run_context: Current agent context.
            tool: Tool about to execute.
            tool_args: Arguments supplied to the tool.
        """
        self.called_tools.append(tool.name)


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
        self.provider_health = get_provider_health(plugin, context)
        self.caption_config = caption_config
        self.media_caption_provider_ids = [
            p for p in (caption_config.get("media_caption_provider_ids") or []) if p
        ]
        self.inspect_media_provider_ids = [
            p for p in (caption_config.get("inspect_media_provider_ids") or []) if p
        ] or self.media_caption_provider_ids
        self.media_caption_retry_times = network_config.get(
            "media_caption_retry_times", 2
        )

    async def call_llm_decision(
        self,
        provider_ids: list[str],
        system_prompt: str,
        user_prompt: str,
        bot_name: str = "",
        group_or_user_id: str = "",
        use_meme_manager: bool = False,
    ) -> Decision | None:
        """调用LLM进行决策"""
        bot_config = (
            self.plugin.bot_map.get(bot_name, {}) if self.plugin is not None else {}
        )
        decision_rules = get_decision_rules(
            use_meme_manager=use_meme_manager,
            relation_score_guide=bot_config.get("relation_score_guide", ""),
        )
        if system_prompt:
            actual_system_prompt = system_prompt.strip() + "\n\n" + decision_rules
        else:
            actual_system_prompt = decision_rules

        logger.debug(f"\n<system_prompt>{actual_system_prompt}</system_prompt>")
        logger.debug(f"\n<user_prompt>{user_prompt}</user_prompt>")

        for provider_id in provider_ids:
            for i in range(self.network_conf["decision_retry_times"]):
                if i > 0:
                    logger.warning(f"LLM决策失败，{provider_id} 重试第 {i} 次")
                try:
                    llm_resp = await self.provider_health.call(
                        self.context.llm_generate,
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
                except ProviderUnavailableError:
                    break
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

    async def call_llm_reply_review(
        self,
        provider_id: str,
        user_prompt: str,
        bot_name: str,
        group_or_user_id: str,
    ) -> bool | None:
        """Review an unsent draft once without invoking tools.

        Args:
            provider_id: Explicitly selected review provider.
            user_prompt: Original messages, draft and new conversation messages.
            bot_name: Owning bot configuration.
            group_or_user_id: Owning conversation.

        Returns:
            True to send, False to regenerate, or None on timeout or invalid output.
        """
        logger.debug(f"\n<system_prompt>{REPLY_REVIEW_PROMPT}</system_prompt>")
        logger.debug(f"\n<user_prompt>{user_prompt}</user_prompt>")

        response = None
        passed = None
        extra_info = {"status": "api_failed"}
        try:
            response = await self.provider_health.call(
                self.context.llm_generate,
                chat_provider_id=provider_id,
                system_prompt=REPLY_REVIEW_PROMPT,
                prompt=user_prompt,
                request_timeout=10,
            )
            extra_info["status"] = "parse_failed"
            completion = (getattr(response, "completion_text", "") or "").strip()
            if completion.startswith("```") and completion.endswith("```"):
                completion = "\n".join(completion.splitlines()[1:-1]).strip()
            node = fromstring(completion)
            verdict = (node.get("pass") or "").strip().lower()
            if node.tag != "review" or verdict not in ("true", "false"):
                raise ValueError("Invalid reply review verdict")
            passed = verdict == "true"
            extra_info = {
                "status": "success",
                "passed": passed,
                "reason": node.get("reason", ""),
            }
            logger.info(
                "[Giftia] Reply review for %s:%s: passed=%s, reason=%s",
                bot_name,
                group_or_user_id,
                passed,
                node.get("reason", ""),
            )
        except ProviderUnavailableError:
            return None
        except Exception as error:
            extra_info["error"] = str(error)
            extra_info["error_type"] = type(error).__name__
            logger.warning(
                f"[Giftia] Reply review unavailable for {bot_name}:{group_or_user_id}; "
                f"sending draft: provider={provider_id}, status={extra_info['status']}, "
                f"error_type={type(error).__name__}, error={str(error)!r}"
            )
        await self._log_token_usage_safely(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            type_name="reply_review",
            provider_id=provider_id,
            llm_resp=response,
            extra_info=extra_info,
        )
        return passed

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
        use_meme_manager: bool = False,
        tool_context: dict | None = None,
    ) -> XmlLlmResult | None:
        """Generate a reply through the primary and optional fallback lists.

        Request errors retry within the current list. Invalid primary output
        enters the fallback list; invalid fallback output ends the reply.
        An empty fallback list preserves legacy output handling.

        Args:
            event: Incoming event used for routing and native tools.
            group_or_user_id: Conversation used for media and usage records.
            provider_ids: Primary providers in their selected order.
            system_prompt: Persona instructions.
            user_prompt: Frozen conversation input.
            timeout: Native tool timeout in seconds.
            use_source_tools: Allow native function calling.
            force_xml_tools: Describe function tools through XML instead.
            enabled_features: Allowed interactive XML actions.
            tts_instruction: Speech output instructions.
            is_qq_official: Apply official QQ output restrictions.
            persona_tools: Tool names allowed by the persona.
            use_meme_manager: Enable external sticker instructions.
            tool_context: Trusted state shared with tool execution.

        Returns:
            A valid reply, including intentional silence, or None on failure.
        """
        is_qq_official_flag = is_qq_official or is_qq_official_platform(event)
        bot_name = (tool_context or {}).get("bot_name") or resolve_bot_name(
            self.plugin, event
        )
        bot_config = (
            self.plugin.bot_map.get(bot_name, {}) if self.plugin is not None else {}
        )
        if enabled_features is None and self.plugin is not None:
            enabled_features = bot_config.get("enabled_interactive_features")
        relation_score_guide = (
            bot_config.get("relation_score_guide", "").strip() or RELATION_SCORE_GUIDE
        )
        fallback_provider_ids = bot_config.get("llm_reply_conf", {}).get(
            "invalid_reply_fallback_provider_ids", []
        )
        fallback_enabled = bool(fallback_provider_ids)

        xml_inst = build_xml_instructions(
            enabled_features,
            tts_instruction,
            is_qq_official=is_qq_official_flag,
            use_meme_manager=use_meme_manager,
        )
        actual_system_prompt = "\n\n".join(
            (
                system_prompt or "",
                REPLY_CONTEXT_GUIDE,
                relation_score_guide,
                xml_inst,
            )
        )
        tools_set = None
        native_tools = ToolSet()
        xml_tools = []
        if use_source_tools or force_xml_tools:
            tools_set = self._get_reply_tools(persona_tools)
            if force_xml_tools:
                xml_tools = tools_set.tools
            else:
                native_tools = tools_set
            if xml_tools:
                xml_tools_str = "\n".join(
                    f'  - <tool_call name="{t.name}" description="{t.description}">{json.dumps(t.parameters, ensure_ascii=False)}</tool_call>'
                    for t in xml_tools
                )
                xml_tools_instruction = (
                    "\n\n# 可用工具 (强制使用 XML 标签调用)\n"
                    '所有工具统一通过并列的 <tool_call name="工具名">参数JSON</tool_call> 标签调用，不使用原生 function calling。工具结果会在执行后提供，可据此继续调用或组织回复。若没有需要调用的工具，则不要输出 tool_call 标签。\n'
                    "注意：参数部分必须是正确的 JSON 格式对象，例如：\n"
                    "<status>...</status>\n"
                    '<tool_call name="search_chat_history">{"keyword": "查询词"}</tool_call>\n\n'
                    "当前可用的工具列表：\n"
                ) + xml_tools_str
                actual_system_prompt = actual_system_prompt + xml_tools_instruction

        agent_context = None
        if native_tools.tools:
            agent_context = AstrAgentContext(
                context=self.context,
                event=event,
                extra={NATIVE_CONTEXT_KEY: json.dumps(tool_context, ensure_ascii=False)}
                if tool_context is not None
                else {},
            )
        use_native_tools = bool(native_tools.tools) or (
            use_source_tools and not force_xml_tools
        )
        request_kwargs = {
            "event": event,
            "system_prompt": actual_system_prompt,
            "prompt": user_prompt,
            "tool_call_timeout": timeout,
            "stream": True,
        }
        if use_native_tools:
            request_kwargs.update(
                tools=native_tools,
                max_steps=getattr(self.plugin, "tools_config", {}).get("max_loop", 10),
            )
            if agent_context is not None:
                request_kwargs["agent_context"] = agent_context
        logger.debug(f"\n<system_prompt>\n{actual_system_prompt}\n</system_prompt>")
        logger.debug(f"\n<user_prompt>\n{user_prompt}\n</user_prompt>")

        attempted_providers: set[str] = set()
        for is_fallback, current_providers in (
            (False, provider_ids),
            (True, fallback_provider_ids),
        ):
            invalid_output = False
            for provider_id in current_providers:
                if is_fallback and provider_id in attempted_providers:
                    continue
                attempted_providers.add(provider_id)
                for attempt in range(self.network_conf["reply_retry_times"]):
                    if attempt:
                        logger.warning(
                            f"Retrying reply request {attempt}. provider_id: {provider_id}"
                        )
                    reply_hooks = _ReplyToolHooks()
                    llm_resp = None
                    error = None
                    try:
                        llm_resp = await self.provider_health.call(
                            self.context.tool_loop_agent,
                            count_failure=lambda: not reply_hooks.called_tools,
                            chat_provider_id=provider_id,
                            **request_kwargs,
                            **(
                                {"agent_hooks": reply_hooks} if use_native_tools else {}
                            ),
                        )
                    except ProviderUnavailableError:
                        break
                    except Exception as exc:
                        error = exc

                    request_failed = error is not None and not isinstance(
                        error, EmptyModelOutputError
                    )
                    native_tools_called = list(
                        dict.fromkeys(
                            [
                                *reply_hooks.called_tools,
                                *(getattr(llm_resp, "tools_call_name", None) or []),
                            ]
                        )
                    )
                    parsed_result = None
                    if error is None and llm_resp is not None:
                        if agent_context is not None and tool_context is not None:
                            tool_context.update(
                                json.loads(
                                    agent_context.extra.get(NATIVE_CONTEXT_KEY, "{}")
                                )
                            )
                        if llm_resp.completion_text:
                            parsed_result = await self.xml_parse.decode_llm_xml(
                                llm_resp.completion_text,
                                group_or_user_id,
                                require_reply_tags=fallback_enabled,
                            )
                        elif fallback_enabled:
                            if native_tools_called:
                                parsed_result = XmlLlmResult()
                        elif not llm_resp.reasoning_content:
                            parsed_result = XmlLlmResult()
                        if parsed_result is not None:
                            parsed_result.native_tools_called = native_tools_called

                    status = "success" if parsed_result is not None else "parse_failed"
                    extra_info = {"status": status, "output_fallback": is_fallback}
                    if error is not None:
                        extra_info["error"] = (
                            str(error) if request_failed else "EmptyModelOutputError"
                        )
                        if request_failed or not fallback_enabled:
                            extra_info["status"] = "api_failed"
                        log = logger.error if request_failed else logger.warning
                        log(
                            "Reply request failed. provider_id: %s, error: %s",
                            provider_id,
                            extra_info["error"],
                        )
                    await self._log_token_usage_safely(
                        bot_name=bot_name,
                        group_or_user_id=group_or_user_id,
                        type_name="reply",
                        provider_id=provider_id,
                        llm_resp=llm_resp,
                        extra_info=extra_info,
                    )
                    if llm_resp is not None:
                        if native_tools_called:
                            logger.info(
                                f"\n<tools_call>\n{native_tools_called}\n</tools_call>"
                            )
                        if llm_resp.reasoning_content:
                            logger.info(
                                f"\n<reasoning>\n{llm_resp.reasoning_content}\n</reasoning>"
                            )
                        if llm_resp.completion_text:
                            logger.info(
                                f"\n<completion>\n{llm_resp.completion_text}\n</completion>"
                            )

                    if parsed_result is not None:
                        return parsed_result
                    if native_tools_called:
                        logger.error(
                            "Reply failed after native tool execution; refusing to replay side effects"
                        )
                        return None
                    if request_failed:
                        continue
                    if not fallback_enabled:
                        # Legacy empty-output errors are terminal; reasoning-only
                        # or unparsable replies retain their existing retry rules.
                        if isinstance(error, EmptyModelOutputError):
                            return None if use_native_tools else XmlLlmResult()
                        if use_native_tools:
                            return None
                        continue

                    # Invalid output ends this list. Only the primary list has
                    # another stage; request errors never reach this transition.
                    invalid_output = True
                    logger.warning(
                        "Reply has no recognized XML. provider_id: %s, action: %s",
                        provider_id,
                        "stop fallback" if is_fallback else "switch to fallback list",
                    )
                    break
                if invalid_output:
                    break
            if not invalid_output:
                return None
        return None

    def _get_reply_tools(self, persona_tools: list[str] | None) -> ToolSet:
        """Prepare the available tool set once for all reply attempts.

        Args:
            persona_tools: Tool names allowed by the persona, or None for all.

        Returns:
            Active tools permitted by both plugin and persona configuration.
        """
        tool_manager = self.context.get_llm_tool_manager()
        tools_set = tool_manager.get_full_tool_set()
        # Built-in Tavily tools are not included in the full tool set.
        # Add them only when Tavily web search is enabled.
        merged_builtin_tools: list[str] = []
        try:
            provider_settings = self.context.astrbot_config_mgr.default_conf.get(
                "provider_settings", {}
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
                if builtin_name in existing_names or not tool_manager.is_builtin_tool(
                    builtin_name
                ):
                    continue
                builtin_tool = tool_manager.get_builtin_tool(builtin_name)
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
            elif tool.name in NATIVE_TOOL_CONFIG_KEYS:
                if not getattr(self.plugin, "tools_config", {}).get(
                    NATIVE_TOOL_CONFIG_KEYS[tool.name], True
                ):
                    tools_set.remove_tool(tool.name)

        if persona_tools is not None:
            if isinstance(persona_tools, (list, tuple, set)):
                allowed_names = {str(name).strip() for name in persona_tools if name}
                for tool in tools_set.tools[:]:
                    if tool.name not in allowed_names:
                        tools_set.remove_tool(tool.name)
                logger.info(
                    f"[Giftia] Persona allows {len(allowed_names)} tool names; {len(tools_set.tools)} tools remain"
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
                f"  hint: {'Built-in tool is excluded from the full tool set' if is_builtin else 'Tool is not registered'}\n"
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
        return tools_set

    async def call_llm_image_caption(
        self,
        image_urls: list[str],
        question: str | None = None,
        bot_name: str = "",
        group_or_user_id: str = "",
        *,
        provider_ids: list[str] | None = None,
    ) -> MediaCaption | None:
        """Describe images using the selected providers.

        Args:
            image_urls: Image payloads or URLs.
            question: Optional focus for the caption.
            bot_name: Bot requesting the caption.
            group_or_user_id: Conversation identifier.
            provider_ids: Request-specific providers, or None for the regular list.

        Returns:
            The parsed caption, or None when disabled or all attempts fail.
        """
        if not self.caption_config.get("image_caption_enabled", True):
            return None
        logger.info(f"调用LLM生成图片描述，共{len(image_urls)}张图片")
        for provider_id in (
            self.media_caption_provider_ids if provider_ids is None else provider_ids
        ):
            for i in range(self.media_caption_retry_times):
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
                    llm_resp = await self.provider_health.call(
                        self.context.llm_generate,
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
                except ProviderUnavailableError:
                    break
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
        *,
        provider_ids: list[str] | None = None,
    ) -> MediaCaption | None:
        """Describe audio using the selected providers.

        Args:
            audio_urls: Audio files or URLs.
            question: Optional focus for the caption.
            bot_name: Bot requesting the caption.
            group_or_user_id: Conversation identifier.
            provider_ids: Request-specific providers, or None for the regular list.

        Returns:
            The parsed caption, or None when disabled or all attempts fail.
        """
        if not self.caption_config.get("audio_caption_enabled", True):
            return None
        logger.info(f"调用LLM生成音频描述，共{len(audio_urls)}个音频")
        for provider_id in (
            self.media_caption_provider_ids if provider_ids is None else provider_ids
        ):
            for i in range(self.media_caption_retry_times):
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
                    llm_resp = await self.provider_health.call(
                        self.context.llm_generate,
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
                except ProviderUnavailableError:
                    break
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
        if not self.caption_config.get("image_caption_enabled", True):
            return False, None
        logger.info(f"调用LLM生成表情包分析结果，共{len(image_urls)}张图片")
        for provider_id in self.media_caption_provider_ids:
            for i in range(self.media_caption_retry_times):
                if i > 0:
                    logger.warning(f"LLM表情包分析失败，{provider_id} 重试第 {i} 次")
                try:
                    unique_prompt = get_sticker_analysis_prompt(
                        categories=categories,
                        fingerprint=",".join(image_urls),
                    )
                    llm_resp = await self.provider_health.call(
                        self.context.llm_generate,
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
                except ProviderUnavailableError:
                    break
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
        *,
        provider_ids: list[str] | None = None,
    ) -> MediaCaption | None:
        """Describe a video using a provider with native video understanding.

        Args:
            video_url: Video payload or URL.
            question: Optional question about the video.
            bot_name: Bot requesting the caption.
            group_or_user_id: Conversation identifier.
            provider_ids: Request-specific providers, or None for the regular list.

        Returns:
            The parsed caption, or None when disabled or all attempts fail.
        """
        if not self.caption_config.get("video_caption_enabled", True):
            return None
        logger.info(f"调用LLM生成原生视频描述(音频与画面同频)，带问题={bool(question)}")
        for provider_id in (
            self.media_caption_provider_ids if provider_ids is None else provider_ids
        ):
            for i in range(self.media_caption_retry_times):
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
                        llm_resp = await self.provider_health.call(
                            self.context.llm_generate,
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
                except ProviderUnavailableError:
                    break
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
