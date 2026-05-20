"""Overperformance detector: flags regression risk when form is well above OVR."""
from __future__ import annotations

from ._registry import SignalContext, register_signal


@register_signal("overperformance")
def detect(ctx: SignalContext):
    """Return -0.04 when form_mod >= 1.10 — buying at peak price, expect regression."""
    from services.trade_context import ContextSignal

    if ctx.form_mod >= 1.10:
        ovr = ctx.incoming_player.get("overall", 0)
        return ContextSignal(
            delta=-0.04,
            reason=(
                f"playing above his {ovr} OVR right now (form {ctx.form_mod:.2f}) — "
                f"regression risk; buying at peak price."
            ),
            code="overperforming",
        )
    return None
