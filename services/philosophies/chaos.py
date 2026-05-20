from __future__ import annotations

import hashlib

from ._registry import register_philosophy


@register_philosophy("chaos")
def bias(player: dict, role: str, base_score: float, *, ovr_rank: int, team_context: dict) -> float:
    """Deterministic per-(player, role, season) chaos — reproducible but illogical."""
    seed_str = (
        f"chaos:{player['player_id']}:{role}:{team_context.get('season', 0)}"
    )
    h = int(hashlib.md5(seed_str.encode()).hexdigest()[:8], 16)
    bucket = h % 10
    if bucket < 3:
        return base_score + 30
    if bucket < 6:
        return base_score - 20
    return base_score
