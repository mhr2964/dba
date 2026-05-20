from __future__ import annotations

from ._registry import register_philosophy

_DEFENSIVE_ARCHETYPES = frozenset({
    "rim_protector", "wing_stopper", "on_ball_pest", "two_way_big", "switching_big",
})


@register_philosophy("defense_first")
def bias(player: dict, role: str, base_score: float, *, ovr_rank: int, team_context: dict) -> float:
    """Players with meaningful defensive skill get pushed toward defensive roles."""
    has_def_skill = (
        (player.get("defense_tendency") or 0) >= 60
        or (player.get("defensive_archetype") or "") in _DEFENSIVE_ARCHETYPES
    )
    if has_def_skill:
        if role in (
            "rim_protector", "wing_stopper", "on_ball_pest",
            "two_way_big", "switching_big", "two_way_wing",
        ):
            return base_score + 20
        if role in ("iso_scorer", "spark_plug_scorer", "movement_shooter"):
            return base_score - 12
    return base_score
