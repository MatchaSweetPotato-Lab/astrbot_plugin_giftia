def build_relation_display(record: dict) -> dict[str, float | str]:
    """Format affinity components with the actual signed recent correction.

    Args:
        record: Shared profile or score data with unrounded interaction and
            long-term scores. Missing long-term data implies no known correction.

    Returns:
        The one-decimal recent effect and a consistent component summary.
        The effect is already included in the interaction score.
    """
    interaction = float(record.get("interaction_score", 50))
    long_term = float(record.get("long_term_score", interaction))
    # Subtract before rounding either score and normalize negative zero.
    recent_effect = round(interaction - long_term, 1) or 0.0
    return {
        "recent_effect": recent_effect,
        "relation_components": (
            f"基础分：{record.get('base_relation', 0)}；互动分：{interaction:.1f}"
            f"（近期：{recent_effect:+.1f}）"
        ),
    }
