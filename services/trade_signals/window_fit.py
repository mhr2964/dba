"""Window-fit detector: checks player prime alignment with team's contention window."""
from __future__ import annotations

from ._registry import SignalContext, register_signal


@register_signal("window_fit")
def detect(ctx: SignalContext):
    """Return +0.07 (aligned) or -0.10 (misaligned) based on age vs plan horizon/goal."""
    from services.trade_context import ContextSignal

    incoming_player = ctx.incoming_player
    plan = ctx.plan

    age = incoming_player.get("age") or 28
    horizon = plan.get("horizon_seasons", 2)
    goal = plan.get("goal", "")
    age_at_horizon_end = age + horizon

    if goal in ("rebuild", "tank"):
        if age_at_horizon_end <= 28:
            return ContextSignal(
                delta=+0.07,
                reason=(
                    f"will be {age_at_horizon_end} when our window opens — "
                    f"prime years align with contention."
                ),
                code="window_fit_match",
            )
        if age_at_horizon_end >= 33:
            return ContextSignal(
                delta=-0.10,
                reason=(
                    f"will be {age_at_horizon_end} when our window opens — "
                    f"past his prime by then; doesn't fit the timeline."
                ),
                code="window_fit_miss",
            )

    if goal == "win_now":
        if age <= 28:
            return ContextSignal(
                delta=+0.07,
                reason="in prime now, several seasons of contention left with him on the roster.",
                code="window_fit_match",
            )
        if age >= 33:
            return ContextSignal(
                delta=-0.10,
                reason=(
                    f"at {age} he's a 1-2 season rental — "
                    f"works for the immediate window but not beyond."
                ),
                code="window_fit_miss",
            )

    return None
