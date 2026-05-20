"""Scheme-fit detector: coaching-scheme alignment between role and philosophy."""
from __future__ import annotations

from ._registry import SignalContext, register_signal


@register_signal("scheme_fit")
def detect(ctx: SignalContext):
    """Return +0.05 for known-good role+philosophy pairings; None for neutral/unknown."""
    from services.trade_context import ContextSignal

    incoming_role = ctx.incoming_role
    coach_philosophy = ctx.coach_philosophy

    if not incoming_role or not coach_philosophy:
        return None

    if incoming_role == "primary_initiator" and coach_philosophy == "egalitarian":
        return ContextSignal(
            delta=+0.05,
            reason=(
                "slots into our egalitarian system as a connector — "
                "touches distribute through him, not just to him."
            ),
            code="scheme_fit_match",
        )
    if incoming_role == "iso_scorer" and coach_philosophy == "star_maxer":
        return ContextSignal(
            delta=+0.05,
            reason="feeds our star-maxer scheme — we'll run more isolation sets to free him up.",
            code="scheme_fit_match",
        )
    if incoming_role == "rim_protector" and coach_philosophy == "defense_first":
        return ContextSignal(
            delta=+0.05,
            reason=(
                "unlocks switching/aggressive scheme — "
                "having him behind us lets perimeter guys press."
            ),
            code="scheme_fit_match",
        )
    if incoming_role in ("two_way_wing", "wing_stopper") and coach_philosophy == "defense_first":
        return ContextSignal(
            delta=+0.05,
            reason="gives our defense another switchable piece — exactly what we scheme around.",
            code="scheme_fit_match",
        )
    if incoming_role == "post_anchor" and coach_philosophy == "tendency_respecter":
        return ContextSignal(
            delta=+0.05,
            reason="fits our tendency-based sets — post catches will open cutters and shooters.",
            code="scheme_fit_match",
        )
    return None
