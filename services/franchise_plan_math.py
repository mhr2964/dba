"""Pure decision/classification logic for franchise plan derivation: goal
derivation, reassessment/pivot gating, player categorisation (core/flex/
surplus), and production-tier classification. No DB, no async, no I/O.

Extracted from franchise_plan_service.py (Phase 3 opportunistic split, see
HANDOFF.md) along with franchise_plan_production.py.
"""
from __future__ import annotations

import datetime
from typing import Optional


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_TOTAL_SEASON_GAMES = 82


def _project_wins(wins: int, losses: int, games_played: int) -> float:
    """Linear pace projection.  Returns wins value if no games played yet."""
    if games_played <= 0:
        return float(wins)
    return wins / games_played * _TOTAL_SEASON_GAMES


def _calc_age(birth_date: Optional[datetime.date], season: int) -> Optional[float]:
    """Rough age from birth_date relative to season-start (October of that year)."""
    if birth_date is None:
        return None
    season_start = datetime.date(season, 10, 1)
    return (season_start - birth_date).days / 365.25


# ---------------------------------------------------------------------------
# Phase 4: checkpoint detection
# ---------------------------------------------------------------------------

# Trade-deadline window: games 55–65 of the regular season.
_DEADLINE_WINDOW_START = 55


_DEADLINE_WINDOW_END = 65


# Minimum games played before a mid-season pivot can fire.
_PIVOT_MIN_GAMES = 30


def _is_reassessment_checkpoint(
    games_played: int,
    games_remaining: int,
    last_derived_game_index: Optional[int],
) -> tuple[bool, str]:
    """Return (should_reassess, reason).

    Reassessment gates (evaluated in priority order):
    - 'initial'              — plan has never been derived for this season
    - 'offseason_start'      — regular season just ended (games_remaining == 0)
    - 'trade_deadline'       — games_played in [55, 65] AND not already derived
                               inside that window
    - 'mid_season_checkpoint'— outside the above windows; allow only pivot-driven
                               reassessment (caller must still call _should_pivot)
    - 'sticky'               — none of the above fired; keep existing plan
    """
    if last_derived_game_index is None:
        return True, "initial"

    if games_remaining == 0:
        return True, "offseason_start"

    in_deadline_window = _DEADLINE_WINDOW_START <= games_played <= _DEADLINE_WINDOW_END
    already_derived_in_window = (
        last_derived_game_index is not None
        and _DEADLINE_WINDOW_START <= last_derived_game_index <= _DEADLINE_WINDOW_END
    )
    if in_deadline_window and not already_derived_in_window:
        return True, "trade_deadline"

    if games_played >= _PIVOT_MIN_GAMES:
        return True, "mid_season_checkpoint"

    return False, "sticky"


# ---------------------------------------------------------------------------
# Phase 4: pivot eligibility
# ---------------------------------------------------------------------------

# Projected-win thresholds that define when a plan's strategy has broken down.
_TANK_PIVOT_WIN_FLOOR = 38


_WIN_NOW_COLLAPSE_CEIL = 40


                                # (contenders start ≥47W projected; 40 = clear miss on
                                # top-4 seed, matches real-NBA pivot triggers)
_TRANSITION_OVERPERFORM_FLOOR = 50


# Rebuild → tank requires R1 pick accumulation (checked against draft_picks table).
_REBUILD_TANK_R1_THRESHOLD = 2


def _should_pivot(
    old_plan: dict,
    current_record: dict,
    r1_picks_banked: int,
) -> tuple[bool, Optional[str], str]:
    """Return (should_pivot, new_goal_if_pivoting, reason).

    Rules require a meaningful performance delta from the plan's expectation.
    Pivot on noise is suppressed by the threshold gaps between rules.
    """
    goal = old_plan.get("goal", "")
    projected_wins = current_record.get("projected_wins", 0.0)
    pw = round(projected_wins)

    if goal == "tank" and projected_wins >= _TANK_PIVOT_WIN_FLOOR:
        return (
            True,
            "rebuild",
            f"tank failed: projecting {pw}W, too competitive to land top draft slot",
        )

    if goal == "win_now" and projected_wins <= _WIN_NOW_COLLAPSE_CEIL:
        return (
            True,
            "transition",
            f"championship window collapsed: projecting {pw}W, no longer on pace for top-4 seed",
        )

    if goal == "transition" and projected_wins >= _TRANSITION_OVERPERFORM_FLOOR:
        return (
            True,
            "win_now",
            f"exceeded expectations at {pw}W projection — pivot to win-now",
        )

    if goal == "rebuild" and r1_picks_banked >= _REBUILD_TANK_R1_THRESHOLD:
        return (
            True,
            "tank",
            f"{r1_picks_banked} R1 picks now banked — tank trajectory coherent",
        )

    return False, None, ""


# ---------------------------------------------------------------------------
# Goal derivation
# ---------------------------------------------------------------------------

def _derive_goal_and_horizon(
    projected_wins: float,
    avg_age: float,
    has_young_star: bool,      # OVR >= 88 AND age <= 25
    has_elite_star: bool,       # OVR >= 90
    has_any_star: bool,         # OVR >= 88
    r1_picks_next3: int,
    games_played: int,
    ovr_list: list[int],
    prime_age_star: bool = False,  # OVR >= 88 AND 26 <= age <= 28
    mode: str = "developing",      # from compute_team_mode; used as tie-breaker
    conf_rank: int | None = None,
) -> tuple[str, int]:
    """Return (goal, horizon_seasons).  Priority order: win_now → tank → rebuild → transition.

    transition is reserved for the genuinely ambiguous middle: ~35-44 wins,
    mixed-age roster, no clear star, no obvious direction.  The widened
    win_now and rebuild gates below are intentionally designed to keep
    transition rare rather than the catch-all default.
    """

    pw = projected_wins  # shorthand for readability
    in_top4 = conf_rank is not None and conf_rank <= 4

    # ---- edge case: very early season with no record ----
    if games_played < 10:
        if avg_age >= 30 and has_any_star:
            return "win_now", 1
        if avg_age <= 24:
            if ovr_list and max(ovr_list) <= 75:
                return "rebuild", 3
            return "tank", 2
        # play_in_fringe team with any star is already pushing for a playoff spot.
        # Same logic as the post-record check (line below games_played >= 10 block)
        # but applied preseason so the plan doesn't fall through to "transition".
        if mode == "play_in_fringe" and has_any_star:
            return "win_now", 2
        # Mode-guided early disambiguation instead of defaulting to transition
        if mode in ("soft_rebuild", "rebuilding"):
            return "rebuild", 3
        if mode == "contending":
            return "win_now", 2
        return "transition", 2

    # ---- 1. WIN_NOW ----
    # Prime-window check: 26-28yo star on a 47+-win trajectory is the most common
    # championship-window case (e.g. Embiid at 29, 47W pace → contender, not transition).
    # Must come before the avg_age≥29 branch so it catches the prime window explicitly.
    if prime_age_star and pw >= 47:
        horizon = 1 if pw >= 55 else 2
        return "win_now", horizon

    if (
        (has_young_star and pw >= 42)           # widened from 45 — legitimate young-star contenders
        or (has_elite_star and pw >= 42)        # widened from 50 — elite star + winning = win_now
        or (has_any_star and pw >= 45)          # new: any star (OVR≥88) + 45-win pace
        or (avg_age >= 29 and pw >= 45)
        or (in_top4 and pw >= 40)              # top-4 conf + 40W = playing above expectation
    ):
        horizon = 1 if pw >= 55 else 2
        return "win_now", horizon

    # Mode-guided win_now: play_in_fringe team with any star is pushing, not treading water
    if mode == "play_in_fringe" and has_any_star:
        return "win_now", 2

    # ---- 2. TANK ----
    # Two coherent-tank paths:
    # A) Picks already banked: ≥2 R1 picks in hand — traditional tank trajectory.
    # B) Young + losing with no star: team MUST tank to ACQUIRE foundational picks.
    #    (pre-2023 OKC model — you commit to tank before the picks arrive, not after.)
    qualifies_tank_record = (
        (pw < 30 and avg_age < 25.5)
        or (pw < 28 and avg_age < 27)
        or (pw < 30 and not has_any_star and avg_age >= 26)  # widened path B gate
    )
    if qualifies_tank_record:
        if r1_picks_next3 >= 2:
            return "tank", 2
        # Path B: young roster with no star + losing record → tank to acquire picks.
        has_no_star = not has_any_star
        if avg_age <= 24 and has_no_star and pw < 30:
            return "tank", 2
        # Mode-guided: developing + young + low projected wins → tank
        if mode == "developing" and avg_age < 25 and pw < 35:
            return "tank", 2
        # Fewer than 2 R1 picks, doesn't qualify path B — fall through to rebuild.

    # ---- 3. REBUILD ----
    if (
        (pw < 30 and avg_age >= 27)
        or (30 <= pw <= 35 and avg_age >= 29)
        or (pw < 35 and avg_age >= 28)          # old + losing = rebuild, not transition
        or (pw < 30 and not has_any_star and avg_age >= 26)  # no foundation + losing
    ):
        return "rebuild", 3

    # Mode-guided rebuild: soft_rebuild mode already signals the team is trending down
    if mode == "soft_rebuild":
        return "rebuild", 3

    # ---- 4. TRANSITION — genuinely ambiguous middle ----
    # Intentionally narrow: 35-44 wins, mixed age, no clear star or direction.
    # Horizon capped at 2 because this window rarely extends further.
    return "transition", 2


_TIER_RANK: dict[str, int] = {"unknown": 0, "depth": 1, "role": 2, "producer": 3, "star": 4}


_DEFENSIVE_ARCHETYPES: frozenset[str] = frozenset(
    {"rim_protector", "wing_stopper", "on_ball_pest", "two_way_big", "switching_big"}
)


def _production_tier(stats: dict) -> str:
    """Classify a player's offensive production level.

    Returns one of: 'star' | 'producer' | 'role' | 'depth' | 'unknown'.

    'unknown' fires when GP < 10 (early season) or stats dict is absent —
    callers must fall back to OVR-based bucketing for 'unknown' players.
    Reads only offensive columns (ppg/apg/rpg) — backward-compatible with
    the extended dict that also includes defensive columns.
    """
    if not stats or stats.get("gp", 0) < 10:
        return "unknown"
    ppg = stats.get("ppg", 0.0)
    apg = stats.get("apg", 0.0)
    rpg = stats.get("rpg", 0.0)

    # Star: clearly elite in at least one dimension.
    if ppg >= 22 or apg >= 8 or rpg >= 11:
        return "star"
    # Producer: meaningful contribution at one dimension.
    if ppg >= 16 or apg >= 6 or rpg >= 10:
        return "producer"
    # Role: supporting but impactful.
    if ppg >= 10 or apg >= 4 or rpg >= 6:
        return "role"
    return "depth"


def _defensive_tier(stats: dict) -> str:
    """Classify a player's defensive production level.

    Returns one of: 'star' | 'producer' | 'role' | 'depth' | 'unknown'.

    'unknown' fires when GP < 10 or MPG < 18 — low-minute samples are
    unreliable (1.5 BPG in 12 MPG is noise, not rim protection).
    """
    if not stats or stats.get("gp", 0) < 10 or (stats.get("mpg") or 0) < 18:
        return "unknown"
    bpg  = stats.get("bpg",  0) or 0
    spg  = stats.get("spg",  0) or 0
    drpg = stats.get("drpg", 0) or 0

    # Elite defensive star: rim-protector or perimeter-disruptor levels.
    if bpg >= 2.0 or spg >= 2.0:
        return "star"
    # Defensive producer: clear plus-defender.
    if bpg >= 1.2 or spg >= 1.5 or drpg >= 7.0:
        return "producer"
    # Defensive role: contributing but not a floor anchor.
    if bpg >= 0.7 or spg >= 1.0 or drpg >= 5.0:
        return "role"
    return "depth"


def _combined_tier(
    off_tier: str, def_tier: str, archetype: str | None = None
) -> str:
    """Return the highest of offensive and defensive tier.

    Defensive-archetype bump: a player explicitly tagged as a defensive
    specialist (rim_protector / wing_stopper / on_ball_pest / two_way_big /
    switching_big) and who registers at least 'role' defensive tier gets
    bumped one additional notch — their defensive identity is intentional, not
    incidental.  The bump is capped at 'star' (no tier above that).
    """
    best = max(off_tier, def_tier, key=lambda t: _TIER_RANK.get(t, 0))

    if (
        archetype in _DEFENSIVE_ARCHETYPES
        and _TIER_RANK.get(def_tier, 0) >= _TIER_RANK["role"]
    ):
        bumped_rank = min(_TIER_RANK[best] + 1, _TIER_RANK["star"])
        # Reverse-lookup tier name by rank.
        for name, rank in _TIER_RANK.items():
            if rank == bumped_rank:
                return name

    return best


# ---------------------------------------------------------------------------
# Player categorisation
# ---------------------------------------------------------------------------

def _categorise_players(
    goal: str,
    roster: list[dict],  # each: {player_id, age, overall, position}
    avg_age: float = 27.0,
    *,
    production_map: "dict[int, dict] | None" = None,
    archetype_map: "dict[int, str | None] | None" = None,
    recently_acquired_ids: "set[int] | None" = None,
) -> tuple[list[int], list[int], list[int], dict[int, str], dict[int, str]]:
    """Return (core_ids, flex_ids, surplus_ids, youth_overrides, shop_intent).

    youth_overrides maps player_id → reason string when the youth-cornerstone
    rule forced a contender's young high-OVR player into CORE.

    shop_intent maps surplus player_id → reason string explaining WHY the player
    is surplus.  Controlled vocabulary:
      "age_misfit"        — age outside the team's build window
      "positional_logjam" — multiple surplus players at the same position
      "flip_asset"        — recently acquired and already categorised as surplus
      "other"             — fallback when no other reason applies

    recently_acquired_ids (keyword-only, default empty):
      Set of player IDs acquired within the last N sim games.  When a player is
      both recently_acquired and classified as surplus they receive "flip_asset"
      intent — the team acquired them intending to flip for a better asset.

    For transition, win_now, rebuild, and soft_rebuild-style goals, core candidates
    are filtered by an age window before taking the top-N by OVR.  Players who would
    otherwise rank into core but fall outside the team's build-window age are pushed
    to surplus ("age-misfit vets in surplus") so the CPU knows to flip them.

    Age window per goal:
      win_now    : avg_age ± 4 years (anyone wildly outside the window isn't core)
      transition : avg_age ± 4 years
      rebuild    : age ≤ team_avg_age + 2 (older players are flip candidates)
      tank/soft_rebuild: age-aware rules already embedded in their own blocks

    production_map (keyword-only, default {}):
      {player_id: {ppg, apg, rpg, bpg, spg, drpg, mpg, gp}} from _fetch_season_production.
      After OVR/age bucketing completes, a production override pass fires using the
      combined offensive + defensive tier (_combined_tier):
        - 'star' combined tier     → forced into core (removes from surplus/flex).
        - 'producer' combined tier → minimum flex (removed from surplus).
      Players with 'unknown' combined tier (GP < 10 or absent) are untouched.
      A pure-defense big (Gobert-type) with 'unknown' offensive but 'producer'
      defensive tier will be rescued from surplus via the combined tier.

    archetype_map (keyword-only, default {}):
      {player_id: defensive_archetype_str | None}.
      Feeds _combined_tier's archetype bump — defensive specialists with ≥ 'role'
      defensive tier get one additional tier bump.
    """

    core: list[int] = []
    surplus: list[int] = []
    flex: list[int] = []
    # Tracks which surplus player IDs were placed due to age window violations,
    # so shop_intent can label them "age_misfit" accurately.
    _age_misfit_surplus_ids: set[int] = set()

    sorted_by_ovr = sorted(roster, key=lambda p: p["overall"], reverse=True)

    if goal == "win_now":
        # Age window: avg_age ± 4
        age_min = avg_age - 4
        age_max = avg_age + 4
        # Core: top 3 OVR within age window; always include young stars (OVR 88+ age <= 25)
        age_fit_candidates = [
            p for p in sorted_by_ovr
            if p["age"] is None or (age_min <= p["age"] <= age_max)
        ]
        top3_ids = {p["player_id"] for p in age_fit_candidates[:3]}
        young_star_ids = {
            p["player_id"] for p in roster
            if p["overall"] >= 88 and p["age"] is not None and p["age"] <= 25
        }
        core_set = top3_ids | young_star_ids
        core = [p["player_id"] for p in sorted_by_ovr if p["player_id"] in core_set]
        # Age-misfit players who would rank into top-3 by OVR but don't fit window → surplus.
        top3_by_ovr_ids = {p["player_id"] for p in sorted_by_ovr[:3]}
        age_misfit_ids = top3_by_ovr_ids - core_set
        surplus = [
            p["player_id"] for p in roster
            if p["player_id"] in age_misfit_ids
        ]
        _age_misfit_surplus_ids.update(age_misfit_ids)
        # Also surplus: age >= 31 AND OVR < 78, not already placed
        _extra_age_surplus_win_now = [
            p["player_id"] for p in roster
            if p["age"] is not None and p["age"] >= 31 and p["overall"] < 78
            and p["player_id"] not in core_set and p["player_id"] not in age_misfit_ids
        ]
        surplus += _extra_age_surplus_win_now
        _age_misfit_surplus_ids.update(_extra_age_surplus_win_now)

    elif goal == "tank":
        # Core: U22 with OVR >= 78
        core_set = {
            p["player_id"] for p in roster
            if p["age"] is not None and p["age"] < 22 and p["overall"] >= 78
        }
        core = [p["player_id"] for p in sorted_by_ovr if p["player_id"] in core_set]
        # Surplus: age >= 27 AND OVR >= 78 (vets who win too many games)
        _tank_surplus = [
            p["player_id"] for p in roster
            if p["age"] is not None and p["age"] >= 27 and p["overall"] >= 78
            and p["player_id"] not in core_set
        ]
        surplus = _tank_surplus
        _age_misfit_surplus_ids.update(_tank_surplus)

    elif goal == "rebuild":
        # Age window: age ≤ avg_age + 2 for core eligibility
        age_ceiling = avg_age + 2
        age_fit_candidates = [
            p for p in sorted_by_ovr
            if p["age"] is None or p["age"] <= age_ceiling
        ]
        # Core: U23 with OVR >= 75 (original rule, within the rebuild age ceiling)
        core_set = {
            p["player_id"] for p in age_fit_candidates
            if p["age"] is not None and p["age"] < 23 and p["overall"] >= 75
        }
        # Fallback: if core is empty, take top-1 OVR from age-fit candidates regardless of age.
        if not core_set and age_fit_candidates:
            core_set = {age_fit_candidates[0]["player_id"]}
        core = [p["player_id"] for p in sorted_by_ovr if p["player_id"] in core_set]
        # Age-misfit vets (above ceiling, not already core) → surplus
        age_misfit_ids = {
            p["player_id"] for p in roster
            if p["age"] is not None and p["age"] > age_ceiling
            and p["player_id"] not in core_set
        }
        _age_misfit_surplus_ids.update(age_misfit_ids)
        # Surplus: age >= 28 OR OVR < 72 (non-future pieces), excluding core
        _rebuild_age_surplus = [
            p["player_id"] for p in roster
            if p["player_id"] not in core_set
            and p["player_id"] not in age_misfit_ids
            and (p["age"] is not None and p["age"] >= 28)
        ]
        surplus = [
            p["player_id"] for p in roster
            if p["player_id"] not in core_set
            and (
                p["player_id"] in age_misfit_ids
                or (p["age"] is not None and p["age"] >= 28)
                or p["overall"] < 72
            )
        ]
        _age_misfit_surplus_ids.update(_rebuild_age_surplus)

    else:  # transition
        # Age window: avg_age ± 4
        age_min = avg_age - 4
        age_max = avg_age + 4
        # Core: top 2 OVR within age window.
        age_fit_candidates = [
            p for p in sorted_by_ovr
            if p["age"] is None or (age_min <= p["age"] <= age_max)
        ]
        core_set = {p["player_id"] for p in age_fit_candidates[:2]}
        # Fallback: if core is empty (all top players are outliers), take top-1 OVR.
        if not core_set and sorted_by_ovr:
            core_set = {sorted_by_ovr[0]["player_id"]}
        core = [p["player_id"] for p in sorted_by_ovr if p["player_id"] in core_set]
        # Age-misfit vets — top-2 OVR who were excluded by the age window → surplus.
        top2_by_ovr_ids = {p["player_id"] for p in sorted_by_ovr[:2]}
        age_misfit_ids = top2_by_ovr_ids - core_set
        surplus = [
            p["player_id"] for p in roster
            if p["player_id"] in age_misfit_ids
        ]
        _age_misfit_surplus_ids.update(age_misfit_ids)
        # Also surplus: age >= 32 AND OVR >= 75, not already placed
        _extra_age_surplus_transition = [
            p["player_id"] for p in roster
            if p["age"] is not None and p["age"] >= 32 and p["overall"] >= 75
            and p["player_id"] not in core_set and p["player_id"] not in age_misfit_ids
        ]
        surplus += _extra_age_surplus_transition
        _age_misfit_surplus_ids.update(_extra_age_surplus_transition)

    allocated = set(core) | set(surplus)
    # Flex: OVR 75-87 not already placed, not low-OVR bench
    flex = [
        p["player_id"] for p in sorted_by_ovr
        if 75 <= p["overall"] <= 87 and p["player_id"] not in allocated
    ]

    # ------------------------------------------------------------------
    # Production override — producers must never land in surplus.
    #
    # This runs AFTER OVR/age bucketing so it can't be suppressed by any
    # goal-specific logic (win_now age windows, tank vet purge, etc.).
    # Only players with enough games to trust the data (GP >= 10) are
    # eligible; early-season players remain in their OVR-assigned bucket.
    #
    # Combined tier is the max of offensive and defensive tier, with an
    # extra archetype bump for confirmed defensive specialists.  This ensures
    # pure-defense players (Gobert, Allen, wing stoppers) are never shipped
    # as surplus on the grounds that they score only 8 PPG.
    # ------------------------------------------------------------------
    _archetype_map: dict = archetype_map or {}
    if production_map:
        for pid, stats in production_map.items():
            off_tier = _production_tier(stats)
            def_tier = _defensive_tier(stats)
            archetype = _archetype_map.get(pid)
            tier = _combined_tier(off_tier, def_tier, archetype)
            if tier == "star":
                # Force into core regardless of existing bucket.
                if pid in surplus:
                    surplus.remove(pid)
                if pid in flex:
                    flex.remove(pid)
                if pid not in core:
                    core.append(pid)
            elif tier == "producer":
                # At minimum flex — never surplus.
                if pid in surplus:
                    surplus.remove(pid)
                    if pid not in flex and pid not in core:
                        flex.append(pid)

    # ------------------------------------------------------------------
    # Youth + OVR cornerstone override — contenders only.
    # A win_now team trading away a 24-year-old OVR 84 PG for older role
    # players is exactly the bug this guards against.  Production tier
    # may miss when sim PPG/APG sits just below thresholds; raw OVR + age
    # catches the cornerstone case regardless.
    # ------------------------------------------------------------------
    _CONTENDER_GOALS: frozenset[str] = frozenset({"win_now"})
    youth_overrides: dict[int, str] = {}
    if goal in _CONTENDER_GOALS:
        for p in roster:
            pid = p["player_id"]
            ovr = p.get("overall") or 0
            age = p.get("age") or 28
            if ovr >= 82 and age <= 26:
                if pid in surplus:
                    surplus.remove(pid)
                if pid in flex:
                    flex.remove(pid)
                if pid not in core:
                    core.append(pid)
                youth_overrides[pid] = "young cornerstone (OVR≥82, age≤26 on contender)"

    # ------------------------------------------------------------------
    # Compute shop_intent for each surplus player.
    #
    # Priority order when multiple reasons could apply:
    #   1. flip_asset   — recently acquired; team intends to re-sell quickly
    #   2. age_misfit   — age window violation (most common organic surplus)
    #   3. positional_logjam — multiple surplus players at the same position
    #   4. other        — fallback
    # ------------------------------------------------------------------
    _recently_acq: set[int] = recently_acquired_ids or set()

    # Count surplus players per position to detect positional logjam.
    _surplus_set = set(surplus)
    _position_for: dict[int, str] = {
        p["player_id"]: p.get("position", "") or ""
        for p in roster
    }
    _surplus_pos_counts: dict[str, int] = {}
    for pid in surplus:
        pos = _position_for.get(pid, "")
        if pos:
            _surplus_pos_counts[pos] = _surplus_pos_counts.get(pos, 0) + 1
    _logjam_positions: set[str] = {
        pos for pos, cnt in _surplus_pos_counts.items() if cnt >= 2
    }

    shop_intent: dict[int, str] = {}
    for pid in surplus:
        if pid in _recently_acq:
            shop_intent[pid] = "flip_asset"
        elif pid in _age_misfit_surplus_ids:
            shop_intent[pid] = "age_misfit"
        elif _position_for.get(pid, "") in _logjam_positions:
            shop_intent[pid] = "positional_logjam"
        else:
            shop_intent[pid] = "other"

    return core, flex, surplus, youth_overrides, shop_intent


# ---------------------------------------------------------------------------
# Asset targets
# ---------------------------------------------------------------------------

_ASSET_TARGETS: dict[str, list[str]] = {
    "win_now": ["role_players", "veterans"],
    "tank": ["picks_r1", "young_u23", "cap_space"],
    # Standardised on young_u23 (not young_u22) — matches cpu_trade_service _plan_bias token.
    "rebuild": ["picks_any", "young_u23", "expiring_contracts"],
    "transition": ["picks_r1"],
}


# ---------------------------------------------------------------------------
# Rationale builder
# ---------------------------------------------------------------------------

def _build_rationale(
    goal: str,
    star_name: Optional[str],
    avg_age: float,
    projected_wins: float,
    r1_picks_next3: int,
    target_year: int,
    has_any_star: bool = True,
) -> str:
    pw = round(projected_wins)
    age = round(avg_age, 1)

    if goal == "win_now":
        if star_name:
            return (
                f"{star_name} prime window + {pw}-win pace contending; "
                "core is set, target one difference-making upgrade."
            )
        return (
            f"Avg-age {age} veteran core at {pw}-win pace; "
            "last-gasp window, protect core and add proven contributors."
        )

    if goal == "tank":
        if r1_picks_next3 >= 2:
            # Picks-banked path — traditional tank.
            return (
                f"{r1_picks_next3} R1 picks banked + losing record + young — tank trajectory coherent; "
                f"accumulate more for {target_year} draft."
            )
        # No-picks path (young + losing + no star).
        return (
            f"Young roster ({age} avg, no OVR-88+ star) + {pw}-win pace — "
            "tank to acquire foundational pick assets."
        )

    if goal == "rebuild":
        return (
            f"Avg-age {age} roster not winning ({pw} pace); "
            "trade veterans for picks and young players, full reset."
        )

    # transition
    return (
        f"Competitive at {pw}-win pace but not contending (avg {age} yrs); "
        "selectively flip vets for first-round picks."
    )
