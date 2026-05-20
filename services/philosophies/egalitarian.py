from __future__ import annotations

from ._registry import register_philosophy


@register_philosophy("egalitarian")
def bias(player: dict, role: str, base_score: float, *, ovr_rank: int, team_context: dict) -> float:
    """Penalise ball-dominant roles; boost distributive secondary roles."""
    if role in ("iso_scorer", "primary_initiator"):
        return base_score - 20
    if role in ("secondary_creator", "wing_creator", "catch_and_shoot", "movement_shooter"):
        return base_score + 10
    return base_score
