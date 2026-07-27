import json

USER_PROFILE_FIELD_KEYS = (
    "call_name",
    "personality",
    "interests",
    "attitude",
    "agreements",
    "extra",
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
