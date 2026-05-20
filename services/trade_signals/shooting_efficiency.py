"""Shooting-efficiency detector: rewards elite TS%, flags empty-calorie volume scorers."""
from __future__ import annotations

from ._registry import SignalContext, register_signal


@register_signal("shooting_efficiency")
def detect(ctx: SignalContext):
    """Reward elite shooters; penalise inefficient volume scorers. Requires GP>=10."""
    from services.trade_context import ContextSignal

    stats = ctx.stats
    if not stats or (stats.get("gp") or stats.get("games_played") or 0) < 10:
        return None
    ts = stats.get("ts_pct") or 0.0
    fg3 = stats.get("fg3_pct") or 0.0
    fg3a = stats.get("fg3a") or 0.0   # per-game 3PA

    if ts >= 0.60:
        return ContextSignal(
            delta=+0.06,
            reason=f"elite efficiency (TS {ts:.3f}) — quality shots, quality looks.",
            code="efficiency_elite",
        )
    if fg3 >= 0.40 and fg3a >= 4.0:
        return ContextSignal(
            delta=+0.05,
            reason=f"reliable 3-point threat ({fg3:.3f} on {fg3a:.1f} att/g) — opens the floor.",
            code="efficiency_3pt_specialist",
        )
    ppg = stats.get("ppg") or 0.0
    if ts <= 0.50 and ppg >= 14:
        return ContextSignal(
            delta=-0.05,
            reason=f"empty-calorie scorer (TS {ts:.3f}) — chucks but doesn't make it count.",
            code="efficiency_inefficient_volume",
        )
    return None
