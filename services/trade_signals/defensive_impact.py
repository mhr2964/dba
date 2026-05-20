"""Defensive-impact detector: stat-production layer beyond archetype tagging."""
from __future__ import annotations

from ._registry import SignalContext, register_signal


@register_signal("defensive_impact")
def detect(ctx: SignalContext):
    """Reward elite defensive stat production (BPG/SPG). Requires GP>=10, MPG>=18."""
    from services.trade_context import ContextSignal

    stats = ctx.stats
    if not stats or (stats.get("gp") or stats.get("games_played") or 0) < 10:
        return None
    mpg = stats.get("mpg") or 0.0
    if mpg < 18:
        return None
    bpg = stats.get("bpg") or 0.0
    spg = stats.get("spg") or 0.0

    if bpg >= 2.0:
        return ContextSignal(
            delta=+0.07,
            reason=f"elite shot blocker ({bpg:.1f} BPG) — opponents change their drives because of him.",
            code="defense_elite_blocker",
        )
    if spg >= 2.0:
        return ContextSignal(
            delta=+0.06,
            reason=f"elite ball-hawk ({spg:.1f} SPG) — generates extra possessions every night.",
            code="defense_elite_steals",
        )
    if bpg >= 1.2 and spg >= 1.2:
        return ContextSignal(
            delta=+0.05,
            reason=f"two-way disruption ({bpg:.1f} BPG, {spg:.1f} SPG) — guards multiple positions.",
            code="defense_two_way_disruption",
        )
    return None
