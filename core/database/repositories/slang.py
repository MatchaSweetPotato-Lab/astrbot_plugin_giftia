from datetime import datetime

from .base import BaseRepository


class SlangRepository(BaseRepository):
    """Persist literal vocabulary definitions within a bot and session."""

    async def get_entries(self, bot_name: str, group_or_user_id: str) -> list[dict]:
        """Read the current session vocabulary in a deterministic order.

        Args:
            bot_name: Owning bot name.
            group_or_user_id: Owning group or private conversation.

        Returns:
            Stored terms, definitions, and timestamps.
        """
        async with self.conn.execute(
            "SELECT * FROM slang WHERE bot_name = ? AND group_or_user_id = ? "
            "ORDER BY term COLLATE BINARY",
            (bot_name, group_or_user_id),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def set_entry(
        self, bot_name: str, group_or_user_id: str, term: str, description: str
    ) -> None:
        """Insert a term or atomically replace its definition.

        Args:
            bot_name: Owning bot name.
            group_or_user_id: Owning group or private conversation.
            term: Literal, case-sensitive term without whitespace.
            description: Complete replacement definition.

        Raises:
            ValueError: A required value is blank, invalid, or too long.
        """
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (bot_name, group_or_user_id, term, description)
        ):
            raise ValueError("机器人、会话、词和描述必须是非空文本")
        term, description = term.strip(), description.strip()
        if any(char.isspace() for char in term):
            raise ValueError("词不能包含空白字符")
        if len(term) > 100 or len(description) > 2000:
            raise ValueError("词最多 100 字，描述最多 2000 字")
        now = datetime.now().isoformat(timespec="microseconds")
        await self.conn.execute(
            """
            INSERT INTO slang (bot_name, group_or_user_id, term, description,
                               created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(bot_name, group_or_user_id, term) DO UPDATE SET
                description = excluded.description, updated_at = excluded.updated_at
            """,
            (bot_name, group_or_user_id, term, description, now, now),
        )
        await self.conn.commit()

    async def delete_entry(
        self, bot_name: str, group_or_user_id: str, term: str
    ) -> bool:
        """Delete only the exact term in its owning session.

        Args:
            bot_name: Owning bot name.
            group_or_user_id: Owning group or private conversation.
            term: Literal term to remove.

        Returns:
            Whether an entry was deleted.

        Raises:
            ValueError: A required value is blank or not text.
        """
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (bot_name, group_or_user_id, term)
        ):
            raise ValueError("机器人、会话和词必须是非空文本")
        cursor = await self.conn.execute(
            "DELETE FROM slang WHERE bot_name = ? AND group_or_user_id = ? AND term = ?",
            (bot_name, group_or_user_id, term.strip()),
        )
        await self.conn.commit()
        return cursor.rowcount > 0
