import asyncio
import json
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
    assert (
        changes[0]["relation_before"],
        changes[0]["relation_after"],
    ) == pytest.approx((60, 10 + 700 / 12))
    assert changes[0]["reason"] == "他认真感谢了我的解释"
    assert json.loads(changes[0]["source_ids"]) == [7]
    assert changes[0]["occurred_at"] == source.time
    profile = await runtime.db.get_user_profile_record("bot", "group", "user")
    assert profile["attitude"] == "礼貌交流"
    assert profile["relation"] == pytest.approx(10 + 700 / 12)


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
            "SELECT relation, attitude, attitude_evaluated_change_id FROM user_profiles"
        ) as cursor:
            assert tuple(await cursor.fetchone()) == (23, "熟络", 0)
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
    assert runtime.cache.user_profile_records["bot:group:user"]["relation"] == 60


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
    ] == pytest.approx(800 / 13)
    history = await runtime.db.get_relation_changes("bot", "group", "user")
    assert history[0]["relation_after"] == history[1]["relation_before"]
    score = await runtime.db.get_relation_score("bot", "group", "user")
    assert score["total_count"] == 2
    assert score["recent_count"] == 2
    assert score["interaction_score"] == pytest.approx(800 / 13)

    for scope in (
        ("other", "group", "user"),
        ("bot", "other", "user"),
        ("bot", "group", "other"),
    ):
        assert await runtime.cache.update_relation(*scope, 1, "独立会话互动", [1])
        assert len(await runtime.db.get_relation_changes(*scope)) == 1
        assert (await runtime.db.get_relation_score(*scope))["total_count"] == 1
    assert (await runtime.db.get_relation_score("bot", "group", "user"))[
        "total_count"
    ] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "deltas,recent,long_term,interaction",
    [
        ((), 50, 50, 50),
        ((1,), 600 / 11, 600 / 11, 600 / 11),
        ((-5,), 100 / 3, 100 / 3, 100 / 3),
        ((1,) * 10, 75, 75, 75),
        ((1,) * 100, 75, 1050 / 11, 915 / 11),
        ((1,) * 100 + (-1,) * 10, 25, 87.5, 50),
        ((1, 1, -5), 700 / 17, 700 / 17, 700 / 17),
        ((5, -1), 62.5, 62.5, 62.5),
    ],
)
async def test_scores_weight_magnitude_smooth_sparse_samples_and_use_all_history(
    runtime, deltas, recent, long_term, interaction
):
    await runtime.cache.set_user_profile("bot", "group", "user", base_relation=-20)
    for delta in deltas:
        await runtime.cache.update_relation("bot", "group", "user", delta, "实际互动")
    score = await runtime.db.get_relation_score("bot", "group", "user")
    assert score["recent_count"] == min(10, len(deltas))
    assert score["total_count"] == len(deltas)
    assert score["base_relation"] == -20
    assert score["recent_score"] == pytest.approx(recent)
    assert score["long_term_score"] == pytest.approx(long_term)
    assert score["interaction_score"] == pytest.approx(interaction)
    assert score["relation"] == pytest.approx(interaction - 20)
    assert (await runtime.cache.get_user_relation("bot", "group", "user"))[
        0
    ] == pytest.approx(interaction - 20)
    prompt = build_reply_prompt([], [], None, user_id="user", relation_score=score)
    assert f"互动分：{interaction:.1f}" in prompt
    assert f"最近{min(10, len(deltas))}次" in prompt
    assert f"共{len(deltas)}次" in prompt
    assert "正向变化占比" not in prompt


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
        assert profile["relation"] == pytest.approx(base + 700 / 12)
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
async def test_positive_rating_can_lower_score_when_a_stronger_rating_leaves_window(
    runtime,
):
    for delta in [5] + [1] * 9:
        await runtime.cache.update_relation("bot", "group", "user", delta, "先前互动")
    await runtime.cache.update_relation("bot", "group", "user", 1, "本次友好互动")
    last = (await runtime.db.get_relation_changes("bot", "group", "user"))[-1]
    assert last["delta"] == 1
    assert last["relation_after"] < last["relation_before"]
    assert last["relation_after"] == pytest.approx(77)


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
    assert payload["current_relation"] == changes[-1]["relation_after"]
    assert payload["current_attitude"] == "礼貌交流"
    assert [item["content"] for item in payload["related_chat_history"]] == [
        "共同笑话",
        "接梗回应",
    ]
    profile = await runtime.cache.get_user_profile_record("bot", "group", "user")
    assert profile["attitude"] == "熟络，常互相接梗"
    assert profile["relation"] == changes[-1]["relation_after"]


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
    assert profile["relation"] == pytest.approx(200 / 3)
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
    assert profile["relation"] == pytest.approx(600 / 11)
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
    assert await runtime.db.get_relation_changes(*scopes[0]) == []
    assert (await runtime.db.get_relation_score(*scopes[0]))["total_count"] == 0
    for scope in scopes[1:]:
        assert len(await runtime.db.get_relation_changes(*scope)) == 1
        assert (await runtime.db.get_relation_score(*scope))["total_count"] == 1


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
    assert "近期分：70.0（最近10次）" in text
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
        900 / 14
    )
    if review_response is None:
        assert (
            len(await runtime.db.get_pending_attitude_evaluations("bot", "group")) == 1
        )
        runtime.manager._call_summary_llm.side_effect = None
        runtime.manager._call_summary_llm.return_value = '{"attitude": null}'
        await runtime.manager.maintain_memories()
    assert await runtime.db.get_pending_attitude_evaluations("bot", "group") == []
