from datetime import datetime

from ..utils.schemas import normalize_energy


def build_status_report(bot_name: str, nickname: str, session_id: str, status) -> dict:
    """Build the public status data shared by text and image reports.

    Args:
        bot_name: Internal bot name.
        nickname: Display name.
        session_id: Current group or private conversation ID.
        status: Current cached bot status.

    Returns:
        JSON-compatible template data, excluding private thoughts and memory.
    """
    energy_text = f"{normalize_energy(status.energy)}%"
    percent = int(energy_text.rstrip("%"))
    return {
        "report_type": "status",
        "title": "Bot 状态看板",
        "bot_name": bot_name,
        "nickname": nickname,
        "session_id": str(session_id),
        "generated_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
        "mood": status.mood or "平稳",
        "state": status.state or "空闲",
        "action": status.action or "待机",
        "energy": energy_text,
        "energy_percent": percent,
        "custom_status": {
            str(key): str(value)
            for key, value in (status.custom_status or {}).items()
            if str(value).strip()
        },
    }
