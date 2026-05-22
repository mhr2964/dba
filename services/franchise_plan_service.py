"""Franchise plan derivation and persistence.

A franchise_plan captures a CPU team's multi-year strategic direction:
  goal          — win_now | transition | rebuild | tank
  horizon       — seasons until expected payoff
  core/flex/surplus player buckets
  asset_targets — what the team is hunting in trades

Phase 1: plans are derived and stored only.
Phase 2 wires plans into trade decision logic.
Phase 3 adds targeted counterparty scanning before proposals.
Phase 4 adds reassessment checkpoints + pivot eligibility so plans carry
         strategic commitment between sim batches rather than recomputing
         blindly every time.

Public API
----------
derive_plan(pool, league_id, team_id, season) -> dict
    Compute without persisting.

persist_plan(pool, plan) -> int
    Upsert; returns plan id.

get_plan(pool, league_id, team_id, season) -> dict | None
    Read stored plan; None if absent.

get_or_derive(pool, league_id, team_id, season) -> dict
    Read existing; derive + persist if missing.

derive_and_persist_all(pool, league_id, season, current_game_index=None) -> int
    Bulk refresh for all CPU teams.  Phase 4: respects plan stickiness —
    only re-derives at checkpoint windows or when a pivot condition fires.
"""
from __future__ import annotations

import datetime
import json
import unicodedata
from pathlib import Path
from typing import Optional

from core.logging import get_logger
from data.repositories import franchise_plan_repo, player_repo, team_repo
from services import trade_evaluator as _trade_eval

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_TOTAL_SEASON_GAMES = 82  # used to project wins from current W/L pace

_BDL_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "bdl_cache"


def _norm_name(name: str) -> str:
    """Lowercase + strip accents + collapse whitespace + remove punctuation.

    Mirrors the normalisation used in backfill_overall_from_defense.py so that
    BDL cache look-ups are consistent with how the backfill script keys players.
    """
    return " ".join(
        unicodedata.normalize("NFKD", name)
        .encode("ascii", "ignore")
        .decode()
        .lower()
        .replace(".", "")
        .replace("'", "")
        .strip()
        .split()
    )


def _safe_ts_pct(s: dict) -> float:
    """Compute TS% from BDL stat dict with range validation.

    Prefers the pre-computed ts_pct field (advanced cache); falls back to
    PTS / (2 * (FGA + 0.44 * FTA)) from totals (base cache).  Returns 0.0 on
    divide-by-zero or if the result is outside [0.0, 1.0] (corrupt data).
    """
    if s.get("ts_pct"):
        val = float(s["ts_pct"])
        if 0.0 <= val <= 1.0:
            return val
        log.warning(
            "franchise_plan: BDL ts_pct=%s out of [0,1] range — skipping", val
        )
        return 0.0
    fga = float(s.get("fga") or 0)
    fta = float(s.get("fta") or 0)
    pts = float(s.get("pts") or 0)
    denom = 2.0 * (fga + 0.44 * fta)
    if denom <= 0:
        return 0.0
    val = pts / denom
    if not (0.0 <= val <= 1.0):
        log.warning(
            "franchise_plan: computed ts_pct=%s out of [0,1] range — skipping", val
        )
        return 0.0
    return val


def _load_bdl_production_fallback(
    player_rows: list[dict],
    current_season: int,
) -> dict[int, dict]:
    """Return {player_id: {ppg, apg, rpg, bpg, spg, drpg, mpg, gp}} from the
    most recent BDL cache file that pre-dates current_season.

    Tries current_season-1 first, then current_season-2.  Returns empty dict
    when no usable cache is found.  Never raises — all errors are logged as
    warnings so plan derivation is never interrupted by a missing/corrupt cache.

    BDL base cache JSON structure: list of
      {"player": {"first_name": ..., "last_name": ...}, "stats": {pts, ast, reb,
       blk, stl, dreb, min, gp, ...}}

    Players are matched by normalised full name (case-insensitive, accent-stripped).
    Name-match misses are silently skipped; the caller falls back to OVR/age logic.
    """
    if not player_rows:
        return {}

    for prior in (current_season - 1, current_season - 2):
        base_path = _BDL_CACHE_DIR / f"season_{prior}_base.json"
        if not base_path.exists():
            continue

        try:
            with base_path.open(encoding="utf-8") as fh:
                base_data = json.load(fh)
        except Exception as exc:
            log.warning(
                "franchise_plan: BDL fallback — could not load %s: %s",
                base_path.name,
                exc,
            )
            continue

        # Index BDL records by normalised name for O(1) look-up.
        bdl_by_name: dict[str, dict] = {}
        for rec in base_data:
            p = rec.get("player", {})
            s = rec.get("stats", {})
            full = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            key = _norm_name(full)
            if key:
                bdl_by_name[key] = s

        out: dict[int, dict] = {}
        for row in player_rows:
            key = _norm_name(
                f"{row.get('first_name', '')} {row.get('last_name', '')}".strip()
            )
            s = bdl_by_name.get(key)
            if s is None:
                continue
            gp_bdl  = int(s.get("gp") or 0)
            fg3a_bdl = float(s.get("fg3a") or 0.0)
            fg3a_pg_bdl = (fg3a_bdl / gp_bdl) if gp_bdl else 0.0
            # BDL base cache stores per-season totals; percentages are pre-computed.
            out[row["id"]] = {
                "ppg":    float(s.get("pts")     or 0),
                "apg":    float(s.get("ast")     or 0),
                "rpg":    float(s.get("reb")     or 0),
                "bpg":    float(s.get("blk")     or 0),
                "spg":    float(s.get("stl")     or 0),
                "drpg":   float(s.get("dreb")    or 0),
                "mpg":    float(s.get("min")     or 0),
                "gp":     gp_bdl,
                "fg_pct": float(s.get("fg_pct")  or 0),
                "fg3_pct":float(s.get("fg3_pct") or 0),
                "ft_pct": float(s.get("ft_pct")  or 0),
                # BDL advanced cache has ts_pct but base cache does not;
                # compute from fgm/fga/ftm/fta totals if available, else zero.
                # Validate that ts_pct lands in [0.0, 1.0]; log and skip to 0 if corrupt.
                "ts_pct": _safe_ts_pct(s),
                "fg3a":   round(fg3a_pg_bdl, 2),
                "_source": f"bdl_{prior}",
            }

        if out:
            log.debug(
                "franchise_plan: BDL fallback season=%d matched %d/%d players",
                prior,
                len(out),
                len(player_rows),
            )
            return out

    return {}


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
_TANK_PIVOT_WIN_FLOOR = 38      # tank failing → pivot to rebuild
_WIN_NOW_COLLAPSE_CEIL = 40     # win-now window broken → pivot to transition
                                # (contenders start ≥47W projected; 40 = clear miss on
                                # top-4 seed, matches real-NBA pivot triggers)
_TRANSITION_OVERPERFORM_FLOOR = 50  # transition team catching fire → push to win_now
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


# ---------------------------------------------------------------------------
# Production helpers
# ---------------------------------------------------------------------------

async def _fetch_season_production(
    pool,
    league_id: int,
    season: int,
    player_rows: list[dict],
) -> "dict[int, dict]":
    """Return {player_id: {ppg, apg, rpg, bpg, spg, drpg, mpg, gp}} for the current regular season.

    player_rows must be dicts with at least {id, first_name, last_name} so the
    BDL prior-season fallback can do name-based matching.

    Scoped to the supplied players only — never pulls league-wide.
    Returns {} on empty input or DB error (callers treat missing data as 'unknown' tier).
    Defensive columns (bpg, spg, drpg, mpg) are included so _defensive_tier can run
    without a second query; _production_tier ignores them (backward-compatible).

    Early-season / fresh-league fallback: any player with current-season GP < 10
    is looked up in the most recent BDL cache (prior season).  This prevents the
    production-tier classifier from returning 'unknown' for every player when the
    sim is at game 0, which was causing stars like Haliburton to be bucketed as
    'flex' rather than 'core'.  The fallback is tagged with _source='bdl_YYYY' so
    diagnostics can distinguish live stats from cached ones.
    """
    player_ids = [r["id"] for r in player_rows]
    if not player_ids:
        return {}
    try:
        rows = await pool.fetch(
            """
            SELECT b.player_id,
                   COUNT(b.id)::int                               AS gp,
                   AVG(b.points)::float                           AS ppg,
                   AVG(b.assists)::float                          AS apg,
                   AVG(b.rebounds_off + b.rebounds_def)::float    AS rpg,
                   AVG(b.blocks)::float                           AS bpg,
                   AVG(b.steals)::float                           AS spg,
                   AVG(b.rebounds_def)::float                     AS drpg,
                   AVG(b.minutes)::float                          AS mpg,
                   SUM(b.fgm)::float                              AS fgm_total,
                   SUM(b.fga)::float                              AS fga_total,
                   SUM(b.tpm)::float                              AS tpm_total,
                   SUM(b.tpa)::float                              AS tpa_total,
                   SUM(b.ftm)::float                              AS ftm_total,
                   SUM(b.fta)::float                              AS fta_total
            FROM game_box_scores b
            JOIN games g ON g.id = b.game_id
            WHERE g.league_id = $1
              AND g.season    = $2
              AND g.season_type = 'regular'
              AND b.player_id = ANY($3::int[])
            GROUP BY b.player_id
            """,
            league_id,
            season,
            player_ids,
        )
    except Exception as exc:
        log.warning(
            "franchise_plan: _fetch_season_production failed league=%d season=%d — %s",
            league_id,
            season,
            exc,
        )
        return {}

    def _pct_row(r: dict) -> dict:
        """Compute shooting percentages from aggregate sums in a DB row."""
        gp = int(r["gp"] or 0)
        fgm = float(r["fgm_total"] or 0.0)
        fga = float(r["fga_total"] or 0.0)
        tpm = float(r["tpm_total"] or 0.0)
        tpa = float(r["tpa_total"] or 0.0)
        ftm = float(r["ftm_total"] or 0.0)
        fta = float(r["fta_total"] or 0.0)
        fg3a_pg = (tpa / gp) if gp else 0.0
        fg_pct  = (fgm / fga)  if fga else 0.0
        fg3_pct = (tpm / tpa)  if tpa else 0.0
        ft_pct  = (ftm / fta)  if fta else 0.0
        pts_total = float(r["ppg"] or 0.0) * gp
        ts_denom  = 2.0 * (fga + 0.44 * fta)
        ts_pct    = (pts_total / ts_denom) if ts_denom else 0.0
        return {
            "fg_pct":  round(fg_pct,  4),
            "fg3_pct": round(fg3_pct, 4),
            "ft_pct":  round(ft_pct,  4),
            "ts_pct":  round(ts_pct,  4),
            "fg3a":    round(fg3a_pg, 2),
        }

    current_stats: dict[int, dict] = {
        r["player_id"]: {
            "ppg":  r["ppg"]  or 0.0,
            "apg":  r["apg"]  or 0.0,
            "rpg":  r["rpg"]  or 0.0,
            "bpg":  r["bpg"]  or 0.0,
            "spg":  r["spg"]  or 0.0,
            "drpg": r["drpg"] or 0.0,
            "mpg":  r["mpg"]  or 0.0,
            "gp":   r["gp"]   or 0,
            **_pct_row(r),
        }
        for r in rows
    }

    # Players whose current-season sample is too small to classify reliably.
    # Threshold: GP < 10 (same guard used by _production_tier / _defensive_tier).
    needs_fallback = [
        r for r in player_rows
        if (current_stats.get(r["id"], {}).get("gp") or 0) < 10
    ]
    if needs_fallback:
        fallback = _load_bdl_production_fallback(needs_fallback, current_season=season)
        for pid, stats in fallback.items():
            if pid not in current_stats or (current_stats[pid].get("gp") or 0) < 10:
                current_stats[pid] = stats

    return current_stats


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


_TIER_RANK: dict[str, int] = {"unknown": 0, "depth": 1, "role": 2, "producer": 3, "star": 4}

_DEFENSIVE_ARCHETYPES: frozenset[str] = frozenset(
    {"rim_protector", "wing_stopper", "on_ball_pest", "two_way_big", "switching_big"}
)


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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def derive_plan(
    pool, league_id: int, team_id: int, season: int
) -> dict:
    """Compute a fresh franchise plan.  Does NOT persist — caller decides."""

    # 1. Roster — active players only.
    # IL/inactive/two-way players skew avg_age and star flags; exclude them here.
    # We do NOT modify get_roster itself because many other callers need the
    # unfiltered list (salary accounting, full-team lookups, etc.).
    players = await player_repo.get_roster(pool, league_id, team_id)
    players = [p for p in players if p.roster_status == "active"]

    roster: list[dict] = []
    archetype_map: dict[int, str | None] = {}
    for p in players:
        age = _calc_age(p.birth_date, season)
        roster.append({
            "id": p.id,           # used by _fetch_season_production / BDL fallback
            "player_id": p.id,    # used by _categorise_players and callers
            "first_name": p.first_name,
            "last_name": p.last_name,
            "age": age,
            "overall": p.overall,
            "position": p.position,
            "full_name": p.full_name,
        })
        archetype_map[p.id] = p.defensive_archetype

    ovr_list = [r["overall"] for r in roster]

    # 2. Record from standings_cache + team conference
    sc_row = await pool.fetchrow(
        """
        SELECT sc.wins, sc.losses, t.conference
        FROM standings_cache sc
        JOIN teams t ON t.id = sc.team_id
        WHERE sc.league_id=$1 AND sc.team_id=$2 AND sc.season=$3
        """,
        league_id, team_id, season,
    )
    wins = sc_row["wins"] if sc_row else 0
    losses = sc_row["losses"] if sc_row else 0
    conference = sc_row["conference"] if sc_row else None
    games_played = wins + losses
    projected_wins = _project_wins(wins, losses, games_played)

    # Conference rank: peers with strictly more wins → 1-based rank
    conf_rank: int | None = None
    if conference:
        rank_val = await pool.fetchval(
            """
            SELECT COUNT(*) + 1
            FROM standings_cache sc2
            JOIN teams t2 ON t2.id = sc2.team_id
            WHERE sc2.league_id = $1 AND sc2.season = $2
              AND t2.conference = $3
              AND sc2.wins > $4
            """,
            league_id, season, conference, wins,
        )
        conf_rank = int(rank_val) if rank_val is not None else None

    # 3. Avg age of top-8 by OVR
    top8 = sorted(roster, key=lambda p: p["overall"], reverse=True)[:8]
    valid_ages = [p["age"] for p in top8 if p["age"] is not None]
    avg_age = sum(valid_ages) / len(valid_ages) if valid_ages else 27.0

    # Team mode — shared 5-bucket classification; computed here so avg_age is available.
    # star_count derived from roster already loaded above (OVR >= 85 threshold matches
    # cpu_trade_posture._compute_team_posture).  plan_goal is None here because this IS
    # the derive step — the goal doesn't exist yet.  B7 posture floor still fires via
    # star_count; the plan_goal floor only applies when re-evaluating an existing plan.
    _derive_star_count = sum(1 for r in roster if r["overall"] >= 85)
    team_mode = _trade_eval.compute_team_mode(
        projected_wins if games_played >= 10 else None,
        avg_age,
        conf_rank,
        star_count=_derive_star_count,
        plan_goal=None,  # REQUIRED for B7 posture floor — goal not yet derived at this step
    )

    # 4. Star flags
    has_young_star = any(
        p["overall"] >= 88 and p["age"] is not None and p["age"] <= 25
        for p in roster
    )
    has_elite_star = any(p["overall"] >= 90 for p in roster)
    has_any_star = any(p["overall"] >= 88 for p in roster)
    # Prime-window star: OVR >= 88 AND 26 <= age <= 28 — the most common championship
    # window that was previously misclassified as transition.
    has_prime_age_star = any(
        p["overall"] >= 88 and p["age"] is not None and 26 <= p["age"] <= 28
        for p in roster
    )

    # Best star name for rationale (prefer prime-age, then young)
    prime_stars = [
        p for p in sorted(roster, key=lambda x: x["overall"], reverse=True)
        if p["overall"] >= 88 and p["age"] is not None and 26 <= p["age"] <= 28
    ]
    young_stars = [
        p for p in sorted(roster, key=lambda x: x["overall"], reverse=True)
        if p["overall"] >= 88 and p["age"] is not None and p["age"] <= 25
    ]
    star_name: Optional[str] = (
        (prime_stars or young_stars or [None])[0] or {}
    ).get("full_name")
    if not star_name:
        top_star = next((p for p in sorted(roster, key=lambda x: x["overall"], reverse=True) if p["overall"] >= 88), None)
        star_name = top_star["full_name"] if top_star else None

    # 5. Draft picks (R1, next 3 seasons)
    r1_count = await pool.fetchval(
        """
        SELECT COUNT(*) FROM draft_picks
        WHERE league_id = $1 AND current_team_id = $2
          AND round = 1 AND season > $3 AND season <= $3 + 3
        """,
        league_id, team_id, season,
    )
    r1_picks_next3 = int(r1_count or 0)

    # 6. Derive goal + horizon
    goal, horizon = _derive_goal_and_horizon(
        projected_wins=projected_wins,
        avg_age=avg_age,
        has_young_star=has_young_star,
        has_elite_star=has_elite_star,
        has_any_star=has_any_star,
        r1_picks_next3=r1_picks_next3,
        games_played=games_played,
        ovr_list=ovr_list,
        prime_age_star=has_prime_age_star,
        mode=team_mode,
        conf_rank=conf_rank,
    )

    # 7. Player categorisation — augmented with current-season production.
    #    Players with GP < 10 fall back to prior-season BDL cache so that stars
    #    like Haliburton aren't bucketed as 'flex' at game 0 of a fresh league.
    #    recently_acquired_ids: players acquired within the last 60 sim games
    #    are eligible for "flip_asset" shop_intent if they land in surplus.
    production_map = await _fetch_season_production(pool, league_id, season, roster)
    _recently_acq_ids: set[int] = set()
    try:
        _last_game = await pool.fetchval(
            "SELECT MAX(game_index) FROM games WHERE league_id=$1 AND status='simmed'",
            league_id,
        )
        if _last_game is not None:
            _acq_rows = await pool.fetch(
                """SELECT id FROM players
                   WHERE team_id IN (
                       SELECT id FROM teams WHERE league_id=$1
                   )
                   AND last_traded_game_index IS NOT NULL
                   AND last_traded_game_index >= $2""",
                league_id, int(_last_game) - 60,
            )
            _recently_acq_ids = {r["id"] for r in _acq_rows}
    except Exception:
        pass  # recently_acquired fallback gracefully — flip_asset won't fire

    core_ids, flex_ids, surplus_ids, youth_overrides, shop_intent = _categorise_players(
        goal, roster, avg_age,
        production_map=production_map,
        archetype_map=archetype_map,
        recently_acquired_ids=_recently_acq_ids,
    )

    # 8. Asset targets
    asset_targets = _ASSET_TARGETS[goal]

    # 9. Rationale
    rationale = _build_rationale(
        goal=goal,
        star_name=star_name,
        avg_age=avg_age,
        projected_wins=projected_wins,
        r1_picks_next3=r1_picks_next3,
        target_year=season + horizon,
        has_any_star=has_any_star,
    )

    # 10. Snapshot of derivation inputs.
    #     production_tiers captures {player_id: {off, def, combined, archetype}} for
    #     players with enough GP to classify — useful for diagnostics and ride-along
    #     rationale without a separate DB re-query.
    production_tiers: dict[str, dict] = {}
    for pid, stats in production_map.items():
        off_t = _production_tier(stats)
        def_t = _defensive_tier(stats)
        arch  = archetype_map.get(pid)
        comb  = _combined_tier(off_t, def_t, arch)
        # Only persist when we have enough data to be meaningful.
        if off_t != "unknown" or def_t != "unknown":
            production_tiers[str(pid)] = {
                "off":       off_t,
                "def":       def_t,
                "combined":  comb,
                "archetype": arch,
            }
    derived_from_record = {
        "wins": wins,
        "losses": losses,
        "games_played": games_played,
        "projected_wins": round(projected_wins, 1),
        "avg_age_top8": round(avg_age, 2),
        "has_young_star": has_young_star,
        "has_elite_star": has_elite_star,
        "has_prime_age_star": has_prime_age_star,
        "r1_picks_next3": r1_picks_next3,
        "roster_size": len(roster),
        "production_tiers": production_tiers,
        "youth_overrides": {str(pid): reason for pid, reason in youth_overrides.items()},
        # shop_intent: why each surplus player is available.  Keyed by str(player_id)
        # for JSON-serialisation compatibility (JSONB column).
        "shop_intent": {str(pid): reason for pid, reason in shop_intent.items()},
    }

    return {
        "league_id": league_id,
        "team_id": team_id,
        "season": season,
        "goal": goal,
        "horizon_seasons": horizon,
        "core_player_ids": core_ids,
        "flex_player_ids": flex_ids,
        "surplus_player_ids": surplus_ids,
        "asset_targets": asset_targets,
        "rationale": rationale,
        "derived_from_record": derived_from_record,
    }


async def persist_plan(pool, plan: dict) -> int:
    """Upsert the plan row.  Returns the plan id.

    Pivot metadata (pivot_from_goal, pivot_reason, pivot_game_index) are merged
    into derived_from_record before the upsert so they survive across restarts
    without a schema change — they're ephemeral annotations, not structural fields.
    """
    _pivot_keys = ("pivot_from_goal", "pivot_reason", "pivot_game_index")
    _has_pivot = any(k in plan for k in _pivot_keys)
    if _has_pivot:
        plan = dict(plan)  # shallow copy — don't mutate caller's dict
        dfr = dict(plan.get("derived_from_record") or {})
        for k in _pivot_keys:
            if k in plan:
                dfr[k] = plan[k]
        plan["derived_from_record"] = dfr
    return await franchise_plan_repo.upsert_plan(pool, plan)


async def get_plan(pool, league_id: int, team_id: int, season: int) -> Optional[dict]:
    """Read latest stored plan.  Returns None if never derived."""
    return await franchise_plan_repo.get_plan(pool, league_id, team_id, season)


async def get_or_derive(pool, league_id: int, team_id: int, season: int) -> dict:
    """Return stored plan; derive + persist if absent."""
    existing = await get_plan(pool, league_id, team_id, season)
    if existing is not None:
        return existing
    plan = await derive_plan(pool, league_id, team_id, season)
    await persist_plan(pool, plan)
    return plan


async def derive_and_persist_all(
    pool,
    league_id: int,
    season: int,
    current_game_index: Optional[int] = None,
) -> int:
    """Bulk refresh for all CPU teams.  Returns count re-derived.

    Phase 4 stickiness — for each CPU team:
    1. Read existing plan + last_derived_game_index.
    2. Check _is_reassessment_checkpoint:
       - 'sticky': skip (plan stays as-is).
       - checkpoint reason is 'mid_season_checkpoint': run _should_pivot first;
         only re-derive if a pivot condition fires.
       - any other checkpoint: always re-derive.
    3. On re-derive, log pivot events (old_goal → new_goal) as FRANCHISE-PIVOT.

    Human-managed teams are skipped — they don't use CPU franchise logic.
    current_game_index is passed by sim batch runners; None means offseason/admin call.
    """
    teams = await team_repo.get_all(pool, league_id)
    cpu_teams = [t for t in teams if t.manager_user_id is None]

    count = 0
    skipped = 0

    for team in cpu_teams:
        try:
            existing = await get_plan(pool, league_id, team.id, season)
            last_idx = existing.get("last_derived_game_index") if existing else None

            # Determine games_played + games_remaining for checkpoint detection.
            sc_row = await pool.fetchrow(
                "SELECT wins, losses FROM standings_cache "
                "WHERE league_id=$1 AND team_id=$2 AND season=$3",
                league_id, team.id, season,
            )
            wins = sc_row["wins"] if sc_row else 0
            losses = sc_row["losses"] if sc_row else 0
            games_played = wins + losses

            # Total regular season games: used to compute games_remaining.
            total_row = await pool.fetchrow(
                "SELECT COUNT(*) AS cnt FROM games "
                "WHERE league_id=$1 AND season=$2 AND season_type='regular'",
                league_id, season,
            )
            total_games = int(total_row["cnt"]) if total_row else 82
            games_remaining = max(0, total_games - games_played)

            should_derive, reason = _is_reassessment_checkpoint(
                games_played, games_remaining, last_idx
            )

            if not should_derive:
                skipped += 1
                continue

            # Mid-season checkpoint: pivot-gated — only re-derive if a pivot fires.
            if reason == "mid_season_checkpoint" and existing is not None:
                projected_wins = _project_wins(wins, losses, games_played)
                current_record = {
                    "wins": wins,
                    "losses": losses,
                    "games_played": games_played,
                    "projected_wins": projected_wins,
                }
                # Count R1 picks for rebuild → tank pivot check.
                r1_count = await pool.fetchval(
                    """
                    SELECT COUNT(*) FROM draft_picks
                    WHERE league_id=$1 AND current_team_id=$2
                      AND round=1 AND season > $3 AND season <= $3 + 3
                    """,
                    league_id, team.id, season,
                )
                r1_banked = int(r1_count or 0)

                pivot_ok, _new_goal, _pivot_reason = _should_pivot(
                    existing, current_record, r1_banked
                )
                if not pivot_ok:
                    skipped += 1
                    continue
                # Fall through — pivot fires; full re-derive below.

            old_goal = existing.get("goal") if existing else None
            plan = await derive_plan(pool, league_id, team.id, season)
            plan["last_derived_game_index"] = current_game_index

            new_goal = plan["goal"]
            if old_goal is not None and old_goal != new_goal:
                # Derive a short reason label — prefer the pivot reason if we
                # just confirmed a mid-season pivot; otherwise label by checkpoint.
                if reason == "mid_season_checkpoint":
                    # Re-compute pivot reason to surface in the log line.
                    projected_wins_now = plan["derived_from_record"].get("projected_wins", 0.0)
                    r1_b = plan["derived_from_record"].get("r1_picks_next3", 0)
                    _, _, _log_reason = _should_pivot(
                        {"goal": old_goal},
                        {"projected_wins": projected_wins_now},
                        r1_b,
                    )
                    pivot_log_reason = _log_reason or reason
                else:
                    pivot_log_reason = reason

                log.info(
                    "[FRANCHISE-PIVOT] league=%d team=%s season=%d game=%s "
                    "%s %s → %s — \"%s\"",
                    league_id,
                    team.nba_team_code,
                    season,
                    str(current_game_index) if current_game_index is not None else "offseason",
                    team.nba_team_code,
                    old_goal,
                    new_goal,
                    pivot_log_reason,
                )
                # Embed pivot metadata in the plan dict so plan_alignment blocks
                # (cpu_trade_service) can surface it in headless/ride-along output
                # without a separate DB lookup.
                plan["pivot_from_goal"] = old_goal
                plan["pivot_reason"] = pivot_log_reason
                plan["pivot_game_index"] = current_game_index
            else:
                # Carry forward any existing pivot metadata so it stays visible
                # until the next reassessment window clears it.
                # NOTE: persist_plan embeds pivot keys inside derived_from_record
                # before upserting; _parse_plan does NOT lift them back to the top
                # level.  Read from derived_from_record, not from existing directly.
                if existing and isinstance(existing.get("derived_from_record"), dict):
                    _src = existing["derived_from_record"]
                    for _k in ("pivot_from_goal", "pivot_reason", "pivot_game_index"):
                        if _k in _src:
                            plan[_k] = _src[_k]
                log.debug(
                    "franchise_plan refreshed: league=%d team=%s season=%d "
                    "game=%s goal=%s reason=%s",
                    league_id, team.nba_team_code, season,
                    str(current_game_index) if current_game_index is not None else "offseason",
                    new_goal, reason,
                )

            await persist_plan(pool, plan)
            count += 1

        except Exception as exc:
            log.warning(
                "franchise_plan derive failed: league=%d team=%d season=%d — %s",
                league_id, team.id, season, exc,
            )

    log.info(
        "derive_and_persist_all: league=%d season=%d derived=%d skipped=%d/%d CPU teams",
        league_id, season, count, skipped, len(cpu_teams),
    )

    # Phase 1: derive CPU role assignments for all teams after plans settle.
    # Phase 2 wires roles into the sim engine touch-share calculation.
    try:
        from services import role_service  # local import to avoid circular dep
        await role_service.derive_and_persist_all(pool, league_id, season)
    except Exception as exc:
        log.warning(
            "role_service.derive_and_persist_all failed: league=%d season=%d — %s",
            league_id, season, exc,
        )

    return count
