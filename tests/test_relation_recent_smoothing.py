import pytest
from core.database.relation_history_store import _smooth_recent_relation_score


@pytest.mark.parametrize("long_term", [5, 50, 95])
def test_empty_recent_history_keeps_the_established_score(long_term):
    assert _smooth_recent_relation_score(long_term, ()) == long_term


def test_latest_negative_is_effective_while_the_recent_net_is_positive():
    latest_negative = _smooth_recent_relation_score(80, (1,) * 9 + (-1,))
    older_negative = _smooth_recent_relation_score(80, (-1,) + (1,) * 9)
    assert latest_negative == pytest.approx(862 / 11)
    assert latest_negative < older_negative < 80


def test_positive_recovery_does_not_increase_the_negative_correction():
    negative = _smooth_recent_relation_score(95, (-1,))
    recovery = _smooth_recent_relation_score(95, (-1, 1))
    assert negative == pytest.approx(11450 / 121)
    assert negative < recovery < 95


@pytest.mark.parametrize("long_term,direction", [(95, 1), (5, -1)])
def test_routine_ratings_do_not_erase_established_high_or_low_affinity(
    long_term, direction
):
    assert (
        _smooth_recent_relation_score(long_term, (direction, 2 * direction) * 5)
        == long_term
    )


@pytest.mark.parametrize("long_term", [5, 50, 95])
def test_each_rating_moves_the_replayed_score_only_in_its_own_direction(long_term):
    deltas = (-5, 1, -1, 2, -2, 3, -3, 4, -4, 5)
    previous = long_term
    for index, delta in enumerate(deltas, start=1):
        current = _smooth_recent_relation_score(long_term, deltas[:index])
        assert delta * (current - previous) >= 0
        previous = current


def test_low_tier_overflow_does_not_repair_a_stronger_negative_event():
    effective = _smooth_recent_relation_score(95, (-5,) + (2,) * 5)
    overflow = _smooth_recent_relation_score(95, (-5,) + (2,) * 9)
    assert effective == pytest.approx(73783460 / 823543)
    assert overflow == effective
    assert overflow < 95


def test_new_positive_ratings_keep_recovery_budget_after_older_positive_overflow():
    before = _smooth_recent_relation_score(85, (2,) * 8 + (-1,))
    after = _smooth_recent_relation_score(85, (2,) * 8 + (-1, 2))
    assert before < after < 85


def test_weaker_ratings_cannot_take_budget_from_stronger_evidence():
    strong = _smooth_recent_relation_score(50, (4,) * 5)
    with_weak = _smooth_recent_relation_score(50, (4,) * 5 + (1,))
    assert with_weak == strong


@pytest.mark.parametrize("direction", [-1, 1])
def test_budget_redistribution_does_not_reverse_the_newest_rating(direction):
    deltas = tuple(direction * delta for delta in (-3, -3, -4, 5, -4, -3, -5, -3, -5))
    before = _smooth_recent_relation_score(50, deltas)
    after = _smooth_recent_relation_score(50, deltas + (-3 * direction,))
    assert after == before


def test_one_important_rating_has_more_influence_than_five_mild_ratings():
    important = _smooth_recent_relation_score(50, (5,))
    mild = _smooth_recent_relation_score(50, (1,) * 5)
    assert important == pytest.approx(3200 / 49)
    assert important > mild > 50


@pytest.mark.parametrize("long_term", [5, 50, 95])
@pytest.mark.parametrize("deltas", [(1,) * 10, (-1,) * 10, (1, -1) * 5])
def test_ten_effective_units_limit_recent_influence_to_thirty_percent(
    long_term, deltas
):
    score = _smooth_recent_relation_score(long_term, deltas)
    assert 0.7 * long_term <= score <= 0.7 * long_term + 30


@pytest.mark.parametrize("deltas", [(1,) * 9 + (-1,), (-5,) + (2,) * 9, (5, -3, 1)])
def test_recent_correction_is_symmetric_in_both_directions(deltas):
    positive = _smooth_recent_relation_score(80, deltas)
    negative = _smooth_recent_relation_score(20, tuple(-delta for delta in deltas))
    assert positive + negative == pytest.approx(100)
