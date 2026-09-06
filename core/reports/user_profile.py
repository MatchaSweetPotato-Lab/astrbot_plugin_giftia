from datetime import datetime

USER_PROFILE_FIELDS = {
    "call_name": "称呼",
    "aliases": "其他外号",
    "personality": "性格风格",
    "interests": "兴趣话题",
    "attitude": "互动态度",
    "agreements": "关键约定",
    "extra": "其他补充",
    "avatar_description": "头像描述",
}


def build_user_profile_report(
    bot_name: str, nickname: str, session_id: str, user_id: str, record: dict
) -> dict:
    """Build shared text and image data from a profile in the current session.

    Args:
        bot_name: Internal bot name.
        nickname: Bot display name.
        session_id: Current group or private conversation ID.
        user_id: Target user's platform ID.
        record: Stored profile, including relationship data.

    Returns:
        JSON-compatible template data with consistent labels and empty values.
    """
    fields = {
        field: str(record.get(field) or "").strip() or "暂无记录"
        for field in USER_PROFILE_FIELDS
    }
    return {
        "report_type": "user_profile",
        "title": "用户画像",
        "bot_name": bot_name,
        "nickname": nickname,
        "session_id": str(session_id),
        "user_id": str(user_id),
        "generated_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
        **fields,
        "relation": record["relation"]
        if record.get("relation") is not None
        else "暂无记录",
        "relation_title": str(record.get("title") or "").strip() or "暂无记录",
        "profile_fields": {
            label: fields[field] for field, label in USER_PROFILE_FIELDS.items()
        },
    }
