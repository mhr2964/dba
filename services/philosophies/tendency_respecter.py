from __future__ import annotations

from ._registry import register_philosophy


@register_philosophy("tendency_respecter")
def bias(player: dict, role: str, base_score: float, *, ovr_rank: int, team_context: dict) -> float:
    """No bias — pure tendency-based scoring (Phase 1 baseline)."""
    return base_score
