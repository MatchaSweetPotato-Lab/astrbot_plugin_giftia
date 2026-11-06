from types import SimpleNamespace

import pytest
import pytest_asyncio
from core.bot.bot_config_manager import BotConfigManager
from core.database.data_cache import DataCache
from core.database.database import Database
from core.utils.relation_scoring import DEFAULT_RELATION_TIER_COUNTS


@pytest_asyncio.fixture
async def scoring_db(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "astrbot.api.star.StarTools.get_data_dir", lambda *args: tmp_path
    )
    db = await Database.connect()
    try:
        yield db
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("magnitude", range(1, 6))
@pytest.mark.parametrize("count", [1, 2, 7, 30])
@pytest.mark.parametrize("direction", [-1, 1])
async def test_configured_count_reaches_its_own_interval_on_exactly_that_rating(
    scoring_db, magnitude, count, direction
):
    db = scoring_db
    counts = list(DEFAULT_RELATION_TIER_COUNTS)
    counts[magnitude - 1] = count
    db.relation_tier_counts["bot"] = counts
    old_time = "2020-01-01T00:00:00+00:00"
    rows = []
    if magnitude > 1:
        rows.extend(
            [(direction * (magnitude - 1), "前一档已满", old_time, old_time)] * 100
        )
    rows.extend([(direction * magnitude, "本档评价", old_time, old_time)] * (count - 1))
    await db.conn.executemany(
        "INSERT INTO relation_changes (bot_name, group_or_user_id, user_id, delta, reason, occurred_at, created_at) "
        "VALUES ('bot', 'group', 'user', ?, ?, ?, ?)",
        rows,
    )
    await db.conn.commit()
    target = (80, 85, 90, 95, 99)[magnitude - 1] - 50
    before = await db.get_relation_score("bot", "another-group", "user")
    assert direction * (before["long_term_score"] - 50) < target
    await db.apply_relation_change(
        "bot", "group", "user", direction * magnitude, "最后一次", occurred_at=old_time
    )
    after = await db.get_relation_score("bot", "group", "user")
    reached = direction * (after["long_term_score"] - 50)
    if magnitude < 5:
        assert reached == pytest.approx(target)
    else:
        assert target <= reached < 50
    assert after["interaction_score"] == after["long_term_score"]


@pytest.mark.asyncio
async def test_changes_recompute_both_windows_and_baseline_without_rewriting_history(
    scoring_db,
):
    db = scoring_db
    old_time = "2020-01-01T00:00:00+00:00"
    for bot in ("bot-a", "bot-b"):
        await db.upsert_user_profile(bot, "group", "user", base_relation=7)
        for _ in range(5):
            await db.apply_relation_change(
                bot, "group", "user", 1, "历史友好交流", occurred_at=old_time
            )
        await db.apply_relation_change(bot, "group", "user", 2, "具体关心")
        await db.apply_relation_change(bot, "group", "user", -1, "轻微不尊重")
    cache = DataCache(db, None, None, plugin=SimpleNamespace())
    original = await cache.get_user_profile_record("bot-a", "group", "user")
    other_before = await db.get_relation_score("bot-b", "group", "user")
    history_before = await db.get_relation_changes("bot-a", "group", "user")
    db.relation_tier_counts["bot-a"] = [26, 4, 7, 6, 28]
    updated = await cache.get_user_profile_record("bot-a", "another-group", "user")
    for field in (
        "long_term_score",
        "recent_score",
        "recent_baseline_score",
        "interaction_score",
    ):
        assert updated[field] != original[field]
    assert updated["relation"] == updated["interaction_score"] + 7
    assert updated["base_relation"] == 7
    assert updated["recent_count"] == 2
    assert updated["total_count"] == 7
    baseline = await db.get_relation_score("bot-a", "group", "user", through_id=5)
    assert updated["recent_baseline_score"] == baseline["long_term_score"]
    assert await db.get_relation_score("bot-b", "group", "user") == other_before
    assert await db.get_relation_changes("bot-a", "group", "user") == history_before
    db.relation_tier_counts["bot-a"] = list(DEFAULT_RELATION_TIER_COUNTS)
    restored = await db.get_relation_score("bot-a", "group", "user")
    for field in restored:
        assert restored[field] == original[field]


@pytest.mark.asyncio
async def test_custom_counts_preserve_cancellation_and_do_not_require_lower_tiers(
    scoring_db,
):
    db = scoring_db
    db.relation_tier_counts["bot"] = [26, 4, 7, 6, 28]
    old_time = "2020-01-01T00:00:00+00:00"
    await db.apply_relation_change(
        "bot", "group", "user", 5, "直接发生重要互动", occurred_at=old_time
    )
    assert (await db.get_relation_score("bot", "group", "user"))["relation"] > 50
    await db.apply_relation_change(
        "bot", "group", "user", -5, "同档反向影响", occurred_at=old_time
    )
    assert (await db.get_relation_score("bot", "group", "user"))["relation"] == 50


@pytest.mark.parametrize("value", [None, [], [True, 2, 2, 3, 14], [13, 2, 0, 3, 14]])
def test_invalid_file_counts_fall_back_without_sharing_mutable_defaults(value):
    manager = BotConfigManager.__new__(BotConfigManager)
    first = manager.normalize_bot_config({"relation_tier_counts": value})
    second = manager.normalize_bot_config({})
    assert first["relation_tier_counts"] == list(DEFAULT_RELATION_TIER_COUNTS)
    first["relation_tier_counts"][0] = 99
    assert second["relation_tier_counts"] == list(DEFAULT_RELATION_TIER_COUNTS)
