import ast
import json
import logging
import re

from ..utils.schemas import MediaCaption

logger = logging.getLogger("astrbot")


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

    # 2. 移除末尾多余逗号 (trailing commas)
    try:
        s_fixed = re.sub(r",\s*([\]\}])", r"\1", s)
        return json.loads(s_fixed)
    except Exception:
        pass

    # 3. 移除 // 单行注释与 /* */ 块级注释
    try:
        s_no_comments = re.sub(r"^\s*//.*$", "", s, flags=re.MULTILINE)
        s_no_comments = re.sub(r"(?<![:\\])//.*$", "", s_no_comments, flags=re.MULTILINE)
        s_no_comments = re.sub(r"/\*.*?\*/", "", s_no_comments, flags=re.DOTALL)
        s_no_comments = re.sub(r",\s*([\]\}])", r"\1", s_no_comments)
        return json.loads(s_no_comments)
    except Exception:
        pass

    # 4. 尝试通过 ast.literal_eval 兜底（处理单引号或弱语法）
    try:
        ast_str = re.sub(r"\btrue\b", "True", s, flags=re.IGNORECASE)
        ast_str = re.sub(r"\bfalse\b", "False", ast_str, flags=re.IGNORECASE)
        ast_str = re.sub(r"\bnull\b", "None", ast_str, flags=re.IGNORECASE)
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
