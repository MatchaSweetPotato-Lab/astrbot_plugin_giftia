import asyncio
import json
from datetime import datetime, timedelta, timezone

import aiosqlite

from .operations import serialized_operation

RELATION_CURVE_SCALE = 60 / 7
RELATION_TIER_SCORE_LIMITS = (80, 85, 90, 95, 100)
RELATION_TIER_WEIGHT_LIMITS = tuple(
    RELATION_CURVE_SCALE * (score - 50) / (100 - score) if score < 100 else float("inf")
    for score in RELATION_TIER_SCORE_LIMITS
)
RELATION_RECENT_CONFIDENCE_SCALE = 10
RELATION_RECENT_SAMPLE_LIMIT = 10


def _allocate_relation_weight(rating_weights: tuple[float, ...]) -> float:
    """Limit accumulated evidence before adding each stronger rating tier.

    Args:
        rating_weights: Signed net squared weights for magnitudes one through five.

    Returns:
        Effective signed weight after applying each tier's symmetric score limits.
        Lower-tier overflow cannot cancel stronger evidence.

    Raises:
        ValueError: The number of tier weights is not five.
    """
    weight = 0.0
    for tier_weight, limit in zip(
        rating_weights, RELATION_TIER_WEIGHT_LIMITS, strict=True
    ):
        weight = max(-limit, min(limit, weight + tier_weight))
    return weight


def _smooth_recent_relation_score(
    long_term_score: float, deltas: tuple[int, ...]
) -> float:
    """Replay bounded recent evidence without making positive ratings lower affinity.

    Args:
        long_term_score: Relationship established by all recorded evaluations.
        deltas: Selected recent ratings in occurrence order, oldest first.

    Returns:
        Interaction score anchored to the long-term score. Each rating responds
        at strength / (10 + strength) toward its confidence-weighted target,
        so later ratings retain more influence.
        Positive and negative evidence have separate progressive tier budgets,
        assigned to stronger magnitudes and then newer records first. Overflow
        cannot buy influence or repair stronger evidence. Total effective
        strength sets the target share, 0.6 * strength / (10 + strength). Prefix
        direction guards prevent budget redistribution reversing a new rating.
    """
    interaction = long_term_score
    # At most ten prefixes are replayed; every read remains deterministic.
    for end, last_delta in enumerate(deltas, start=1):
        prefix = deltas[:end]
        directional_weights = [[0.0] * 5 for _ in range(2)]
        for delta in prefix:
            direction = 0 if delta > 0 else 1
            directional_weights[direction][abs(delta) - 1] += delta * delta
        # Stronger evidence keeps its budget; lower tiers use remaining capacity.
        for weights in directional_weights:
            remaining = float("inf")
            for tier in range(4, -1, -1):
                remaining = min(remaining, RELATION_TIER_WEIGHT_LIMITS[tier])
                weights[tier] = min(weights[tier], remaining)
                remaining -= weights[tier]
        steps = []
        for delta in reversed(prefix):
            direction = 0 if delta > 0 else 1
            tier = abs(delta) - 1
            strength = min(delta * delta, directional_weights[direction][tier])
            directional_weights[direction][tier] -= strength
            steps.append((delta, tier, strength))
        smoothed = long_term_score
        evidence = 0.0
        for delta, tier, strength in reversed(steps):
            if strength <= 0:
                continue
            evidence += strength
            recent_share = (
                0.6 * evidence / (RELATION_RECENT_CONFIDENCE_SCALE + evidence)
            )
            limit = RELATION_TIER_SCORE_LIMITS[tier]
            # Low ratings can repair established affinity without creating
            # positive inventory that shields the next negative interaction.
            bound = (
                max(long_term_score, limit)
                if delta > 0
                else min(long_term_score, 100 - limit)
            )
            target = long_term_score + recent_share * (bound - long_term_score)
            target = max(smoothed, target) if delta > 0 else min(smoothed, target)
            response = strength / (RELATION_RECENT_CONFIDENCE_SCALE + strength)
            smoothed += response * (target - smoothed)
        # Reassigning older budgets must not reverse the newest rating's effect.
        interaction = (
            max(interaction, smoothed) if last_delta > 0 else min(interaction, smoothed)
        )
    return interaction


class RelationHistoryConflictError(Exception):
    """The member's history changed after a manual editor loaded it."""


class RelationHistoryStoreMixin:
    @serialized_operation
    async def get_relation_score(
        self,
        bot_name: str,
        group_or_user_id: str,
        user_id: str,
        *,
        connection: aiosqlite.Connection | None = None,
        through_id: int | None = None,
    ) -> dict[str, int | float]:
        """Calculate affinity from five progressively limited rating tiers.

        Args:
            bot_name: Owning bot configuration.
            group_or_user_id: Current conversation; affinity is shared across conversations.
            user_id: Member whose affinity is calculated.
            connection: Transaction connection when recording an interaction.
            through_id: Inclusive history boundary, or None for current scores.

        Returns:
            Unrounded final, base, recent, long-term and interaction scores with
            sample counts. The recent baseline excludes the selected recent
            ratings and applies the same historical aggregation without replaying
            or replacing them. Each magnitude cancels its own signed squared weights.
            Accumulated weights are limited to the 80, 85, 90 and 95 score bounds
            before adding the next tier. Magnitude five can approach 100 or zero.
            Stronger weights fill lower ranges before carrying into higher ones.
            Recent scores use at most ten ratings from the past seven days,
            ordered by occurrence time and ID, and fall back to the long-term
            score when empty. If recent samples cover all history, the interaction
            score equals the long-term score; otherwise recent corrections replay
            the selected ratings in order. Positive ratings never lower the
            replayed score, and negative ratings never raise it. Effective recent
            strength sets the share, approaching sixty percent. Empty history is
            fifty.
        """
        scope = (bot_name, user_id)
        now = datetime.now(timezone.utc)
        tier_stats = ", ".join(
            f"COALESCE(SUM(CASE WHEN magnitude={magnitude} THEN weight ELSE 0 END), 0) "
            f"AS weight_{magnitude}, "
            f"COALESCE(SUM(CASE WHEN magnitude={magnitude} THEN weight * is_recent "
            f"ELSE 0 END), 0) AS recent_weight_{magnitude}"
            for magnitude in range(1, 6)
        )
        async with (connection or self.conn).execute(
            # Legacy message times are local and naive; explicit offsets already
            # identify instants and must not receive SQLite's local-to-UTC shift.
            "WITH history AS (SELECT id, delta, ABS(delta) AS magnitude, "
            "delta * ABS(delta) AS weight, "
            "julianday(occurred_at, CASE "
            "WHEN substr(occurred_at, -1)='Z' "
            "OR substr(occurred_at, -6, 1) IN ('+', '-') "
            "THEN '+0 seconds' ELSE 'utc' END) AS occurred_time "
            "FROM relation_changes WHERE bot_name=? AND user_id=? "
            "AND (? IS NULL OR id<=?)), recent AS ("
            "SELECT id, delta, occurred_time FROM history "
            "WHERE occurred_time BETWEEN julianday(?) AND julianday(?) "
            "ORDER BY occurred_time DESC, id DESC LIMIT ?), scored AS ("
            "SELECT history.*, recent.id IS NOT NULL AS is_recent "
            "FROM history LEFT JOIN recent ON recent.id=history.id) "
            "SELECT COUNT(*) AS total_count, "
            "COALESCE(SUM(is_recent), 0) AS recent_count, "
            f"{tier_stats}, "
            "(SELECT GROUP_CONCAT(delta) FROM (SELECT delta FROM recent "
            "ORDER BY occurred_time, id)) AS recent_deltas, "
            "COALESCE((SELECT base_relation FROM user_profiles WHERE bot_name=? "
            "AND user_id=?), 0) AS base_relation "
            "FROM scored",
            (
                *scope,
                through_id,
                through_id,
                (now - timedelta(days=7)).isoformat(),
                now.isoformat(),
                RELATION_RECENT_SAMPLE_LIMIT,
                *scope,
            ),
        ) as cursor:
            row = await cursor.fetchone()
        # Each window cancels and limits its own tiers before combining scores.
        long_term_weight = _allocate_relation_weight(
            tuple(row[f"weight_{magnitude}"] for magnitude in range(1, 6))
        )
        recent_weight = _allocate_relation_weight(
            tuple(row[f"recent_weight_{magnitude}"] for magnitude in range(1, 6))
        )
        # Remove raw tier weights before cancellation and limits are reapplied.
        recent_baseline_weight = _allocate_relation_weight(
            tuple(
                row[f"weight_{magnitude}"] - row[f"recent_weight_{magnitude}"]
                for magnitude in range(1, 6)
            )
        )
        long_term_score, recent_score, recent_baseline_score = (
            50 + 50 * weight / (RELATION_CURVE_SCALE + abs(weight))
            for weight in (long_term_weight, recent_weight, recent_baseline_weight)
        )
        if not row["recent_count"]:
            recent_score = long_term_score
        recent_deltas = tuple(
            int(value) for value in (row["recent_deltas"] or "").split(",") if value
        )
        # Identical samples have no separate recent evidence to amplify.
        interaction_score = (
            long_term_score
            if row["total_count"] == row["recent_count"]
            else _smooth_recent_relation_score(long_term_score, recent_deltas)
        )
        return {
            "base_relation": row["base_relation"],
            "interaction_score": interaction_score,
            "recent_score": recent_score,
            "long_term_score": long_term_score,
            "recent_baseline_score": recent_baseline_score,
            "relation": row["base_relation"] + interaction_score,
            "total_count": row["total_count"],
            "recent_count": row["recent_count"],
        }

    @serialized_operation
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

    @serialized_operation
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

    @serialized_operation
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
                    "relation_history_revision+1, "
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
