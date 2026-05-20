from __future__ import annotations

from ._registry import register_philosophy


@register_philosophy("star_maxer")
def bias(player: dict, role: str, base_score: float, *, ovr_rank: int, team_context: dict) -> float:
    """Top-2 OVR stars always get iso/initiator preference; bench players pushed to depth roles."""
    if ovr_rank <= 2:
        if role in ("iso_scorer", "primary_initiator"):
            return base_score + 25
        if role in ("secondary_creator", "wing_creator"):
            return base_score - 15
    if ovr_rank >= 10:
        if role in ("end_of_bench", "veteran_mentor"):
            return base_score + 5
    return base_score
