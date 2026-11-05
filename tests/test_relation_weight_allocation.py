import pytest
from core.database.relation_history_store import _allocate_relation_weight


@pytest.mark.parametrize(
    "weights,expected",
    [
        ((1, 0, 0, 0, 25), 26),
        ((25, 0, 0, 0, -25), -85 / 7),
        ((25 - 5, 0, 0, 0, -25), -85 / 7),
        ((25 - 10, 0, 0, 0, -25), -85 / 7),
        ((25 - 15, 0, 0, 0, -25), -15),
        ((-25, 0, 0, 0, 25), 85 / 7),
        ((25, 0, 0, 0, 0), 90 / 7),
        ((-25, 0, 0, 0, 0), -90 / 7),
        ((0, 0, 9, 0, 0), 9),
        ((9, 0, 0, 0, 0), 9),
        ((0, 0, 9, 0, -25), -16),
        ((0, 24, 0, 0, -25), -5),
        ((0, 100, 0, -16, 0), 4),
        ((0, 0, 900, 0, -25), 65 / 7),
        ((0, 0, 900, -16, 0), 128 / 7),
        ((0, 0, 0, 800, -100), -160 / 7),
        ((0, 0, 0, -800, 100), 160 / 7),
        ((25, 100, 900, 800, 25), 715 / 7),
    ],
)
def test_each_net_tier_is_limited_before_adding_stronger_weight(weights, expected):
    assert _allocate_relation_weight(weights) == pytest.approx(expected)


def test_major_weight_fills_the_regular_range_once_before_carrying_over():
    weight = _allocate_relation_weight((1, 0, 0, 0, 25))
    assert min(weight, 20) == 20
    assert weight - 20 == 6


@pytest.mark.parametrize("overflow", [1000, 1000000])
@pytest.mark.parametrize(
    "tier,limit", [(0, 90 / 7), (1, 20), (2, 240 / 7), (3, 540 / 7)]
)
@pytest.mark.parametrize("direction", [-1, 1])
def test_lower_tier_overflow_cannot_pay_stronger_negative_debt(
    overflow, tier, limit, direction
):
    weights = [0, 0, 0, 0, -100 * direction]
    weights[tier] = overflow * direction
    assert _allocate_relation_weight(tuple(weights)) == pytest.approx(
        direction * (limit - 100)
    )
