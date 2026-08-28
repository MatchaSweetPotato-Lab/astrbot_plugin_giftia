import json
import time
from datetime import datetime
import aiosqlite
from .base import BaseRepository
from ...utils.schemas import Status

def parse_custom_status_json(raw: str | None) -> dict[str, str]:
    """安全解析 custom_status 的 JSON 字符串并过滤空键值对。"""
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
        if isinstance(loaded, dict):
            return {
                str(k).strip(): str(v).strip()
                for k, v in loaded.items()
                if str(k).strip() and str(v).strip()
            }
    except Exception:
        pass
    return {}


class BotStatusRepository(BaseRepository):
    async def get_bot_status(self, group_or_user_id: str, bot_name: str) -> Status:
        async with self.conn.execute(
            """
            SELECT mood, state, memory, action, energy, custom_status, updated_at FROM bot_status WHERE group_or_user_id = ? AND bot_name = ?
            """,
            (group_or_user_id, bot_name),
        ) as cursor:
            row = await cursor.fetchone()

        if row:
            try:
                ts = datetime.strptime(
                    row["updated_at"], "%Y-%m-%d %H:%M:%S"
                ).timestamp()
            except Exception:
                ts = 0.0

            custom_status = parse_custom_status_json(
                row["custom_status"] if "custom_status" in row.keys() else None
            )

            return Status(
                mood=row["mood"],
                state=row["state"],
                memory=row["memory"],
                action=row["action"],
                energy=row["energy"],
                timestamp=ts,
                last_updated=ts,
                custom_status=custom_status,
            )
        else:
            return Status(
                mood="开心",
                state="发呆",
                memory="",
                action="拿起手机聊天",
                energy="80",
                timestamp=0.0,
                last_updated=0.0,
                custom_status={},
            )

    async def upsert_bot_status(
        self, group_or_user_id: str, bot_name: str, status: Status
    ):
        now = datetime.now()
        update_time = now.strftime("%Y-%m-%d %H:%M:%S")
        status.last_updated = now.replace(microsecond=0).timestamp()
        custom_status_str = (
            json.dumps(status.custom_status, ensure_ascii=False)
            if status.custom_status
            else "{}"
        )
        await self.conn.execute(
            """
            INSERT INTO bot_status (group_or_user_id, bot_name, mood, state, memory, action, energy, custom_status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(group_or_user_id, bot_name) DO UPDATE SET
                mood=excluded.mood,
                state=excluded.state,
                memory=excluded.memory,
                action=excluded.action,
                energy=excluded.energy,
                custom_status=excluded.custom_status,
                updated_at=excluded.updated_at
            """,
            (
                group_or_user_id,
                bot_name,
                status.mood,
                status.state,
                status.memory,
                status.action,
                status.energy,
                custom_status_str,
                update_time,
                update_time,
            ),
        )
        await self.conn.commit()

    async def delete_bot_status(self, group_or_user_id: str, bot_name: str):
        await self.conn.execute(
            """
            DELETE FROM bot_status WHERE group_or_user_id = ? AND bot_name = ?
            """,
            (group_or_user_id, bot_name),
        )
        await self.conn.commit()

