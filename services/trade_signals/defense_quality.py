"""Defense-quality detector: tendency-based archetype signal (+0.06 elite, -0.03 floor)."""
from __future__ import annotations

from ._registry import SignalContext, register_signal


@register_signal("defense_quality")
def detect(ctx: SignalContext):
    """Add a defensive-impact note when stats show elite or floor output."""
    from services.trade_context import ContextSignal

    incoming_player = ctx.incoming_player
    archetype = incoming_player.get("defensive_archetype")
    if not archetype or archetype in ("non_defender", "generalist", None):
        return None

    blk = incoming_player.get("blk_tendency") or 0
    stl = incoming_player.get("stl_tendency") or 0
    defense = incoming_player.get("defense_tendency") or 0

    if archetype == "rim_protector" and blk >= 70:
        return ContextSignal(
            delta=+0.06,
            reason="elite shot-blocker — top-tier rim protection at any tempo.",
            code="defense_elite",
        )
    if archetype in ("wing_stopper", "on_ball_pest") and (stl >= 65 or defense >= 70):
        return ContextSignal(
            delta=+0.06,
            reason="one of the best perimeter defenders at his position.",
            code="defense_elite",
        )
    if archetype == "two_way_big" and blk >= 60 and defense >= 60:
        return ContextSignal(
            delta=+0.06,
            reason="genuinely two-way — anchors the paint AND can guard in space.",
            code="defense_elite",
        )
    if archetype == "size_defender" and (blk >= 60 or defense >= 65):
        return ContextSignal(
            delta=+0.06,
            reason="real shot-altering presence — changes how opponents attack the paint.",
            code="defense_elite",
        )
    # Floor: tagged as elite archetype but numbers don't support it
    if archetype == "rim_protector" and blk < 40:
        return ContextSignal(
            delta=-0.03,
            reason="tagged as a rim_protector but the actual block rate doesn't back it up.",
            code="defense_floor",
        )
    if archetype == "wing_stopper" and defense < 40 and stl < 40:
        return ContextSignal(
            delta=-0.03,
            reason="listed as a wing stopper but the defensive numbers don't support the billing.",
            code="defense_floor",
        )
    return None
