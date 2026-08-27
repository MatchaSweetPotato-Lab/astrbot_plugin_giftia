from datetime import datetime

from .utils import parse_aliases


class ProfileStoreMixin:
    async def get_user_profile(
        self, bot_name: str, group_or_user_id: str, user_id: str
    ) -> str | None:
        """获取用户画像（通过结构化记录组装）"""
        record = await self.get_user_profile_record(bot_name, group_or_user_id, user_id)
        if not record:
            return None
        parts = [f"{k}：{v}" for k, v in record.items() if v and k not in ("aliases", "relation", "title")]
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

    async def get_session_user_aliases(
        self, bot_name: str, group_or_user_id: str
    ) -> list[dict]:
        """获取当前会话内所有已知用户外号，用于后端观测计数。"""
        async with self.conn.execute(
            """
            SELECT ua.user_id, ua.alias
            FROM user_aliases ua
            LEFT JOIN user_profiles up ON ua.bot_name = up.bot_name
                AND ua.group_or_user_id = up.group_or_user_id
                AND ua.user_id = up.user_id
            WHERE ua.bot_name = ? AND ua.group_or_user_id = ?
                AND (up.call_name IS NULL OR LOWER(TRIM(ua.alias)) != LOWER(TRIM(up.call_name)))
            ORDER BY ua.user_id ASC, ua.alias_count DESC, ua.first_seen_at ASC, ua.id ASC
            """,
            (bot_name, group_or_user_id),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def increment_user_alias_counts(
        self,
        bot_name: str,
        group_or_user_id: str,
        observations: list[tuple[str, str, int]],
    ) -> None:
        """批量增加已知外号的观测次数。不存在的外号不会被创建。"""
        if not observations:
            return

        update_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for user_id, alias, count in observations:
            clean_user_id = str(user_id or "").strip()
            clean_alias = str(alias or "").strip()
            try:
                clean_count = max(1, int(count or 1))
            except (TypeError, ValueError):
                clean_count = 1
            if not clean_user_id or not clean_alias:
                continue
            await self.conn.execute(
                """
                UPDATE user_aliases
                SET
                    alias_count = alias_count + ?,
                    last_seen_at = ?,
                    updated_at = ?
                WHERE bot_name = ?
                  AND group_or_user_id = ?
                  AND user_id = ?
                  AND alias = ?
                """,
                (
                    clean_count,
                    update_time,
                    update_time,
                    bot_name,
                    group_or_user_id,
                    clean_user_id,
                    clean_alias,
                ),
            )
        await self.conn.commit()

    async def upsert_user_aliases(
        self,
        bot_name: str,
        group_or_user_id: str,
        user_id: str,
        aliases: str | list[str] | tuple[str, ...] | None,
        increment_count: bool = True,
    ) -> None:
        """记录用户外号。increment_count=True 表示本窗口观测到一次。"""
        alias_items = parse_aliases(aliases)
        if not alias_items:
            return

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

        for alias in alias_items:
            if call_name and alias.strip().lower() == call_name:
                continue
            await self.conn.execute(
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
                VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)
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
                ),
            )
        await self.conn.commit()

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
                COALESCE(up.relation, r.relation) AS relation,
                CASE WHEN up.title IS NOT NULL THEN up.title ELSE r.title END AS title
            FROM user_profiles up
            LEFT JOIN relations r ON up.bot_name = r.bot_name
                AND up.group_or_user_id = r.group_or_user_id
                AND up.user_id = r.user_id
            WHERE up.user_id = ? AND up.group_or_user_id = ? AND up.bot_name = ?
            LIMIT 1
            """,
            (user_id, group_or_user_id, bot_name),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None

        record = dict(row)
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
        """在当前会话内模糊搜索用户画像"""
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
                up.personality,
                up.interests,
                up.attitude,
                up.agreements,
                up.extra,
                up.avatar_description,
                COALESCE(up.relation, r.relation) AS relation,
                CASE WHEN up.title IS NOT NULL THEN up.title ELSE r.title END AS title,
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
            LEFT JOIN relations r ON up.bot_name = r.bot_name
                AND up.group_or_user_id = r.group_or_user_id
                AND up.user_id = r.user_id
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
                OR up.title LIKE ?
                OR up.personality LIKE ?
                OR up.interests LIKE ?
                OR up.attitude LIKE ?
                OR up.agreements LIKE ?
                OR up.extra LIKE ?
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
                like,  # up.title
                like,  # up.personality
                like,  # up.interests
                like,  # up.attitude
                like,  # up.agreements
                like,  # up.extra
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
        relation: int | None = None,
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
        if relation is not None:
            update_fields.insert(-1, "relation=excluded.relation")
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
                relation,
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
                relation,
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
            """
            DELETE FROM user_profiles WHERE user_id = ? AND group_or_user_id = ? AND bot_name = ?
            LIMIT 1
            """,
            (user_id, group_or_user_id, bot_name),
        )
        await self.conn.execute(
            """
            DELETE FROM relations WHERE user_id = ? AND group_or_user_id = ? AND bot_name = ?
            LIMIT 1
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
            """
            DELETE FROM user_profiles WHERE group_or_user_id = ? AND bot_name = ?
            """,
            (group_or_user_id, bot_name),
        )
        await self.conn.execute(
            """
            DELETE FROM relations WHERE group_or_user_id = ? AND bot_name = ?
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
        """获取群画像"""
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
        """删除群画像"""
        await self.conn.execute(
            """
            DELETE FROM group_profiles WHERE group_or_user_id = ? AND bot_name = ?
            LIMIT 1
            """,
            (group_or_user_id, bot_name),
        )
        await self.conn.commit()


    async def upsert_relation(
        self, bot_name: str, group_or_user_id: str, user_id: str, relation: int
    ):
        update_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await self.conn.execute(
            """
            INSERT INTO user_profiles (bot_name, group_or_user_id, user_id, relation, title, created_at, updated_at)
            VALUES (
                ?, ?, ?, ?,
                (
                    SELECT title
                    FROM relations
                    WHERE user_id = ? AND group_or_user_id = ? AND bot_name = ?
                    LIMIT 1
                ),
                ?, ?
            )
            ON CONFLICT(user_id, group_or_user_id, bot_name) DO UPDATE SET
                relation=excluded.relation,
                title=CASE
                    WHEN user_profiles.title IS NULL THEN excluded.title
                    ELSE user_profiles.title
                END,
                updated_at=excluded.updated_at
            """,
            (
                bot_name,
                group_or_user_id,
                user_id,
                relation,
                user_id,
                group_or_user_id,
                bot_name,
                update_time,
                update_time,
            ),
        )
        await self.conn.commit()

    # 更新关系头衔
    async def upsert_relation_title(
        self, bot_name: str, group_or_user_id: str, user_id: str, title: str
    ):
        update_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await self.conn.execute(
            """
            INSERT INTO user_profiles (bot_name, group_or_user_id, user_id, relation, title, created_at, updated_at)
            VALUES (
                ?, ?, ?,
                (
                    SELECT relation
                    FROM relations
                    WHERE user_id = ? AND group_or_user_id = ? AND bot_name = ?
                    LIMIT 1
                ),
                ?, ?, ?
            )
            ON CONFLICT(user_id, group_or_user_id, bot_name) DO UPDATE SET
                relation=CASE
                    WHEN user_profiles.relation IS NULL THEN excluded.relation
                    ELSE user_profiles.relation
                END,
                title=excluded.title,
                updated_at=excluded.updated_at
            """,
            (
                bot_name,
                group_or_user_id,
                user_id,
                user_id,
                group_or_user_id,
                bot_name,
                title,
                update_time,
                update_time,
            ),
        )
        await self.conn.commit()

    # 获取关系数据
    async def get_relation(
        self, bot_name: str, group_or_user_id: str, user_id: str
    ) -> tuple[int, str]:
        async with self.conn.execute(
            """
            SELECT relation, title FROM user_profiles WHERE user_id = ? AND group_or_user_id = ? AND bot_name = ?
            LIMIT 1
            """,
            (user_id, group_or_user_id, bot_name),
        ) as cursor:
            row = await cursor.fetchone()
        if row and (row["relation"] is not None or row["title"] is not None):
            return row["relation"] if row["relation"] is not None else 0, row["title"] or ""

        async with self.conn.execute(
            """
            SELECT relation, title FROM relations WHERE user_id = ? AND group_or_user_id = ? AND bot_name = ?
            LIMIT 1
            """,
            (user_id, group_or_user_id, bot_name),
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            return row["relation"], row["title"]
        return 0, ""

    async def delete_all_relations(self, bot_name: str, group_or_user_id: str):
        """删除指定群或私聊的所有好感度和头衔数据"""
        await self.conn.execute(
            """
            UPDATE user_profiles
            SET relation = NULL, title = NULL, updated_at = ?
            WHERE group_or_user_id = ? AND bot_name = ?
            """,
            (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                group_or_user_id,
                bot_name,
            ),
        )
        await self.conn.execute(
            """
            DELETE FROM relations WHERE group_or_user_id = ? AND bot_name = ?
            """,
            (group_or_user_id, bot_name),
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

