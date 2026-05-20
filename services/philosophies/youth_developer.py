from __future__ import annotations

from ._registry import register_philosophy


@register_philosophy("youth_developer")
def bias(player: dict, role: str, base_score: float, *, ovr_rank: int, team_context: dict) -> float:
    """Young players get bigger roles than OVR warrants; aging vets pushed down."""
    age = player.get("_age") or player.get("age") or 25
    if age <= 24:
        if role in ("primary_initiator", "wing_creator", "secondary_creator", "slashing_lead"):
            return base_score + 18
        if role in ("end_of_bench", "developmental"):
            return base_score - 10
    if age >= 32:
        if role in ("iso_scorer", "primary_initiator"):
            return base_score - 18
        if role in ("veteran_mentor",):
            return base_score + 10
    return base_score
