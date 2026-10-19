from datetime import datetime

USER_PROFILE_FIELDS = {
    "call_name": "称呼",
    "aliases": "其他外号",
    "attitude": "互动态度",
    "avatar_description": "头像描述",
}


def build_user_profile_report(
    bot_name: str,
    nickname: str,
    session_id: str,
    user_id: str,
    record: dict,
    recent_relation_changes: list[dict] | None = None,
) -> dict:
    """Build shared text and image data from a profile in the current session.

    Args:
        bot_name: Internal bot name.
        nickname: Bot display name.
        session_id: Current group or private conversation ID.
        user_id: Target user's platform ID.
        record: Stored profile, including relationship data.
        recent_relation_changes: Affinity changes in newest-first order.

    Returns:
        JSON-compatible template data with consistent labels and empty values.
    """
    empty_values = {"", "暂无记录", "暂无", "无", "未知", "none", "null", "n/a"}
    fields = {}
    for field in USER_PROFILE_FIELDS:
        value = str(record.get(field) or "").strip()
        fields[field] = "" if value.casefold() in empty_values else value
    relation = record.get("relation")
    if isinstance(relation, str) and relation.strip().casefold() in empty_values:
        relation = None
    if relation is not None:
        relation = round(float(relation), 1)

    changes = []
    for change in (recent_relation_changes or [])[:3]:
        timestamp = str(change.get("occurred_at") or change.get("created_at") or "")
        reason = str(change.get("reason") or "").strip()
        display_time = timestamp
        if timestamp:
            try:
                display_time = (
                    datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    .astimezone()
                    .strftime("%Y-%m-%d %H:%M")
                )
            except ValueError:
                pass
        changes.append(
            {
                "delta": int(change["delta"]),
                "group_or_user_id": str(change.get("group_or_user_id") or ""),
                "source_label": str(change.get("source_label") or ""),
                "reason": "" if reason.casefold() in empty_values else reason,
                "occurred_at": str(change.get("occurred_at") or ""),
                "created_at": str(change.get("created_at") or ""),
                "time": display_time,
            }
        )
    return {
        "report_type": "user_profile",
        "title": "用户画像",
        "bot_name": bot_name,
        "nickname": nickname,
        "session_id": str(session_id),
        "user_id": str(user_id),
        "generated_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
        **fields,
        "relation": relation,
        "base_relation": record.get("base_relation", 0),
        "interaction_score": round(float(record.get("interaction_score", 50)), 1),
        "recent_score": round(float(record.get("recent_score", 50)), 1),
        "long_term_score": round(float(record.get("long_term_score", 50)), 1),
        "total_count": record.get("total_count", 0),
        "recent_count": record.get("recent_count", 0),
        "profile_fields": {
            label: fields[field]
            for field, label in USER_PROFILE_FIELDS.items()
            if fields[field]
        },
        "recent_relation_changes": changes,
    }
