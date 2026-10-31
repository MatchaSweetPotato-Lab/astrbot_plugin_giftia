from datetime import datetime

from ..utils.schemas import ACCOUNT_TYPE_LABELS
from .operations import serialized_operation
from .relation_history_store import RelationHistoryStoreMixin
from .utils import parse_aliases

# Both placeholders identify the requesting conversation. All readers use the
# same resolution order, including SQL search and dashboard filtering.
PROFILE_NAMES_CTE = """
    WITH profiles AS (
        SELECT up.*,
            (
                SELECT cn.call_name FROM user_call_names cn
                WHERE cn.bot_name=up.bot_name AND cn.user_id=up.user_id
                ORDER BY (cn.group_or_user_id=? AND cn.group_or_user_id!='') DESC,
                         cn.updated_at DESC, cn.id DESC
                LIMIT 1
            ) AS resolved_call_name,
            (
                SELECT cn.call_name FROM user_call_names cn
                WHERE cn.bot_name=up.bot_name AND cn.user_id=up.user_id
                  AND cn.group_or_user_id=?
                LIMIT 1
            ) AS local_call_name
        FROM user_profiles up
    )
"""


class ProfileStoreMixin(RelationHistoryStoreMixin):
    @serialized_operation
    async def initialize_user_profile(
        self,
        bot_name: str,
        group_or_user_id: str,
        user_id: str,
        account_type: str | None = None,
    ) -> tuple[str, bool]:
        """Ensure membership and fill an unconfirmed shared account category.

        Args:
            bot_name: Bot owning the shared profile.
            group_or_user_id: Conversation where the member has just spoken.
            user_id: Actual sender whose profile should exist.
            account_type: Optional category obtained from the platform API.

        Returns:
            The stored category and whether the shared profile was created or
            its category changed. Existing categories and profile fields survive.

        Raises:
            ValueError: The supplied category is not a confirmed account type.
        """
        if account_type is not None and account_type not in (
            "official_bot",
            "qq_account",
        ):
            raise ValueError("Invalid confirmed account type")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self.conn.execute(
            "INSERT INTO user_profiles (bot_name, user_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(user_id, bot_name) DO NOTHING",
            (bot_name, user_id, now, now),
        )
        changed = cursor.rowcount > 0
        if group_or_user_id:
            await self.conn.execute(
                "INSERT OR IGNORE INTO user_profile_sessions "
                "(bot_name, group_or_user_id, user_id) VALUES (?, ?, ?)",
                (bot_name, group_or_user_id, user_id),
            )
        if account_type is not None:
            cursor = await self.conn.execute(
                "UPDATE user_profiles SET account_type=?, updated_at=? "
                "WHERE bot_name=? AND user_id=? AND account_type='unknown'",
                (account_type, now, bot_name, user_id),
            )
            changed = changed or cursor.rowcount > 0
        async with self.conn.execute(
            "SELECT account_type FROM user_profiles WHERE bot_name=? AND user_id=?",
            (bot_name, user_id),
        ) as cursor:
            row = await cursor.fetchone()
        await self.conn.commit()
        return row[0], changed

    @serialized_operation
    async def get_user_common_groups(
        self, bot_name: str, group_or_user_id: str, user_ids: list[str]
    ) -> dict[str, list[dict]]:
        """Find other groups where this bot has recorded each member speaking.

        Args:
            bot_name: Bot owning the recorded messages.
            group_or_user_id: Current conversation, excluded from the results.
            user_ids: Members whose known groups should be fetched together.

        Returns:
            Groups per member with their last message timestamp and a recent
            activity flag for speech within the past 24 hours. Active groups
            come first, followed by the latest message time and group ID.
        """
        groups = {str(uid): [] for uid in user_ids if uid}
        if not groups:
            return groups

        placeholders = ",".join("?" for _ in groups)
        async with self.conn.execute(
            f"""
            SELECT user_id, group_or_user_id, MAX(created_at) AS last_message_at
            FROM chat_history
            WHERE bot_name = ? AND user_id IN ({placeholders})
              AND group_or_user_id IS NOT NULL AND group_or_user_id != ''
              AND group_or_user_id != ? AND group_or_user_id != user_id
              AND COALESCE(role, 'message') = 'message'
              AND COALESCE(is_recalled, 0) = 0
            GROUP BY user_id, group_or_user_id
            """,
            (bot_name, *groups, group_or_user_id),
        ) as cursor:
            rows = await cursor.fetchall()

        now = datetime.now().timestamp()
        for row in rows:
            try:
                last_message_at = datetime.fromisoformat(
                    row["last_message_at"]
                ).timestamp()
            except (TypeError, ValueError, OverflowError):
                last_message_at = 0.0
            groups[row["user_id"]].append(
                {
                    "group_id": row["group_or_user_id"],
                    "last_message_at": last_message_at,
                    "recently_active": 0 <= now - last_message_at <= 24 * 60 * 60,
                }
            )
        for user_groups in groups.values():
            user_groups.sort(
                key=lambda group: (
                    not group["recently_active"],
                    -group["last_message_at"],
                    group["group_id"],
                )
            )
        return groups

    @serialized_operation
    async def get_user_call_names(
        self, bot_name: str, group_or_user_id: str, user_id: str
    ) -> dict:
        """Resolve the local name, falling back to the latest name within this bot.

        Args:
            bot_name: Bot owning the names.
            group_or_user_id: Requesting conversation.
            user_id: Member whose names are requested.

        Returns:
            Effective call_name and the independently editable local_call_name.
        """
        async with self.conn.execute(
            PROFILE_NAMES_CTE
            + "SELECT resolved_call_name AS call_name, local_call_name FROM profiles "
            "WHERE bot_name=? AND user_id=?",
            (group_or_user_id, group_or_user_id, bot_name, user_id),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else {"call_name": None, "local_call_name": None}

    @serialized_operation
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

    @serialized_operation
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
        local_call_name = (
            await self.get_user_call_names(bot_name, group_or_user_id, user_id)
        )["local_call_name"]
        params.extend([local_call_name, local_call_name])

        count_clause = "AND ua.alias_count >= 3" if not ignore_count_filter else ""

        limit_clause = ""
        if limit is not None:
            limit_clause = "LIMIT ?"
            params.append(max(1, int(limit or 6)))

        async with self.conn.execute(
            f"""
            SELECT ua.alias, ua.alias_count, ua.first_seen_at, ua.last_seen_at
            FROM user_aliases ua
            WHERE ua.bot_name = ? AND ua.group_or_user_id = ? AND ua.user_id = ?
                {count_clause}
                AND (? IS NULL OR LOWER(TRIM(ua.alias)) != LOWER(TRIM(?)))
            ORDER BY ua.alias_count DESC, ua.first_seen_at ASC, ua.id ASC
            {limit_clause}
            """,
            params,
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    @serialized_operation
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

    @serialized_operation
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

        local_call_name = (
            await self.get_user_call_names(bot_name, group_or_user_id, user_id)
        )["local_call_name"]
        local_call_name = local_call_name.strip().lower() if local_call_name else None

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
            if local_call_name and alias.strip().lower() == local_call_name:
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
        await self.conn.execute(
            "INSERT OR IGNORE INTO user_profile_sessions "
            "(bot_name, group_or_user_id, user_id) VALUES (?, ?, ?)",
            (bot_name, group_or_user_id, user_id),
        )
        await self.conn.commit()
        return rejected_aliases

    @serialized_operation
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

    @serialized_operation
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

    @serialized_operation
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

    @serialized_operation
    async def get_user_profile_record(
        self, bot_name: str, group_or_user_id: str, user_id: str
    ) -> dict | None:
        """获取用户画像完整记录"""
        async with self.conn.execute(
            PROFILE_NAMES_CTE
            + """
            SELECT
                up.resolved_call_name AS call_name,
                up.local_call_name,
                up.account_type,
                up.aliases,
                up.personality,
                up.interests,
                up.attitude,
                up.agreements,
                up.extra,
                up.avatar_description,
                up.title
            FROM profiles up
            WHERE up.user_id = ? AND up.bot_name = ?
            LIMIT 1
            """,
            (group_or_user_id, group_or_user_id, user_id, bot_name),
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

    @serialized_operation
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
            Matching profiles with current scores, local aliases and all scoped names.
        """
        query = (query or "").strip()
        if not query:
            return []

        like = f"%{query}%"
        limit = max(1, min(int(limit or 5), 20))
        async with self.conn.execute(
            PROFILE_NAMES_CTE
            + """
            SELECT
                up.user_id,
                ? AS group_or_user_id,
                up.bot_name,
                up.resolved_call_name AS call_name,
                up.local_call_name,
                up.account_type,
                up.attitude,
                up.avatar_description,
                (
                    SELECT ch.nickname
                    FROM chat_history ch
                    WHERE ch.bot_name = up.bot_name
                      AND ch.group_or_user_id = ?
                      AND ch.user_id = up.user_id
                      AND ch.nickname IS NOT NULL
                      AND ch.nickname != ''
                    ORDER BY ch.created_at DESC
                    LIMIT 1
                ) AS nickname
            FROM profiles up
            WHERE up.bot_name = ?
              AND (
                up.user_id LIKE ?
                OR EXISTS (
                    SELECT 1 FROM user_call_names cn
                    WHERE cn.bot_name = up.bot_name
                      AND cn.user_id = up.user_id
                      AND cn.call_name LIKE ?
                )
                OR EXISTS (
                    SELECT 1
                    FROM user_aliases ua
                    WHERE ua.bot_name = up.bot_name
                      AND ua.group_or_user_id = ?
                      AND ua.user_id = up.user_id
                      AND ua.alias LIKE ?
                )
                OR up.avatar_description LIKE ?
                OR EXISTS (
                    SELECT 1
                    FROM chat_history ch
                    WHERE ch.bot_name = up.bot_name
                      AND ch.group_or_user_id = ?
                      AND ch.user_id = up.user_id
                      AND ch.nickname LIKE ?
                )
              )
            ORDER BY
                CASE
                    WHEN up.user_id = ? THEN 0
                    WHEN up.resolved_call_name = ? THEN 1
                    WHEN EXISTS (
                        SELECT 1
                        FROM user_aliases ua
                        WHERE ua.bot_name = up.bot_name
                          AND ua.group_or_user_id = ?
                          AND ua.user_id = up.user_id
                          AND ua.alias = ?
                    ) THEN 2
                    ELSE 4
                END,
                up.updated_at DESC
            LIMIT ?
            """,
            (
                group_or_user_id,
                group_or_user_id,
                group_or_user_id,
                group_or_user_id,
                bot_name,
                like,  # up.user_id
                like,  # cn.call_name
                group_or_user_id,
                like,  # ua.alias
                like,  # up.avatar_description
                group_or_user_id,
                like,  # ch.nickname
                query,  # ORDER BY exact user_id
                query,  # ORDER BY exact call_name
                group_or_user_id,
                query,  # ORDER BY exact alias
                limit,
            ),
        ) as cursor:
            rows = await cursor.fetchall()

        results = [dict(row) for row in rows]
        for item in results:
            async with self.conn.execute(
                "SELECT group_or_user_id, call_name FROM user_call_names "
                "WHERE bot_name=? AND user_id=? "
                "ORDER BY (group_or_user_id=? AND group_or_user_id!='') DESC, "
                "updated_at DESC, id DESC",
                (bot_name, item["user_id"], group_or_user_id),
            ) as cursor:
                item["call_names"] = [dict(row) for row in await cursor.fetchall()]
            for name in item["call_names"]:
                source = name["group_or_user_id"]
                if not source:
                    name["source_label"] = "来源未知（旧共享称呼）"
                elif source == group_or_user_id:
                    name["source_label"] = ""
                elif source == item["user_id"]:
                    name["source_label"] = "来源私聊"
                else:
                    name["source_label"] = f"来源群聊：{source}"
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

    @serialized_operation
    async def upsert_user_profile(
        self,
        bot_name: str,
        group_or_user_id: str,
        user_id: str,
        base_relation: int | None = None,
        title: str | None = None,
        profile_fields: dict[str, str | None] | None = None,
    ):
        profile_fields = profile_fields or {}
        if "account_type" in profile_fields and (
            not isinstance(profile_fields["account_type"], str)
            or profile_fields["account_type"] not in ACCOUNT_TYPE_LABELS
        ):
            raise ValueError("Invalid account type")
        update_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        profile_columns = [
            "account_type",
            "personality",
            "interests",
            "attitude",
            "agreements",
            "extra",
            "avatar_description",
        ]
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
                bot_name,
                account_type,
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
            ON CONFLICT(user_id, bot_name) DO UPDATE SET
                {update_clause}
            """,
            (
                user_id,
                bot_name,
                profile_fields.get("account_type", "unknown"),
                None,
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
        if group_or_user_id:
            await self.conn.execute(
                "INSERT OR IGNORE INTO user_profile_sessions "
                "(bot_name, group_or_user_id, user_id) VALUES (?, ?, ?)",
                (bot_name, group_or_user_id, user_id),
            )
        new_call_name = str(profile_fields.get("call_name") or "").strip()
        if "call_name" in profile_fields:
            if new_call_name:
                await self.conn.execute(
                    """
                    INSERT INTO user_call_names
                        (bot_name, group_or_user_id, user_id, call_name, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(bot_name, group_or_user_id, user_id) DO UPDATE SET
                        call_name=excluded.call_name, updated_at=excluded.updated_at
                    WHERE user_call_names.call_name != excluded.call_name
                    """,
                    (
                        bot_name,
                        group_or_user_id,
                        user_id,
                        new_call_name,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"),
                    ),
                )
            else:
                await self.conn.execute(
                    "DELETE FROM user_call_names WHERE bot_name=? "
                    "AND group_or_user_id=? AND user_id=?",
                    (bot_name, group_or_user_id, user_id),
                )
        if new_call_name:
            stripped = new_call_name.strip()
            if stripped:
                await self.conn.execute(
                    """
                    DELETE FROM user_aliases
                    WHERE user_id = ? AND bot_name = ? AND group_or_user_id = ?
                        AND LOWER(TRIM(alias)) = LOWER(?)
                    """,
                    (user_id, bot_name, group_or_user_id, stripped),
                )
        await self.conn.commit()

    @serialized_operation
    async def delete_user_profile(
        self, bot_name: str, group_or_user_id: str, user_id: str
    ):
        """Delete a user's shared profile, history and aliases in every conversation.

        Args:
            bot_name: Owning bot configuration.
            group_or_user_id: Current conversation, retained for the caller's context.
            user_id: User whose complete shared data is deleted.
        """
        await self.conn.execute(
            "DELETE FROM relation_changes WHERE bot_name=? AND user_id=?",
            (bot_name, user_id),
        )
        await self.conn.execute(
            """
            DELETE FROM user_profiles WHERE user_id = ? AND bot_name = ?
            """,
            (user_id, bot_name),
        )
        await self.conn.execute(
            """
            DELETE FROM user_aliases WHERE user_id = ? AND bot_name = ?
            """,
            (user_id, bot_name),
        )
        await self.conn.execute(
            "DELETE FROM user_profile_sessions WHERE bot_name=? AND user_id=?",
            (bot_name, user_id),
        )
        await self.conn.execute(
            "DELETE FROM user_call_names WHERE bot_name=? AND user_id=?",
            (bot_name, user_id),
        )
        await self.conn.commit()

    @serialized_operation
    async def delete_group_user_profiles(self, bot_name: str, group_or_user_id: str):
        """Clear session aliases and history while preserving shared profiles.

        Args:
            bot_name: Owning bot configuration.
            group_or_user_id: Conversation whose local data is cleared.
        """
        await self.delete_all_relations(bot_name, group_or_user_id)
        await self.conn.execute(
            "DELETE FROM user_call_names WHERE bot_name=? AND group_or_user_id=?",
            (bot_name, group_or_user_id),
        )
        await self.conn.execute(
            """
            DELETE FROM user_profile_sessions WHERE group_or_user_id = ? AND bot_name = ?
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

    @serialized_operation
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

    @serialized_operation
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

    @serialized_operation
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

    @serialized_operation
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

    @serialized_operation
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
            "SELECT title FROM user_profiles WHERE bot_name=? AND user_id=?",
            (bot_name, user_id),
        ) as cursor:
            row = await cursor.fetchone()
        return score["relation"], (row["title"] or "") if row else ""

    @serialized_operation
    async def delete_all_relations(self, bot_name: str, group_or_user_id: str):
        """Remove a conversation's ratings and invalidate affected shared tone reviews.

        Args:
            bot_name: Owning bot configuration.
            group_or_user_id: Source conversation whose ratings are deleted.
        """
        await self.conn.execute(
            """
            UPDATE user_profiles
            SET attitude = NULL, attitude_evaluated_change_id = 0,
                relation_history_revision = relation_history_revision + 1, updated_at = ?
            WHERE bot_name = ? AND user_id IN (
                SELECT user_id FROM relation_changes WHERE bot_name = ? AND group_or_user_id = ?
            )
            """,
            (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                bot_name,
                bot_name,
                group_or_user_id,
            ),
        )
        await self.conn.execute(
            "DELETE FROM relation_changes WHERE bot_name=? AND group_or_user_id=?",
            (bot_name, group_or_user_id),
        )
        await self.conn.commit()

    @serialized_operation
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
