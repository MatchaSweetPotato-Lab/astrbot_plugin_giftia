from datetime import datetime

from .relation_history_store import RelationHistoryStoreMixin
from .utils import parse_aliases


class ProfileStoreMixin(RelationHistoryStoreMixin):
    async def get_user_profile(
        self, bot_name: str, group_or_user_id: str, user_id: str
    ) -> str | None:
        """获取用户画像（通过结构化记录组装）"""
        record = await self.get_user_profile_record(bot_name, group_or_user_id, user_id)
        if not record:
            return None
        parts = [
            f"{k}：{v}"
            for k, v in record.items()
            if v
            and k
            in (
                "call_name",
                "personality",
                "interests",
                "attitude",
                "agreements",
                "extra",
                "avatar_description",
            )
        ]
        return "\n".join(parts) if parts else None

    async def get_user_aliases(
        self,
        bot_name: str,
        group_or_user_id: str,
        user_id: str,
        limit: int | None = 6,
        ignore_count_filter: bool = False,
    ) -> list[dict]:
        """获取用户外号，按统计数量优先，同数量时旧外号优先"""
        params: list = [bot_name, group_or_user_id, user_id]

        count_clause = "AND ua.alias_count >= 3" if not ignore_count_filter else ""

        limit_clause = ""
        if limit is not None:
            limit_clause = "LIMIT ?"
            params.append(max(1, int(limit or 6)))

        async with self.conn.execute(
            f"""
            SELECT ua.alias, ua.alias_count, ua.first_seen_at, ua.last_seen_at
            FROM user_aliases ua
            LEFT JOIN user_profiles up ON ua.bot_name = up.bot_name
                AND ua.group_or_user_id = up.group_or_user_id
                AND ua.user_id = up.user_id
            WHERE ua.bot_name = ? AND ua.group_or_user_id = ? AND ua.user_id = ?
                {count_clause}
                AND (up.call_name IS NULL OR LOWER(TRIM(ua.alias)) != LOWER(TRIM(up.call_name)))
            ORDER BY ua.alias_count DESC, ua.first_seen_at ASC, ua.id ASC
            {limit_clause}
            """,
            params,
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_user_aliases_text(
        self,
        bot_name: str,
        group_or_user_id: str,
        user_id: str,
        limit: int = 6,
    ) -> str:
        aliases = await self.get_user_aliases(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            user_id=user_id,
            limit=limit,
        )
        return "，".join(item["alias"] for item in aliases)

    async def upsert_user_aliases(
        self,
        bot_name: str,
        group_or_user_id: str,
        user_id: str,
        aliases: str | list[str] | tuple[str, ...] | None,
        increment_count: bool = True,
    ) -> list[str]:
        """Record aliases without assigning another member's alias to this user.

        Args:
            bot_name: Bot owning the session.
            group_or_user_id: Session containing the observations.
            user_id: Member addressed by these aliases.
            aliases: Aliases observed or entered manually.
            increment_count: Whether this is a new observation window.

        Returns:
            Aliases rejected because another member already owns them.
        """
        alias_items = parse_aliases(aliases)
        if not alias_items:
            return []

        call_name = None
        async with self.conn.execute(
            "SELECT call_name FROM user_profiles WHERE user_id = ? AND group_or_user_id = ? AND bot_name = ? LIMIT 1",
            (user_id, group_or_user_id, bot_name),
        ) as cursor:
            row = await cursor.fetchone()
            if row and row["call_name"]:
                call_name = str(row["call_name"]).strip().lower()

        update_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if increment_count:
            conflict_update = """
                alias_count=user_aliases.alias_count + excluded.alias_count,
                last_seen_at=excluded.last_seen_at,
                updated_at=excluded.updated_at
            """
        else:
            conflict_update = """
                updated_at=excluded.updated_at
            """

        rejected_aliases = []
        for alias in alias_items:
            if call_name and alias.strip().lower() == call_name:
                continue
            # Check ownership in the write itself to prevent concurrent claims.
            cursor = await self.conn.execute(
                f"""
                INSERT INTO user_aliases (
                    bot_name,
                    group_or_user_id,
                    user_id,
                    alias,
                    alias_count,
                    first_seen_at,
                    last_seen_at,
                    created_at,
                    updated_at
                )
                SELECT ?, ?, ?, ?, 1, ?, ?, ?, ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM user_aliases
                    WHERE bot_name = ? AND group_or_user_id = ?
                      AND user_id != ? AND LOWER(TRIM(alias)) = LOWER(?)
                )
                ON CONFLICT(bot_name, group_or_user_id, user_id, alias) DO UPDATE SET
                    {conflict_update}
                """,
                (
                    bot_name,
                    group_or_user_id,
                    user_id,
                    alias,
                    update_time,
                    update_time,
                    update_time,
                    update_time,
                    bot_name,
                    group_or_user_id,
                    user_id,
                    alias,
                ),
            )
            if cursor.rowcount == 0:
                rejected_aliases.append(alias)
        await self.conn.commit()
        return rejected_aliases

    async def delete_user_aliases(
        self, bot_name: str, group_or_user_id: str, user_id: str
    ) -> None:
        await self.conn.execute(
            """
            DELETE FROM user_aliases WHERE user_id = ? AND group_or_user_id = ? AND bot_name = ?
            """,
            (user_id, group_or_user_id, bot_name),
        )
        await self.conn.commit()

    async def delete_user_alias(
        self, bot_name: str, group_or_user_id: str, user_id: str, alias: str
    ) -> bool:
        clean_alias = str(alias or "").strip()
        if not clean_alias:
            return False
        cursor = await self.conn.execute(
            """
            DELETE FROM user_aliases
            WHERE bot_name = ? AND group_or_user_id = ? AND user_id = ? AND alias = ?
            """,
            (bot_name, group_or_user_id, user_id, clean_alias),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def set_user_alias_count(
        self,
        bot_name: str,
        group_or_user_id: str,
        user_id: str,
        alias: str,
        alias_count: int,
    ) -> bool:
        clean_alias = str(alias or "").strip()
        try:
            clean_count = int(alias_count)
        except (TypeError, ValueError):
            return False
        if not clean_alias or clean_count < 1:
            return False

        update_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self.conn.execute(
            """
            UPDATE user_aliases
            SET alias_count = ?, updated_at = ?
            WHERE bot_name = ? AND group_or_user_id = ? AND user_id = ? AND alias = ?
            """,
            (
                clean_count,
                update_time,
                bot_name,
                group_or_user_id,
                user_id,
                clean_alias,
            ),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def get_user_profile_record(
        self, bot_name: str, group_or_user_id: str, user_id: str
    ) -> dict | None:
        """获取用户画像完整记录"""
        async with self.conn.execute(
            """
            SELECT
                up.call_name,
                up.aliases,
                up.personality,
                up.interests,
                up.attitude,
                up.agreements,
                up.extra,
                up.avatar_description,
                up.title
            FROM user_profiles up
            WHERE up.user_id = ? AND up.group_or_user_id = ? AND up.bot_name = ?
            LIMIT 1
            """,
            (user_id, group_or_user_id, bot_name),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None

        record = dict(row)
        record.update(
            await self.get_relation_score(bot_name, group_or_user_id, user_id)
        )
        record["aliases"] = await self.get_user_aliases_text(
            bot_name=bot_name,
            group_or_user_id=group_or_user_id,
            user_id=user_id,
            limit=6,
        )
        return record

    async def search_user_profiles(
        self,
        bot_name: str,
        group_or_user_id: str,
        query: str,
        limit: int = 5,
    ) -> list[dict]:
        """Search profiles by account ID, nickname, preferred name, alias or avatar.

        Args:
            bot_name: Owning bot configuration.
            group_or_user_id: Owning conversation.
            query: Text to match against member identifiers, names or avatars.
            limit: Maximum number of matching members, capped at twenty.

        Returns:
            Matching profiles with current scores and aliases.
        """
        query = (query or "").strip()
        if not query:
            return []

        like = f"%{query}%"
        limit = max(1, min(int(limit or 5), 20))
        async with self.conn.execute(
            """
            SELECT
                up.user_id,
                up.group_or_user_id,
                up.bot_name,
                up.call_name,
                up.attitude,
                up.avatar_description,
                (
                    SELECT ch.nickname
                    FROM chat_history ch
                    WHERE ch.bot_name = up.bot_name
                      AND ch.group_or_user_id = up.group_or_user_id
                      AND ch.user_id = up.user_id
                      AND ch.nickname IS NOT NULL
                      AND ch.nickname != ''
                    ORDER BY ch.created_at DESC
                    LIMIT 1
                ) AS nickname
            FROM user_profiles up
            WHERE up.bot_name = ?
              AND up.group_or_user_id = ?
              AND (
                up.user_id LIKE ?
                OR up.call_name LIKE ?
                OR EXISTS (
                    SELECT 1
                    FROM user_aliases ua
                    WHERE ua.bot_name = up.bot_name
                      AND ua.group_or_user_id = up.group_or_user_id
                      AND ua.user_id = up.user_id
                      AND ua.alias LIKE ?
                )
                OR up.avatar_description LIKE ?
                OR EXISTS (
                    SELECT 1
                    FROM chat_history ch
                    WHERE ch.bot_name = up.bot_name
                      AND ch.group_or_user_id = up.group_or_user_id
                      AND ch.user_id = up.user_id
                      AND ch.nickname LIKE ?
                )
              )
            ORDER BY
                CASE
                    WHEN up.user_id = ? THEN 0
                    WHEN up.call_name = ? THEN 1
                    WHEN EXISTS (
                        SELECT 1
                        FROM user_aliases ua
                        WHERE ua.bot_name = up.bot_name
                          AND ua.group_or_user_id = up.group_or_user_id
                          AND ua.user_id = up.user_id
                          AND ua.alias = ?
                    ) THEN 2
                    ELSE 4
                END,
                up.updated_at DESC
            LIMIT ?
            """,
            (
                bot_name,
                group_or_user_id,
                like,  # up.user_id
                like,  # up.call_name
                like,  # ua.alias
                like,  # up.avatar_description
                like,  # ch.nickname
                query,  # ORDER BY exact user_id
                query,  # ORDER BY exact call_name
                query,  # ORDER BY exact alias
                limit,
            ),
        ) as cursor:
            rows = await cursor.fetchall()

        results = [dict(row) for row in rows]
        for item in results:
            item.update(
                await self.get_relation_score(
                    bot_name, group_or_user_id, item["user_id"]
                )
            )
            item["aliases"] = await self.get_user_aliases_text(
                bot_name=item["bot_name"],
                group_or_user_id=item["group_or_user_id"],
                user_id=item["user_id"],
                limit=6,
            )
        return results

    async def upsert_user_profile(
        self,
        bot_name: str,
        group_or_user_id: str,
        user_id: str,
        base_relation: int | None = None,
        title: str | None = None,
        profile_fields: dict[str, str | None] | None = None,
    ):
        update_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        profile_columns = [
            "call_name",
            "personality",
            "interests",
            "attitude",
            "agreements",
            "extra",
            "avatar_description",
        ]
        profile_fields = profile_fields or {}
        update_fields = ["updated_at=excluded.updated_at"]
        for column in profile_columns:
            if column in profile_fields:
                update_fields.insert(-1, f"{column}=excluded.{column}")
        if base_relation is not None:
            if type(base_relation) is not int:
                raise ValueError("Base affinity must be an integer")
            update_fields.insert(-1, "base_relation=excluded.base_relation")
        if title is not None:
            update_fields.insert(-1, "title=excluded.title")
        update_clause = ",\n                ".join(update_fields)
        await self.conn.execute(
            f"""
            INSERT INTO user_profiles (
                user_id,
                group_or_user_id,
                bot_name,
                call_name,
                aliases,
                personality,
                interests,
                attitude,
                agreements,
                extra,
                avatar_description,
                base_relation,
                title,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, group_or_user_id, bot_name) DO UPDATE SET
                {update_clause}
            """,
            (
                user_id,
                group_or_user_id,
                bot_name,
                profile_fields.get("call_name"),
                None,
                profile_fields.get("personality"),
                profile_fields.get("interests"),
                profile_fields.get("attitude"),
                profile_fields.get("agreements"),
                profile_fields.get("extra"),
                profile_fields.get("avatar_description"),
                base_relation if base_relation is not None else 0,
                title,
                update_time,
                update_time,
            ),
        )
        new_call_name = profile_fields.get("call_name")
        if new_call_name:
            stripped = new_call_name.strip()
            if stripped:
                await self.conn.execute(
                    """
                    DELETE FROM user_aliases
                    WHERE user_id = ? AND group_or_user_id = ? AND bot_name = ?
                        AND LOWER(TRIM(alias)) = LOWER(?)
                    """,
                    (user_id, group_or_user_id, bot_name, stripped),
                )
        await self.conn.commit()

    async def delete_user_profile(
        self, bot_name: str, group_or_user_id: str, user_id: str
    ):
        """删除用户画像"""
        await self.conn.execute(
            "DELETE FROM relation_changes WHERE bot_name=? AND group_or_user_id=? AND user_id=?",
            (bot_name, group_or_user_id, user_id),
        )
        await self.conn.execute(
            """
            DELETE FROM user_profiles WHERE user_id = ? AND group_or_user_id = ? AND bot_name = ?
            """,
            (user_id, group_or_user_id, bot_name),
        )
        await self.conn.execute(
            """
            DELETE FROM user_aliases WHERE user_id = ? AND group_or_user_id = ? AND bot_name = ?
            """,
            (user_id, group_or_user_id, bot_name),
        )
        await self.conn.commit()

    # 删除整个群的用户画像
    async def delete_group_user_profiles(self, bot_name: str, group_or_user_id: str):
        """删除整个群的用户画像"""
        await self.conn.execute(
            "DELETE FROM relation_changes WHERE bot_name=? AND group_or_user_id=?",
            (bot_name, group_or_user_id),
        )
        await self.conn.execute(
            """
            DELETE FROM user_profiles WHERE group_or_user_id = ? AND bot_name = ?
            """,
            (group_or_user_id, bot_name),
        )
        await self.conn.execute(
            """
            DELETE FROM user_aliases WHERE group_or_user_id = ? AND bot_name = ?
            """,
            (group_or_user_id, bot_name),
        )
        await self.conn.commit()

    async def get_group_profile(
        self, group_or_user_id: str, bot_name: str
    ) -> str | None:
        """Read group rules from the legacy profile table.

        Args:
            group_or_user_id: Session whose rules are requested.
            bot_name: Bot owning the session.

        Returns:
            Stored rules, or None when no record exists.
        """
        async with self.conn.execute(
            """
            SELECT profile FROM group_profiles WHERE group_or_user_id = ? AND bot_name = ?
            LIMIT 1
            """,
            (group_or_user_id, bot_name),
        ) as cursor:
            row = await cursor.fetchone()
        return row["profile"] if row else None

    async def upsert_group_profile(
        self, group_or_user_id: str, bot_name: str, profile: str
    ):
        update_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await self.conn.execute(
            """
            INSERT INTO group_profiles (group_or_user_id, bot_name, profile, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(group_or_user_id, bot_name) DO UPDATE SET
                profile=excluded.profile,
                updated_at=excluded.updated_at
            """,
            (group_or_user_id, bot_name, profile, update_time, update_time),
        )
        await self.conn.commit()

    async def delete_group_profile(self, bot_name: str, group_or_user_id: str):
        """Delete the rules for one bot and session.

        Args:
            bot_name: Bot owning the session.
            group_or_user_id: Session whose rules are deleted.
        """
        await self.conn.execute(
            """
            DELETE FROM group_profiles WHERE group_or_user_id = ? AND bot_name = ?
            """,
            (group_or_user_id, bot_name),
        )
        await self.conn.commit()

    async def upsert_relation_title(
        self, bot_name: str, group_or_user_id: str, user_id: str, title: str
    ):
        """Store a title without changing the affinity base.

        Args:
            bot_name: Owning bot configuration.
            group_or_user_id: Owning conversation.
            user_id: Member whose title is changed.
            title: Complete replacement title.
        """
        await self.upsert_user_profile(bot_name, group_or_user_id, user_id, title=title)

    async def get_relation(
        self, bot_name: str, group_or_user_id: str, user_id: str
    ) -> tuple[float, str]:
        """Read derived affinity and the stored title.

        Args:
            bot_name: Owning bot configuration.
            group_or_user_id: Owning conversation.
            user_id: Member whose relationship is requested.

        Returns:
            Final affinity and title, defaulting to fifty and an empty title.
        """
        score = await self.get_relation_score(bot_name, group_or_user_id, user_id)
        async with self.conn.execute(
            "SELECT title FROM user_profiles WHERE bot_name=? AND group_or_user_id=? AND user_id=?",
            (bot_name, group_or_user_id, user_id),
        ) as cursor:
            row = await cursor.fetchone()
        return score["relation"], (row["title"] or "") if row else ""

    async def delete_all_relations(self, bot_name: str, group_or_user_id: str):
        """删除指定群或私聊的所有好感度和头衔数据"""
        await self.conn.execute(
            """
            UPDATE user_profiles
            SET base_relation = 0, title = NULL, attitude_evaluated_change_id = 0, updated_at = ?
            WHERE group_or_user_id = ? AND bot_name = ?
            """,
            (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                group_or_user_id,
                bot_name,
            ),
        )
        await self.conn.execute(
            "DELETE FROM relation_changes WHERE bot_name=? AND group_or_user_id=?",
            (bot_name, group_or_user_id),
        )
        await self.conn.commit()

    async def delete_expired_user_aliases(
        self, min_age_days: int = 7, count_threshold: int = 3
    ) -> int:
        """自动清理未达标且过期的外号记录（alias_count < count_threshold 且 first_seen_at <= min_age_days 天前）"""
        clean_days = max(1, int(min_age_days or 7))
        clean_threshold = max(1, int(count_threshold or 3))

        async with self.conn.execute(
            f"""
            DELETE FROM user_aliases
            WHERE alias_count < ?
              AND (first_seen_at < datetime('now', '-{clean_days} days') OR first_seen_at IS NULL)
            """,
            (clean_threshold,),
        ) as cursor:
            deleted_count = cursor.rowcount
        await self.conn.commit()
        return deleted_count
