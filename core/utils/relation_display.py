def build_relation_display(record: dict) -> dict[str, float | str]:
    """Format final affinity with the signed net effect of recent ratings.

    Args:
        record: Shared profile or score data with unrounded interaction and
            recent baseline scores. The baseline excludes selected recent ratings;
            missing baseline data implies no known recent effect.

    Returns:
        The one-decimal recent effect and final-score summary. The component
        summary remains available to existing custom report templates.
        The effect is already included in the interaction score.
    """
    interaction = float(record.get("interaction_score", 50))
    baseline = float(record.get("recent_baseline_score", interaction))
    # Subtract before rounding either score and normalize negative zero.
    recent_effect = round(interaction - baseline, 1) or 0.0
    relation = record.get("relation")
    return {
        "recent_effect": recent_effect,
        "relation_summary": (
            f"{float(relation):.1f}（近期：{recent_effect:+.1f}）"
            if relation is not None
            else ""
        ),
        "relation_components": (
            f"基础分：{record.get('base_relation', 0)}；互动分：{interaction:.1f}"
            f"（近期：{recent_effect:+.1f}）"
        ),
    }
