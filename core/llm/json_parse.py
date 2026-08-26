import ast
import json
import logging
import re

from ..utils.schemas import MediaCaption

logger = logging.getLogger("astrbot")


def _clean_json_comments_and_trailing_commas(s: str) -> str:
    """安全移除 JSON/JSON5 中的注释与尾随逗号，严格保护带引号字符串内部内容（如 URL、//、/* 等）"""
    # 1. 移除 // 单行注释与 /* */ 块级注释 (在引号保护下匹配)
    pattern_comments = r'("(?:\\.|[^"\\])*")|//[^\r\n]*|/\*[\s\S]*?\*/'

    def comment_replacer(m: re.Match) -> str:
        if m.group(1) is not None:
            return m.group(1)
        return ""

    no_comments = re.sub(pattern_comments, comment_replacer, s)

    # 2. 移除对象或数组末尾多余的逗号 (trailing commas)
    pattern_trailing_commas = r'("(?:\\.|[^"\\])*")|,\s*([\]\}])'

    def comma_replacer(m: re.Match) -> str:
        if m.group(1) is not None:
            return m.group(1)
        return m.group(2)

    return re.sub(pattern_trailing_commas, comma_replacer, no_comments)


def _robust_json_loads(s: str) -> dict | list | None:
    """尝试以多种容错方式解析 JSON 字符串"""
    if not s:
        return None
    s = s.strip()

    # 1. 标准 json.loads 解析
    try:
        return json.loads(s)
    except Exception:
        pass

    # 2. 安全清理注释与尾随逗号后再次尝试标准解析
    try:
        cleaned = _clean_json_comments_and_trailing_commas(s)
        return json.loads(cleaned)
    except Exception:
        pass

    # 3. 尝试通过 ast.literal_eval 兜底（处理单引号与 Python 风格字面量）
    try:
        # 引号感知替换：仅在单双引号外部将 true/false/null 替换为 Python 的 True/False/None
        # 避免破坏如 {'caption': 'null'} 或 {'text': 'it is true'} 等字面量内容
        ast_str = re.sub(
            r"([\"'])(?:\\[\s\S]|(?!\1)[\s\S])*?\1|(?<!\w)(true|false|null)(?!\w)",
            lambda match: match.group(0)
            if match.group(1)
            else {"true": "True", "false": "False", "null": "None"}[match.group(2).lower()],
            s,
            flags=re.IGNORECASE,
        )
        res = ast.literal_eval(ast_str)
        if isinstance(res, (dict, list)):
            return res
    except Exception:
        pass

    return None


def parse_markdown_json(text: str) -> dict | list | None:
    """解析可能包含前导思考文本或包裹在 markdown 语法中的 JSON 字符串 (支持 Object 与 Array)"""
    if not text:
        return None
    clean_text = text.strip()

    # 1. 尝试从 ```json [ ... ] ``` 或 ``` { ... } ``` 代码块中提取
    codeblock_match = re.search(r"```(?:json)?\s*([\[\{].*?[\]\}])\s*```", clean_text, re.DOTALL)
    if codeblock_match:
        json_str = codeblock_match.group(1).strip()
        res = _robust_json_loads(json_str)
        if res is not None:
            return res

    # 2. 查找最外层的 JSON 结构: 对象 { ... } 或 数组 [ ... ]
    obj_first = clean_text.find("{")
    obj_last = clean_text.rfind("}")
    arr_first = clean_text.find("[")
    arr_last = clean_text.rfind("]")

    candidates = []
    if obj_first != -1 and obj_last > obj_first:
        candidates.append((obj_first, clean_text[obj_first : obj_last + 1]))
    if arr_first != -1 and arr_last > arr_first:
        candidates.append((arr_first, clean_text[arr_first : arr_last + 1]))

    candidates.sort(key=lambda x: x[0])

    for _, snippet in candidates:
        res = _robust_json_loads(snippet.strip())
        if res is not None:
            return res

    # 3. 兜底直接解析整体文本
    res = _robust_json_loads(clean_text)
    if res is not None:
        return res

    logger.error(f"解析 JSON 失败, 原始文本: {text[:1000]}")
    return None


def decode_media_caption_json(
    json_str: str,
    media_type: str = "image",
) -> MediaCaption | None:
    """解析图片/视频描述的 JSON，返回 MediaCaption 对象"""
    data = parse_markdown_json(json_str)
    if not isinstance(data, dict):
        return None

    caption_text = str(
        data.get("caption") or data.get("image_description") or data.get("description") or ""
    ).strip()
    text_content = str(data.get("text") or "").strip()
    genre_val = str(data.get("genre") or "").strip()
    character_val = str(data.get("character") or "").strip()
    source_val = str(data.get("source") or "").strip()

    if not caption_text:
        if text_content:
            caption_text = f"文字内容：{text_content[:100]}"
        elif genre_val or character_val or source_val:
            parts = [p for p in [genre_val, character_val, source_val] if p]
            caption_text = f"识别结果：{' - '.join(parts)}"
        else:
            logger.warning(f"媒体转述主字段 caption 为空且无任何有效识别内容, 原始数据: {json_str[:500]}")
            return None

    return MediaCaption(
        media_type=media_type,
        genre=genre_val,
        character=character_val,
        source=source_val,
        text=text_content,
        caption=caption_text,
        is_captioned=True,
    )


def decode_media_audio_json(json_str: str) -> MediaCaption | None:
    """解析音频转述的 JSON，返回 MediaCaption 对象"""
    data = parse_markdown_json(json_str)
    if not isinstance(data, dict):
        return None

    caption_text = str(
        data.get("caption") or data.get("audio_description") or data.get("description") or ""
    ).strip()
    text_content = str(data.get("text") or "").strip()
    genre_val = str(data.get("genre") or "").strip()
    character_val = str(data.get("character") or "").strip()
    source_val = str(data.get("source") or "").strip()

    if not caption_text:
        if text_content:
            caption_text = f"语音内容：{text_content[:100]}"
        elif genre_val or character_val or source_val:
            parts = [p for p in [genre_val, character_val, source_val] if p]
            caption_text = f"音频识别：{' - '.join(parts)}"
        else:
            logger.warning(f"音频转述主字段 caption 为空且无任何有效识别内容, 原始数据: {json_str[:500]}")
            return None

    return MediaCaption(
        media_type="audio",
        genre=genre_val,
        character=character_val,
        source=source_val,
        text=text_content,
        caption=caption_text,
        is_captioned=True,
    )
