"""Role taxonomy and the pure scoring/derivation logic that assigns each
rostered player one role per (league, team, season): _score_role_fit
(per-role fit scoring, philosophy-bias-aware) and
_derive_tendency_respecter (the greedy assignment algorithm -- exactly one
primary scorer, exactly one defensive anchor, then best-fit for the rest).

Extracted from role_service.py (Phase 3 opportunistic split, see
HANDOFF.md). ROLE_REGISTRY is re-imported into the slimmed role_service.py
so `role_service.ROLE_REGISTRY` keeps working for external callers
(bot/cogs/coach_cog.py accesses it directly) without any import change
there.
"""
from __future__ import annotations

import datetime
from typing import Optional

from core.logging import get_logger
from services.philosophies import PHILOSOPHY_BIASES

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Role taxonomy — 23 roles, final for all phases
# ---------------------------------------------------------------------------

ROLE_REGISTRY: dict[str, dict] = {
    # OFFENSIVE LEAD (high touches, primary scoring)
    # Touch shares bumped 2026-05-20: league leading scorer was capping at
    # ~24 ppg vs NBA's 33+. Raising star touch shares concentrates scoring
    # on the offensive lead so post-normalization share-of-team for stars
    # lands at NBA-realistic 27-32% (was ~18% at the old 0.25 cap).
    "iso_scorer": {
        "touch_share": 0.38, "fga_3pa_pct": 0.32, "fta_per_fga": 0.30,
        "minutes_tier": "starter", "defensive_role": "general",
        "scheme_synergy": ["isolation"],
        "tendencies_boosted": ["tendency_drive", "tendency_3pt"],
    },
    "primary_initiator": {
        "touch_share": 0.34, "fga_3pa_pct": 0.34, "fta_per_fga": 0.25,
        "minutes_tier": "starter", "defensive_role": "general",
        "scheme_synergy": ["pick_and_roll"],
        "tendencies_boosted": ["tendency_pass", "ast_tendency", "tendency_drive"],
    },
    "post_anchor": {
        "touch_share": 0.31, "fga_3pa_pct": 0.05, "fta_per_fga": 0.40,
        "minutes_tier": "starter", "defensive_role": "anchor",
        "scheme_synergy": ["post_up"],
        "tendencies_boosted": ["reb_tendency"],
    },
    "movement_shooter": {
        "touch_share": 0.28, "fga_3pa_pct": 0.65, "fta_per_fga": 0.18,
        "minutes_tier": "starter", "defensive_role": "perimeter",
        "scheme_synergy": ["ball_movement"],
        "tendencies_boosted": ["tendency_3pt"],
    },
    "slashing_lead": {
        "touch_share": 0.31, "fga_3pa_pct": 0.20, "fta_per_fga": 0.45,
        "minutes_tier": "starter", "defensive_role": "general",
        "scheme_synergy": ["transition"],
        "tendencies_boosted": ["tendency_drive"],
    },
    # OFFENSIVE SECONDARY (mid touches)
    "secondary_creator": {
        "touch_share": 0.18, "fga_3pa_pct": 0.40, "fta_per_fga": 0.25,
        "minutes_tier": "starter", "defensive_role": "general",
        "scheme_synergy": ["ball_movement"],
        "tendencies_boosted": ["tendency_pass", "ast_tendency"],
    },
    "wing_creator": {
        "touch_share": 0.19, "fga_3pa_pct": 0.38, "fta_per_fga": 0.28,
        "minutes_tier": "starter", "defensive_role": "perimeter",
        "scheme_synergy": ["isolation", "ball_movement"],
        "tendencies_boosted": ["tendency_drive", "tendency_3pt"],
    },
    "pick_and_pop": {
        "touch_share": 0.14, "fga_3pa_pct": 0.55, "fta_per_fga": 0.15,
        "minutes_tier": "starter", "defensive_role": "anchor",
        "scheme_synergy": ["pick_and_roll"],
        "tendencies_boosted": ["tendency_3pt"],
    },
    "spark_plug_scorer": {
        "touch_share": 0.20, "fga_3pa_pct": 0.42, "fta_per_fga": 0.30,
        "minutes_tier": "bench", "defensive_role": "general",
        "scheme_synergy": ["isolation"],
        "tendencies_boosted": ["tendency_drive", "tendency_3pt"],
    },
    "transition_engine": {
        "touch_share": 0.17, "fga_3pa_pct": 0.30, "fta_per_fga": 0.32,
        "minutes_tier": "starter", "defensive_role": "general",
        "scheme_synergy": ["transition"],
        "tendencies_boosted": ["tendency_drive", "ast_tendency"],
    },
    # OFFENSIVE SPECIALIST (narrow, low/mid touches)
    "catch_and_shoot": {
        "touch_share": 0.10, "fga_3pa_pct": 0.75, "fta_per_fga": 0.10,
        "minutes_tier": "rotation", "defensive_role": "general",
        "scheme_synergy": ["ball_movement"],
        "tendencies_boosted": ["tendency_3pt"],
    },
    "floor_spacer": {
        "touch_share": 0.08, "fga_3pa_pct": 0.80, "fta_per_fga": 0.08,
        "minutes_tier": "rotation", "defensive_role": "passive",
        "scheme_synergy": ["ball_movement"],
        "tendencies_boosted": ["tendency_3pt"],
    },
    "cutter_finisher": {
        "touch_share": 0.11, "fga_3pa_pct": 0.15, "fta_per_fga": 0.25,
        "minutes_tier": "rotation", "defensive_role": "general",
        "scheme_synergy": ["ball_movement"],
        "tendencies_boosted": ["tendency_drive"],
    },
    "rim_runner": {
        "touch_share": 0.10, "fga_3pa_pct": 0.05, "fta_per_fga": 0.45,
        "minutes_tier": "starter", "defensive_role": "anchor",
        "scheme_synergy": ["pick_and_roll"],
        "tendencies_boosted": ["reb_tendency"],
    },
    "screen_roller": {
        "touch_share": 0.09, "fga_3pa_pct": 0.05, "fta_per_fga": 0.38,
        "minutes_tier": "rotation", "defensive_role": "anchor",
        "scheme_synergy": ["pick_and_roll"],
        "tendencies_boosted": ["reb_tendency"],
    },
    # DEFENSE-FIRST
    "rim_protector": {
        "touch_share": 0.07, "fga_3pa_pct": 0.05, "fta_per_fga": 0.35,
        "minutes_tier": "starter", "defensive_role": "anchor",
        "scheme_synergy": ["drop_coverage"],
        "tendencies_boosted": ["blk_tendency", "reb_tendency"],
    },
    "wing_stopper": {
        "touch_share": 0.10, "fga_3pa_pct": 0.55, "fta_per_fga": 0.15,
        "minutes_tier": "starter", "defensive_role": "perimeter",
        "scheme_synergy": ["switching"],
        "tendencies_boosted": ["stl_tendency"],
    },
    "on_ball_pest": {
        "touch_share": 0.11, "fga_3pa_pct": 0.45, "fta_per_fga": 0.20,
        "minutes_tier": "rotation", "defensive_role": "perimeter",
        "scheme_synergy": ["pressure"],
        "tendencies_boosted": ["stl_tendency"],
    },
    "switching_big": {
        "touch_share": 0.13, "fga_3pa_pct": 0.30, "fta_per_fga": 0.28,
        "minutes_tier": "starter", "defensive_role": "anchor",
        "scheme_synergy": ["switching"],
        "tendencies_boosted": ["blk_tendency"],
    },
    # TWO-WAY / BALANCED
    "two_way_wing": {
        "touch_share": 0.16, "fga_3pa_pct": 0.42, "fta_per_fga": 0.25,
        "minutes_tier": "starter", "defensive_role": "perimeter",
        "scheme_synergy": ["switching", "ball_movement"],
        "tendencies_boosted": ["tendency_3pt", "tendency_drive"],
    },
    "two_way_big": {
        "touch_share": 0.15, "fga_3pa_pct": 0.20, "fta_per_fga": 0.30,
        "minutes_tier": "starter", "defensive_role": "anchor",
        "scheme_synergy": ["switching"],
        "tendencies_boosted": ["blk_tendency", "reb_tendency"],
    },
    "glue_guy": {
        "touch_share": 0.08, "fga_3pa_pct": 0.40, "fta_per_fga": 0.18,
        "minutes_tier": "rotation", "defensive_role": "general",
        "scheme_synergy": [],
        "tendencies_boosted": [],
    },
    # DEPTH
    "veteran_mentor": {
        "touch_share": 0.06, "fga_3pa_pct": 0.45, "fta_per_fga": 0.15,
        "minutes_tier": "bench", "defensive_role": "passive",
        "scheme_synergy": [],
        "tendencies_boosted": [],
    },
    "developmental": {
        "touch_share": 0.07, "fga_3pa_pct": 0.30, "fta_per_fga": 0.22,
        "minutes_tier": "bench", "defensive_role": "general",
        "scheme_synergy": [],
        "tendencies_boosted": [],
    },
    "end_of_bench": {
        "touch_share": 0.02, "fga_3pa_pct": 0.35, "fta_per_fga": 0.15,
        "minutes_tier": "depth", "defensive_role": "passive",
        "scheme_synergy": [],
        "tendencies_boosted": [],
    },
}


# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

# Positions treated as guards (favor ball-handling offensive roles)
_GUARD_POSITIONS = frozenset({"PG", "SG"})


# Positions treated as wings
_WING_POSITIONS = frozenset({"SF", "SG"})


# Positions treated as bigs (interior)
_BIG_POSITIONS = frozenset({"PF", "C"})


# Offensive lead roles — every team must get exactly ONE
_PRIMARY_SCORING_ROLES = frozenset({
    "iso_scorer", "primary_initiator", "post_anchor",
    "movement_shooter", "slashing_lead",
})


# Finding #3b (realism audit): primary-scoring role pool for a team whose
# top-3 OVR players are ALL bigs (no real guard/wing present). Pulling from
# the guard/wing pool in that case forced a ball-handling identity (e.g.
# primary_initiator) onto a center -- a personnel-fit bug even though "top-3
# OVR" is otherwise a reasonable proxy for "who deserves offensive
# recognition." Tuple (not frozenset) for deterministic score-tie iteration.
_BIG_PRIMARY_ROLES = ("post_anchor", "pick_and_pop", "rim_runner", "screen_roller")


# Defensive anchor roles — every team must get exactly ONE.
# Tuple (not frozenset) so iteration order is deterministic on score ties.
_DEFENSIVE_ANCHOR_ROLES = (
    "rim_protector", "two_way_big", "switching_big", "post_anchor",
)


# Tendency thresholds (DB stores 0-100 integers)
_3PT_SHOOTER_THRESHOLD = 40


_DRIVE_THRESHOLD = 35


_BLK_THRESHOLD = 30


_DEFENSE_LOW_USAGE_THRESHOLD = 40


def _age_from_birth(birth_date: Optional[datetime.date], season: int) -> Optional[float]:
    """Estimate age at season-start (October of that year)."""
    if birth_date is None:
        return None
    season_start = datetime.date(season, 10, 1)
    return (season_start - birth_date).days / 365.25


# ---------------------------------------------------------------------------
# Scoring: fit of a player to a candidate role (higher = better fit)
# ---------------------------------------------------------------------------

def _score_role_fit(
    player: dict,
    role: str,
    ovr_rank: int,
    n_players: int,
    team_context: Optional[dict] = None,
) -> float:
    """Return a numeric fit score for assigning `role` to `player`.

    Higher is better.  Scores are relative — only used for ranking candidates
    within a single team assignment pass.

    player dict keys:
        player_id, overall, birth_date, position, tendency_3pt, tendency_drive,
        tendency_pass, ast_tendency, reb_tendency, blk_tendency, stl_tendency,
        defense_tendency, usage_weight, defensive_archetype

    team_context (optional): {"league_id", "team_id", "season", "philosophy"}
        When provided the coach philosophy bias is applied on top of the base score.
    """
    ovr = player["overall"]
    pos = player.get("position", "")
    t3 = player.get("tendency_3pt", 50)
    td = player.get("tendency_drive", 50)
    tp = player.get("tendency_pass", 50)
    ta = player.get("ast_tendency", 50)
    tr = player.get("reb_tendency", 50)
    tb = player.get("blk_tendency", 50)
    ts = player.get("stl_tendency", 50)
    tdef = player.get("defense_tendency", 50)
    uw = player.get("usage_weight", 50)
    arch = player.get("defensive_archetype") or ""

    score = 0.0

    # --- Primary scoring roles ---
    if role == "iso_scorer":
        # Guards preferred; top OVR only
        if pos in _GUARD_POSITIONS:
            score += 20
        if ovr_rank <= 2:
            score += 30
        score += td * 0.3 + ovr * 0.5
    elif role == "primary_initiator":
        if pos == "PG":
            score += 25
        if ovr_rank <= 2:
            score += 25
        score += tp * 0.4 + ta * 0.3 + td * 0.2 + ovr * 0.4
    elif role == "post_anchor":
        if pos == "C":
            score += 30
        elif pos == "PF":
            score += 15
        if ovr_rank <= 2:
            # Rank bonus is capped to avoid overriding a clear rim_protector archetype
            # match.  rim_protector adds +25 for arch match OR NULL-arch blk profile;
            # post_anchor's +20 rank bonus cannot outscore that.
            score += 20
        score += tr * 0.3 + ovr * 0.4
        # Strong penalty for high 3pt tendency — post anchors don't shoot 3s
        score -= t3 * 0.3
    elif role == "movement_shooter":
        if pos in _GUARD_POSITIONS or pos == "SF":
            score += 15
        if ovr_rank <= 2:
            score += 20
        score += t3 * 0.6 + ovr * 0.3
    elif role == "slashing_lead":
        if pos in _GUARD_POSITIONS or pos == "SF":
            score += 15
        if ovr_rank <= 2:
            score += 20
        score += td * 0.6 + ovr * 0.3
    # --- Secondary ---
    elif role == "secondary_creator":
        if pos in ("PG", "SG", "SF"):
            score += 10
        if 2 < ovr_rank <= 5:
            score += 15
        score += tp * 0.4 + ta * 0.4 + ovr * 0.2
    elif role == "wing_creator":
        if pos in ("SG", "SF"):
            score += 20
        score += td * 0.3 + t3 * 0.2 + ovr * 0.25
    elif role == "pick_and_pop":
        if pos in _BIG_POSITIONS:
            score += 20
        score += t3 * 0.7
        score -= td * 0.1
    elif role == "spark_plug_scorer":
        # Bench role; slot > 8 preferred
        if player.get("slot", 5) > 8:
            score += 20
        score += td * 0.3 + t3 * 0.3 + ovr * 0.2
    elif role == "transition_engine":
        if pos in _GUARD_POSITIONS:
            score += 10
        score += td * 0.4 + ta * 0.3 + ovr * 0.1
    # --- Specialist ---
    elif role == "catch_and_shoot":
        score += t3 * 0.7
        score -= td * 0.1
    elif role == "floor_spacer":
        score += t3 * 0.8
        score -= td * 0.2
        # Low usage preferred
        score += max(0, 60 - uw) * 0.2
    elif role == "cutter_finisher":
        score += td * 0.5 + tr * 0.2
        score -= t3 * 0.2
    elif role == "rim_runner":
        if pos == "C":
            score += 30
        elif pos == "PF":
            score += 10
        score += tr * 0.4
        score -= t3 * 0.4
    elif role == "screen_roller":
        if pos in _BIG_POSITIONS:
            score += 20
        score += tr * 0.3
        score -= t3 * 0.3
    # --- Defense-first ---
    elif role == "rim_protector":
        if pos == "C":
            score += 30
        elif pos == "PF":
            score += 10
        score += tb * 0.6 + tr * 0.2
        score -= t3 * 0.1
        if arch in ("rim_protector",) and ovr >= 75:
            score += 25
        # Centers who don't shoot 3s and block shots are rim protectors even
        # when defensive_archetype hasn't been backfilled (NULL).  This captures
        # players like Gobert (C, blk~50, t3=0) correctly without requiring the
        # archetype column to be populated.
        elif not arch and pos == "C" and ovr >= 70 and tb >= 40 and t3 <= 10:
            # Larger bonus than the post_anchor rank bonus (+20) so a defensive
            # center like Gobert (blk~50, t3=0) wins rim_protector over post_anchor
            # even when they're a top-2 OVR big.
            score += 30
    elif role == "wing_stopper":
        if pos in ("SG", "SF"):
            score += 20
        score += ts * 0.5 + tdef * 0.3
        score -= uw * 0.1  # low-usage defenders preferred
        if arch in ("wing_stopper",) and ovr >= 75:
            score += 25
    elif role == "on_ball_pest":
        if pos in _GUARD_POSITIONS:
            score += 15
        score += ts * 0.6
    elif role == "switching_big":
        if pos in _BIG_POSITIONS:
            score += 20
        score += tb * 0.3 + ts * 0.2 + ovr * 0.1
        if arch in ("switching_big", "two_way_big") and ovr >= 75:
            score += 20
    # --- Two-way ---
    elif role == "two_way_wing":
        if pos in ("SG", "SF"):
            score += 20
        score += (t3 + td) * 0.2 + (ts + tdef) * 0.2 + ovr * 0.15
        if arch in ("wing_stopper", "two_way_big") and ovr >= 75:
            score += 15
    elif role == "two_way_big":
        if pos in _BIG_POSITIONS:
            score += 20
        score += tb * 0.3 + tr * 0.2 + ovr * 0.2
        if arch in ("two_way_big", "rim_protector") and ovr >= 75:
            score += 20
    elif role == "glue_guy":
        # Falls to players that don't excel at anything specific
        score += 5
    # --- Depth ---
    elif role == "veteran_mentor":
        age = player.get("_age")
        if age is not None and age >= 32:
            score += 30
        # Low OVR end-of-roster only
        if ovr_rank > n_players - 4:
            score += 10
    elif role == "developmental":
        age = player.get("_age")
        if age is not None and age <= 23:
            score += 30
        if ovr_rank > n_players - 4:
            score += 10
    elif role == "end_of_bench":
        if ovr_rank > n_players - 3:
            score += 40
        score -= ovr * 0.3  # lowest OVR players

    # --- Phase 3: apply philosophy bias on top of base score ---
    if team_context:
        philosophy = team_context.get("philosophy") or "tendency_respecter"
        bias_fn = PHILOSOPHY_BIASES.get(philosophy, PHILOSOPHY_BIASES["tendency_respecter"])
        score = bias_fn(
            player,
            role,
            score,
            ovr_rank=ovr_rank,
            team_context=team_context,
        )

    return score


# ---------------------------------------------------------------------------
# Core derivation — tendency_respecter
# ---------------------------------------------------------------------------

def _derive_tendency_respecter(
    players: list[dict],
    team_context: Optional[dict] = None,
) -> list[dict]:
    """Greedy role assignment respecting player tendencies and team-structure rules.

    team_context is threaded into _score_role_fit so that philosophy biases are
    applied at every scoring decision, including primary and anchor slot selection.

    Assignment order:
    1. Bottom-3 OVR → depth roles (end_of_bench / developmental / veteran_mentor).
    2. Top-3 OVR → primary scoring role candidates (one winner per team).
    3. Top-3 OVR (or top non-assigned big) → defensive anchor (one winner per team).
    4. Remaining players → best available role by fit score, excluding already-claimed
       primary and anchor slots.

    Returns list of {player_id, role, touch_share, rationale} — NOT yet normalised.
    """
    if not players:
        return []

    ctx = team_context or {}
    philosophy = ctx.get("philosophy") or "tendency_respecter"

    n = len(players)
    sorted_by_ovr = sorted(players, key=lambda p: p["overall"], reverse=True)
    ovr_rank_map = {p["player_id"]: i for i, p in enumerate(sorted_by_ovr)}

    assigned: dict[int, str] = {}   # player_id -> role
    rationales: dict[int, str] = {}

    # --- Step 1: Depth slots for bottom-3 OVR ---
    # List (not set): deterministic iteration order preserves idempotent assignment.
    bottom_3_ids = [p["player_id"] for p in sorted_by_ovr[max(0, n - 3):]]
    for pid in bottom_3_ids:
        p = next(x for x in players if x["player_id"] == pid)
        age = p.get("_age")
        if age is not None and age >= 32:
            role = "veteran_mentor"
            rationale = f"OVR {p['overall']} bottom-roster + age {age:.0f} → veteran_mentor"
        elif age is not None and age <= 23:
            role = "developmental"
            rationale = f"OVR {p['overall']} bottom-roster + age {age:.0f} → developmental"
        else:
            role = "end_of_bench"
            rationale = f"OVR {p['overall']} bottom-roster → end_of_bench"
        assigned[pid] = role
        rationales[pid] = rationale

    # --- Step 2: Primary scoring role — highest-OVR guard/wing in top-3 gets the slot ---
    # The PLAYER is chosen first (highest OVR from the guard/wing pool), then we
    # find their best-fitting role among the guard/wing primary roles.  This prevents
    # a lower-OVR guard from stealing the primary slot by scoring marginally better
    # on one specific role, leaving the team's best player without a scoring identity.
    # post_anchor is excluded here; high-OVR bigs earn their scoring recognition through
    # the anchor step instead.
    # Sorted tuple for deterministic iteration on score ties (frozenset order is random).
    _guard_wing_primary_roles = tuple(sorted(_PRIMARY_SCORING_ROLES - {"post_anchor"}))
    top_3_ids = [p["player_id"] for p in sorted_by_ovr[:3] if p["player_id"] not in assigned]
    top_3_guard_wing_ids = [
        pid for pid in top_3_ids
        if next(x for x in players if x["player_id"] == pid).get("position") not in _BIG_POSITIONS
    ]
    # Finding #3b: when NO guard/wing exists in the top-3 (all-big top-3), do
    # NOT fall back to the guard/wing pool -- that forces a ball-handling
    # identity onto a center. Route to a big-appropriate pool instead.
    if top_3_guard_wing_ids:
        primary_pool_ids = top_3_guard_wing_ids
        _primary_role_options = _guard_wing_primary_roles
    else:
        primary_pool_ids = top_3_ids
        _primary_role_options = _BIG_PRIMARY_ROLES

    primary_role_filled = False
    for pid in primary_pool_ids:  # already ordered highest OVR first
        if pid in assigned:
            continue
        p = next(x for x in players if x["player_id"] == pid)
        rank = ovr_rank_map[pid]
        # Pick the best-fitting role for THIS player specifically
        best_role_score = -9999.0
        best_role = _primary_role_options[0]
        for role in _primary_role_options:
            s = _score_role_fit(p, role, rank, n, ctx)
            if s > best_role_score:
                best_role_score = s
                best_role = role
        assigned[pid] = best_role
        base_rationale = (
            f"Top OVR {p['overall']} {p.get('position', '')} "
            f"+ tendency fit → {best_role}"
        )
        if philosophy != "tendency_respecter":
            base_rationale += f"  [philosophy: {philosophy}]"
        rationales[pid] = base_rationale
        primary_role_filled = True
        break

    if not primary_role_filled:
        # Fallback: best unassigned player for iso_scorer
        for p in sorted_by_ovr:
            if p["player_id"] not in assigned:
                assigned[p["player_id"]] = "iso_scorer"
                rationales[p["player_id"]] = (
                    f"Fallback primary scorer: OVR {p['overall']} → iso_scorer"
                )
                break

    # --- Step 3: Defensive anchor — best fit among top unassigned bigs (or any if no big) ---
    anchor_candidates: list[tuple[float, int, str]] = []
    anchor_role_options = _DEFENSIVE_ANCHOR_ROLES  # post_anchor now eligible here
    # Bigs first; fall back to full unassigned pool if no big available
    candidate_pool = (
        [p for p in sorted_by_ovr if p.get("position") in _BIG_POSITIONS and p["player_id"] not in assigned]
        or [p for p in sorted_by_ovr if p["player_id"] not in assigned]
    )
    for p in candidate_pool[:5]:  # top 5 eligible
        rank = ovr_rank_map[p["player_id"]]
        for role in anchor_role_options:
            s = _score_role_fit(p, role, rank, n, ctx)
            anchor_candidates.append((s, p["player_id"], role))
    anchor_candidates.sort(key=lambda t: t[0], reverse=True)

    anchor_filled = False
    for score, pid, role in anchor_candidates:
        if pid not in assigned:
            assigned[pid] = role
            p = next(x for x in players if x["player_id"] == pid)
            anchor_rationale = (
                f"OVR {p['overall']} {p.get('position', '')} "
                f"+ defensive fit → {role}"
            )
            if philosophy != "tendency_respecter":
                anchor_rationale += f"  [philosophy: {philosophy}]"
            rationales[pid] = anchor_rationale
            anchor_filled = True
            break

    if not anchor_filled:
        # No eligible candidate (e.g. no centers on roster) — log and skip
        log.debug("role_service: no defensive anchor candidate available for team")

    # --- Step 4: Fill remaining players with best available role ---
    # The guard/wing primary scoring slot is filled — block all of those roles so a
    # second player can't also claim a primary-scorer role.  Once ANY defensive
    # anchor role is filled, ALL anchor roles are blocked so Step 4 cannot assign
    # a second player to post_anchor, two_way_big, etc.  This is the intended
    # "exactly one anchor" invariant.
    #
    # Finding #3b: the guard/wing exclusion only applies when Step 2 actually
    # contested the guard/wing pool (top_3_guard_wing_ids non-empty). When an
    # all-big top-3 routed to _BIG_PRIMARY_ROLES instead, the guard/wing primary
    # roles were never claimed -- leave them open so a genuine guard/wing
    # further down the roster (rank 4+) can still earn one via its own best-fit
    # score in this step, instead of the whole team going without a
    # ball-handling primary identity.
    _depth_role_names = frozenset({"end_of_bench", "developmental", "veteran_mentor"})
    exclusions: set[str] = set()
    if primary_role_filled and top_3_guard_wing_ids:
        exclusions.update(_guard_wing_primary_roles)
    # anchor_filled is the only source of defensive anchor roles in `assigned` at
    # this point — Step 2 can never produce a post_anchor because it is excluded
    # from _guard_wing_primary_roles.  No secondary check needed.
    if anchor_filled:
        exclusions.update(_DEFENSIVE_ANCHOR_ROLES)

    general_roles = [
        r for r in ROLE_REGISTRY
        if r not in exclusions and r not in _depth_role_names
    ]

    for p in sorted_by_ovr:
        pid = p["player_id"]
        if pid in assigned:
            continue
        rank = ovr_rank_map[pid]

        best_score = -9999.0
        best_role = "glue_guy"
        for role in general_roles:
            s = _score_role_fit(p, role, rank, n, ctx)
            if s > best_score:
                best_score = s
                best_role = role

        assigned[pid] = best_role
        # Build rationale from the strongest tendency signal
        sig_parts = []
        if p.get("tendency_3pt", 0) > _3PT_SHOOTER_THRESHOLD:
            sig_parts.append(f"3pt={p['tendency_3pt']}")
        if p.get("tendency_drive", 0) > _DRIVE_THRESHOLD:
            sig_parts.append(f"drive={p['tendency_drive']}")
        if p.get("blk_tendency", 0) > _BLK_THRESHOLD:
            sig_parts.append(f"blk={p['blk_tendency']}")
        if p.get("defensive_archetype"):
            sig_parts.append(f"arch={p['defensive_archetype']}")
        sig = " + ".join(sig_parts) if sig_parts else f"OVR {p['overall']}"
        rationale = f"{sig} → {best_role}"
        if philosophy != "tendency_respecter":
            rationale += f"  [philosophy: {philosophy}]"
        rationales[pid] = rationale

    # --- Build output list ---
    result = []
    for p in players:
        pid = p["player_id"]
        role = assigned.get(pid, "end_of_bench")
        touch_share = ROLE_REGISTRY[role]["touch_share"]
        result.append({
            "player_id": pid,
            "role": role,
            "touch_share": touch_share,
            "rationale": rationales.get(pid, f"→ {role}"),
        })
    return result
