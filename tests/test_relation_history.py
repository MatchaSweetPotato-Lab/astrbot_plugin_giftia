import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.etree.ElementTree import fromstring

import aiosqlite
import pytest
import pytest_asyncio
from core.database.data_cache import DataCache
from core.database.database import Database
from core.database.schema import initialize_database
from core.llm.prompt import build_decision_prompt, build_reply_prompt
from core.memory.passive_memory import PassiveMemoryManager
from core.utils.schemas import MessageData


@pytest.fixture
def relation_clock(monkeypatch):
    clock = SimpleNamespace(now=datetime(2026, 11, 4, 4, tzinfo=timezone.utc))
    monkeypatch.setattr(
        "core.database.relation_history_store.datetime",
        SimpleNamespace(now=lambda tz: clock.now.astimezone(tz)),
    )
    return clock


@pytest_asyncio.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "astrbot.api.star.StarTools.get_data_dir", lambda *args: tmp_path
    )
    db = await Database.connect()
    plugin = SimpleNamespace(
        db=db,
        conf={"memory_config": {}},
        embedding_conf={"enabled": False},
        passive_memory_enabled=True,
        passive_memory_provider_ids=["summary"],
        active_reply_counters={},
        bot_map={"bot": {"nickname": "Giftia"}},
    )
    plugin.data_cache = DataCache(db, None, None, plugin=plugin)
    manager = PassiveMemoryManager(plugin)
    manager._call_summary_llm = AsyncMock(return_value='{"attitude": null}')
    try:
        yield SimpleNamespace(db=db, cache=plugin.data_cache, manager=manager)
    finally:
        await manager.close()
        await db.close()


@pytest.mark.asyncio
async def test_profile_stage_records_one_reason_per_source_batch_and_ignores_attitude(
    runtime,
):
    await runtime.db.upsert_user_profile(
        "bot",
        "group",
        "user",
        base_relation=10,
        profile_fields={"attitude": "礼貌交流"},
    )
    source = MessageData(
        db_id=7,
        user_id="user",
        content="谢谢你刚才认真解释，我明白了",
        time="2026-10-18T12:00:00+08:00",
    )
    context = await runtime.manager._build_summary_context(
        "bot", "group", "self", [source]
    )
    runtime.manager._call_summary_llm.return_value = (
        '<summary_user_profile user_id="user"><attitude>擅自覆盖</attitude>'
        '<update_relation delta="+1"> </update_relation>'
        '<update_relation delta="+2">他认真感谢了我的解释</update_relation>'
        '<update_relation delta="-5">重复增减</update_relation></summary_user_profile>'
        '<summary_user_profile user_id="user">'
        '<update_relation delta="+3">重复标签</update_relation></summary_user_profile>'
    )
    for _ in range(2):
        assert await runtime.manager._run_profile_summary_task(
            "bot", "group", "Giftia", "self", context
        )
    changes = await runtime.db.get_relation_changes("bot", "group", "user")
    assert len(changes) == 1
    assert changes[0]["delta"] == 2
    assert "relation_before" not in changes[0]
    assert "relation_after" not in changes[0]
    assert changes[0]["reason"] == "他认真感谢了我的解释"
    assert json.loads(changes[0]["source_ids"]) == [7]
    assert changes[0]["occurred_at"] == source.time
    profile = await runtime.db.get_user_profile_record("bot", "group", "user")
    assert profile["attitude"] == "礼貌交流"
    assert profile["relation"] == pytest.approx(10 + 725 / 11)


@pytest.mark.asyncio
async def test_affinity_context_does_not_change_when_long_term_tone_changes(runtime):
    source = MessageData(user_id="user", nickname="User", content="今天聊点新鲜事")
    contexts = []
    for attitude in ("长期亲近，互相接梗", "长期疏远，交流紧张"):
        await runtime.cache.set_user_profile(
            "bot",
            "group",
            "user",
            base_relation=12,
            profile_fields={"attitude": attitude, "personality": "不应注入的长期性格"},
        )
        contexts.append(
            await runtime.manager._build_summary_context(
                "bot", "group", "self", [source]
            )
        )
    assert contexts[0]["user_profiles_text"] == contexts[1]["user_profiles_text"]
    prompt = runtime.manager._build_profile_user_prompt("group", contexts[0])
    assert "好感度:" not in prompt
    assert source.content in prompt
    for omitted in ("互动态度", "长期亲近", "长期疏远", "长期性格"):
        assert omitted not in prompt


@pytest.mark.asyncio
async def test_existing_numeric_affinity_is_ignored_without_inventing_history():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(
            "CREATE TABLE user_profiles (id INTEGER PRIMARY KEY, "
            "bot_name TEXT NOT NULL, group_or_user_id TEXT NOT NULL, "
            "user_id TEXT NOT NULL, relation INTEGER, attitude TEXT, "
            "created_at TEXT, updated_at TEXT)"
        )
        await conn.execute(
            "INSERT INTO user_profiles (bot_name, group_or_user_id, user_id, relation, attitude) "
            "VALUES ('bot', 'group', 'user', 23, '熟络')"
        )
        await conn.commit()
        for _ in range(2):
            await initialize_database(conn)
        async with conn.execute(
            "SELECT attitude, attitude_evaluated_change_id FROM user_profiles"
        ) as cursor:
            assert tuple(await cursor.fetchone()) == (None, 0)
        db = Database(conn)
        score = await db.get_relation_score("bot", "group", "user")
        assert score["base_relation"] == 0
        assert score["interaction_score"] == 50
        assert score["relation"] == 50
        assert score["total_count"] == 0


@pytest.mark.asyncio
async def test_failed_affinity_write_rolls_back_history_and_does_not_change_cache(
    runtime,
):
    await runtime.cache.set_user_profile("bot", "group", "user", base_relation=10)
    await runtime.cache.get_user_profile_record("bot", "group", "user")
    await runtime.db.conn.execute(
        "CREATE TRIGGER reject_affinity BEFORE UPDATE OF updated_at ON user_profiles "
        "BEGIN SELECT RAISE(ABORT, 'forced write failure'); END"
    )
    await runtime.db.conn.commit()
    with pytest.raises(aiosqlite.IntegrityError, match="forced write failure"):
        await runtime.cache.update_relation("bot", "group", "user", 1, "他主动道谢")
    assert await runtime.db.get_relation_changes("bot", "group", "user") == []
    assert (await runtime.db.get_relation("bot", "group", "user"))[0] == 60
    assert (await runtime.cache.get_user_relation("bot", "group", "user"))[0] == 60
    assert runtime.cache.user_profile_records["bot:user"]["relation"] == 60


@pytest.mark.asyncio
async def test_concurrent_changes_are_atomic_and_replays_are_scoped(runtime):
    results = await asyncio.gather(
        runtime.cache.update_relation("bot", "group", "user", 1, "先前互动", [1]),
        runtime.cache.update_relation("bot", "group", "user", 2, "后续互动", [2]),
    )
    assert results == [True, True]
    assert not await runtime.cache.update_relation(
        "bot", "group", "user", 5, "重复批次", [1]
    )
    assert (await runtime.cache.get_user_relation("bot", "group", "user"))[
        0
    ] == pytest.approx(1300 / 19)
    history = await runtime.db.get_relation_changes("bot", "group", "user")
    assert [item["delta"] for item in history] == [1, 2]
    score = await runtime.db.get_relation_score("bot", "group", "user")
    assert score["total_count"] == 2
    assert score["recent_count"] == 2
    assert score["interaction_score"] == pytest.approx(1300 / 19)

    for scope in (
        ("other", "group", "user"),
        ("bot", "other", "user"),
        ("bot", "group", "other"),
    ):
        assert await runtime.cache.update_relation(*scope, 1, "独立会话互动", [1])
        expected_count = 3 if scope == ("bot", "other", "user") else 1
        assert len(await runtime.db.get_relation_changes(*scope)) == expected_count
        assert (await runtime.db.get_relation_score(*scope))[
            "total_count"
        ] == expected_count
    assert (await runtime.db.get_relation_score("bot", "group", "user"))[
        "total_count"
    ] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "deltas,expected",
    [
        ((), 50),
        ((1,), 3700 / 67),
        ((2,), 725 / 11),
        ((3,), 3100 / 41),
        ((4,), 3550 / 43),
        ((5,), 4100 / 47),
        ((-5,), 600 / 47),
        ((1,) * 10, 1000 / 13),
        ((1,) * 20, 85),
        ((1,) * 40, 85),
        ((2,) * 40, 85),
        ((-1,) * 40, 15),
        ((1,) * 100, 85),
        ((1,) * 100 + (-1,) * 10, 85),
        ((1, 1, -5), 3000 / 221),
        ((5, -1), 1650 / 19),
        ((1, 1, 1, 1, -1), 1700 / 27),
    ],
)
async def test_identical_windows_share_layered_scores_and_preserve_sample_counts(
    runtime, deltas, expected
):
    await runtime.cache.set_user_profile("bot", "group", "user", base_relation=-20)
    for delta in deltas:
        await runtime.cache.update_relation("bot", "group", "user", delta, "实际互动")
    score = await runtime.db.get_relation_score("bot", "group", "user")
    assert score["recent_count"] == len(deltas)
    assert score["total_count"] == len(deltas)
    assert score["base_relation"] == -20
    assert score["recent_score"] == score["long_term_score"]
    assert score["long_term_score"] == pytest.approx(expected)
    assert score["interaction_score"] == pytest.approx(expected)
    assert score["relation"] == pytest.approx(expected - 20)
    assert (await runtime.cache.get_user_relation("bot", "group", "user"))[
        0
    ] == pytest.approx(expected - 20)
    prompt = build_reply_prompt([], [], None, user_id="user", relation_score=score)
    assert f"互动分：{expected:.1f}" in prompt
    assert f"近7天{len(deltas)}次" in prompt
    assert f"共{len(deltas)}次" in prompt
    assert "正向变化占比" not in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("age_days", [0, 8])
@pytest.mark.parametrize("major_delta", [3, 4, 5])
async def test_daily_ratings_stay_below_85_and_major_events_control_the_upper_layer(
    runtime, relation_clock, age_days, major_delta
):
    occurred_at = (relation_clock.now - timedelta(days=age_days)).isoformat()
    await runtime.db.conn.executemany(
        "INSERT INTO relation_changes (bot_name, group_or_user_id, user_id, delta, "
        "reason, occurred_at, created_at) VALUES ('bot', 'group', 'user', 2, ?, ?, ?)",
        [("日常互动", occurred_at, occurred_at)] * 1000,
    )
    await runtime.db.conn.commit()
    daily = await runtime.db.get_relation_score("bot", "group", "user")
    assert daily["total_count"] == 1000
    for field in ("recent_score", "long_term_score", "interaction_score"):
        assert daily[field] == pytest.approx(85)

    await runtime.db.apply_relation_change(
        "bot", "group", "user", major_delta, "重要正向事件", occurred_at=occurred_at
    )
    supported = await runtime.db.get_relation_score("bot", "group", "user")
    for field in ("recent_score", "long_term_score", "interaction_score"):
        assert 85 < supported[field] <= 100

    await runtime.db.apply_relation_change(
        "bot", "group", "user", -1, "日常负向互动", occurred_at=occurred_at
    )
    daily_negative = await runtime.db.get_relation_score("bot", "group", "user")
    for field in ("recent_score", "long_term_score", "interaction_score"):
        assert daily_negative[field] == pytest.approx(supported[field])

    await runtime.db.apply_relation_change(
        "bot", "group", "user", -major_delta, "重要负向事件", occurred_at=occurred_at
    )
    cancelled = await runtime.db.get_relation_score("bot", "group", "user")
    for field in ("recent_score", "long_term_score", "interaction_score"):
        assert cancelled[field] == pytest.approx(85)

    await runtime.db.apply_relation_change(
        "bot",
        "group",
        "user",
        -5,
        "重要负向权重不能被日常溢出抵消",
        occurred_at=occurred_at,
    )
    debt = await runtime.db.get_relation_score("bot", "group", "user")
    for field in ("recent_score", "long_term_score", "interaction_score"):
        assert debt[field] == pytest.approx(600 / 19)


@pytest.mark.asyncio
@pytest.mark.parametrize("age_days", [0, 8])
@pytest.mark.parametrize(
    "ordinary,major,expected",
    [
        ((1,), (5,), 10600 / 121),
        ((1,) * 25, (-5,), 600 / 19),
        ((1,) * 25 + (-1,) * 5, (-5,), 600 / 19),
        ((1,) * 25 + (-1,) * 10, (-5,), 300 / 13),
        ((-1,) * 25, (5,), 1300 / 19),
        ((1,) * 9, (), 3100 / 41),
        ((), (3,), 3100 / 41),
    ],
)
async def test_weight_carryover_and_cancellation_follow_the_same_budget_in_each_window(
    runtime, relation_clock, age_days, ordinary, major, expected
):
    occurred_at = (relation_clock.now - timedelta(days=age_days)).isoformat()
    for delta in ordinary + major:
        await runtime.db.apply_relation_change(
            "bot", "group", "user", delta, "权重分配依据", occurred_at=occurred_at
        )
    score = await runtime.db.get_relation_score("bot", "other-group", "user")
    assert score["total_count"] == len(ordinary + major)
    assert score["recent_count"] == (len(ordinary + major) if age_days == 0 else 0)
    for field in ("recent_score", "long_term_score", "interaction_score"):
        assert score[field] == pytest.approx(expected)
    history = await runtime.db.get_relation_changes("bot", "group", "user")
    assert tuple(change["delta"] for change in history) == ordinary + major


@pytest.mark.asyncio
@pytest.mark.parametrize("major_is_old", [True, False])
async def test_each_window_uses_only_its_own_ordinary_and_major_evidence(
    runtime, relation_clock, major_is_old
):
    old_time = (relation_clock.now - timedelta(days=8)).isoformat()
    new_time = relation_clock.now.isoformat()
    regular_time = new_time if major_is_old else old_time
    await runtime.db.apply_relation_change(
        "bot",
        "group",
        "user",
        -5,
        "重要负向事件",
        occurred_at=old_time if major_is_old else new_time,
    )
    for _ in range(25):
        await runtime.db.apply_relation_change(
            "bot", "group", "user", 1, "日常正向互动", occurred_at=regular_time
        )
    before = await runtime.db.get_relation_score("bot", "group", "user")
    assert before["long_term_score"] == pytest.approx(600 / 19)
    assert before["recent_score"] == pytest.approx(85 if major_is_old else 600 / 47)
    await runtime.db.conn.executemany(
        "INSERT INTO relation_changes (bot_name, group_or_user_id, user_id, delta, "
        "reason, occurred_at, created_at) VALUES ('bot', 'group', 'user', 1, ?, ?, ?)",
        [("更多日常正向互动", regular_time, new_time)] * 1000,
    )
    await runtime.db.conn.commit()
    after = await runtime.db.get_relation_score("bot", "group", "user")
    assert after["total_count"] == 1026
    for field in ("recent_score", "long_term_score", "interaction_score"):
        assert after[field] == pytest.approx(before[field])


@pytest.mark.asyncio
async def test_recent_window_excludes_old_major_evidence(runtime, relation_clock):
    old_time = (relation_clock.now - timedelta(days=8)).isoformat()
    for user_id in ("major", "mild"):
        await runtime.db.apply_relation_change(
            "bot", "group", user_id, -5, "先前的重要负向事件", occurred_at=old_time
        )
    await runtime.db.apply_relation_change(
        "bot", "group", "major", 3, "近期重要正向事件"
    )
    for _ in range(9):
        await runtime.db.apply_relation_change(
            "bot", "group", "mild", 1, "近期日常互动"
        )
    major = await runtime.db.get_relation_score("bot", "group", "major")
    mild = await runtime.db.get_relation_score("bot", "group", "mild")
    for field in ("recent_score", "long_term_score", "interaction_score"):
        assert major[field] == pytest.approx(mild[field])
    assert major["recent_score"] == pytest.approx(3100 / 41)
    assert major["long_term_score"] == pytest.approx(750 / 43)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "positive_age,negative_age,expected_recent,expected_interaction",
    [
        (0, 0, 50, 50),
        (8, 8, 50, 50),
        (8, 0, 600 / 47, 1600 / 47),
        (0, 8, 4100 / 47, 3100 / 47),
    ],
)
async def test_major_weights_cancel_before_scoring_within_each_windows_sample_set(
    runtime,
    relation_clock,
    positive_age,
    negative_age,
    expected_recent,
    expected_interaction,
):
    for delta, age in ((5, positive_age), (-5, negative_age)):
        await runtime.db.apply_relation_change(
            "bot",
            "group",
            "user",
            delta,
            "重要评价的原始权重",
            occurred_at=(relation_clock.now - timedelta(days=age)).isoformat(),
        )
    score = await runtime.db.get_relation_score("bot", "group", "user")
    assert score["total_count"] == 2
    assert score["recent_count"] == sum(
        age == 0 for age in (positive_age, negative_age)
    )
    assert score["long_term_score"] == 50
    assert score["recent_score"] == pytest.approx(expected_recent)
    assert score["interaction_score"] == pytest.approx(expected_interaction)


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", [-1, 1])
@pytest.mark.parametrize("age_days", [0, 8])
async def test_major_event_outweighs_repeated_mild_ratings_in_both_windows(
    runtime, relation_clock, direction, age_days
):
    occurred_at = (relation_clock.now - timedelta(days=age_days)).isoformat()
    await runtime.db.apply_relation_change(
        "bot", "group", "major", 5 * direction, "重要事件", occurred_at=occurred_at
    )
    for _ in range(5):
        await runtime.db.apply_relation_change(
            "bot", "group", "mild", direction, "日常互动", occurred_at=occurred_at
        )
    major = await runtime.db.get_relation_score("bot", "group", "major")
    mild = await runtime.db.get_relation_score("bot", "group", "mild")
    assert major["total_count"] == 1
    assert mild["total_count"] == 5
    for field in ("long_term_score", "recent_score", "interaction_score"):
        assert direction * (major[field] - mild[field]) > 0
    assert (await runtime.db.get_relation_changes("bot", "group", "major"))[0][
        "delta"
    ] == 5 * direction
    assert [
        change["delta"]
        for change in await runtime.db.get_relation_changes("bot", "group", "mild")
    ] == [direction] * 5

    for _ in range(10):
        await runtime.db.apply_relation_change(
            "bot",
            "group",
            "major",
            -direction,
            "后续相反方向的日常互动",
            occurred_at=occurred_at,
        )
    mixed = await runtime.db.get_relation_score("bot", "group", "major")
    assert mixed["total_count"] == 11
    for field in ("long_term_score", "recent_score", "interaction_score"):
        assert direction * (mixed[field] - 50) > 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "occurred_at,recent_count",
    [
        ("2026-10-28T04:00:00Z", 1),
        ("2026-10-28T04:00:00+00:00", 1),
        ("2026-10-28T12:00:00+08:00", 1),
        ("2026-10-28T03:59:59Z", 0),
        ("2026-10-28T11:59:59+08:00", 0),
        ("2026-11-04T04:00:00Z", 1),
        ("2026-11-04T04:00:01Z", 0),
        ("unknown", 0),
    ],
)
async def test_recent_window_uses_occurrence_time_offsets_and_exact_seven_day_bounds(
    runtime, relation_clock, occurred_at, recent_count
):
    await runtime.db.apply_relation_change(
        "bot", "group", "user", 1, "延迟归纳的互动", occurred_at=occurred_at
    )
    score = await runtime.db.get_relation_score("bot", "other-group", "user")
    assert score["total_count"] == 1
    assert score["recent_count"] == recent_count
    assert score["long_term_score"] == pytest.approx(3700 / 67)
    assert score["recent_score"] == score["long_term_score"]
    assert score["interaction_score"] == pytest.approx(3700 / 67)


@pytest.mark.asyncio
async def test_legacy_naive_occurrence_times_use_the_servers_local_timezone(
    runtime, relation_clock
):
    cutoff = relation_clock.now - timedelta(days=7)
    for offset in (-1, 0, 1):
        local_time = (cutoff + timedelta(seconds=offset)).astimezone()
        await runtime.db.apply_relation_change(
            "bot",
            "group",
            "user",
            1,
            "旧格式的本地时间",
            occurred_at=local_time.replace(tzinfo=None).isoformat(),
        )
    score = await runtime.db.get_relation_score("bot", "group", "user")
    assert score["total_count"] == 3
    assert score["recent_count"] == 2
    assert score["recent_score"] == pytest.approx(2200 / 37)
    assert score["long_term_score"] == pytest.approx(1700 / 27)


@pytest.mark.asyncio
async def test_sparse_recent_window_uses_its_own_records_and_keeps_id_boundary(
    runtime, relation_clock
):
    for _ in range(20):
        await runtime.db.apply_relation_change(
            "bot",
            "old-group",
            "user",
            5,
            "长期的友好互动",
            occurred_at=(relation_clock.now - timedelta(days=8)).isoformat(),
        )
    established = await runtime.db.get_relation_score("bot", "group", "user")
    assert established["recent_count"] == 0
    assert established["long_term_score"] == pytest.approx(8825 / 89)
    assert established["recent_score"] == established["long_term_score"]
    assert established["interaction_score"] == established["long_term_score"]
    await runtime.db.apply_relation_change("bot", "group", "user", -1, "轻微分歧")
    history = await runtime.db.get_relation_changes("bot", "group", "user")
    boundary = history[-1]["id"]
    score = await runtime.db.get_relation_score("bot", "group", "user")
    assert score["total_count"] == 21
    assert score["recent_count"] == 1
    assert score["long_term_score"] == pytest.approx(352300 / 3553)
    assert score["recent_score"] == pytest.approx(3000 / 67)
    assert score["long_term_score"] > 85
    assert score["interaction_score"] == pytest.approx(96.18948728)
    await runtime.db.apply_relation_change("bot", "user", "user", -5, "后续分歧")
    await runtime.db.apply_relation_change("other-bot", "group", "user", 5, "其他 Bot")
    await runtime.db.apply_relation_change("bot", "group", "other", 5, "其他成员")
    assert (
        await runtime.db.get_relation_score(
            "bot", "another-group", "user", through_id=boundary
        )
        == score
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", [-1, 1])
@pytest.mark.parametrize("deltas", [(1,), (1,) * 20, (2,) * 40, (1, 1, -1), (5,)])
async def test_recent_direction_preserves_established_affinity_without_opposing_evidence(
    runtime, relation_clock, direction, deltas
):
    await runtime.cache.set_user_profile("bot", "group", "user", base_relation=7)
    old_time = (relation_clock.now - timedelta(days=8)).isoformat()
    for _ in range(4):
        await runtime.db.apply_relation_change(
            "bot",
            "old-group",
            "user",
            direction * 5,
            "历史重要事件",
            occurred_at=old_time,
        )
    for delta in deltas:
        await runtime.db.apply_relation_change(
            "bot", "group", "user", direction * delta, "近期互动"
        )
    score = await runtime.db.get_relation_score("bot", "other-group", "user")
    assert score["recent_count"] == len(deltas)
    assert direction * (score["long_term_score"] - score["recent_score"]) > 0
    assert score["interaction_score"] == score["long_term_score"]
    assert score["relation"] == 7 + score["long_term_score"]


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", [-1, 1])
@pytest.mark.parametrize(
    "deltas,expected",
    [
        ((1,), 37060 / 737),
        ((1, 1, -1), 37060 / 737),
        ((5, -5, 1), 37060 / 737),
        ((1,) * 5, 1020 / 19),
        ((1,) * 10, 755 / 13),
        ((1,) * 20, 64),
        ((5,), 3100 / 47),
        ((5, -3), 34670 / 559),
        ((1,) * 25 + (-5,), 880 / 19),
    ],
)
async def test_recent_correction_strength_uses_cancelled_and_capped_net_weight(
    runtime, relation_clock, direction, deltas, expected
):
    old_time = (relation_clock.now - timedelta(days=8)).isoformat()
    for delta in deltas:
        await runtime.db.apply_relation_change(
            "bot",
            "old-group",
            "user",
            -direction * delta,
            "更早的反向评价",
            occurred_at=old_time,
        )
        await runtime.db.apply_relation_change(
            "bot", "group", "user", direction * delta, "近期评价"
        )
    score = await runtime.db.get_relation_score("bot", "group", "user")
    assert score["recent_count"] == len(deltas)
    assert score["total_count"] == 2 * len(deltas)
    assert score["long_term_score"] == 50
    assert score["interaction_score"] == pytest.approx(50 + direction * (expected - 50))


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", [-1, 1])
@pytest.mark.parametrize("deltas", [(5, -5), (2, -2), (1,) * 9 + (-3,)])
async def test_zero_recent_net_keeps_long_term_score_with_nonempty_recent_history(
    runtime, relation_clock, direction, deltas
):
    old_time = (relation_clock.now - timedelta(days=8)).isoformat()
    for _ in range(4):
        await runtime.db.apply_relation_change(
            "bot", "group", "user", direction * 5, "历史重要事件", occurred_at=old_time
        )
    for delta in deltas:
        await runtime.db.apply_relation_change(
            "bot", "group", "user", direction * delta, "近期正负抵消"
        )
    score = await runtime.db.get_relation_score("bot", "group", "user")
    assert score["recent_count"] == len(deltas)
    assert score["recent_score"] == 50
    assert score["long_term_score"] != 50
    assert score["interaction_score"] == score["long_term_score"]


@pytest.mark.asyncio
async def test_ordinary_overflow_cannot_increase_the_strength_of_recent_major_debt(
    runtime, relation_clock
):
    old_time = (relation_clock.now - timedelta(days=8)).isoformat()
    for _ in range(4):
        await runtime.db.apply_relation_change(
            "bot", "group", "user", 5, "历史重要正向事件", occurred_at=old_time
        )
    for delta in (1,) * 25 + (-5,):
        await runtime.db.apply_relation_change(
            "bot", "group", "user", delta, "近期评价"
        )
    before = await runtime.db.get_relation_score("bot", "group", "user")
    assert before["recent_score"] == pytest.approx(600 / 19)
    assert before["interaction_score"] == pytest.approx(45736 / 551)
    assert before["interaction_score"] < before["long_term_score"]
    now = relation_clock.now.isoformat()
    await runtime.db.conn.executemany(
        "INSERT INTO relation_changes (bot_name, group_or_user_id, user_id, delta, "
        "reason, occurred_at, created_at) VALUES ('bot', 'group', 'user', 2, ?, ?, ?)",
        [("溢出的日常正向互动", now, now)] * 1000,
    )
    await runtime.db.conn.commit()
    after = await runtime.db.get_relation_score("bot", "group", "user")
    assert after["recent_count"] == 1026
    for field in ("recent_score", "long_term_score", "interaction_score", "relation"):
        assert after[field] == before[field]


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", [-1, 1])
async def test_expiring_major_evidence_does_not_erase_established_relationship(
    runtime, relation_clock, direction
):
    await runtime.cache.update_relation(
        "bot",
        "group",
        "user",
        direction * 5,
        "重要事件",
        occurred_at=(relation_clock.now - timedelta(days=7, seconds=-1)).isoformat(),
    )
    await runtime.cache.update_relation("bot", "group", "user", direction, "日常互动")
    before = await runtime.cache.get_user_profile_record("bot", "group", "user")
    assert before["recent_count"] == 2
    assert before["recent_score"] == before["long_term_score"]
    relation_clock.now += timedelta(seconds=2)
    after = await runtime.cache.get_user_profile_record("bot", "other-group", "user")
    assert after["recent_count"] == 1
    assert after["recent_score"] != before["recent_score"]
    assert after["long_term_score"] == before["long_term_score"]
    assert after["relation"] == before["relation"]


@pytest.mark.asyncio
async def test_cached_scores_expire_without_new_interactions_and_keep_long_term_relation(
    runtime, relation_clock
):
    await runtime.cache.set_user_profile(
        "bot", "group", "user", base_relation=7, title="朋友"
    )
    for delta, age in [(5, timedelta(days=8)), (-5, timedelta(days=7, seconds=-1))]:
        await runtime.cache.update_relation(
            "bot",
            "group",
            "user",
            delta,
            "先前互动",
            occurred_at=(relation_clock.now - age).isoformat(),
        )
    before = await runtime.cache.get_user_profile_record("bot", "group", "user")
    assert before["recent_count"] == 1
    assert before["relation"] == pytest.approx(1929 / 47)
    assert await runtime.cache.get_user_relation("bot", "other-group", "user") == (
        pytest.approx(1929 / 47),
        "朋友",
    )
    relation_clock.now += timedelta(seconds=2)
    assert await runtime.cache.get_user_relation("bot", "group", "user") == (57, "朋友")
    for group in ("group", "other-group", "user"):
        after = await runtime.cache.get_user_profile_record("bot", group, "user")
        assert after["recent_count"] == 0
        assert after["total_count"] == 2
        assert after["recent_score"] == after["long_term_score"] == 50
        assert after["relation"] == 57


@pytest.mark.asyncio
async def test_new_member_starts_neutral_without_creating_a_profile(runtime):
    assert await runtime.db.get_user_profile_record("bot", "group", "new") is None
    assert await runtime.cache.get_user_relation("bot", "group", "new") == (50, "")
    assert await runtime.db.get_user_profile_record("bot", "group", "new") is None


@pytest.mark.asyncio
async def test_active_member_with_zero_final_affinity_is_kept(runtime):
    await runtime.cache.set_user_profile("bot", "group", "user", base_relation=-50)
    briefs = await runtime.cache.build_active_user_briefs(
        "bot", "group", [MessageData(user_id="user", content="hello")]
    )
    assert len(briefs) == 1
    assert briefs[0]["relation"] == 0
    assert briefs[0]["base_relation"] == -50
    assert briefs[0]["interaction_score"] == 50


@pytest.mark.asyncio
async def test_base_edits_refresh_all_scores_without_changing_history(runtime):
    await runtime.cache.update_relation("bot", "group", "user", 2, "认真帮助")
    before = await runtime.cache.get_user_profile_record("bot", "group", "user")
    history = await runtime.db.get_relation_changes("bot", "group", "user")
    for base in (200, -200, 0):
        await runtime.cache.set_user_profile("bot", "group", "user", base_relation=base)
        profile = await runtime.cache.get_user_profile_record("bot", "group", "user")
        assert profile["base_relation"] == base
        assert profile["interaction_score"] == before["interaction_score"]
        assert profile["relation"] == pytest.approx(base + 725 / 11)
        assert (await runtime.cache.get_user_relation("bot", "group", "user"))[
            0
        ] == profile["relation"]
        assert await runtime.db.get_relation_changes("bot", "group", "user") == history
    await runtime.cache.set_user_profile("bot", "group", "user", base_relation=17)
    await runtime.cache.set_user_profile(
        "bot", "group", "user", profile_fields={"call_name": "小明"}
    )
    assert (await runtime.db.get_relation_score("bot", "group", "user"))[
        "base_relation"
    ] == 17


@pytest.mark.asyncio
async def test_eleventh_positive_rating_keeps_other_ratings_from_the_same_week(
    runtime,
):
    for delta in [5] + [1] * 9:
        await runtime.cache.update_relation("bot", "group", "user", delta, "先前互动")
    before = (await runtime.db.get_relation_score("bot", "group", "user"))["relation"]
    await runtime.cache.update_relation("bot", "group", "user", 1, "本次友好互动")
    last = (await runtime.db.get_relation_changes("bot", "group", "user"))[-1]
    assert last["delta"] == 1
    after = (await runtime.db.get_relation_score("bot", "group", "user"))["relation"]
    assert after > before
    assert (await runtime.db.get_relation_score("bot", "group", "user"))[
        "recent_count"
    ] == 11


@pytest.mark.asyncio
@pytest.mark.parametrize("delta", [0, 6, -6, True, 1.5])
async def test_invalid_ratings_never_create_history(runtime, delta):
    with pytest.raises(ValueError):
        await runtime.db.apply_relation_change("bot", "group", "user", delta, "互动")
    assert await runtime.db.get_relation_changes("bot", "group", "user") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("deltas", "expected_calls"),
    [((1, -1, 1, -1), 0), ((1, -1, 1, -1, 1), 1), ((4,), 1), ((-4,), 1), ((3,), 0)],
)
async def test_tone_review_counts_changes_in_both_directions_and_reviews_major_events(
    runtime, deltas, expected_calls
):
    for delta in deltas:
        await runtime.cache.update_relation("bot", "group", "user", delta, "具体互动")
    score_before = await runtime.db.get_relation("bot", "group", "user")
    await runtime.manager._run_attitude_summary_task("bot", "group", "Giftia", "self")
    assert runtime.manager._call_summary_llm.await_count == expected_calls
    assert await runtime.db.get_relation("bot", "group", "user") == score_before
    if expected_calls:
        assert await runtime.db.get_pending_attitude_evaluations("bot", "group") == []
        await runtime.manager._run_attitude_summary_task(
            "bot", "group", "Giftia", "self"
        )
        assert runtime.manager._call_summary_llm.await_count == 1


@pytest.mark.asyncio
async def test_review_sees_fifty_reasons_new_markers_and_only_related_direct_messages(
    runtime,
):
    await runtime.cache.set_user_profile(
        "bot", "group", "user", profile_fields={"attitude": "礼貌交流"}
    )
    for index in range(60):
        await runtime.cache.update_relation(
            "bot", "group", "user", 1, f"互动理由 {index}"
        )
    changes = await runtime.db.get_relation_changes("bot", "group", "user")
    assert await runtime.db.save_attitude_evaluation(
        "bot", "group", "user", 0, changes[-6]["id"]
    )
    await runtime.cache.get_user_profile_record("bot", "group", "user")
    runtime.manager._call_summary_llm.return_value = '{"attitude": "熟络，常互相接梗"}'
    await runtime.manager._run_attitude_summary_task(
        "bot",
        "group",
        "Giftia",
        "self",
        {
            "alias_observation_messages": [
                MessageData(user_id="user", content="共同笑话"),
                MessageData(user_id="self", content="接梗回应", role="assistant"),
                MessageData(user_id="other", content="不相关发言"),
                MessageData(user_id="user", content="已撤回", is_recalled=1),
                MessageData(user_id="self", content="操作日志", role="operation_log"),
            ]
        },
    )
    payload = json.loads(runtime.manager._call_summary_llm.call_args.args[2])
    assert len(payload["relation_changes"]) == 50
    assert payload["relation_changes"][0]["reason"] == "互动理由 10"
    assert [item["is_new"] for item in payload["relation_changes"]] == [False] * 45 + [
        True
    ] * 5
    expected_relation = (await runtime.db.get_relation_score("bot", "group", "user"))[
        "relation"
    ]
    assert payload["current_relation"] == expected_relation
    assert payload["current_attitude"] == "礼貌交流"
    assert [item["content"] for item in payload["related_chat_history"]] == [
        "共同笑话",
        "接梗回应",
    ]
    profile = await runtime.cache.get_user_profile_record("bot", "group", "user")
    assert profile["attitude"] == "熟络，常互相接梗"
    assert profile["relation"] == expected_relation


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wrapper",
    [
        "```json\n{}\n```",
        "```\n{}\n```",
        "```json {} ```",
        "评估结果：\n```json\n{}\n```",
    ],
)
@pytest.mark.parametrize("attitude", ["欢喜冤家，日常互怼拌嘴，偶尔温馨互动", None])
async def test_markdown_review_updates_attitude_and_consumes_pending_history(
    runtime, wrapper, attitude
):
    await runtime.cache.set_user_profile(
        "bot", "group", "user", profile_fields={"attitude": "礼貌交流"}
    )
    for _ in range(5):
        await runtime.cache.update_relation("bot", "group", "user", 1, "日常互动")
    profile_before = await runtime.cache.get_user_profile_record("bot", "group", "user")
    changes_before = await runtime.db.get_relation_changes("bot", "group", "user")
    runtime.manager._call_summary_llm.return_value = wrapper.format(
        json.dumps({"attitude": attitude}, ensure_ascii=False)
    )

    await runtime.manager._run_attitude_summary_task("bot", "group", "Giftia", "self")

    profile = await runtime.cache.get_user_profile_record("bot", "group", "user")
    assert profile["attitude"] == (attitude or "礼貌交流")
    assert profile["relation"] == profile_before["relation"]
    assert (
        await runtime.db.get_relation_changes("bot", "group", "user") == changes_before
    )
    assert await runtime.db.get_pending_attitude_evaluations("bot", "group") == []
    await runtime.manager._run_attitude_summary_task("bot", "group", "Giftia", "self")
    assert runtime.manager._call_summary_llm.await_count == 1


@pytest.mark.asyncio
async def test_large_backlog_is_reviewed_in_bounded_windows_without_skipping_changes(
    runtime,
):
    for index in range(55):
        await runtime.cache.update_relation("bot", "group", "user", 1, f"互动 {index}")
    await runtime.manager._run_attitude_summary_task("bot", "group", "Giftia", "self")
    calls = runtime.manager._call_summary_llm.call_args_list
    assert len(calls) == 2
    payloads = [json.loads(call.args[2]) for call in calls]
    assert [len(item["relation_changes"]) for item in payloads] == [50, 50]
    reviewed = [
        change["id"]
        for item in payloads
        for change in item["relation_changes"]
        if change["is_new"]
    ]
    assert reviewed == list(range(1, 56))
    assert await runtime.db.get_pending_attitude_evaluations("bot", "group") == []


@pytest.mark.asyncio
async def test_failed_small_backlog_tail_remains_eligible_for_periodic_retry(runtime):
    await runtime.manager._remember_session("bot", "group", "self")
    for index in range(51):
        await runtime.cache.update_relation("bot", "group", "user", 1, f"互动 {index}")
    runtime.manager._call_summary_llm.side_effect = [
        '{"attitude": null}',
        None,
        '{"attitude": null}',
    ]
    await runtime.manager._run_attitude_summary_task("bot", "group", "Giftia", "self")
    pending = await runtime.db.get_pending_attitude_evaluations("bot", "group")
    assert pending[0]["evaluated_id"] == 46
    assert pending[0]["change_count"] == 5
    await runtime.manager.maintain_memories()
    assert await runtime.db.get_pending_attitude_evaluations("bot", "group") == []
    assert runtime.manager._call_summary_llm.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_response",
    [
        None,
        "bad json",
        "[]",
        "{}",
        '{"attitude": 1}',
        '{"attitude": ""}',
        '{"attitude": "无"}',
        json.dumps({"attitude": "长" * 21}),
        '{"attitude": "友好", "relation": 99}',
        '```json\n{"attitude": 1}\n```',
        '```json\n{"attitude": "友好", "relation": 99}\n```',
        '```json\n{"attitude": "长长长长长长长长长长长长长长长长长长长长长"}\n```',
    ],
)
async def test_invalid_review_preserves_cursor_and_periodic_retry_needs_no_new_messages(
    runtime, invalid_response
):
    await runtime.manager._remember_session("bot", "group", "self")
    await runtime.cache.set_user_profile(
        "bot", "group", "user", profile_fields={"attitude": "礼貌交流"}
    )
    for _ in range(5):
        await runtime.cache.update_relation(
            "bot", "group", "user", 1, "他分享近况，我认真回应"
        )
    runtime.manager._call_summary_llm.side_effect = [
        invalid_response,
        '{"attitude": null}',
    ]
    await runtime.manager._run_attitude_summary_task("bot", "group", "Giftia", "self")
    pending = await runtime.db.get_pending_attitude_evaluations("bot", "group")
    assert pending[0]["evaluated_id"] == 0
    await runtime.manager.maintain_memories()
    assert runtime.manager._call_summary_llm.await_count == 2
    assert await runtime.db.get_pending_attitude_evaluations("bot", "group") == []
    profile = await runtime.db.get_user_profile_record("bot", "group", "user")
    assert profile["attitude"] == "礼貌交流"
    assert profile["relation"] == pytest.approx(1300 / 19)
    reopened = await Database.connect()
    try:
        assert len(await reopened.get_relation_changes("bot", "group", "user")) == 5
        assert await reopened.get_pending_attitude_evaluations("bot", "group") == []
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_changes_arriving_during_inference_stay_pending(runtime):
    for _ in range(5):
        await runtime.cache.update_relation("bot", "group", "user", 1, "日常互动")

    async def arrive_during_review(*args, **kwargs):
        await runtime.cache.update_relation(
            "bot", "group", "user", -4, "发生新的明显冲突"
        )
        return '{"attitude": null}'

    runtime.manager._call_summary_llm.side_effect = arrive_during_review
    await runtime.manager._run_attitude_summary_task("bot", "group", "Giftia", "self")
    pending = await runtime.db.get_pending_attitude_evaluations("bot", "group")
    assert (pending[0]["evaluated_id"], pending[0]["through_id"]) == (5, 6)
    runtime.manager._call_summary_llm.side_effect = None
    await runtime.manager._run_attitude_summary_task("bot", "group", "Giftia", "self")
    assert await runtime.db.get_pending_attitude_evaluations("bot", "group") == []


@pytest.mark.asyncio
async def test_manual_history_edit_rejects_inflight_tone_and_recalculates_review_score(
    runtime,
):
    await runtime.cache.set_user_profile(
        "bot", "group", "user", profile_fields={"attitude": "礼貌交流"}
    )
    for _ in range(5):
        await runtime.cache.update_relation("bot", "group", "user", 1, "原评价")
    changes = await runtime.db.get_relation_changes("bot", "group", "user")

    async def edit_during_review(*args, **kwargs):
        await runtime.db.mutate_relation_history(
            "bot",
            "group",
            "user",
            action="update",
            expected_revision=0,
            change_id=changes[0]["id"],
            delta=-5,
            reason="修正为严重冒犯",
        )
        return '{"attitude": "过期结论"}'

    runtime.manager._call_summary_llm.side_effect = edit_during_review
    await runtime.manager._run_attitude_summary_task("bot", "group", "Giftia", "self")
    assert (await runtime.db.get_user_profile_record("bot", "group", "user"))[
        "attitude"
    ] == "礼貌交流"
    pending = await runtime.db.get_pending_attitude_evaluations("bot", "group")
    assert pending[0]["evaluated_id"] == 0
    assert pending[0]["history_revision"] == 1
    runtime.manager._call_summary_llm.side_effect = None
    runtime.manager._call_summary_llm.return_value = '{"attitude": null}'
    await runtime.manager._run_attitude_summary_task("bot", "group", "Giftia", "self")
    payload = json.loads(runtime.manager._call_summary_llm.call_args.args[2])
    assert payload["current_relation"] == pytest.approx(1000 / 69)
    assert payload["current_relation"] < 50
    assert all(
        "relation_before" not in row and "relation_after" not in row
        for row in payload["relation_changes"]
    )
    assert await runtime.db.get_pending_attitude_evaluations("bot", "group") == []


@pytest.mark.asyncio
async def test_deleting_and_recreating_profile_during_review_rejects_stale_result(
    runtime,
):
    await runtime.cache.update_relation("bot", "group", "user", 4, "重要互动")

    async def replace_during_review(*args, **kwargs):
        await runtime.db.delete_user_profile("bot", "group", "user")
        await runtime.cache.update_relation("bot", "group", "user", 1, "重新开始互动")
        return '{"attitude": "过期结论"}'

    runtime.manager._call_summary_llm.side_effect = replace_during_review
    await runtime.manager._run_attitude_summary_task("bot", "group", "Giftia", "self")
    profile = await runtime.db.get_user_profile_record("bot", "group", "user")
    assert profile["attitude"] is None
    assert profile["relation"] == pytest.approx(3700 / 67)
    changes = await runtime.db.get_relation_changes("bot", "group", "user")
    assert [item["reason"] for item in changes] == ["重新开始互动"]
    async with runtime.db.conn.execute(
        "SELECT attitude_evaluated_change_id FROM user_profiles"
    ) as cursor:
        assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    ["delete_user_profile", "delete_group_user_profiles", "delete_all_relations"],
)
async def test_profile_and_relation_cleanup_removes_only_scoped_history(
    runtime, operation
):
    scopes = [
        ("bot", "group", "user"),
        ("other", "group", "user"),
        ("bot", "other", "user"),
    ]
    for scope in scopes:
        await runtime.cache.update_relation(*scope, 4, "互动依据")
    args = scopes[0] if operation == "delete_user_profile" else scopes[0][:2]
    await getattr(runtime.db, operation)(*args)
    expected_count = 0 if operation == "delete_user_profile" else 1
    remaining = await runtime.db.get_relation_changes(*scopes[0])
    assert len(remaining) == expected_count
    if remaining:
        assert remaining[0]["group_or_user_id"] == "other"
    assert (await runtime.db.get_relation_score(*scopes[0]))[
        "total_count"
    ] == expected_count
    assert len(await runtime.db.get_relation_changes(*scopes[1])) == 1
    assert (await runtime.db.get_relation_score(*scopes[2]))[
        "total_count"
    ] == expected_count


@pytest.mark.parametrize("score", [0, 3])
def test_only_reply_speaker_profile_shows_three_escaped_change_reasons(score):
    changes = [
        {"delta": delta, "reason": reason, "occurred_at": "2026-10-18T12:00:00+08:00"}
        for delta, reason in [
            (1, "最早记录应省略"),
            (2, "他主动道谢"),
            (-1, "双方发生分歧"),
            (1, "接梗 <玩笑> & 回应"),
        ]
    ]
    stats = {
        "total_count": 50,
        "base_relation": 0,
        "interaction_score": score,
        "recent_score": 70,
        "long_term_score": 72,
        "relation": score,
        "recent_count": 10,
    }
    profile = {
        "attitude": "熟络",
        "relation_changes": changes,
        "relation_score": stats,
    }
    brief = {
        "user_id": "other",
        "relation": 5,
        "relation_changes": changes,
        "relation_score": stats,
    }
    reply = build_reply_prompt(
        [],
        [],
        None,
        user_id="user",
        user_profile=profile,
        user_relation=(score, ""),
        active_user_briefs=[brief],
        relation_changes=changes,
        relation_score=stats,
    )
    root = fromstring("<context>" + reply + "</context>")
    text = root.find("user_profile").text
    assert "最近互动评价（已参与计分" in text
    assert "最早记录应省略" not in reply
    assert "+2：他主动道谢" in text
    assert "-1：双方发生分歧" in text
    assert "+1：接梗 <玩笑> & 回应" in text
    assert "近期分：70.0（近7天10次）" in text
    assert "长期分：72.0（共50次）" in text
    assert "好感变化" not in root.find("active_user_briefs/user").text
    assert "正向变化占比" not in root.find("active_user_briefs/user").text
    decision = build_decision_prompt(
        "user",
        "",
        [],
        [],
        None,
        user_profile=profile,
        user_relation=(score, ""),
        active_user_briefs=[brief],
    )
    assert "好感变化" not in decision
    assert "正向变化占比" not in decision
    assert "他主动道谢" not in decision


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "review_response", [None, '{"attitude": "熟络，认真回应彼此"}']
)
async def test_background_stages_persist_reason_and_keep_review_failure_retryable(
    runtime, review_response
):
    source = MessageData(
        user_id="user",
        nickname="User",
        group_or_user_id="group",
        message_id="source",
        content="重要约定已经完成，谢谢你之前帮忙",
        time="2026-10-18T12:00:00+08:00",
    )
    await runtime.db.insert_message("bot", source)
    await runtime.manager.observe_messages("bot", "group", "self", [source])
    first_id, last_id, _ = await runtime.db.chat_history_repo.get_pending_memory_stats(
        "bot", "group"
    )
    runtime.manager._call_summary_llm.side_effect = [
        '{"memories": []}',
        '<summary_user_profile user_id="user"><update_relation delta="+4">'
        "他完成重要约定并认真道谢</update_relation></summary_user_profile>",
        review_response,
    ]
    assert await runtime.manager._run_background_summarize(
        "bot", "group", "self", first_id, last_id
    )
    assert [
        call.args[0] for call in runtime.manager._call_summary_llm.call_args_list
    ] == ["原子记忆归纳", "好感度维护", "互动态度维护"]
    assert await runtime.db.chat_history_repo.get_pending_memory_stats(
        "bot", "group"
    ) == (0, 0, 0)
    history = await runtime.db.get_relation_changes("bot", "group", "user")
    assert len(history) == 1
    assert history[0]["reason"] == "他完成重要约定并认真道谢"
    assert (await runtime.db.get_relation("bot", "group", "user"))[0] == pytest.approx(
        3550 / 43
    )
    if review_response is None:
        assert (
            len(await runtime.db.get_pending_attitude_evaluations("bot", "group")) == 1
        )
        runtime.manager._call_summary_llm.side_effect = None
        runtime.manager._call_summary_llm.return_value = '{"attitude": null}'
        await runtime.manager.maintain_memories()
    assert await runtime.db.get_pending_attitude_evaluations("bot", "group") == []
