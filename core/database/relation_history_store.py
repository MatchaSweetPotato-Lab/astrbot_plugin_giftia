import asyncio
import json
from datetime import datetime, timezone

import aiosqlite


class RelationHistoryStoreMixin:
    async def get_relation_score(
        self,
        bot_name: str,
        group_or_user_id: str,
        user_id: str,
        *,
        connection: aiosqlite.Connection | None = None,
    ) -> dict[str, int | float]:
        """Calculate affinity from the manual base and weighted interaction history.

        Args:
            bot_name: Owning bot configuration.
            group_or_user_id: Owning conversation.
            user_id: Member whose affinity is calculated.
            connection: Transaction connection when recording an interaction.

        Returns:
            Unrounded final, base, recent, long-term and interaction scores with
            sample counts. An empty history has an interaction score of fifty.
        """
        scope = (bot_name, group_or_user_id, user_id)
        async with (connection or self.conn).execute(
            "WITH recent AS (SELECT delta FROM relation_changes WHERE bot_name=? "
            "AND group_or_user_id=? AND user_id=? ORDER BY id DESC LIMIT 10) "
            "SELECT COUNT(*) AS total_count, "
            "COALESCE(SUM(MAX(delta, 0)), 0) AS positive, "
            "COALESCE(SUM(MAX(-delta, 0)), 0) AS negative, "
            "(SELECT COUNT(*) FROM recent) AS recent_count, "
            "(SELECT COALESCE(SUM(MAX(delta, 0)), 0) FROM recent) AS recent_positive, "
            "(SELECT COALESCE(SUM(MAX(-delta, 0)), 0) FROM recent) AS recent_negative, "
            "COALESCE((SELECT base_relation FROM user_profiles WHERE bot_name=? "
            "AND group_or_user_id=? AND user_id=?), 0) AS base_relation "
            "FROM relation_changes WHERE bot_name=? AND group_or_user_id=? AND user_id=?",
            (*scope, *scope, *scope),
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
                    "INSERT INTO user_profiles (bot_name, group_or_user_id, user_id, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(user_id, group_or_user_id, bot_name) DO NOTHING",
                    (*scope, now, now),
                )
                async with writer.execute(
                    "SELECT COALESCE(title, '') FROM user_profiles "
                    "WHERE bot_name=? AND group_or_user_id=? AND user_id=?",
                    scope,
                ) as cursor:
                    title = (await cursor.fetchone())[0]
                current_relation = (
                    await self.get_relation_score(*scope, connection=writer)
                )["relation"]
                if source_key:
                    async with writer.execute(
                        "SELECT 1 FROM relation_changes WHERE bot_name=? "
                        "AND group_or_user_id=? AND user_id=? AND source_ids=?",
                        (*scope, source_key),
                    ) as cursor:
                        if await cursor.fetchone():
                            await writer.rollback()
                            return current_relation, title, False
                inserted = await writer.execute(
                    "INSERT INTO relation_changes (bot_name, group_or_user_id, user_id, "
                    "delta, relation_before, relation_after, reason, source_ids, "
                    "occurred_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        *scope,
                        delta,
                        current_relation,
                        current_relation,
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
                    "UPDATE relation_changes SET relation_after=? WHERE id=?",
                    (new_relation, inserted.lastrowid),
                )
                await writer.execute(
                    "UPDATE user_profiles SET updated_at=? "
                    "WHERE bot_name=? AND group_or_user_id=? AND user_id=?",
                    (now, *scope),
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
    ) -> list[dict]:
        """Read a bounded affinity history in chronological order.

        Args:
            bot_name: Owning bot configuration.
            group_or_user_id: Owning conversation.
            user_id: Member whose history is requested.
            limit: Maximum number of records, capped at fifty.
            after_id: Exclusive lower boundary.
            through_id: Inclusive frozen upper boundary.
            oldest_first: Select the oldest matching records for backlog processing.

        Returns:
            Matching records ordered from oldest to newest.
        """
        order = "ASC" if oldest_first else "DESC"
        async with self.conn.execute(
            "SELECT * FROM relation_changes WHERE bot_name=? AND group_or_user_id=? "
            "AND user_id=? AND id>? AND (? IS NULL OR id<=?) "
            f"ORDER BY id {order} LIMIT ?",
            (
                bot_name,
                group_or_user_id,
                user_id,
                after_id,
                through_id,
                through_id,
                max(1, min(50, int(limit))),
            ),
        ) as cursor:
            rows = [dict(row) for row in await cursor.fetchall()]
        return rows if oldest_first else rows[::-1]

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
            "MAX(rc.id) AS through_id, COUNT(*) AS change_count FROM relation_changes rc "
            "JOIN user_profiles up ON up.bot_name=rc.bot_name "
            "AND up.group_or_user_id=rc.group_or_user_id AND up.user_id=rc.user_id "
            "WHERE rc.bot_name=? AND rc.group_or_user_id=? "
            "AND rc.id>up.attitude_evaluated_change_id "
            "GROUP BY rc.user_id, up.attitude_evaluated_change_id "
            "HAVING COUNT(*)>=? OR MAX(ABS(rc.delta))>=? ORDER BY MIN(rc.id)",
            (bot_name, group_or_user_id, min_changes, major_delta),
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
    ) -> bool:
        """Save the interaction tone and its exact successful review boundary.

        Args:
            bot_name: Owning bot configuration.
            group_or_user_id: Owning conversation.
            user_id: Evaluated member.
            expected_cursor: Cursor read before inference.
            through_id: Last history record actually provided to the model.
            attitude: New mutual interaction tone, or None to preserve it.

        Returns:
            Whether the unchanged profile generation accepted this evaluation.
        """
        cursor = await self.conn.execute(
            "UPDATE user_profiles SET attitude=COALESCE(?, attitude), "
            "attitude_evaluated_change_id=?, updated_at=? "
            "WHERE bot_name=? AND group_or_user_id=? AND user_id=? "
            "AND attitude_evaluated_change_id=? AND attitude_evaluated_change_id<? "
            "AND EXISTS (SELECT 1 FROM relation_changes WHERE id=? AND bot_name=? "
            "AND group_or_user_id=? AND user_id=?)",
            (
                attitude,
                through_id,
                datetime.now(timezone.utc).isoformat(),
                bot_name,
                group_or_user_id,
                user_id,
                expected_cursor,
                through_id,
                through_id,
                bot_name,
                group_or_user_id,
                user_id,
            ),
        )
        await self.conn.commit()
        return cursor.rowcount > 0
