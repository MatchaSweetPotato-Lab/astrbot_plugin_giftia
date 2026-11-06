DEFAULT_RELATION_TIER_COUNTS = (13, 2, 2, 3, 14)
MAX_RELATION_TIER_COUNT = 1_000_000


def valid_relation_tier_counts(value: object) -> bool:
    """Validate the five symmetric tier completion counts.

    Args:
        value: Candidate counts in magnitude order from one through five.

    Returns:
        Whether every count is a supported positive integer, excluding booleans.
    """
    return (
        isinstance(value, (list, tuple))
        and len(value) == 5
        and all(
            type(count) is int and 1 <= count <= MAX_RELATION_TIER_COUNT
            for count in value
        )
    )
