"""Role-synergy detector: flags role overlap (negative) or complementary pairing (positive)."""
from __future__ import annotations

from core.logging import get_logger
from ._registry import SignalContext, register_signal

log = get_logger(__name__)

_COMPLEMENTARY: dict[str, list[str]] = {
    "primary_initiator":  ["catch_and_shoot", "movement_shooter", "rim_runner", "rim_protector", "floor_spacer"],
    "post_anchor":        ["movement_shooter", "wing_stopper", "floor_spacer", "catch_and_shoot"],
    "iso_scorer":         ["rim_protector", "wing_stopper", "catch_and_shoot", "screen_roller"],
    "movement_shooter":   ["post_anchor", "primary_initiator", "rim_runner"],
    "rim_protector":      ["primary_initiator", "wing_stopper", "on_ball_pest"],
    "wing_stopper":       ["iso_scorer", "post_anchor", "rim_protector"],
    "transition_engine":  ["rim_protector", "floor_spacer"],
}

_SKIP_ROLES: frozenset[str] = frozenset({
    "developmental", "end_of_bench", "veteran_mentor", "secondary_creator",
})


@register_signal("synergy")
async def detect(ctx: SignalContext):
    """Flag role overlap (negative) or complementary pairing (positive)."""
    from services.trade_context import ContextSignal  # avoid circular at module level

    incoming_role = ctx.incoming_role
    if not incoming_role or incoming_role in _SKIP_ROLES:
        return None

    try:
        existing_roles = await ctx.pool.fetch(
            """
            SELECT pr.role, p.first_name || ' ' || p.last_name AS name, pr.touch_share
            FROM player_roles pr
            JOIN players p ON p.id = pr.player_id
            WHERE pr.league_id = $1 AND pr.season = $2 AND pr.team_id = $3
            ORDER BY pr.touch_share DESC
            LIMIT 5
            """,
            ctx.league_id, ctx.season, ctx.perspective_team_id,
        )
    except Exception as exc:
        log.debug("detect_synergy query failed: %s", exc)
        return None

    same_role = [r for r in existing_roles if r["role"] == incoming_role]
    if same_role:
        top = same_role[0]
        role_label = incoming_role.replace("_", " ")
        return ContextSignal(
            delta=-0.08,
            reason=(
                f"creates overlap with {top['name']} ({role_label}) — "
                f"touches will have to split or one of them changes role."
            ),
            code="synergy_overlap",
        )

    for r in existing_roles:
        if incoming_role in _COMPLEMENTARY.get(r["role"], []):
            role_label = incoming_role.replace("_", " ")
            return ContextSignal(
                delta=+0.06,
                reason=(
                    f"pairs naturally with {r['name']} ({r['role'].replace('_', ' ')}) — "
                    f"the {role_label} role gives them the spacing/help they need."
                ),
                code="synergy_complementary",
            )

    return None
