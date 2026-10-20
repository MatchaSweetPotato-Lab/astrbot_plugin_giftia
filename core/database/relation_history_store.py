import asyncio
import json
from datetime import datetime, timezone

import aiosqlite


class RelationHistoryConflictError(Exception):
    """The member's history changed after a manual editor loaded it."""


class RelationHistoryStoreMixin:
    async def get_relation_score(
        self,
        bot_name: str,
        group_or_user_id: str,
        user_id: str,
        *,
        connection: aiosqlite.Connection | None = None,
        through_id: int | None = None,
    ) -> dict[str, int | float]:
        """Calculate affinity from the manual base and weighted interaction history.

        Args:
            bot_name: Owning bot configuration.
            group_or_user_id: Current conversation; affinity is shared across conversations.
            user_id: Member whose affinity is calculated.
            connection: Transaction connection when recording an interaction.
            through_id: Inclusive history boundary, or None for current scores.

        Returns:
            Unrounded final, base, recent, long-term and interaction scores with
            sample counts. An empty history has an interaction score of fifty.
        """
        scope = (bot_name, user_id)
        async with (connection or self.conn).execute(
            "WITH recent AS (SELECT delta FROM relation_changes WHERE bot_name=? "
            "AND user_id=? AND (? IS NULL OR id<=?) "
            "ORDER BY id DESC LIMIT 10) "
            "SELECT COUNT(*) AS total_count, "
            "COALESCE(SUM(MAX(delta, 0)), 0) AS positive, "
            "COALESCE(SUM(MAX(-delta, 0)), 0) AS negative, "
            "(SELECT COUNT(*) FROM recent) AS recent_count, "
            "(SELECT COALESCE(SUM(MAX(delta, 0)), 0) FROM recent) AS recent_positive, "
            "(SELECT COALESCE(SUM(MAX(-delta, 0)), 0) FROM recent) AS recent_negative, "
            "COALESCE((SELECT base_relation FROM user_profiles WHERE bot_name=? "
            "AND user_id=?), 0) AS base_relation "
            "FROM relation_changes WHERE bot_name=? AND user_id=? "
            "AND (? IS NULL OR id<=?)",
            (*scope, through_id, through_id, *scope, *scope, through_id, through_id),
        ) as cursor:
            row = await cursor.fetchone()
        # Five positive and five negative prior weight units keep sparse samples neutral.
        recent_score = (
            100
            * (5 + row["recent_positive"])
            / (10 + row["recent_positive"] + row["recent_negative"])
        )
        long_term_score = (
            100 * (5 + row["positive"]) / (10 + row["positive"] + row["negative"])
        )
        interaction_score = 0.6 * recent_score + 0.4 * long_term_score
        return {
            "base_relation": row["base_relation"],
            "interaction_score": interaction_score,
            "recent_score": recent_score,
            "long_term_score": long_term_score,
            "relation": row["base_relation"] + interaction_score,
            "total_count": row["total_count"],
            "recent_count": row["recent_count"],
        }

    async def apply_relation_change(
        self,
        bot_name: str,
        group_or_user_id: str,
        user_id: str,
        delta: int,
        reason: str,
        source_ids: list[int] | None = None,
        occurred_at: str = "",
    ) -> tuple[float, str, bool]:
        """Commit an interaction rating and its derived scores without replaying a batch.

        Args:
            bot_name: Owning bot configuration.
            group_or_user_id: Owning conversation.
            user_id: Member whose affinity changes.
            delta: Signed interaction rating from minus five to plus five.
            reason: Concise description of the observed interaction.
            source_ids: Exact source batch, also used for replay protection.
            occurred_at: Time of the last message in the source batch.

        Returns:
            Current affinity, legacy title, and whether a change was inserted.

        Raises:
            ValueError: The rating is invalid or its reason is empty.
        """
        reason = " ".join(reason.split()).strip()
        if type(delta) is not int or not 1 <= abs(delta) <= 5 or not reason:
            raise ValueError(
                "Interaction ratings require a nonzero integer within [-5, 5] and a reason"
            )
        scope = (bot_name, group_or_user_id, user_id)
        source_ids = sorted({int(value) for value in source_ids or [] if value > 0})
        source_key = json.dumps(source_ids) if source_ids else None
        if not hasattr(self, "_relation_change_lock"):
            self._relation_change_lock = asyncio.Lock()
        async with self._relation_change_lock:
            async with self.conn.execute("PRAGMA database_list") as cursor:
                database_file = (await cursor.fetchone())[2]
            # Isolate the transaction from commits made by other stores.
            writer = (
                await aiosqlite.connect(database_file) if database_file else self.conn
            )
            writer.row_factory = aiosqlite.Row
            try:
                await writer.execute("BEGIN IMMEDIATE")
                now = datetime.now(timezone.utc).isoformat()
                await writer.execute(
                    "INSERT INTO user_profiles (bot_name, user_id, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(user_id, bot_name) DO NOTHING",
                    (bot_name, user_id, now, now),
                )
                await writer.execute(
                    "INSERT OR IGNORE INTO user_profile_sessions "
                    "(bot_name, group_or_user_id, user_id) VALUES (?, ?, ?)",
                    scope,
                )
                async with writer.execute(
                    "SELECT COALESCE(title, '') FROM user_profiles "
                    "WHERE bot_name=? AND user_id=?",
                    (bot_name, user_id),
                ) as cursor:
                    title = (await cursor.fetchone())[0]
                if source_key:
                    async with writer.execute(
                        "SELECT 1 FROM relation_changes WHERE bot_name=? "
                        "AND group_or_user_id=? AND user_id=? AND source_ids=?",
                        (*scope, source_key),
                    ) as cursor:
                        if await cursor.fetchone():
                            current_relation = (
                                await self.get_relation_score(*scope, connection=writer)
                            )["relation"]
                            await writer.rollback()
                            return current_relation, title, False
                await writer.execute(
                    "INSERT INTO relation_changes (bot_name, group_or_user_id, user_id, "
                    "delta, reason, source_ids, "
                    "occurred_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        *scope,
                        delta,
                        reason,
                        source_key,
                        occurred_at or now,
                        now,
                    ),
                )
                new_relation = (
                    await self.get_relation_score(*scope, connection=writer)
                )["relation"]
                await writer.execute(
                    "UPDATE user_profiles SET updated_at=? "
                    "WHERE bot_name=? AND user_id=?",
                    (now, bot_name, user_id),
                )
                await writer.commit()
                return new_relation, title, True
            except BaseException:
                await writer.rollback()
                raise
            finally:
                if database_file:
                    await writer.close()

    async def get_relation_changes(
        self,
        bot_name: str,
        group_or_user_id: str,
        user_id: str,
        limit: int = 50,
        after_id: int = 0,
        through_id: int | None = None,
        oldest_first: bool = False,
        offset: int = 0,
        *,
        current_session_only: bool = False,
    ) -> list[dict]:
        """Read a bounded affinity history in chronological order.

        Args:
            bot_name: Owning bot configuration.
            group_or_user_id: Current conversation used to label external sources.
            user_id: Member whose history is requested.
            limit: Maximum number of records, capped at fifty.
            after_id: Exclusive lower boundary.
            through_id: Inclusive frozen upper boundary.
            oldest_first: Select the oldest matching records for backlog processing.
            offset: Number of matching records to skip in the selected order.
            current_session_only: Restrict display records to the current conversation.
                Scores and counts are calculated separately from shared history.

        Returns:
            Matching records ordered from oldest to newest.
        """
        order = "ASC" if oldest_first else "DESC"
        session_clause = "AND group_or_user_id=? " if current_session_only else ""
        params = [bot_name, user_id]
        if current_session_only:
            params.append(group_or_user_id)
        async with self.conn.execute(
            "SELECT * FROM relation_changes WHERE bot_name=? "
            f"AND user_id=? {session_clause}AND id>? AND (? IS NULL OR id<=?) "
            f"ORDER BY id {order} LIMIT ? OFFSET ?",
            (
                *params,
                after_id,
                through_id,
                through_id,
                max(1, min(50, int(limit))),
                max(0, int(offset)),
            ),
        ) as cursor:
            rows = [dict(row) for row in await cursor.fetchall()]
        for row in rows:
            source = row["group_or_user_id"]
            row["source_label"] = (
                ("来源私聊" if source == user_id else f"来源群聊：{source}")
                if source != group_or_user_id
                else ""
            )
        return rows if oldest_first else rows[::-1]

    async def mutate_relation_history(
        self,
        bot_name: str,
        group_or_user_id: str,
        user_id: str,
        *,
        action: str,
        expected_revision: int,
        change_id: int | None = None,
        delta: int | None = None,
        reason: str = "",
    ) -> dict:
        """Atomically add, edit or delete a member's interaction rating.

        Args:
            bot_name: Owning bot configuration.
            group_or_user_id: Owning conversation.
            user_id: Member whose history is being managed.
            action: One of add, update or delete.
            expected_revision: Manual-history revision read by the editor.
            change_id: Scoped record ID for update or delete.
            delta: Signed nonzero integer within minus five to plus five.
            reason: Interaction description, containing at most 120 characters.

        Returns:
            Record ID, new history revision and recomputed score components.

        Raises:
            ValueError: The action, revision, rating or reason is invalid.
            LookupError: The member or scoped record no longer exists.
            RelationHistoryConflictError: Another editor changed this history.
        """
        if not isinstance(action, str) or action not in {"add", "update", "delete"}:
            raise ValueError("不支持的互动评价操作")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("互动评价历史版本必须是非负整数")
        if action != "add" and (
            type(change_id) is not int or not 1 <= change_id <= 2**63 - 1
        ):
            raise ValueError("评价记录 ID 必须是正整数")
        if action != "delete":
            if type(delta) is not int or not 1 <= abs(delta) <= 5:
                raise ValueError("评价得分必须是 -5～+5 的非零整数")
            if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 120:
                raise ValueError("评价内容须为 1～120 字")
            reason = reason.strip()
        if action == "add" and not group_or_user_id:
            raise ValueError("新增互动评价需要指定来源会话")
        scope = (bot_name, group_or_user_id, user_id)
        if not hasattr(self, "_relation_change_lock"):
            self._relation_change_lock = asyncio.Lock()
        async with self._relation_change_lock:
            async with self.conn.execute("PRAGMA database_list") as cursor:
                database_file = (await cursor.fetchone())[2]
            writer = (
                await aiosqlite.connect(database_file) if database_file else self.conn
            )
            writer.row_factory = aiosqlite.Row
            try:
                await writer.execute("BEGIN IMMEDIATE")
                async with writer.execute(
                    "SELECT relation_history_revision FROM user_profiles "
                    "WHERE bot_name=? AND user_id=?",
                    (bot_name, user_id),
                ) as cursor:
                    profile = await cursor.fetchone()
                if profile is None:
                    raise LookupError("成员画像不存在，请刷新后重试")
                if profile[0] != expected_revision:
                    raise RelationHistoryConflictError(
                        "互动评价历史已更新，请刷新后重试"
                    )
                now = datetime.now(timezone.utc).isoformat()
                if action == "add":
                    changed = await writer.execute(
                        "INSERT INTO relation_changes (bot_name, group_or_user_id, "
                        "user_id, delta, reason, "
                        "occurred_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (*scope, delta, reason, now, now),
                    )
                    change_id = changed.lastrowid
                elif action == "update":
                    changed = await writer.execute(
                        "UPDATE relation_changes SET delta=?, reason=? "
                        "WHERE id=? AND bot_name=? AND user_id=?",
                        (delta, reason, change_id, bot_name, user_id),
                    )
                else:
                    changed = await writer.execute(
                        "DELETE FROM relation_changes "
                        "WHERE id=? AND bot_name=? AND user_id=?",
                        (change_id, bot_name, user_id),
                    )
                if changed.rowcount != 1:
                    raise LookupError("评价记录不存在或不属于当前成员，请刷新后重试")
                score = await self.get_relation_score(*scope, connection=writer)
                if action == "add":
                    await writer.execute(
                        "INSERT OR IGNORE INTO user_profile_sessions "
                        "(bot_name, group_or_user_id, user_id) VALUES (?, ?, ?)",
                        scope,
                    )
                await writer.execute(
                    "UPDATE user_profiles SET relation_history_revision="
                    "relation_history_revision+1, attitude_evaluated_change_id=0, "
                    "updated_at=? WHERE bot_name=? AND user_id=?",
                    (now, bot_name, user_id),
                )
                await writer.commit()
                return {
                    "id": change_id,
                    "revision": expected_revision + 1,
                    "score": score,
                }
            except BaseException:
                await writer.rollback()
                raise
            finally:
                if database_file:
                    await writer.close()

    async def get_pending_attitude_evaluations(
        self,
        bot_name: str,
        group_or_user_id: str,
        min_changes: int = 5,
        major_delta: int = 4,
    ) -> list[dict]:
        """Find members whose unevaluated history warrants a review.

        Args:
            bot_name: Owning bot configuration.
            group_or_user_id: Owning conversation.
            min_changes: Number of changes needed for an ordinary review.
            major_delta: Absolute increment that triggers an early review.

        Returns:
            Member IDs, their saved cursors, and frozen review boundaries.
        """
        async with self.conn.execute(
            "SELECT rc.user_id, up.attitude_evaluated_change_id AS evaluated_id, "
            "up.relation_history_revision AS history_revision, "
            "MAX(rc.id) AS through_id, COUNT(*) AS change_count FROM relation_changes rc "
            "JOIN user_profiles up ON up.bot_name=rc.bot_name "
            "AND up.user_id=rc.user_id "
            "WHERE rc.bot_name=? "
            "AND rc.id>up.attitude_evaluated_change_id "
            "GROUP BY rc.user_id, up.attitude_evaluated_change_id, up.relation_history_revision "
            "HAVING COUNT(*)>=? OR MAX(ABS(rc.delta))>=? ORDER BY MIN(rc.id)",
            (bot_name, min_changes, major_delta),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def save_attitude_evaluation(
        self,
        bot_name: str,
        group_or_user_id: str,
        user_id: str,
        expected_cursor: int,
        through_id: int,
        attitude: str | None = None,
        expected_history_revision: int | None = None,
    ) -> bool:
        """Save the interaction tone and its exact successful review boundary.

        Args:
            bot_name: Owning bot configuration.
            group_or_user_id: Owning conversation.
            user_id: Evaluated member.
            expected_cursor: Cursor read before inference.
            through_id: Last history record actually provided to the model.
            attitude: New mutual interaction tone, or None to preserve it.
            expected_history_revision: Reject conclusions based on manually revised history.

        Returns:
            Whether the unchanged profile generation accepted this evaluation.
        """
        cursor = await self.conn.execute(
            "UPDATE user_profiles SET attitude=COALESCE(?, attitude), "
            "attitude_evaluated_change_id=?, updated_at=? "
            "WHERE bot_name=? AND user_id=? "
            "AND attitude_evaluated_change_id=? AND attitude_evaluated_change_id<? "
            "AND (? IS NULL OR relation_history_revision=?) "
            "AND EXISTS (SELECT 1 FROM relation_changes WHERE id=? AND bot_name=? "
            "AND user_id=?)",
            (
                attitude,
                through_id,
                datetime.now(timezone.utc).isoformat(),
                bot_name,
                user_id,
                expected_cursor,
                through_id,
                expected_history_revision,
                expected_history_revision,
                through_id,
                bot_name,
                user_id,
            ),
        )
        await self.conn.commit()
        return cursor.rowcount > 0
