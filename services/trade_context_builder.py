"""DB-touching trade context builders: per-player form modifiers (in-season
performance vs. expectation) and per-team receiving context (posture mode,
payroll, roster construction) used by player_team_specific_value and
cpu_should_accept.

Extracted from trade_evaluator.py (Phase 3 opportunistic split, see
HANDOFF.md).
"""
from __future__ import annotations

import logging
import time
from typing import Any

from services.trade_value_math import _expected_apg, _expected_ppg, _ratio_to_modifier

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Form-modifier cache
# ---------------------------------------------------------------------------
# Key: (league_id, season)  →  {"ts": float, "data": dict[int, tuple[float, dict]]}
# "data" maps player_id → (modifier, stats_dict)
# stats_dict keys: ppg, apg, rpg, games_played (all float or int)
# TTL: 60 seconds — short enough to stay fresh during a sim batch, long enough
# to avoid re-querying the same player set on back-to-back trade eval calls.
_FORM_CACHE: dict[tuple[int, int], dict[str, Any]] = {}


_FORM_CACHE_TTL = 60.0


async def compute_form_map(
    pool,
    player_ids: list[int],
    ovr_map: dict[int, int],
    position_map: dict[int, str],
    league_id: int,
    season: int,
) -> dict[int, tuple[float, dict]]:
    """Batch-compute form modifiers for a list of players in one SQL query.

    Returns dict[player_id → (modifier: float, stats: dict)].
    stats keys: ppg, apg, rpg, games_played (floats / int; 0 if no data).

    Results are cached per (league_id, season) with a 60-second TTL so
    repeated calls within the same sim batch don't hit the DB each time.
    Missing players (no box-score rows) receive modifier=1.0, stats all-zero.
    """
    if not player_ids:
        return {}

    cache_key = (league_id, season)
    now = time.monotonic()
    cached = _FORM_CACHE.get(cache_key)
    if cached and (now - cached["ts"]) < _FORM_CACHE_TTL:
        missing_ids = [p for p in player_ids if p not in cached["data"]]
        if not missing_ids:
            # Full cache hit — return only requested IDs.
            return {pid: cached["data"][pid] for pid in player_ids}
        # Partial cache hit — query only the missing IDs and merge into the
        # existing cache entry.  Do NOT reset ts: preserve the original 60s window.
        fetch_ids = missing_ids
        is_partial_fill = True
    else:
        fetch_ids = player_ids
        is_partial_fill = False

    # Cache miss, expired, or partial — query DB for the IDs we still need.
    try:
        rows = await pool.fetch(
            """
            SELECT
                b.player_id,
                COUNT(b.id)                          AS games_played,
                AVG(b.points)                        AS ppg,
                AVG(b.rebounds_off + b.rebounds_def) AS rpg,
                AVG(b.assists)                       AS apg,
                AVG(b.blocks)                        AS bpg,
                AVG(b.steals)                        AS spg,
                AVG(b.minutes)                       AS mpg,
                SUM(b.fgm)::float                    AS fgm_total,
                SUM(b.fga)::float                    AS fga_total,
                SUM(b.tpm)::float                    AS tpm_total,
                SUM(b.tpa)::float                    AS tpa_total,
                SUM(b.ftm)::float                    AS ftm_total,
                SUM(b.fta)::float                    AS fta_total
            FROM game_box_scores b
            JOIN games g ON g.id = b.game_id
            WHERE b.player_id = ANY($1)
              AND g.league_id = $2
              AND g.season    = $3
            GROUP BY b.player_id
            """,
            fetch_ids,
            league_id,
            season,
        )
    except Exception as exc:
        log.warning(f"compute_form_map DB query failed: {exc}")
        _neutral_err = {
            "ppg": 0.0, "apg": 0.0, "rpg": 0.0, "bpg": 0.0, "spg": 0.0, "mpg": 0.0,
            "gp": 0, "games_played": 0,
            "fg_pct": 0.0, "fg3_pct": 0.0, "ft_pct": 0.0, "ts_pct": 0.0, "fg3a": 0.0,
        }
        return {pid: (1.0, _neutral_err) for pid in player_ids}

    # Ensure cache entry exists (only create/reset ts on a non-partial fill).
    if not is_partial_fill:
        _FORM_CACHE[cache_key] = {"ts": now, "data": {}}
    elif cache_key not in _FORM_CACHE:
        # Defensive: partial fill but no existing entry — create one.
        _FORM_CACHE[cache_key] = {"ts": now, "data": {}}

    cache_data = _FORM_CACHE[cache_key]["data"]

    stat_by_pid: dict[int, dict] = {}
    for row in rows:
        gp = int(row["games_played"] or 0)
        fgm = float(row["fgm_total"] or 0.0)
        fga = float(row["fga_total"] or 0.0)
        tpm = float(row["tpm_total"] or 0.0)
        tpa = float(row["tpa_total"] or 0.0)
        ftm = float(row["ftm_total"] or 0.0)
        fta = float(row["fta_total"] or 0.0)
        # Per-game shooting attempt averages (for specialist detection)
        fg3a_pg = (tpa / gp) if gp else 0.0
        # Shooting percentages — per-game attempt averages for display
        fg_pct = (fgm / fga) if fga else 0.0
        fg3_pct = (tpm / tpa) if tpa else 0.0
        ft_pct = (ftm / fta) if fta else 0.0
        # True shooting: pts / (2 * (fga + 0.44 * fta))
        pts_total = float(row["ppg"] or 0.0) * gp
        ts_denom = 2.0 * (fga + 0.44 * fta)
        ts_pct = (pts_total / ts_denom) if ts_denom else 0.0
        stat_by_pid[row["player_id"]] = {
            "ppg": float(row["ppg"] or 0.0),
            "apg": float(row["apg"] or 0.0),
            "rpg": float(row["rpg"] or 0.0),
            "bpg": float(row["bpg"] or 0.0),
            "spg": float(row["spg"] or 0.0),
            "mpg": float(row["mpg"] or 0.0),
            "gp": gp,
            "games_played": gp,
            "fg_pct": round(fg_pct, 4),
            "fg3_pct": round(fg3_pct, 4),
            "ft_pct": round(ft_pct, 4),
            "ts_pct": round(ts_pct, 4),
            "fg3a": round(fg3a_pg, 2),  # per-game 3PA for specialist detection
        }

    # Compute and cache entries only for the IDs we actually fetched.
    # On a partial fill, the already-cached IDs are left untouched in cache_data.
    _neutral_stats = {
        "ppg": 0.0, "apg": 0.0, "rpg": 0.0, "bpg": 0.0, "spg": 0.0, "mpg": 0.0,
        "gp": 0, "games_played": 0,
        "fg_pct": 0.0, "fg3_pct": 0.0, "ft_pct": 0.0, "ts_pct": 0.0, "fg3a": 0.0,
    }
    for pid in fetch_ids:
        stats = stat_by_pid.get(pid)
        if stats is None or stats["games_played"] < 10:
            # Insufficient sample — neutral modifier.
            entry = (1.0, stats or _neutral_stats)
        else:
            ovr = ovr_map.get(pid, 80)
            pos = position_map.get(pid, "")
            exp_ppg = _expected_ppg(ovr)
            ratio = stats["ppg"] / max(exp_ppg, 1.0)
            modifier = _ratio_to_modifier(ratio)

            # Assist bonus for ball-handlers (+0.02 per APG above expected, cap +0.05).
            exp_apg = _expected_apg(ovr, pos)
            apg_bonus = max(0.0, min(0.05, (stats["apg"] - exp_apg) * 0.02))
            modifier = min(1.30, modifier + apg_bonus)

            entry = (round(modifier, 4), stats)

        cache_data[pid] = entry

    # Build result for the ORIGINAL full player_ids list (partial fill: some entries
    # were already in cache before this call; we just added the missing ones above).
    _neutral = {
        "ppg": 0.0, "apg": 0.0, "rpg": 0.0, "bpg": 0.0, "spg": 0.0, "mpg": 0.0,
        "gp": 0, "games_played": 0,
        "fg_pct": 0.0, "fg3_pct": 0.0, "ft_pct": 0.0, "ts_pct": 0.0, "fg3a": 0.0,
    }
    return {pid: cache_data.get(pid, (1.0, _neutral)) for pid in player_ids}


def compute_team_mode(
    projected_wins: int | None,
    avg_age: float,
    conf_rank: int | None,
    *,
    star_count: int = 0,
    plan_goal: str | None = None,
) -> str:
    """
    Derive the 5-bucket trade posture mode from season projection data.

    This is the single source of truth for CPU mode computation.
    Both cpu_trade_service._compute_team_posture and
    cpu_trade_evaluation._cpu_evaluate call this function so propose-side
    and accept-side always agree.

    Buckets (in priority order):
      contending    — projected ≥50 wins, or ≥45 + veteran core + top-4 conf
      play_in_fringe — 40-49 wins, or bubble team (35-44 + older + top-10 conf)
      soft_rebuild  — ≤35 wins with aging roster (avg_age ≥26) — sell vets
      rebuilding    — <26 projected wins OR very young avg_age (<24)
      developing    — middle band, ambiguous direction

    When projected_wins is None (< 10 games played), falls back to avg_age tiers.

    star_count: number of players with OVR ≥ 85 on the roster.  A team with 2+
    stars cannot be classified as transition/soft_rebuild/rebuilding/tanking
    regardless of record dip — their floor is play_in_fringe.

    plan_goal: the franchise_plans.goal value ('win_now'|'transition'|'rebuild'|
    'tank').  When explicitly set to 'win_now', the floor rises to play_in_fringe
    unless record evidence STRONGLY contradicts (< 30 projected wins).
    """
    in_top4 = conf_rank is not None and conf_rank <= 4
    in_top10 = conf_rank is not None and conf_rank <= 10

    # ── Star-count floor: 2+ OVR-85 players = never below play_in_fringe ────
    # Even a bad-record season (e.g. NYK starting 18-25 with Brunson + KAT)
    # is a contender in a slump, not a transition/rebuild team.
    has_star_floor = star_count >= 2

    # ── Plan-goal floor: explicit win_now goal = at least play_in_fringe ─────
    # Respect the front office's stated direction unless record is lottery-pace.
    # Valid franchise_plans.goal values: win_now | transition | rebuild | tank
    # "contend" is not a DB value — using it here would silently never match.
    plan_says_contend = plan_goal == "win_now"

    if projected_wins is not None:
        raw_mode: str
        if projected_wins >= 50 or (projected_wins >= 45 and avg_age >= 27.0 and in_top4):
            raw_mode = "contending"
        elif (40 <= projected_wins <= 49) or (35 <= projected_wins <= 44 and avg_age >= 26.0 and in_top10):
            raw_mode = "play_in_fringe"
        # Hard rebuild: bottom-tier record — 22-win teams are clearly rebuilding,
        # not "threading the needle."  Conf_rank ≥ 14 catches the bottom of a 15-team
        # conference regardless of how the wins pace is computed.
        elif projected_wins <= 25 or (conf_rank is not None and conf_rank >= 14):
            raw_mode = "rebuilding"
        # Soft rebuild: clearly losing but not at rock bottom
        elif projected_wins <= 30:
            raw_mode = "soft_rebuild"
        elif projected_wins <= 35 and avg_age >= 26.0:
            raw_mode = "soft_rebuild"
        elif avg_age < 24.0:
            raw_mode = "rebuilding"
        else:
            raw_mode = "developing"

        # Apply star-count floor: 2+ stars can't be mis-labeled soft_rebuild/rebuilding/developing
        # when the record is just a slump (projected >= 30W).  Below 30W even star teams
        # can be in genuine trouble so don't override there.
        if has_star_floor and raw_mode in ("soft_rebuild", "rebuilding", "developing", "transition") and projected_wins >= 30:
            raw_mode = "play_in_fringe"

        # Apply plan-goal floor: explicit win_now plan can't be below play_in_fringe
        # unless the record is deep-lottery pace (< 30 projected wins).
        if plan_says_contend and raw_mode in ("soft_rebuild", "rebuilding", "developing", "transition") and projected_wins >= 30:
            raw_mode = "play_in_fringe"

        return raw_mode

    # Too early to project — use age as proxy
    base_mode: str
    if avg_age > 29.0:
        base_mode = "contending"
    elif avg_age >= 27.0:
        base_mode = "play_in_fringe"
    elif avg_age >= 24.5:
        base_mode = "developing"
    else:
        base_mode = "rebuilding"

    # Apply floors even in the early-season no-data path.
    if has_star_floor and base_mode in ("soft_rebuild", "rebuilding", "developing"):
        base_mode = "play_in_fringe"
    if plan_says_contend and base_mode in ("soft_rebuild", "rebuilding", "developing"):
        base_mode = "play_in_fringe"

    return base_mode


# Team-context cache: (team_id, season) → context dict.
# Populated by build_team_context; valid for the duration of a sim batch.
_TEAM_CONTEXT_CACHE: dict[tuple[int, int], dict] = {}


async def build_team_context(pool, league_id: int, team_id: int, season: int) -> dict:
    """
    Build the receiving_team_context dict for player_team_specific_value.

    Queries:
    - standings_cache for wins/losses → compute mode via compute_team_mode
    - contracts for current_payroll
    - lineups + players for position_counts (OVR ≥ 75 players per position)

    Results cached per (team_id, season) for the life of the process.
    Falls back to a neutral context on any DB error.
    """
    cache_key = (team_id, season)
    if cache_key in _TEAM_CONTEXT_CACHE:
        return _TEAM_CONTEXT_CACHE[cache_key]

    try:
        # Mode from standings
        row = await pool.fetchrow(
            """
            SELECT sc.wins, sc.losses, t.conference
            FROM standings_cache sc
            JOIN teams t ON t.id = sc.team_id
            WHERE sc.league_id = $1 AND sc.team_id = $2 AND sc.season = $3
            """,
            league_id, team_id, season,
        )
        wins = row["wins"] if row else 0
        losses = row["losses"] if row else 0
        conference = row["conference"] if row else None
        games_played = wins + losses

        projected_wins: int | None = None
        if games_played >= 10:
            projected_wins = round((wins / games_played) * 82)

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

        age_rows = await pool.fetch(
            """
            SELECT EXTRACT(YEAR FROM AGE(p.birth_date))::int AS age
            FROM lineups l
            JOIN players p ON p.id = l.player_id
            WHERE l.league_id = $1 AND l.team_id = $2
              AND p.birth_date IS NOT NULL
            ORDER BY l.slot ASC
            LIMIT 8
            """,
            league_id, team_id,
        )
        ages = [r["age"] for r in age_rows if r["age"] is not None]
        avg_age = sum(ages) / len(ages) if ages else 27.0

        # Star count for posture floor (OVR >= 85).
        _star_val = await pool.fetchval(
            """
            SELECT COUNT(*) FROM lineups l JOIN players p ON p.id = l.player_id
            WHERE l.league_id = $1 AND l.team_id = $2 AND p.overall >= 85
            """,
            league_id, team_id,
        )
        _star_count = int(_star_val or 0)

        _plan_row = await pool.fetchrow(
            "SELECT goal FROM franchise_plans WHERE league_id=$1 AND team_id=$2 AND season=$3",
            league_id, team_id, season,
        )
        _plan_goal = _plan_row["goal"] if _plan_row else None

        mode = compute_team_mode(
            projected_wins, avg_age, conf_rank,
            star_count=_star_count, plan_goal=_plan_goal,
        )

        # Current payroll
        payroll_val = await pool.fetchval(
            """
            SELECT COALESCE(SUM(c.salary), 0)
            FROM contracts c
            JOIN players p ON p.id = c.player_id
            JOIN lineups l ON l.player_id = p.id AND l.league_id = $1 AND l.team_id = $2
            WHERE c.is_active = TRUE
            """,
            league_id, team_id,
        )
        current_payroll = int(payroll_val or 0)

        # Position counts (OVR >= 75)
        pos_rows = await pool.fetch(
            """
            SELECT p.position, COUNT(*) AS cnt
            FROM lineups l
            JOIN players p ON p.id = l.player_id
            WHERE l.league_id = $1 AND l.team_id = $2 AND p.overall >= 75
            GROUP BY p.position
            """,
            league_id, team_id,
        )
        position_counts = {r["position"]: int(r["cnt"]) for r in pos_rows}

        context = {
            "team_id": team_id,
            "mode": mode,
            "current_payroll": current_payroll,
            "position_counts": position_counts,
        }

    except Exception as exc:
        log.warning(f"build_team_context failed for team {team_id}: {exc}")
        context = {
            "team_id": team_id,
            "mode": "developing",
            "current_payroll": 0,
            "position_counts": {},
        }

    _TEAM_CONTEXT_CACHE[cache_key] = context
    return context
