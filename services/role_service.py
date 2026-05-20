"""CPU role assignment for player touch-share and offensive identity.

Each rostered player is assigned one role per (league, team, season).  The role
carries a base touch_share, shot-profile flags (fga_3pa_pct, fta_per_fga),
minutes tier, defensive responsibility, and which player tendencies it amplifies.

Phase 1: single philosophy implemented — 'tendency_respecter'.
          All other philosophies fall back to it.
Phase 2: sim engine reads player_roles.touch_share instead of raw usage_weight.
Phase 3: per-philosophy bias functions applied at scoring step — 30 CPU teams
         now reach DIFFERENT conclusions about the same player.

Public API
----------
derive_roles(conn, league_id, team_id, season, *, philosophy=None) -> list[dict]
    Compute without persisting.

persist_roles(conn, league_id, team_id, season, assignments) -> None
    UPSERT into player_roles; does NOT overwrite locked=TRUE rows.

get_or_derive_roles(conn, league_id, team_id, season) -> list[dict]
    Read existing; derive + persist if missing.

derive_and_persist_all(conn, league_id, season) -> None
    Bulk refresh for every team in the league.
"""
from __future__ import annotations

import datetime
import re
import random
from typing import Optional

from core.logging import get_logger
from services.philosophies import PHILOSOPHY_BIASES, assert_philosophy_constraint_sync  # noqa: F401

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Role taxonomy — 23 roles, final for all phases
# ---------------------------------------------------------------------------

ROLE_REGISTRY: dict[str, dict] = {
    # OFFENSIVE LEAD (high touches, primary scoring)
    "iso_scorer": {
        "touch_share": 0.25, "fga_3pa_pct": 0.32, "fta_per_fga": 0.30,
        "minutes_tier": "starter", "defensive_role": "general",
        "scheme_synergy": ["isolation"],
        "tendencies_boosted": ["tendency_drive", "tendency_3pt"],
    },
    "primary_initiator": {
        "touch_share": 0.24, "fga_3pa_pct": 0.34, "fta_per_fga": 0.25,
        "minutes_tier": "starter", "defensive_role": "general",
        "scheme_synergy": ["pick_and_roll"],
        "tendencies_boosted": ["tendency_pass", "ast_tendency", "tendency_drive"],
    },
    "post_anchor": {
        "touch_share": 0.23, "fga_3pa_pct": 0.05, "fta_per_fga": 0.40,
        "minutes_tier": "starter", "defensive_role": "anchor",
        "scheme_synergy": ["post_up"],
        "tendencies_boosted": ["reb_tendency"],
    },
    "movement_shooter": {
        "touch_share": 0.22, "fga_3pa_pct": 0.65, "fta_per_fga": 0.18,
        "minutes_tier": "starter", "defensive_role": "perimeter",
        "scheme_synergy": ["ball_movement"],
        "tendencies_boosted": ["tendency_3pt"],
    },
    "slashing_lead": {
        "touch_share": 0.23, "fga_3pa_pct": 0.20, "fta_per_fga": 0.45,
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
# Coach philosophy enum
# ---------------------------------------------------------------------------

# PHILOSOPHY_BIASES and assert_philosophy_constraint_sync are imported from
# services.philosophies at the top of this file — kept as public re-exports.

COACH_PHILOSOPHIES: list[str] = list(PHILOSOPHY_BIASES.keys())


async def assert_role_constraint_sync(conn) -> None:
    """Assert that ROLE_REGISTRY keys are covered by the DB CHECK constraint.

    Call once at bot startup (after DB connection is established) to catch drift
    between ROLE_REGISTRY and the player_roles_role_check constraint before it
    causes role-assignment failures in production.

    Raises RuntimeError if any key in ROLE_REGISTRY is absent from the
    constraint's IN-list, so the problem is immediately visible in startup logs.
    """
    row = await conn.fetchrow(
        """
        SELECT pg_get_constraintdef(c.oid) AS def
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE c.conname = 'player_roles_role_check'
          AND t.relname = 'player_roles'
        """
    )
    if row is None:
        log.warning(
            "role_service: player_roles_role_check constraint not found — "
            "run migration 041 to add it"
        )
        return

    constraint_def = row["def"]

    def _in_constraint(key: str) -> bool:
        return bool(re.search(r"\b" + re.escape(key) + r"\b", constraint_def))

    missing = [k for k in ROLE_REGISTRY if not _in_constraint(k)]
    if missing:
        msg = (
            f"role_service: ROLE_REGISTRY ↔ CHECK constraint drift detected. "
            f"Keys missing from constraint: {missing}. "
            f"Create a new Alembic migration to add them."
        )
        log.error(msg)
        raise RuntimeError(msg)

    # Reverse check: warn if the DB constraint mentions values not in ROLE_REGISTRY.
    # Don't crash — the DB may temporarily have legacy values during migration.
    db_values = set(re.findall(r"'([^']+)'", constraint_def))
    legacy = db_values - set(ROLE_REGISTRY.keys())
    if legacy:
        log.warning(
            "role_service: DB CHECK constraint has values not in ROLE_REGISTRY "
            "(legacy migration values?): %s", sorted(legacy)
        )

    log.debug("role_service: role constraint sync OK (%d roles)", len(ROLE_REGISTRY))


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

# Defensive anchor roles — every team must get exactly ONE.
# Tuple (not frozenset) so iteration order is deterministic on score ties.
_DEFENSIVE_ANCHOR_ROLES = (
    "rim_protector", "two_way_big", "switching_big", "post_anchor",
)

# Tendency thresholds (DB stores 0-100 integers)
_3PT_SHOOTER_THRESHOLD = 40       # tendency_3pt > 40 → shooter profile
_DRIVE_THRESHOLD = 35             # tendency_drive > 35 → slasher profile
_BLK_THRESHOLD = 30              # blk_tendency > 30 → shot-blocker profile
_DEFENSE_LOW_USAGE_THRESHOLD = 40 # defense_tendency threshold for wing-stopper eligibility

# Veto-loop safety cap: if the user vetoes this many times without accepting,
# the last derivation is accepted automatically to avoid infinite loops.
MAX_VETO_ATTEMPTS = 7


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
    primary_pool_ids = top_3_guard_wing_ids or top_3_ids  # fallback to full top-3 if no guards

    primary_role_filled = False
    for pid in primary_pool_ids:  # already ordered highest OVR first
        if pid in assigned:
            continue
        p = next(x for x in players if x["player_id"] == pid)
        rank = ovr_rank_map[pid]
        # Pick the best-fitting role for THIS player specifically
        best_role_score = -9999.0
        best_role = "iso_scorer"
        for role in _guard_wing_primary_roles:
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
    _depth_role_names = frozenset({"end_of_bench", "developmental", "veteran_mentor"})
    exclusions: set[str] = set()
    if primary_role_filled:
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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def derive_roles(
    conn,
    league_id: int,
    team_id: int,
    season: int,
    *,
    philosophy: Optional[str] = None,
) -> list[dict]:
    """Compute role assignments for one team WITHOUT persisting.

    Returns: [{player_id, role, touch_share, rationale}, ...] with touch_share
    normalised so the team total equals 1.0.  The rationale includes a
    [philosophy: <name>] marker for any non-tendency_respecter team.
    """
    # Read philosophy from teams table if not passed explicitly
    if philosophy is None:
        row = await conn.fetchrow(
            "SELECT coach_philosophy FROM teams WHERE id = $1",
            team_id,
        )
        philosophy = row["coach_philosophy"] if row else "tendency_respecter"

    # All non-implemented philosophies fall back to tendency_respecter in Phase 1.
    if philosophy not in COACH_PHILOSOPHIES:
        log.warning("role_service: unknown philosophy '%s', falling back", philosophy)
        philosophy = "tendency_respecter"

    # Fetch rostered players (slots 1-15)
    rows = await conn.fetch(
        """
        SELECT
            p.id            AS player_id,
            p.overall,
            p.birth_date,
            p.position,
            p.tendency_3pt,
            p.tendency_drive,
            p.tendency_pass,
            p.ast_tendency,
            p.reb_tendency,
            p.blk_tendency,
            p.stl_tendency,
            p.defense_tendency,
            p.usage_weight,
            p.defensive_archetype,
            l.slot
        FROM lineups l
        JOIN players p ON p.id = l.player_id
        WHERE l.league_id = $1
          AND l.team_id   = $2
          AND l.slot BETWEEN 1 AND 15
        ORDER BY l.slot
        """,
        league_id,
        team_id,
    )

    if not rows:
        log.warning(
            "role_service: no roster rows for league=%d team=%d season=%d",
            league_id, team_id, season,
        )
        return []

    players = []
    for r in rows:
        age = _age_from_birth(r["birth_date"], season)
        players.append({
            "player_id": r["player_id"],
            "overall": r["overall"],
            "birth_date": r["birth_date"],
            "_age": age,
            "position": r["position"],
            "tendency_3pt": r["tendency_3pt"],
            "tendency_drive": r["tendency_drive"],
            "tendency_pass": r["tendency_pass"],
            "ast_tendency": r["ast_tendency"],
            "reb_tendency": r["reb_tendency"],
            "blk_tendency": r["blk_tendency"],
            "stl_tendency": r["stl_tendency"],
            "defense_tendency": r["defense_tendency"],
            "usage_weight": r["usage_weight"],
            "defensive_archetype": r["defensive_archetype"],
            "slot": r["slot"],
        })

    team_context = {
        "league_id": league_id,
        "team_id": team_id,
        "season": season,
        "philosophy": philosophy,
    }

    # Phase 3: philosophy bias injected via team_context; all derivations route
    # through _derive_tendency_respecter which threads team_context into scoring.
    assignments = _derive_tendency_respecter(players, team_context)

    # Normalise touch_share so team total = 1.0
    total = sum(a["touch_share"] for a in assignments)
    if total > 0:
        for a in assignments:
            a["touch_share"] = round(a["touch_share"] / total, 4)

    return assignments


async def persist_roles(
    conn,
    league_id: int,
    team_id: int,
    season: int,
    assignments: list[dict],
) -> None:
    """UPSERT assignments into player_roles.

    Rows where locked=TRUE are left untouched — human overrides survive
    an automated re-derive.
    """
    # Belt-and-suspenders: catch invalid role names before they hit the DB CHECK
    # constraint (added in migration 041).  Keeps error context rich.
    for a in assignments:
        if a["role"] not in ROLE_REGISTRY:
            raise ValueError(
                f"Invalid role {a['role']!r} for player {a['player_id']} "
                f"(league={league_id} team={team_id} season={season})"
            )

    for a in assignments:
        await conn.execute(
            """
            INSERT INTO player_roles
                (league_id, team_id, season, player_id, role, touch_share, rationale,
                 assigned_by, locked, assigned_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'cpu', FALSE, NOW())
            ON CONFLICT (league_id, team_id, season, player_id) DO UPDATE
                SET role        = EXCLUDED.role,
                    touch_share = EXCLUDED.touch_share,
                    rationale   = EXCLUDED.rationale,
                    assigned_by = EXCLUDED.assigned_by,
                    assigned_at = NOW()
                WHERE player_roles.locked = FALSE
            """,
            league_id,
            team_id,
            season,
            a["player_id"],
            a["role"],
            a["touch_share"],
            a.get("rationale", ""),
        )


async def get_or_derive_roles(
    conn,
    league_id: int,
    team_id: int,
    season: int,
) -> list[dict]:
    """Return stored role assignments; derive + persist if none exist for this team/season."""
    rows = await conn.fetch(
        """
        SELECT player_id, role, touch_share, rationale
        FROM player_roles
        WHERE league_id = $1 AND team_id = $2 AND season = $3
        ORDER BY touch_share DESC
        """,
        league_id, team_id, season,
    )
    if rows:
        return [dict(r) for r in rows]

    assignments = await derive_roles(conn, league_id, team_id, season)
    await persist_roles(conn, league_id, team_id, season, assignments)
    return assignments


async def _fetch_player_display(conn, league_id: int, team_id: int, player_ids: list[int]) -> dict[int, dict]:
    """Return {player_id: {name, position, overall}} for display in ride-along prompts."""
    if not player_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT p.id,
               p.first_name || ' ' || p.last_name AS name,
               p.position,
               p.overall
        FROM players p
        JOIN lineups l ON l.player_id = p.id
        WHERE l.league_id = $1 AND l.team_id = $2 AND p.id = ANY($3::int[])
        """,
        league_id, team_id, player_ids,
    )
    return {r["id"]: {"name": r["name"], "position": r["position"], "overall": r["overall"]} for r in rows}


async def _enrich_assignments(conn, league_id: int, team_id: int, assignments: list[dict]) -> list[dict]:
    """Merge player display info (name, position, overall) into assignment dicts for ride-along."""
    pids = [a["player_id"] for a in assignments]
    display = await _fetch_player_display(conn, league_id, team_id, pids)
    enriched = []
    for a in assignments:
        info = display.get(a["player_id"], {})
        enriched.append({**a, **info})
    return enriched


async def derive_and_persist_all_for_team(
    conn,
    league_id: int,
    team_id: int,
    season: int,
    *,
    silent_emit: bool = False,
) -> None:
    """Re-derive and persist role assignments for a single team.

    Called after roster changes (trades, free agent signings) to keep
    player_roles consistent with the new lineup.  Existing locked=TRUE rows
    are preserved by persist_roles' UPSERT guard.

    When DBA_RIDE_ALONG=1 and RIDE_ALONG_ROLE_PAUSE!=0, surfaces the
    assignment table interactively.  If the user types 'v', re-derives with a
    different philosophy and loops until accepted (capped at MAX_VETO_ATTEMPTS).

    Parameters
    ----------
    silent_emit : bool
        When True and ride-along is active, role events are written to the JSONL
        log but the input() pause is skipped.  Pass True from trade-execution
        call sites so automated re-derives don't block mid-trade.  Manual calls
        (e.g. /coach assign-role) should leave this False (default) so the user
        sees each result interactively.
    """
    from services.ride_along import is_role_pause_enabled, emit_role_assignment, emit_role_change

    # Snapshot existing roles BEFORE any writes so we can detect deltas later.
    prior_rows = await conn.fetch(
        """
        SELECT player_id, role, touch_share
        FROM player_roles
        WHERE league_id = $1 AND team_id = $2 AND season = $3
        """,
        league_id, team_id, season,
    )
    prior_map: dict[int, dict] = {r["player_id"]: dict(r) for r in prior_rows}

    # Fetch team info for ride-along display.
    team_row = await conn.fetchrow(
        "SELECT nba_team_code, coach_philosophy FROM teams WHERE id = $1", team_id
    )
    team_code = team_row["nba_team_code"] if team_row else f"team#{team_id}"
    base_philosophy = (team_row["coach_philosophy"] if team_row else None) or "tendency_respecter"

    try:
        # Veto loop: if ride-along role pause is on (and not silent), let the user
        # re-derive with a different philosophy seed until they accept.
        # Cap at MAX_VETO_ATTEMPTS to prevent an infinite loop.
        current_philosophy = base_philosophy
        veto_count = 0
        assignments: list[dict] = []
        while True:
            assignments = await derive_roles(
                conn, league_id, team_id, season, philosophy=current_philosophy
            )

            pause_active = is_role_pause_enabled() and not silent_emit
            if pause_active and assignments:
                enriched = await _enrich_assignments(conn, league_id, team_id, assignments)
                action = emit_role_assignment(
                    league_id=league_id,
                    team_id=team_id,
                    team_code=team_code,
                    philosophy=current_philosophy,
                    assignments=enriched,
                )
                if action == "veto":
                    veto_count += 1
                    if veto_count >= MAX_VETO_ATTEMPTS:
                        print(
                            f"   [veto limit ({MAX_VETO_ATTEMPTS}) reached for {team_code} — "
                            f"keeping last derivation with philosophy: {current_philosophy}]"
                        )
                    else:
                        # Re-randomize to a DIFFERENT philosophy for this team only.
                        other_philosophies = [p for p in COACH_PHILOSOPHIES if p != current_philosophy]
                        if not other_philosophies:
                            # Degenerate: only one philosophy in the list — stop looping.
                            print(f"   [no alternative philosophies available; accepting {current_philosophy}]")
                        else:
                            current_philosophy = random.choice(other_philosophies)
                            print(f"   [re-deriving {team_code} with philosophy: {current_philosophy}]")
                            continue  # loop back and re-derive
            elif is_role_pause_enabled() and silent_emit and assignments:
                # silent_emit path: log the assignment event to JSONL but skip input().
                enriched = await _enrich_assignments(conn, league_id, team_id, assignments)
                emit_role_assignment(
                    league_id=league_id,
                    team_id=team_id,
                    team_code=team_code,
                    philosophy=current_philosophy,
                    assignments=enriched,
                )
            break

        # Atomic write: DELETE stale rows + INSERT/UPDATE new rows in one transaction.
        # Keeping these in the same transaction means a Ctrl-C or unexpected error
        # during the veto loop (before this block) leaves the prior rows intact —
        # player_roles is never emptied before the replacement is confirmed.
        async with conn.transaction():
            await conn.execute(
                """
                DELETE FROM player_roles
                WHERE league_id = $1 AND team_id = $2 AND season = $3
                  AND player_id NOT IN (
                      SELECT player_id FROM lineups
                      WHERE league_id = $1 AND team_id = $2
                  )
                """,
                league_id, team_id, season,
            )
            await persist_roles(conn, league_id, team_id, season, assignments)

        # Detect and surface role deltas (changes vs prior state).
        # Always log to JSONL; only pause for input when not in silent mode.
        if prior_map and is_role_pause_enabled():
            display_map = await _fetch_player_display(
                conn, league_id, team_id, [a["player_id"] for a in assignments]
            )
            deltas: list[dict] = []
            for a in assignments:
                pid = a["player_id"]
                prior = prior_map.get(pid)
                if prior and prior["role"] != a["role"]:
                    info = display_map.get(pid, {})
                    deltas.append({
                        "player_id": pid,
                        "name": info.get("name", f"player#{pid}"),
                        "old_role": prior["role"],
                        "old_touch_share": prior["touch_share"],
                        "new_role": a["role"],
                        "new_touch_share": a["touch_share"],
                    })
            if deltas:
                emit_role_change(
                    league_id=league_id,
                    team_id=team_id,
                    team_code=team_code,
                    philosophy=current_philosophy,
                    deltas=deltas,
                    reason="Post-trade / lineup re-derive",
                    pause=not silent_emit,
                )

        log.debug(
            "role_service: re-derived roles for league=%d team=%d season=%d",
            league_id, team_id, season,
        )
    except Exception as exc:
        log.warning(
            "role_service: derive_and_persist_all_for_team failed "
            "league=%d team=%d season=%d — %s",
            league_id, team_id, season, exc,
        )


async def derive_and_persist_all(conn, league_id: int, season: int) -> None:
    """Derive and persist role assignments for every team in the league.

    Called at season turnover from franchise_plan_service.derive_and_persist_all.
    When ride-along role pauses are active, each team's assignments are surfaced
    interactively with veto-loop support (same as derive_and_persist_all_for_team).
    """
    from services.ride_along import is_role_pause_enabled, emit_role_assignment

    team_rows = await conn.fetch(
        """
        SELECT id, nba_team_code, coach_philosophy
        FROM teams WHERE league_id = $1 ORDER BY nba_team_code
        """,
        league_id,
    )
    count = 0
    for t in team_rows:
        try:
            team_code = t["nba_team_code"]
            current_philosophy = (t["coach_philosophy"] or "tendency_respecter")
            veto_count = 0

            while True:
                assignments = await derive_roles(
                    conn, league_id, t["id"], season, philosophy=current_philosophy
                )

                if is_role_pause_enabled() and assignments:
                    enriched = await _enrich_assignments(conn, league_id, t["id"], assignments)
                    action = emit_role_assignment(
                        league_id=league_id,
                        team_id=t["id"],
                        team_code=team_code,
                        philosophy=current_philosophy,
                        assignments=enriched,
                    )
                    if action == "veto":
                        veto_count += 1
                        if veto_count >= MAX_VETO_ATTEMPTS:
                            print(
                                f"   [veto limit ({MAX_VETO_ATTEMPTS}) reached for {team_code} — "
                                f"keeping last derivation with philosophy: {current_philosophy}]"
                            )
                        else:
                            other_philosophies = [p for p in COACH_PHILOSOPHIES if p != current_philosophy]
                            if not other_philosophies:
                                print(f"   [no alternative philosophies available; accepting {current_philosophy}]")
                            else:
                                current_philosophy = random.choice(other_philosophies)
                                print(f"   [re-deriving {team_code} with philosophy: {current_philosophy}]")
                                continue
                break

            await persist_roles(conn, league_id, t["id"], season, assignments)
            count += 1
        except Exception as exc:
            log.warning(
                "role_service derive failed: league=%d team=%s season=%d — %s",
                league_id, t["nba_team_code"], season, exc,
            )
    log.info(
        "derive_and_persist_all: league=%d season=%d roles derived for %d/%d teams",
        league_id, season, count, len(team_rows),
    )
