from __future__ import annotations

from ._registry import register_philosophy


@register_philosophy("vet_overrater")
def bias(player: dict, role: str, base_score: float, *, ovr_rank: int, team_context: dict) -> float:
    """Aging vets get inflated scoring roles; young players get pushed to developmental."""
    age = player.get("_age") or player.get("age") or 25
    if age >= 31:
        if role in ("iso_scorer", "primary_initiator", "wing_creator", "secondary_creator"):
            return base_score + 15
        if role in ("veteran_mentor", "end_of_bench", "developmental"):
            return base_score - 20
    if age <= 23 and role == "developmental":
        return base_score + 8
    return base_score
