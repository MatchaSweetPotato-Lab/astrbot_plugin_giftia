from __future__ import annotations

import json
import os


USER_PROFILE_FIELD_KEYS = (
    "call_name",
    "personality",
    "interests",
    "attitude",
    "agreements",
    "extra",
    "avatar_description",
)


def optional_int(
    value, default: int | None = None, min_value: int | None = None
) -> int | None:
    if value is None or value == "":
        return default
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    if min_value is not None:
        result = max(min_value, result)
    return result


def optional_bool(value, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def safe_json_dict(raw) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def safe_path_join(base_dir: str, rel_path: str) -> str | None:
    """
    Safely join base_dir and rel_path ensuring the resulting path is strictly within base_dir.
    Returns absolute target file path if valid, or None if path traversal attempt detected.
    """
    if not base_dir or not rel_path:
        return None
    base_abs = os.path.abspath(base_dir)
    clean_rel = str(rel_path).lstrip("/\\")
    full_path = os.path.abspath(os.path.join(base_abs, clean_rel))
    try:
        common = os.path.commonpath([base_abs, full_path])
        if common == base_abs:
            return full_path
    except ValueError:
        pass
    return None


def read_file_to_base64(file_path: str, fallback_mime: str = "application/octet-stream") -> tuple[str, str]:
    """
    Read file content and convert to base64 string and mime type.
    Returns (b64_string, mime_type).
    """
    import base64
    import mimetypes

    ext = os.path.splitext(file_path)[1].lower()
    custom_map = {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".opus": "audio/opus",
    }
    mime_type = custom_map.get(ext)
    if not mime_type:
        mime_type, _ = mimetypes.guess_type(file_path)
    if not mime_type:
        mime_type = fallback_mime

    with open(file_path, "rb") as f:
        b64_str = base64.b64encode(f.read()).decode("utf-8")

    return b64_str, mime_type


