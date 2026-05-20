"""Role-fit detector: surfaces mismatch or match between player tendencies and current role."""
from __future__ import annotations

from ._registry import SignalContext, register_signal

_ROLE_EXPECTED_TENDENCIES: dict[str, dict[str, int]] = {
    "primary_initiator":  {"tendency_pass": 50, "ast_tendency": 55},
    "iso_scorer":         {"tendency_drive": 45, "usage_weight": 70},
    "post_anchor":        {"reb_tendency": 50},
    "movement_shooter":   {"tendency_3pt": 50},
    "slashing_lead":      {"tendency_drive": 55},
    "catch_and_shoot":    {"tendency_3pt": 55},
    "rim_protector":      {"blk_tendency": 45},
    "wing_stopper":       {"defense_tendency": 55},
    "on_ball_pest":       {"stl_tendency": 45, "defense_tendency": 50},
    "two_way_wing":       {"defense_tendency": 45, "tendency_drive": 40},
    "two_way_big":        {"blk_tendency": 40, "reb_tendency": 45},
    "wing_creator":       {"tendency_drive": 45, "tendency_3pt": 40},
    "spark_plug_scorer":  {"usage_weight": 60, "tendency_drive": 40},
    "floor_spacer":       {"tendency_3pt": 55},
    "screen_roller":      {"reb_tendency": 45},
    "rim_runner":         {"reb_tendency": 50},
    "pick_and_pop":       {"tendency_3pt": 45},
    "transition_engine":  {"tendency_drive": 50},
}


@register_signal("role_fit")
def detect(ctx: SignalContext):
    """Surface mismatch (+0.04 buy-low) or clean match (+0.05) between tendencies and role."""
    from services.trade_context import ContextSignal

    incoming_role = ctx.incoming_role
    incoming_player = ctx.incoming_player

    if not incoming_role or incoming_role not in _ROLE_EXPECTED_TENDENCIES:
        return None

    expected = _ROLE_EXPECTED_TENDENCIES[incoming_role]
    misses = [
        tend for tend, threshold in expected.items()
        if (incoming_player.get(tend) or 0) < threshold - 10
    ]
    matches = [
        tend for tend, threshold in expected.items()
        if (incoming_player.get(tend) or 0) >= threshold + 10
    ]

    role_label = incoming_role.replace("_", " ")

    if misses and not matches:
        return ContextSignal(
            delta=+0.04,
            reason=(
                f"miscast as {role_label} on his old team — "
                f"his profile doesn't match the role's demands."
            ),
            code="role_mismatch",
        )
    if matches and not misses:
        return ContextSignal(
            delta=+0.05,
            reason=f"his tendencies are a clean fit for the {role_label} role.",
            code="role_fit_match",
        )
    return None
