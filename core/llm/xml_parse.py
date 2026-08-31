import ast
import json
import re
from xml.sax.saxutils import escape

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from astrbot.api import logger
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.core.message.components import BaseMessageComponent

from ..database.data_cache import DataCache
from ..utils.anti_drool import clean_llm_completion
from ..utils.emoji_manager import EmojiManager
from ..utils.meme_manager_client import MemeManagerClient
from ..utils.schemas import (
    FLAT_CLOSABLE_TAGS,
    Decision,
    SetCallNameRequest,
    TTSRequest,
    XmlLlmResult,
)


class XmlParse:
    def __init__(
        self,
        data_cache: DataCache,
        emoji_manager: EmojiManager,
        sticker_summaries: list[str] | None = None,
        meme_manager_client: MemeManagerClient | None = None,
    ):
        self.data_cache = data_cache
        self.emoji_manager = emoji_manager
        self.meme_manager_client = meme_manager_client
        import random

        self.random = random
        self.sticker_summaries = sticker_summaries or ["图片"]

    @staticmethod
    def str_to_int_bool(val):
        """将XML属性转为决策整数"""
        if val is None:
            return 2  # 未决策
        v = str(val).lower().strip()
        if v in ["true", "1", "yes", "t"]:
            return 1
        if v in ["false", "0", "no", "f"]:
            return 0
        return 2  # 无法解析的情况

    def decode_decision_xml(self, xml_str: str) -> Decision | None:
        """解码决策XML字符串"""
        if not xml_str:
            return None
        try:
            result = Decision()
            safe_xml = self.preprocess_xml(xml_str)
            soup = BeautifulSoup(safe_xml, "xml")
            decision_node = soup.find("decision")
            if decision_node:
                result.reply_decision = self.str_to_int_bool(decision_node.get("reply"))
                result.use_rag = self.str_to_int_bool(decision_node.get("use_rag"))
                result.rag_query = str(
                    decision_node.get("rag_query", "")
                ) or decision_node.get_text(strip=True)
                raw_meme_tags = decision_node.get("meme_tags")
                if raw_meme_tags:
                    result.meme_tags = str(raw_meme_tags).strip()
            if result.reply_decision == 2 or result.use_rag == 2:
                logger.error(f"决策数据无效: {result}, xml_str: {xml_str[:1000]}")
                return None
            return result
        except Exception as e:
            logger.error(f"解析决策XML失败: {e}, xml_str: {xml_str[:1000]}")
            return None

    async def decode_llm_xml(
        self, xml_str: str, group_or_user_id: str
    ) -> XmlLlmResult | None:
        """解码LLM返回的XML字符串"""
        if not xml_str:
            return None
        try:
            result = XmlLlmResult()
            safe_xml = self.preprocess_xml(xml_str)
            # BeautifulSoup 容错解析
            soup = BeautifulSoup(safe_xml, "xml")
            root = soup.find("root")
            if not root:
                return None

            for child in root.find_all(recursive=False):
                tag_name = child.name

                if tag_name == "status":
                    raw_text = child.get_text(strip=True)
                    parsed_data = dict(re.findall(r"(\w+)[:：]\s*([^\n]+)", raw_text))
                    result.status.mood = (
                        parsed_data.get("心情", "").strip().strip("\"'")
                    )
                    result.status.state = (
                        parsed_data.get("状态", "").strip().strip("\"'")
                    )
                    result.status.action = (
                        parsed_data.get("动作", "").strip().strip("\"'")
                    )
                    result.status.energy = (
                        parsed_data.get("能量", "100").strip().strip("\"'")
                    )
                    result.status.memory = (
                        (parsed_data.get("思考") or parsed_data.get("记忆", ""))
                        .strip()
                        .strip("\"'")
                    )

                elif tag_name == "message":
                    sub_chain: list[BaseMessageComponent] = []
                    sub_text: str = ""
                    sub_log: str = ""

                    if child.get("quote"):
                        sub_chain.append(Reply(id=child.get("quote")))

                    for content in child.contents:
                        # 如果是纯文本内容
                        if isinstance(content, NavigableString):
                            text_content = str(content).strip()
                            if text_content:
                                sub_chain.append(Plain(text=text_content))
                                sub_text += text_content
                                sub_log += text_content

                        # 如果是子标签 (<at>, <sticker>, <image>等)
                        elif isinstance(content, Tag):
                            if content.name == "at":
                                user_id = (
                                    self._attr_str(content, "user_id", "")
                                    or self._attr_str(content, "id", "")
                                    or self._attr_str(content, "qq", "")
                                )
                                if user_id:
                                    sub_chain.append(At(qq=user_id))
                                    sub_log += f" <@{user_id}>"
                                else:
                                    logger.error(
                                        f"At组件缺少user_id/id/qq属性: {content.attrs}, xml_str: {xml_str[:1000]}"
                                    )

                            elif content.name == "sticker":
                                sticker_id = self._attr_str(content, "sticker_id", "")
                                if sticker_id:
                                    img = await self._load_sticker_image(
                                        sticker_id, xml_str
                                    )
                                    if img:
                                        sub_chain.append(img)
                                        result.send_stickers.append(sticker_id)
                                        sub_log += f" [图片:{sticker_id}]"
                                else:
                                    logger.error(
                                        f"Sticker组件缺少sticker_id属性: {content.attrs}, xml_str: {xml_str[:1000]}"
                                    )

                            elif content.name == "image":
                                img_url = (
                                    self._attr_str(content, "url", "")
                                    or self._attr_str(content, "src", "")
                                    or content.get_text(strip=True)
                                )
                                if img_url:
                                    img = self._create_image_component(img_url)
                                    if img:
                                        sub_chain.append(img)
                                        sub_log += f" [图片:{img_url}]"
                                    else:
                                        logger.warning(
                                            f"Image组件URL协议不受支持 (仅支持 http/https/base64): {img_url}"
                                        )
                                else:
                                    logger.error(
                                        f"Image组件缺少url/src属性: {content.attrs}, xml_str: {xml_str[:1000]}"
                                    )

                    result.msg_chains.append(sub_chain)
                    result.msg_texts.append(sub_text)
                    result.msg_logs.append(sub_log)
                    result.output_order.append(("message", len(result.msg_chains) - 1))

                elif tag_name == "tts":
                    text = child.get_text("", strip=True)
                    if text:
                        result.tts_segments.append(
                            TTSRequest(
                                text=text,
                                lang=self._attr_str(child, "lang", "")
                                or self._attr_str(child, "language", ""),
                                emotion=self._attr_str(child, "emotion", ""),
                            )
                        )
                        result.output_order.append(
                            ("tts", len(result.tts_segments) - 1)
                        )

                elif tag_name == "at":
                    user_id = (
                        self._attr_str(child, "user_id", "")
                        or self._attr_str(child, "id", "")
                        or self._attr_str(child, "qq", "")
                    )
                    if user_id:
                        result.msg_chains.append([At(qq=user_id)])
                        result.msg_logs.append(f"<@{user_id}>")
                        result.output_order.append(
                            ("message", len(result.msg_chains) - 1)
                        )

                elif tag_name == "sticker":
                    sticker_id = self._attr_str(child, "sticker_id", "")
                    if sticker_id:
                        before_msg_count = len(result.msg_chains)
                        img = await self._load_sticker_image(sticker_id, xml_str)
                        if img:
                            result.msg_chains.append([img])
                        if len(result.msg_chains) > before_msg_count:
                            result.send_stickers.append(sticker_id)
                            result.msg_logs.append(f"[图片:{sticker_id}]")
                            result.output_order.append(
                                ("sticker", len(result.msg_chains) - 1)
                            )

                elif tag_name == "image":
                    img_url = (
                        self._attr_str(child, "url", "")
                        or self._attr_str(child, "src", "")
                        or child.get_text(strip=True)
                    )
                    if img_url:
                        img = self._create_image_component(img_url)
                        if img:
                            result.msg_chains.append([img])
                            result.msg_logs.append(f"[图片:{img_url}]")
                            result.output_order.append(
                                ("image", len(result.msg_chains) - 1)
                            )
                        else:
                            logger.warning(
                                f"Image组件URL协议不受支持 (仅支持 http/https/base64): {img_url}"
                            )
                    else:
                        logger.error(
                            f"Image组件缺少url/src属性: {child.attrs}, xml_str: {xml_str[:1000]}"
                        )
                elif tag_name == "emoji_like":
                    if self._attr_str(child, "message_id", "") and self._attr_str(
                        child, "emoji_id", ""
                    ):
                        result.emoji_ids.append(
                            (
                                self._attr_str(child, "message_id", ""),
                                self._attr_str(child, "emoji_id", ""),
                            )
                        )
                    else:
                        logger.error(
                            f"贴表情数据不完整: {child.attrs}, xml_str: {xml_str[:1000]}"
                        )

                elif tag_name == "delete":
                    msg_id = self._attr_str(child, "message_id", "") or child.get_text(
                        strip=True
                    )
                    if msg_id:
                        result.delete_message_ids.append(msg_id)

                elif tag_name == "repeat":
                    msg_id = self._attr_str(child, "message_id", "") or child.get_text(
                        strip=True
                    )
                    if msg_id:
                        result.repeat_message_ids.append(msg_id)
                    else:
                        logger.error(
                            f"复读标签缺少message_id属性: {child.attrs}, xml_str: {xml_str[:1000]}"
                        )

                elif tag_name == "like":
                    if self._attr_str(child, "user_id", "") and self._attr_str(
                        child, "count", ""
                    ):
                        result.likes.append(
                            (
                                self._attr_str(child, "user_id", ""),
                                self._attr_str(child, "count", ""),
                            )
                        )
                    else:
                        logger.error(
                            f"点赞标签数据不完整: {child.attrs}, xml_str: {xml_str[:1000]}"
                        )

                elif tag_name == "poke":
                    if self._attr_str(child, "user_id", ""):
                        result.poke.append(
                            (
                                self._attr_str(child, "group_id", "")
                                or group_or_user_id,
                                self._attr_str(child, "user_id", ""),
                            )
                        )
                    else:
                        logger.error(
                            f"戳一戳标签数据不完整: {child.attrs}, xml_str: {xml_str[:1000]}"
                        )

                elif tag_name == "ban":
                    if self._attr_str(child, "user_id", ""):
                        result.ban.append(
                            (
                                self._attr_str(child, "group_id", "")
                                or group_or_user_id,
                                self._attr_str(child, "user_id", ""),
                                self._attr_str(child, "duration", ""),
                            )
                        )
                    else:
                        logger.error(
                            f"禁言标签数据不完整: {child.attrs}, xml_str: {xml_str[:1000]}"
                        )

                elif tag_name == "kick":
                    if self._attr_str(child, "user_id", ""):
                        result.kick.append(
                            (
                                self._attr_str(child, "group_id", "")
                                or group_or_user_id,
                                self._attr_str(child, "user_id", ""),
                            )
                        )
                    else:
                        logger.error(
                            f"踢人标签数据不完整: {child.attrs}, xml_str: {xml_str[:1000]}"
                        )

                elif tag_name == "leave":
                    result.leave.append(group_or_user_id)

                elif tag_name == "set_call_name":
                    user_id = self._attr_str(child, "user_id", "").strip()
                    name = self._attr_str(child, "name", "").strip()
                    if not name:
                        name = child.get_text(strip=True)
                    result.set_call_names.append(
                        SetCallNameRequest(user_id=user_id, name=name)
                    )

                elif tag_name == "set_status":
                    key = (
                        self._attr_str(child, "key", "")
                        or self._attr_str(child, "name", "")
                    ).strip()
                    value = self._attr_str(child, "value", "").strip()
                    if key:
                        if not value and not child.has_attr("value"):
                            value = child.get_text(strip=True)
                        result.set_custom_status[key] = value
                    else:
                        # 属性键值对模式，如 <set_status 服装="女仆装" 场景="客厅"/>
                        for k, v in child.attrs.items():
                            if k not in ("key", "name", "value") and isinstance(v, str):
                                k_clean = k.strip()
                                if k_clean:
                                    result.set_custom_status[k_clean] = v.strip()
                        # 也支持纯文本形式，如 "服装: 女仆装\n场景: 客厅"
                        text_body = child.get_text(strip=True)
                        if text_body:
                            pairs = re.findall(r"([^\n:：]+)[:：]\s*([^\n]+)", text_body)
                            for k, v in pairs:
                                k_clean = k.strip()
                                v_clean = v.strip().strip("\"'")
                                if k_clean:
                                    result.set_custom_status[k_clean] = v_clean

                elif tag_name == "search_memory":
                    text = child.get_text(strip=True)
                    if text:
                        result.search_memories.append((group_or_user_id, text))

                elif tag_name == "search_chat_history":
                    keyword = self._attr_str(child, "keyword", "")
                    user_id = self._attr_str(child, "user_id", "")
                    start_time = self._attr_str(child, "start_time", "")
                    end_time = self._attr_str(child, "end_time", "")
                    sort_order = self._attr_str(child, "sort_order", "desc")
                    limit_str = self._attr_str(child, "limit", "30")
                    try:
                        limit = int(limit_str)
                    except ValueError:
                        limit = 30
                    result.search_histories.append(
                        {
                            "group_or_user_id": group_or_user_id,
                            "keyword": keyword,
                            "user_id": user_id,
                            "start_time": start_time,
                            "end_time": end_time,
                            "sort_order": sort_order,
                            "limit": limit,
                        }
                    )

                elif tag_name == "get_message_context":
                    message_id = self._attr_str(child, "message_id", "")
                    limit_str = self._attr_str(child, "limit", "30")
                    try:
                        limit = int(limit_str)
                    except ValueError:
                        limit = 30
                    if message_id:
                        result.get_message_contexts.append(
                            {
                                "group_or_user_id": group_or_user_id,
                                "message_id": message_id,
                                "limit": limit,
                            }
                        )

                elif tag_name == "delete_memory":
                    memory_id = self._attr_str(child, "id", "")
                    if memory_id:
                        result.delete_memories.append(memory_id)
                    else:
                        logger.error(
                            f"Delete memory数据不完整: {child.attrs}, xml_str: {xml_str[:1000]}"
                        )

                elif tag_name == "tool_call":
                    tool_name = self._attr_str(child, "name", "")
                    text = self._attr_str(child, "arguments", "") or child.get_text(
                        strip=True
                    )
                    arg_dict = None
                    if tool_name and text:
                        arg_dict = self.parse_str_json(text)

                    if tool_name and arg_dict is not None:
                        result.tools_to_call.append((tool_name, arg_dict))
                    else:
                        logger.error(
                            f"Tool call数据不完整或解析失败: {child.attrs}, xml_str: {xml_str[:1000]}"
                        )
                        raise ValueError(f"Malformed tool call in XML: {text}")

                elif tag_name == "schedule_task":
                    task_time = self._attr_str(child, "time", "")
                    text = child.get_text(strip=True)
                    if task_time and text:
                        result.schedule_tasks.append(
                            (
                                group_or_user_id,
                                task_time,
                                text,
                            )
                        )
                    else:
                        logger.error(
                            f"Schedule task数据不完整: {child.attrs}, xml_str: {xml_str[:1000]}"
                        )

                elif tag_name == "delete_task":
                    task_id = self._attr_str(child, "task_id", "")
                    if task_id:
                        result.delete_schedule_tasks.append(task_id)
                    else:
                        logger.error(
                            f"Delete task数据不完整: {child.attrs}, xml_str: {xml_str[:1000]}"
                        )

                elif tag_name == "all_task":
                    group_id = self._attr_str(child, "group_id", group_or_user_id)
                    result.all_tasks.append(group_id)

                elif tag_name in ("task_board", "short_task"):
                    action = self._attr_str(child, "action", "").lower().strip()
                    task_id = self._attr_str(child, "task_id", "")
                    expires_at = self._attr_str(child, "expires_at", "")
                    text = child.get_text(strip=True)
                    if action == "create" and text:
                        result.task_board_actions.append(
                            {
                                "action": "create",
                                "content": text,
                                "expires_at": expires_at,
                            }
                        )
                    elif action in ("complete", "cancel") and task_id:
                        result.task_board_actions.append(
                            {
                                "action": action,
                                "task_id": task_id,
                                "reason": text,
                            }
                        )
                    else:
                        logger.error(
                            f"Task board数据不完整: {child.attrs}, xml_str: {xml_str[:1000]}"
                        )

                elif tag_name == "add_sticker":
                    media_id = self._attr_str(child, "media_id", "")
                    if media_id:
                        result.add_stickers.append(media_id)
                    else:
                        logger.error(
                            f"Add sticker数据不完整: {child.attrs}, xml_str: {xml_str[:1000]}"
                        )

            return result
        except Exception as e:
            logger.error(f"解析LLM XML失败: {e}, xml_str: {xml_str[:1000]}")
            return None

    def parse_str_json(self, response_text: str) -> dict | None:
        """把AI回复的json字符串解析成字典"""
        clean_text = response_text.strip()
        if clean_text.startswith("```json"):
            clean_text = clean_text[7:]
        elif clean_text.startswith("```"):
            clean_text = clean_text[3:]
        if clean_text.endswith("```"):
            clean_text = clean_text[:-3]
        clean_text = clean_text.strip()

        match = re.search(r"\{.*\}", clean_text, re.DOTALL)
        if match:
            clean_text = match.group(0)
        else:
            logger.error(f"未找到JSON对象，原始文本: {response_text[:1000]}")
            return None

        try:
            return json.loads(clean_text)
        except json.JSONDecodeError:
            try:
                return ast.literal_eval(clean_text)
            except Exception as e:
                logger.error(f"解析JSON失败: {e}, clean_text: {clean_text[:1000]}")
                return None

    def close_xml_tags(self, xml_str: str) -> str:
        """Automatically close unclosed sibling XML tags.

        Args:
            xml_str: The raw XML string from the LLM.

        Returns:
            The processed XML string with properly closed tags.
        """
        flat_tags = FLAT_CLOSABLE_TAGS
        pattern = r"<\s*(/?)\s*(" + "|".join(flat_tags) + r")\b([^>]*?)(/?)\s*>"

        result = []
        open_tag = None
        open_tag_pos_in_result = None
        open_content_start_pos = 0
        last_end = 0

        def _finalize_open_tag(content_end_pos: int) -> None:
            nonlocal open_tag, open_tag_pos_in_result
            if open_tag is None:
                return
            inner_text = xml_str[open_content_start_pos:content_end_pos]
            if open_tag == "message" and len(inner_text.strip()) > 50:
                # 只将起始的 '<' 转义为 '&lt;'，精准使开标签失效，避免损坏属性内容
                tag_str = result[open_tag_pos_in_result]
                if tag_str.startswith("<"):
                    result[open_tag_pos_in_result] = "&lt;" + tag_str[1:]
            else:
                result.append(f"</{open_tag}>")
            open_tag = None
            open_tag_pos_in_result = None

        for match in re.finditer(pattern, xml_str):
            is_close = bool(match.group(1))
            tag_name = match.group(2)
            is_self_closing = bool(match.group(4))
            start, end = match.span()

            result.append(xml_str[last_end:start])

            if is_close:
                if open_tag == tag_name:
                    result.append(match.group(0))
                    open_tag = None
                    open_tag_pos_in_result = None
                else:
                    result.append(match.group(0))
            elif is_self_closing:
                _finalize_open_tag(start)
                result.append(match.group(0))
            else:
                _finalize_open_tag(start)
                open_tag = tag_name
                open_tag_pos_in_result = len(result)
                open_content_start_pos = end
                result.append(match.group(0))

            last_end = end

        result.append(xml_str[last_end:])
        _finalize_open_tag(len(xml_str))

        return "".join(result)

    def preprocess_xml(self, xml_raw: str) -> str:
        """全能型 XML 预处理：融合了 escape 的终极安全版本"""
        if not xml_raw:
            return ""

        # 先移除可能包裹整个 XML 的首尾 ```xml ... ``` 标记，避免其内部合法的 XML 标签被 escape_tags_in_code_blocks 误伤
        stripped_raw = xml_raw.strip()
        if stripped_raw.startswith("```") and stripped_raw.endswith("```"):
            first_newline = stripped_raw.find("\n")
            if first_newline != -1:
                stripped_raw = stripped_raw[first_newline + 1 : -3].strip()

        # 进行防流口水清洗（标签修正、JSON修复、哈希表示清洗、重复标签去重等）
        clean_raw = clean_llm_completion(stripped_raw)

        # 兜底清理：移除可能残留的 ``` 代码围栏块（包括 ```xml 等语言标识）
        clean_raw = re.sub(r"```[a-zA-Z0-9_\-]*", "", clean_raw)

        # 自动闭合未闭合的同级标签
        clean_str = self.close_xml_tags(clean_raw)

        # 处理 <think>...</think> 标签内部的xml，进行转义
        pattern_think = re.compile(
            r"(<\s*think\s*>)(.*?)(<\s*/\s*think\s*>)", re.DOTALL | re.IGNORECASE
        )

        def escape_think(match):
            # 使用 escape 自动转义 <, >, &
            return f"{match.group(1)}{escape(match.group(2))}{match.group(3)}"

        clean_str = pattern_think.sub(escape_think, clean_str)

        # # 模型用 Markdown 列表打草稿（比如以 * 或 1. 开头）
        # lines = clean_str.split("\n")
        # processed_lines = []
        # for line in lines:
        #     # 正则匹配：以 *、- 或数字加点开头（前面允许有空格）
        #     if re.match(r"^\s*([\*\-]|\d+\.)\s+", line):
        #         # 用 escape 把整行草稿变为安全的纯文本
        #         line = escape(line)
        #     processed_lines.append(line)

        # clean_str = "\n".join(processed_lines)

        clean_str = clean_str.strip()
        if clean_str.startswith("<root>") and clean_str.endswith("</root>"):
            clean_str = clean_str[6:-7].strip()

        # 包裹根节点
        return f"<root>{clean_str}</root>"

    @staticmethod
    def _attr_str(tag: Tag, attr_name: str, default: str = "") -> str:
        """安全获取 BeautifulSoup 标签的字符串属性"""
        val = tag.get(attr_name, default)
        # 如果类型检查器认为可能是列表，取第一个值或转为字符串
        if isinstance(val, list):
            return val[0] if val else default
        return val if val is not None else default

    @staticmethod
    def _attr_int(tag: Tag, attr_name: str, default: int = 0) -> int:
        """安全获取 BeautifulSoup 标签的整数属性"""
        val_str = XmlParse._attr_str(tag, attr_name, "")
        if not val_str:
            return default
        try:
            return int(val_str)
        except ValueError:
            return default

    @staticmethod
    def _create_image_component(img_url: str) -> Image | None:
        """根据网络 URL 或 base64 创建普通大图 (原图) 消息组件"""
        if (
            img_url.startswith("http://")
            or img_url.startswith("https://")
            or img_url.startswith("base64://")
        ):
            return Image.fromURL(img_url)
        return None

    async def _load_sticker_image(
        self, sticker_id: str, xml_str: str = ""
    ) -> Image | None:
        """加载表情包图片"""
        if not sticker_id:
            return None

        # 1. 优先尝试从 meme_manager 加载短 ID (如 mm_104, mm:104, 或纯数字)
        is_mm_id = sticker_id.startswith("mm_") or sticker_id.startswith("mm:")
        clean_mm_id = None
        if is_mm_id:
            raw_id = sticker_id[3:]
            if raw_id.isdigit():
                clean_mm_id = int(raw_id)
        elif sticker_id.isdigit() and self.meme_manager_client is not None:
            clean_mm_id = int(sticker_id)

        if clean_mm_id is not None and self.meme_manager_client is not None:
            try:
                comp = self.meme_manager_client.build_meme_component(clean_mm_id)
                if comp:
                    return comp
            except Exception as e:
                logger.error(f"[Giftia] 加载 meme_manager 表情包 {sticker_id} 失败: {e}")

        # 2. 原生 Giftia 表情包加载
        local_path = self.emoji_manager.get_sticker_path(sticker_id)
        img = None
        if local_path:
            img = Image.fromFileSystem(str(local_path))
        else:
            media_caption = await self.data_cache.get_caption_by_hash(sticker_id)
            if media_caption and media_caption.url:
                img = Image.fromURL(media_caption.url)
            else:
                logger.error(f"未找到图片: {sticker_id}, xml_str: {xml_str[:1000]}")
        return img
