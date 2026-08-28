import re
import logging
from .schemas import SYSTEM_XML_TAGS

logger = logging.getLogger("astrbot")

def replace_outside_quotes(s: str, target: str, replacement: str) -> str:
    """在忽略双引号（及其转义）内部字符的前提下，替换字符串中的指定字符"""
    in_quotes = False
    escaped = False
    result = []
    for char in s:
        if char == '"' and not escaped:
            in_quotes = not in_quotes
        elif char == '\\' and in_quotes:
            escaped = not escaped
        else:
            escaped = False
            
        if not in_quotes and char == target:
            result.append(replacement)
        else:
            result.append(char)
    return "".join(result)

def escape_tags_in_code_blocks(text: str) -> str:
    """转义 Markdown 代码块（```...```）及行内代码（`...`）中的 < 和 >，防止其中的代码语法破坏 XML 结构树"""
    pattern = r"(```[a-zA-Z]*\n[\s\S]*?\n```|`[^`]+`)"

    def repl(match):
        content = match.group(0)
        return content.replace('<', '&lt;').replace('>', '&gt;')

    return re.sub(pattern, repl, text)


def clean_llm_completion(text: str) -> str:
    """
    针对低智/弱模型输出流口水（如重复的 tool_call、标签拼写错误、中文标点引起的 JSON 损坏、哈希表示泄露等）的清洗函数。
    """
    if not text:
        return ""

    # 0. 转义代码块/行内代码/孤立文本标注中的 XML 标签，防止其干扰 XML 结构树
    cleaned = escape_tags_in_code_blocks(text)

    # 1. 规范化角括号 (例如 ‹ 和 › 替换为 < 和 >)
    cleaned = cleaned.replace("‹", "<").replace("›", ">")

    # 2. 修复常见标签名拼写错误
    # 比如 tool_cal1, too1_ca11, too1_ca11 -> tool_call
    cleaned = re.sub(r'<(\s*/?\s*)[tT][o0l1]{3,4}[_]?[cC][aA][l10o]{2,4}\b', r'<\1tool_call', cleaned)
    cleaned = re.sub(r'<(\s*/?\s*)[mM][eE][sS]+[aA]+[gG]?[eE]?', r'<\1message', cleaned)
    cleaned = re.sub(r'<(\s*/?\s*)[sS][tT][aA]?[tT][uU][sS]\b', r'<\1status', cleaned)

    # 2.5 修复工具调用中把参数错误写在属性上的情况 (如 <tool_call name="xyz" args={"a": 1}</tool_call> 或缺少闭合角括号)
    cleaned = re.sub(
        r'<tool_call\s+name=["\']([^"\']*)["\']\s+(?:args|arguments|parameters)\s*=\s*["\']?([\{｛].*?[\}｝])["\']?\s*(?:>)?\s*</tool_call>',
        r'<tool_call name="\1">\2</tool_call>',
        cleaned,
        flags=re.DOTALL | re.IGNORECASE
    )

    # 3. 修复属性重复或异常 (如 name="send_meme" name="send_meme" 或 name="send_meme"x)
    # 我们只对支持的有效标签执行此操作，以免误伤用户正文中的类似结构
    valid_tags = SYSTEM_XML_TAGS
    tags_pattern = "|".join(valid_tags)
    tag_pattern = r'<\s*(/?)\s*(' + tags_pattern + r')(?=[\s/>])([^>]*?)(/?)\s*>'

    def clean_tag_attributes(match):
        is_close = match.group(1)  # "/" if closing tag (e.g. </tool_call>)
        tag_name = match.group(2)
        attrs_part = match.group(3)
        self_closing = match.group(4) or ""

        if is_close:
            return f"</{tag_name}>"

        # 提取合法的 key="value" 或 key='value' 属性对，或未加引号的 key=value（支持包含中文字符的属性名）
        attrs = re.findall(
            r'([^\s=>/]+)\s*=\s*("[^"]*"|\'[^\']*\'|[^>\s/]+)', attrs_part
        )
        seen = set()
        unique_attrs = []
        for name, val in attrs:
            if name not in seen:
                seen.add(name)
                # 自动为未被单双引号包裹的属性值加上双引号
                if not (val.startswith('"') and val.endswith('"')) and not (val.startswith("'") and val.endswith("'")):
                    val = f'"{val}"'
                unique_attrs.append(f'{name}={val}')

        attrs_str = " " + " ".join(unique_attrs) if unique_attrs else ""
        if self_closing:
            return f"<{tag_name}{attrs_str} />"
        return f"<{tag_name}{attrs_str}>"

    cleaned = re.sub(tag_pattern, clean_tag_attributes, cleaned, flags=re.IGNORECASE)

    # 4. 提取并修复 tool_call 内部的 JSON 格式 (替换全角符号、修正不匹配的括弧)
    def fix_tool_call_json(match):
        start_tag = match.group(1)
        json_content = match.group(2)
        end_tag = match.group(3)

        # 替换全角括号
        json_content = json_content.replace("｛", "{").replace("｝", "}")
        json_content = json_content.replace("［", "[").replace("］", "]")
        
        # 替换中文智能引号，确保内部的普通属性名称能够被双引号正确闭合
        json_content = json_content.replace("“", '"').replace("”", '"')
        json_content = json_content.replace("‘", "'").replace("’", "'")

        # 利用引号状态机替换引号外部的中文逗号和冒号，保留字符串内部的中文标点
        json_content = replace_outside_quotes(json_content, "，", ",")
        json_content = replace_outside_quotes(json_content, "：", ":")

        # 修复括号不匹配 (例如 { 开头，] 结尾)
        json_content = json_content.strip()
        if json_content.startswith("{") and json_content.endswith("]"):
            json_content = json_content[:-1] + "}"
        elif json_content.startswith("[") and json_content.endswith("}"):
            json_content = json_content[:-1] + "]"

        return f'{start_tag}{json_content}{end_tag}'

    # 使用 (?!<\s*/?\s*tool_call) 负向先行断言，防止正则贪婪匹配跨越了多个不同的 tool_call 标签
    cleaned = re.sub(
        r'(<\s*tool_call[^>]*>)((?:(?!<\s*/?\s*tool_call).)*?)(<\s*/\s*tool_call\s*>)', 
        fix_tool_call_json, 
        cleaned, 
        flags=re.DOTALL | re.IGNORECASE
    )

    # 5. 去除 message 标签内可能泄漏的图片/语音哈希文本表示，如 [图片:43064fbb90cd5421]
    def clean_message_leaks(match):
        start_tag = match.group(1)
        msg_content = match.group(2)
        end_tag = match.group(3)
        # 去除格式形如 [图片:hash] 或 [语音:hash] 的文本
        fixed_content = re.sub(r'\[(图片|语音)[:：]\s*[a-zA-Z0-9_-]{8,64}\]', '', msg_content)
        return f'{start_tag}{fixed_content}{end_tag}'

    cleaned = re.sub(
        r'(<\s*message[^>]*>)(.*?)(<\s*/\s*message\s*>)', 
        clean_message_leaks, 
        cleaned, 
        flags=re.DOTALL | re.IGNORECASE
    )

    # 6. 去重完全重复的 tool_call（防流口水输出相同的工具调用）
    cleaned = deduplicate_tool_calls(cleaned)

    return cleaned

def deduplicate_tool_calls(completion_text: str) -> str:
    """从后往前删除内容完全相同的重复 tool_call"""
    pattern = r'(<\s*tool_call[^>]*>.*?<\s*/\s*tool_call\s*>)'
    matches = list(re.finditer(pattern, completion_text, flags=re.DOTALL | re.IGNORECASE))
    
    seen_calls = set()
    to_remove = []
    
    for m in matches:
        tc_str = m.group(1)
        # 忽略所有空白字符来进行内容比对
        normalized = re.sub(r'\s+', '', tc_str)
        if normalized in seen_calls:
            to_remove.append((m.start(), m.end()))
        else:
            seen_calls.add(normalized)
            
    # 从后往前删除，避免索引偏移
    new_text = completion_text
    for start, end in reversed(to_remove):
        new_text = new_text[:start] + new_text[end:]
        
    if to_remove:
        logger.info(f"[Giftia] 防流口水：已从大模型回复中过滤了 {len(to_remove)} 个重复的 tool_call 标签")
        
    return new_text

def normalize_text_for_dedup(text: str) -> str:
    """
    对文本进行归一化处理，剥离所有的 At 提及、图片/语音哈希表示以及首尾空白和常用标点，
    用于防复读（流口水）的核心内容精准比对。
    """
    if not text:
        return ""
    # 1. 移除 <@user_id> 形式的 At 提及 (msg_logs 中的 At 格式)
    normalized = re.sub(r'<@\w+>', '', text)
    # 2. 移除 @昵称 形式的 At 提及 (如 @流萤)
    normalized = re.sub(r'@\S+', '', normalized)
    # 3. 移除可能存在的 [图片:hash] 或 [语音:hash] 占位符
    normalized = re.sub(r'\[(图片|语音)[:：]\s*[a-zA-Z0-9_-]{8,64}\]', '', normalized)
    # 4. 移除首尾空白和常用标点符号
    normalized = normalized.strip(" \t\n\r,.，。!?！？~")
    return normalized

def filter_duplicate_replies(llm_result, sent_messages: list[str]) -> None:
    """
    针对多轮工具递归调用，过滤掉已经发送过的重复会话消息（防止跨轮次/单会话多消息流口水）。
    直接就地修改 llm_result 对象的 msg_chains, msg_logs, msg_texts 属性。
    """
    if not llm_result or not llm_result.msg_chains:
        return

    filtered_chains = []
    filtered_logs = []
    filtered_texts = []
    message_index_map = {}
    
    # 提取已发送历史的归一化核心内容集合，避免重复归一化
    normalized_sent = {normalize_text_for_dedup(m) for m in sent_messages if m}

    for i, chain in enumerate(llm_result.msg_chains):
        log = llm_result.msg_logs[i] if i < len(llm_result.msg_logs) else ""
        text = llm_result.msg_texts[i] if i < len(llm_result.msg_texts) else ""
        
        # 提取当前消息的归一化核心内容
        normalized = normalize_text_for_dedup(log)
        
        if normalized:
            # 核心内容如果在已发送历史或当前响应前面的消息中出现过，判定为流口水并过滤
            if normalized in normalized_sent:
                logger.info(f"[Giftia] 防流口水：拦截到重复的会话消息: {log.strip()} (归一化核心: {normalized})")
                continue
            normalized_sent.add(normalized)
            sent_messages.append(log)
            
        message_index_map[i] = len(filtered_chains)
        filtered_chains.append(chain)
        filtered_logs.append(log)
        filtered_texts.append(text)
        
    llm_result.msg_chains = filtered_chains
    llm_result.msg_logs = filtered_logs
    llm_result.msg_texts = filtered_texts
    if getattr(llm_result, "output_order", None):
        new_order = []
        for kind, old_index in llm_result.output_order:
            if kind == "message":
                if old_index in message_index_map:
                    new_order.append((kind, message_index_map[old_index]))
            else:
                new_order.append((kind, old_index))
        llm_result.output_order = new_order
