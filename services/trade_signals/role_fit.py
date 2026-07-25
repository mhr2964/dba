"""Role-fit detector: surfaces mismatch or match between player tendencies and current role."""
from __future__ import annotations

from typing import Literal, Optional

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


def classify_role_fit(tendencies: dict, role: Optional[str]) -> Optional[Literal["mismatch", "match"]]:
    """Classify a player's tendency profile against a role's expected thresholds.

    Cheap, self-contained classifier — only needs the player's own tendency
    fields (a plain dict) plus their role string, no roster-wide/philosophy
    context. This is the single place `_ROLE_EXPECTED_TENDENCIES` and its
    threshold math live; both the trade-signal detector below and
    `progression_service`'s role-fit compounding signal call this.

    Returns "mismatch" when every expected tendency for the role misses its
    threshold by more than 10, "match" when every one clears it by more than
    10, and None when the role isn't in `_ROLE_EXPECTED_TENDENCIES` or the
    profile is genuinely mixed (some tendencies hit, some miss) — callers
    should treat None as "no signal," not as an error.
    """
    if not role or role not in _ROLE_EXPECTED_TENDENCIES:
        return None

    expected = _ROLE_EXPECTED_TENDENCIES[role]
    misses = [
        tend for tend, threshold in expected.items()
        if (tendencies.get(tend) or 0) < threshold - 10
    ]
    matches = [
        tend for tend, threshold in expected.items()
        if (tendencies.get(tend) or 0) >= threshold + 10
    ]

    if misses and not matches:
        return "mismatch"
    if matches and not misses:
        return "match"
    return None


@register_signal("role_fit")
def detect(ctx: SignalContext):
    """Surface mismatch (+0.04 buy-low) or clean match (+0.05) between tendencies and role."""
    from services.trade_context import ContextSignal

    incoming_role = ctx.incoming_role
    incoming_player = ctx.incoming_player

    classification = classify_role_fit(incoming_player, incoming_role)
    if classification is None:
        return None

    role_label = incoming_role.replace("_", " ")

    if classification == "mismatch":
        return ContextSignal(
            delta=+0.04,
            reason=(
                f"miscast as {role_label} on his old team — "
                f"his profile doesn't match the role's demands."
            ),
            code="role_mismatch",
        )
    return ContextSignal(
        delta=+0.05,
        reason=f"his tendencies are a clean fit for the {role_label} role.",
        code="role_fit_match",
    )
