import pytest
from core.database.relation_history_store import _allocate_relation_weight


@pytest.mark.parametrize(
    "regular,major,expected",
    [
        (1, 25, 26),
        (25, -25, -5),
        (25 - 5, -25, -5),
        (25 - 10, -25, -10),
        (-25, 25, 5),
        (25, 0, 20),
        (-25, 0, -20),
        (0, 9, 9),
        (9, 0, 9),
    ],
)
def test_ordinary_net_is_limited_before_combining_major_weight(
    regular, major, expected
):
    assert _allocate_relation_weight(regular, major) == expected


def test_major_weight_fills_the_regular_range_once_before_carrying_over():
    weight = _allocate_relation_weight(1, 25)
    assert min(weight, 20) == 20
    assert weight - 20 == 6


@pytest.mark.parametrize("ordinary_overflow", [25, 1000, 1000000])
def test_ordinary_overflow_cannot_pay_major_negative_debt(ordinary_overflow):
    assert _allocate_relation_weight(ordinary_overflow, -25) == -5
    assert _allocate_relation_weight(-ordinary_overflow, 25) == 5
