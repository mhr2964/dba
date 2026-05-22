from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from typing import Any

log = logging.getLogger(__name__)

# Imported lazily inside cpu_should_accept to avoid import-time circular deps.
# trade_context → trade_evaluator would be a cycle if imported at module level.
_trade_context = None


def _get_trade_context():
    global _trade_context
    if _trade_context is None:
        from services import trade_context as _tc
        _trade_context = _tc
    return _trade_context

# ---------------------------------------------------------------------------
# Form-modifier cache
# ---------------------------------------------------------------------------
# Key: (league_id, season)  →  {"ts": float, "data": dict[int, tuple[float, dict]]}
# "data" maps player_id → (modifier, stats_dict)
# stats_dict keys: ppg, apg, rpg, games_played (all float or int)
# TTL: 60 seconds — short enough to stay fresh during a sim batch, long enough
# to avoid re-querying the same player set on back-to-back trade eval calls.
_FORM_CACHE: dict[tuple[int, int], dict[str, Any]] = {}
_FORM_CACHE_TTL = 60.0  # seconds

# ---------------------------------------------------------------------------
# Fleecing-floor constants
# ---------------------------------------------------------------------------
# DEFAULT floor (0.85) applies to all win-now and fringe-playoff teams.
# REBUILD floor (0.70) kicks in for rebuild-adjacent teams ONLY when the
# incoming side contains a young, quality player — they're buying age, not value.
_REBUILD_MODES: frozenset[str] = frozenset({"rebuilding", "soft_rebuild", "developing"})
DEFAULT_FLEECING_FLOOR: float = 0.85
REBUILD_BUYS_YOUNG_FLOOR: float = 0.78

# OVR normalization constant so OVR 80 anchors at 40.0 in player_trade_value.
# Derived: 80 ** 1.45 = 488.94  →  _OVR_NORM = 488.94 / 40 = 12.2236
_OVR_NORM: float = 12.2236

# OVR floor for "elite" lead-role players requiring both a starter AND a 1st-round
# pick as return. Guards against cornerstone fire-sales via value math loopholes.
_ELITE_OVR_FLOOR: int = 82


def _effective_fleecing_floor(
    cpu_team_mode: str,
    assets_receiving: list[dict],
) -> tuple[float, bool]:
    """Return (floor, is_rebuild_softened).

    Softens the fleecing floor from 0.85 → 0.70 when:
      1. cpu_team_mode is in _REBUILD_MODES (rebuilding / soft_rebuild / developing)
      2. At least one incoming player is age ≤ 25 AND overall ≥ 75

    Both conditions must hold simultaneously.  Win-now / play_in_fringe teams always
    get the hard 0.85 floor regardless of who is coming back.
    """
    if cpu_team_mode not in _REBUILD_MODES:
        return DEFAULT_FLEECING_FLOOR, False
    has_young_buy = any(
        a.get("asset_type") == "player"
        and (
            (
                (a.get("player") or {}).get("age", 99) <= 23
                and (a.get("player") or {}).get("overall", 0) >= 78
            )
            or (
                (a.get("player") or {}).get("age", 99) <= 24
                and (a.get("player") or {}).get("overall", 0) >= 80
            )
        )
        for a in assets_receiving
    )
    if has_young_buy:
        return REBUILD_BUYS_YOUNG_FLOOR, True
    return DEFAULT_FLEECING_FLOOR, False


def _expected_ppg(ovr: int) -> float:
    """Rough expected PPG from OVR, calibrated to observed sim distribution.

    OVR 95 → ~33 PPG, OVR 90 → ~28 PPG, OVR 85 → ~22 PPG,
    OVR 80 → ~15 PPG, OVR 75 → ~10 PPG, OVR 65 → ~4 PPG.
    Linear from OVR 65 baseline (was OVR 70 floor — that caused sub-70 players
    to compute expected=0, which made any positive actual PPG produce ratio=inf
    and form_modifier=1.15 across the board, inflating bench players in
    trade evaluation. Now sub-70s get a real-but-low expected PPG.)
    """
    return max(0.5, (ovr - 65) * 1.0)


def _expected_apg(ovr: int, position: str) -> float:
    """Rough expected APG for ball-handlers (PG/SG) — used for assist bonus."""
    if position in ("PG", "SG"):
        return max(0.0, (ovr - 70) * 0.18)
    return 0.0


def _ratio_to_modifier(ratio: float) -> float:
    """Map actual/expected ratio → multiplier clamped to [0.85, 1.30].

    Elite production earns a meaningful bump above the old 1.15 ceiling:
      ratio >= 1.50 → 1.30  (elite: 50%+ above expected)
      ratio >= 1.30 → 1.20  (great: 30-50% above)
      ratio >= 1.15 → 1.10  (good: 15-30% above)
      ratio == 1.00 → 1.00  (neutral)
      ratio <= 0.80 → 0.85  (underperforming — floor unchanged)

    Linear interpolation between each adjacent pair of breakpoints.

    Example — Harden OVR 88: expected_ppg = 23, actual 28 → ratio 1.22
      Falls in [1.15, 1.30]: lerp → ≈ 1.11 from PPG alone; assist bonus can
      push to 1.13–1.16, giving a meaningful star-production premium.
    """
    if ratio >= 1.50:
        return 1.30
    if ratio >= 1.30:
        # lerp [1.30, 1.50] → [1.20, 1.30]
        return 1.20 + (ratio - 1.30) / 0.20 * 0.10
    if ratio >= 1.15:
        # lerp [1.15, 1.30] → [1.10, 1.20]
        return 1.10 + (ratio - 1.15) / 0.15 * 0.10
    if ratio >= 1.0:
        # lerp [1.00, 1.15] → [1.00, 1.10]
        return 1.0 + (ratio - 1.0) / 0.15 * 0.10
    if ratio <= 0.8:
        return 0.85
    # lerp [0.80, 1.00] → [0.85, 1.00]
    return 1.0 - (1.0 - ratio) / 0.20 * 0.15


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


def apply_form(base_value: float, form_modifier: float) -> float:
    """Apply a pre-computed form modifier to a base trade value."""
    return round(base_value * form_modifier, 2)


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
    Both cpu_trade_service._compute_team_posture and trade_service._cpu_evaluate
    call this function so propose-side and accept-side always agree.

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


def experience_premium(
    age: int,
    position: str,
    gp: int | None,
    ppg: float | None,
    apg: float | None,
    ovr: int,
) -> float:
    """
    Proven veteran premium — up to +15% on top of base × form_modifier.

    A player earns a premium based on:
    - NBA experience (proxy: age − 22 = years_exp). 4+ yrs = candidate.
    - Durability this season: GP ≥ 50 = proven durable; GP ≥ 70 = workhorse.
    - Playmaker production above expectation (PG/SG with APG above heuristic + GP ≥ 50).

    Formula:
      exp_bump       = min(0.10, years_exp × 0.012)  — caps at 10% by ~year 8
      durability_bump = 0.03 if GP ≥ 70 else 0.02 if GP ≥ 50 else 0
      play_bump      = 0.05 for PG/SG overperforming expected APG with 50+ GP
      total premium = 1.0 + exp_bump + durability_bump + play_bump  (cap 1.15)

    Returns 1.0 for players with < 4 years experience or fewer than 30 GP.

    Example — VanVleet (age 32, OVR 81, 66 GP, 8.2 APG, PG):
      years_exp = 10  → exp_bump = min(0.10, 0.12) = 0.10
      durability = GP 66 ≥ 50 → 0.02
      expected APG ≈ max(2, (81−70)×0.4) = 4.4; 8.2 ≥ 4.4×1.15 → play_bump = 0.05
      premium = 1.0 + 0.10 + 0.02 + 0.05 = 1.17  (≤ 1.15 cap → 1.15)
    """
    years_exp = max(0, age - 22)
    # Premium only applies to established contributors (OVR ≥ 75).
    # Sub-75 role players are already OVR-discounted; a veteran premium would
    # inflate fringe bench players above their actual market value.
    if years_exp < 4 or gp is None or gp < 30 or ovr < 75:
        return 1.0

    exp_bump = min(0.10, years_exp * 0.012)
    durability_bump = 0.03 if gp >= 70 else (0.02 if gp >= 50 else 0.0)

    play_bump = 0.0
    if position in ("PG", "SG") and apg is not None and gp >= 50:
        expected_apg = max(2.0, (ovr - 70) * 0.4)
        if apg >= expected_apg * 1.15:
            play_bump = 0.05

    return min(1.15, 1.0 + exp_bump + durability_bump + play_bump)


def _age_multiplier(age: int, overall: int = 75) -> float:
    """Age value multiplier, tier-scaled by OVR.

    Stars age more gracefully than role players — a 35-year-old superstar
    putting up elite numbers retains most of their trade value (LeBron-tier),
    while a 35-year-old bench player has none. Previously the curve was flat
    by OVR which crushed late-career stars like Harden (age 35 OVR 88 ended
    up at 0.55 multiplier — same as a 35-year-old 12th man).

    Three tiers:
    - Star tier (OVR 85+): gentle decline, retains 80%+ through age 36
    - Quality tier (OVR 80-84): moderate decline
    - Role tier (OVR < 80): original steep curve (career-arc reality for non-stars)
    """
    # Granularity added in 24-26 band (was flat) and post-30 decline steepened
    # so young+good carries a meaningfully higher premium than older+same-OVR.
    if overall >= 85:
        if age <= 21:
            return 1.45
        if age <= 23:
            return 1.35
        if age == 24:
            return 1.25
        if age == 25:
            return 1.20
        if age == 26:
            return 1.15
        if age <= 30:
            return 1.0
        if age <= 33:
            return 0.92
        if age <= 36:
            return 0.78
        return max(0.55, 0.70 - (age - 37) * 0.05)
    if overall >= 80:
        if age <= 21:
            return 1.50
        if age <= 23:
            return 1.38
        if age == 24:
            return 1.28
        if age == 25:
            return 1.22
        if age == 26:
            return 1.15
        if age <= 29:
            return 1.0
        if age == 30:
            return 0.95
        if age <= 33:
            return 0.82
        if age <= 36:
            return 0.60
        return max(0.30, 0.50 - (age - 37) * 0.08)
    # Role / bench tier — original brutal curve, slightly steeper at the edges
    if age <= 21:
        return 1.55
    if age <= 23:
        return 1.42
    if age == 24:
        return 1.28
    if age == 25:
        return 1.20
    if age == 26:
        return 1.12
    if age <= 30:
        return 1.0
    if age <= 33:
        return 0.78
    if age <= 36:
        return 0.55
    return max(0.1, 0.4 - (age - 37) * 0.08)


def _upside_modifier(age: int, overall: int) -> float:
    """Return a trajectory multiplier in [0.85, 1.10] based on age/OVR upside profile.

    Captures the market reality that two players with identical OVR/age can have
    very different trade value depending on whether the league expects growth.

    Tiers (evaluated in order):
    1. Young upside (age ≤ 24): 1.10 — still developing, breakout possible.
    2. Mid-career star (age 25-30, OVR ≥ 84): 1.05 — high ceiling, prime window.
    3. Mid-career role player (age 25-30, OVR < 82): 0.90 — "DiVincenzo case."
       Established role player; no realistic breakout upside.
    4. Mid-career middle tier (age 25-30, OVR 82-83): 1.0 — ambiguous trajectory.
    5. Veteran (age 31+): 1.0 — no upside discount; age_multiplier already handles
       decline. Applying an additional penalty here would double-count.

    Example — DiVincenzo (OVR 79, age 27):
      age 27 in [25-30], OVR 79 < 82 → upside_modifier = 0.90 (~10% discount)

    Example — 27yo with OVR 86:
      age 27 in [25-30], OVR 86 ≥ 84 → upside_modifier = 1.05 (unchanged tier)
    """
    if age <= 24:
        return 1.10
    if age <= 30:
        if overall >= 84:
            return 1.05
        if overall < 82:
            return 0.90
        # OVR 82-83: neutral
        return 1.0
    # age 31+
    return 1.0


def defensive_impact_modifier(
    player: dict,
    season_stats: dict | None = None,
) -> float:
    """Bump value for defensive aces whose impact isn't captured in PPG/APG.

    Returns a multiplier in [1.0, 1.20].

    Two eligibility paths (either triggers the modifier):

    Path A — tendency-based (formula-driven):
      defense_tendency >= 75 AND avg(blk_tendency, stl_tendency) >= 70.
      Works well for players whose tendency formula captured their defense.

    Path B — static-attribute fallback:
      player.defense >= 85.  Catches elite defenders (Gobert, Bam, AD, etc.)
      whose per-game stl/blk numbers are suppressed by the center-average baseline
      even though their curated defense attribute correctly marks them as elite.
      Multipliers from the attribute tier:
        defense >= 92 → 1.20 (DPOY-tier)
        defense 88-91 → 1.15
        defense 85-87 → 1.10
      If season stats also show a defensive_score (bpg*1.5 + spg*1.2) >= 3.0,
      the stat-path cap of 1.20 applies (no double-bump above that ceiling).

    Caps at 1.20 — a true DPOY-tier player gets +20% value, not double their OVR.
    Non-eligible players always return 1.0 so the modifier is invisible to everyone else.
    """
    defense_tendency = player.get("defense_tendency", 0)
    blk_tendency = player.get("blk_tendency", 0)
    stl_tendency = player.get("stl_tendency", 0)
    defense_attr = player.get("defense", 0) or 0

    def_avg = (blk_tendency + stl_tendency) / 2.0

    # Path A: tendency-based eligibility gate.
    tendency_eligible = defense_tendency >= 75 and def_avg >= 70

    # Path B: static defense-attribute gate for curated elite defenders.
    attr_eligible = defense_attr >= 85

    if not tendency_eligible and not attr_eligible:
        return 1.0

    # Compute stat-based modifier when sufficient games are available.
    # This applies to both paths — a big block/steal season earns full 1.20.
    if season_stats is not None and season_stats.get("games_played", 0) >= 30:
        bpg = season_stats.get("bpg", 0.0) or 0.0
        spg = season_stats.get("spg", 0.0) or 0.0
        # Blocks weighted more (rim protection counts more than poke-steals).
        defensive_score = bpg * 1.5 + spg * 1.2
        if defensive_score >= 3.0:
            return 1.20
        if defensive_score >= 2.0:
            return 1.10
        if defensive_score >= 1.5:
            return 1.05

        # Stat path didn't clear minimum threshold. Fall through to attribute path
        # if that's what made the player eligible — don't return 1.0 on a bad
        # sample for someone with defense=90.
        if tendency_eligible:
            return 1.0
        # attr_eligible path: attribute tier applies even when stat sample is weak.

    # Attribute-tier multiplier (Path B or early season with tendency eligibility).
    if attr_eligible:
        if defense_attr >= 92:
            return 1.20
        if defense_attr >= 88:
            return 1.15
        # 85-87
        return 1.10

    # tendency_eligible, no useful stats — benefit of the doubt.
    return 1.08


def _contract_modifier(salary: int, years_remaining: int, salary_cap: int) -> float:
    salary_ratio = salary / (salary_cap * 0.25)

    if years_remaining == 1:
        years_mod = 0.8
    elif years_remaining == 2:
        years_mod = 1.0
    elif years_remaining == 3:
        years_mod = 1.1
    else:
        years_mod = 0.9

    modifier = years_mod
    if salary_ratio > 1.5:
        modifier *= 0.6
    return modifier


def player_trade_value(
    player: dict,
    contract: dict,
    salary_cap: int,
    season_stats: dict | None = None,
    form_modifier: float = 1.0,
) -> float:
    """
    Score a player's trade value — the abstract market value for a fair trade.

    Factors (all compound):
    - overall rating (exponential curve: overall ** 1.45, normalized so OVR 80 anchors
      at 40, matching the R1 pick base value).  The steeper exponent concentrates value
      at the top tier: the OVR 89 vs 88 adjacent gap is ~1.7% (vs 1.5% at ^1.3), and
      the OVR 95 vs 80 span is ~30% (vs 25% at ^1.3).  Bench players (< 80) get a
      larger discount.  Picks stay in scale because the normalization keeps the
      OVR-80 anchor at ~40.
      Normalization: 80 ** 1.45 = 488.94  →  _OVR_NORM = 488.94 / 40 = 12.2236
    - age: younger = more value. peak at 24-26, drops sharply after 36.
    - upside: trajectory modifier based on age/OVR tier (see _upside_modifier).
      Mid-career role players (age 25-30, OVR < 82) get 0.90 — "no-growth" discount.
    - contract: bad contracts reduce value.
      salary_ratio = salary / (salary_cap * 0.25)  # 1.0 = max contract worth
      years_remaining: 1yr = 0.8 modifier, 2yr = 1.0, 3yr = 1.1, 4yr+ = 0.9 (long commitment risk)
      if salary_ratio > 1.5: value *= 0.6  # overpaid, hard to move
    - season_stats (optional): dict with keys ppg, apg, games_played.
      When provided, applies experience_premium() — up to +15% for proven veterans
      with strong durability and playmaking track record.
    - form_modifier (optional, default 1.0): pre-computed PPG-based performance modifier.
      Pass the value from compute_form_map to include in-season form in the final value.
      Now capped at 1.30 (was 1.15) for elite production.

    Formula: base × age_mult × upside_mod × contract_mod × form_modifier × exp_premium × defensive_impact_mod.

    defensive_impact_mod (1.0–1.20): bumps elite defenders whose value isn't
    captured in PPG/APG.  Two paths: tendency gate (defense_tendency >= 75 AND
    avg(blk,stl) >= 70) OR static-attribute gate (player.defense >= 85).
    Attribute tier: >=92→1.20, 88-91→1.15, 85-87→1.10.
    Returns 1.0 for all other players — invisible to them.

    Example — VanVleet (OVR 81, age 32, 66 GP, 8.2 APG, PG, form_modifier=1.15):
      base ≈ 47.9; age_mult = 0.8; upside_mod = 1.0 (age 32 → veteran tier)
      contract_mod = 0.8 → raw ≈ 30.6
      form_modifier = 1.15; exp_premium = 1.15 → final ≈ 40.5

    Example — DiVincenzo (OVR 79, age 27, role player):
      base ≈ 43.7; age_mult = 1.0 (role tier, age 27-30)
      upside_mod = 0.90 (age 25-30, OVR < 82 — "filled in" role player)
      contract_mod ≈ 1.0 → after upside: ~39.3 vs ~43.7 without the modifier
    """
    overall = player.get("overall", 0)
    age = player.get("age", 28)
    position = player.get("position", "")
    salary = contract.get("salary", 0)
    years_remaining = contract.get("years_remaining", 1)

    # Exponential OVR curve normalized so OVR 80 anchors at 40.0.  See _OVR_NORM.
    base = (overall ** 1.45) / _OVR_NORM

    age_mult = _age_multiplier(age, overall)
    upside_mod = _upside_modifier(age, overall)
    contract_mod = _contract_modifier(salary, years_remaining, salary_cap)
    def_mod = defensive_impact_modifier(player, season_stats)

    value = base * age_mult * upside_mod * contract_mod * form_modifier * def_mod

    # Apply proven-veteran experience premium when season stats are available.
    if season_stats is not None:
        gp = season_stats.get("games_played")
        ppg = season_stats.get("ppg")
        apg = season_stats.get("apg")
        exp_mult = experience_premium(age, position, gp, ppg, apg, overall)
        value *= exp_mult

    return round(value, 2)


# player_trade_value is the canonical market-value function.
# Layer 3 adds player_team_specific_value on top; callers that only need the
# abstract fair-market price should continue calling player_trade_value directly.
player_market_value = player_trade_value  # alias used by team-specific value path


def player_team_specific_value(
    player: dict,
    contract: dict,
    receiving_team_context: dict | None,
    salary_cap: int,
    season_stats: dict | None = None,
    form_modifier: float = 1.0,
) -> float:
    """
    Return how much this player is worth specifically to the receiving team.

    Starts from player_market_value, then compounds team-context modifiers:

    1. Cap fit: if contract.salary + team_current_payroll > salary_cap * 1.20,
       multiply by 0.80 — team can't absorb the player without major cuts.

    2. Roster construction fit:
       - If the player's position has 3+ OVR-75+ players already: × 0.85 (saturated)
       - If the player's position has 0-1 OVR-75+ players: × 1.10 (needed)
       - Otherwise: × 1.0

    3. Window match (receiving team mode × player age):
       - contending + age ≥ 33: × 1.05  (win-now help worth premium)
       - rebuilding + age ≥ 30: × 0.70  (old vet doesn't fit timeline)
       - rebuilding + age ≤ 24: × 1.15  (young asset, perfect fit)
       - soft_rebuild + age ≥ 32: × 0.80
       - all other combos: × 1.0

    All three modifiers compound on market value.
    Falls back to market value when receiving_team_context is None.

    receiving_team_context dict keys:
        team_id (int)
        mode (str): one of contending / play_in_fringe / soft_rebuild / rebuilding / developing
        current_payroll (int): sum of all active salaries on the receiving roster
        position_counts (dict[str, int]): position → count of OVR-75+ players on roster
    """
    market_val = player_market_value(player, contract, salary_cap, season_stats, form_modifier)

    if receiving_team_context is None:
        return market_val

    age = player.get("age", 28)
    position = player.get("position", "")
    salary = contract.get("salary", 0)
    mode = receiving_team_context.get("mode", "developing")
    current_payroll = receiving_team_context.get("current_payroll", 0)
    position_counts = receiving_team_context.get("position_counts", {})

    # 1. Cap fit modifier
    if salary + current_payroll > salary_cap * 1.20:
        cap_fit = 0.80
    else:
        cap_fit = 1.0

    # 2. Roster construction fit modifier
    pos_count = position_counts.get(position, 0)
    if pos_count >= 3:
        roster_fit = 0.85
    elif pos_count <= 1:
        roster_fit = 1.10
    else:
        roster_fit = 1.0

    # 3. Window match modifier
    if mode == "contending" and age >= 33:
        window_match = 1.05
    elif mode == "rebuilding" and age >= 30:
        window_match = 0.70
    elif mode == "rebuilding" and age <= 24:
        window_match = 1.15
    elif mode == "soft_rebuild" and age >= 32:
        window_match = 0.80
    else:
        window_match = 1.0

    team_value = market_val * cap_fit * roster_fit * window_match
    return round(team_value, 2)


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


def pick_trade_value(
    season: int,
    round_num: int,
    current_season: int,
    team_win_pct: float | None = None,
) -> float:
    """
    Score a draft pick's value.

    Base values (recalibrated to match realistic NBA pick market):
    - R1 base: 28.0  (most R1s are starter-or-bust; top-3 slots are the rare elite)
    - R2 base:  4.0  (dart throw; fringe rotation player if hit)
    - Decay: −5 per season gap from current, floor at 1.0 (far-future picks never zero)
    - Current-season multiplier: ×1.3 (knowing position helps, but not dramatically)
    - team_win_pct modifier for R1 picks only (lottery vs contender slot matters):
        win_pct 0.25 → ×~1.35  (lottery team, high pick)
        win_pct 0.50 → ×1.0    (average, neutral)
        win_pct 0.70 → ×~0.73  (contender, late pick)

    Reference values:
      R2 current season:            4.0 × 1.3            = 5.2
      R1 current, contender (0.70): 28.0 × 1.3 × 0.73   = 26.6
      R1 next season, average:      (28.0 − 5) × 1.0     = 23.0
      R2 3 yrs out:                 max(1.0, 4.0 − 15)   = 1.0
      R1 3 yrs out:                 max(1.0, 28.0 − 15)  = 13.0

    Pass team_win_pct=None to skip record adjustment (unknown team or pre-season).
    Returns a float score.
    """
    base = 28.0 if round_num == 1 else 4.0
    season_gap = max(0, season - current_season)
    value = base - (5.0 * season_gap)
    value = max(1.0, value)  # far-future picks never decay to zero
    if season_gap == 0:
        value *= 1.3

    # Apply team-quality discount/premium for 1st-round picks when record is known.
    # win_pct 0.25  → multiplier ~1.35  (lottery team, high pick)
    # win_pct 0.50  → multiplier ~1.0   (average, neutral)
    # win_pct 0.70  → multiplier ~0.73  (contender, low pick)
    if round_num == 1 and team_win_pct is not None:
        # Linear: 1.0 + (0.5 - win_pct) * 0.70, clamped [0.50, 1.60]
        record_mult = 1.0 + (0.5 - team_win_pct) * 0.70
        record_mult = max(0.50, min(1.60, record_mult))
        value *= record_mult

    return round(value, 2)


def evaluate_trade(
    side_a_players: list[dict],
    side_a_picks: list[dict],
    side_b_players: list[dict],
    side_b_picks: list[dict],
    salary_cap: int,
    current_season: int,
    receiving_team_context_a: dict | None = None,
    receiving_team_context_b: dict | None = None,
) -> dict:
    """
    Evaluate a trade from both teams' perspectives.

    When receiving_team_context_a/b are provided, uses player_team_specific_value
    so each side's valuation reflects their roster construction, cap situation,
    and rebuild window.  Falls back to player_market_value when context is None.

    score_a = what team A gives up (valued from team B's perspective when context_b provided)
    score_b = what team B gives up (valued from team A's perspective when context_a provided)

    This means: team A "receives" side_b assets and evaluates them via context_a;
                team B "receives" side_a assets and evaluates them via context_b.
    Fairness: both teams must feel their received value ≥ given value.

    Returns {
        "score_a": float,         # value of side_a assets (from team_b context)
        "score_b": float,         # value of side_b assets (from team_a context)
        "market_score_a": float,  # same without team context (abstract market)
        "market_score_b": float,
        "differential": float,    # abs(score_a - score_b) using team-specific values
        "is_fair": bool,          # team-specific differential < 20% of max side
        "rationale": str,
    }
    """
    def _player_val(p: dict, context: dict | None) -> float:
        if context is not None:
            return player_team_specific_value(
                p["player"], p["contract"], context, salary_cap,
                p.get("season_stats"),
                p.get("form_modifier", 1.0),
            )
        return player_trade_value(
            p["player"], p["contract"], salary_cap,
            p.get("season_stats"),
            p.get("form_modifier", 1.0),
        )

    def _market_val(p: dict) -> float:
        return player_trade_value(
            p["player"], p["contract"], salary_cap,
            p.get("season_stats"),
            p.get("form_modifier", 1.0),
        )

    # Team-specific scores: side_a valued from team_b's perspective (they receive it)
    score_a = sum(_player_val(p, receiving_team_context_b) for p in side_a_players)
    score_a += sum(pick_trade_value(p["season"], p["round"], current_season) for p in side_a_picks)

    # side_b valued from team_a's perspective (they receive it)
    score_b = sum(_player_val(p, receiving_team_context_a) for p in side_b_players)
    score_b += sum(pick_trade_value(p["season"], p["round"], current_season) for p in side_b_picks)

    # Abstract market scores (always computed for display/logging)
    market_score_a = sum(_market_val(p) for p in side_a_players)
    market_score_a += sum(pick_trade_value(p["season"], p["round"], current_season) for p in side_a_picks)
    market_score_b = sum(_market_val(p) for p in side_b_players)
    market_score_b += sum(pick_trade_value(p["season"], p["round"], current_season) for p in side_b_picks)

    differential = abs(score_a - score_b)
    max_side = max(score_a, score_b, 1.0)
    is_fair = differential < (max_side * 0.20)

    if is_fair:
        rationale = f"Trade is roughly balanced (A gives {score_a:.1f}, B gives {score_b:.1f})."
    else:
        heavier = "A" if score_a > score_b else "B"
        rationale = (
            f"Team {heavier} gives significantly more value "
            f"(A: {score_a:.1f} vs B: {score_b:.1f}, gap {differential:.1f})."
        )

    return {
        "score_a": score_a,
        "score_b": score_b,
        "market_score_a": market_score_a,
        "market_score_b": market_score_b,
        "differential": differential,
        "is_fair": is_fair,
        "rationale": rationale,
    }


def grade_trade(score_a: float, score_b: float) -> tuple[str, str]:
    """
    Assign letter grades to both sides of a trade based on value differential.

    Returns (grade_for_team_a, grade_for_team_b) where team_a receives score_b
    assets and team_b receives score_a assets. Grades reflect who won the trade.

    Differential thresholds are expressed as a fraction of max(score_a, score_b):
      < 5%  → A / A  (even)
      5-15% → B+ / B- (slight edge)
      15-30% → B / C  (clear winner)
      30%+  → A / D  (lopsided)
    """
    max_side = max(score_a, score_b, 1.0)
    diff = abs(score_a - score_b)
    pct = diff / max_side

    if pct < 0.05:
        return "A", "A"

    winner_grade: str
    loser_grade: str
    if pct < 0.15:
        winner_grade, loser_grade = "B+", "B-"
    elif pct < 0.30:
        winner_grade, loser_grade = "B", "C"
    else:
        winner_grade, loser_grade = "A", "D"

    # score_a = total value team_a gives away (team_b receives)
    # score_b = total value team_b gives away (team_a receives)
    # team_a wins the trade if score_a < score_b (receives more than it gives)
    if score_b >= score_a:
        return winner_grade, loser_grade
    return loser_grade, winner_grade


def _player_archetype(player: dict) -> str | None:
    """
    Classify a player into one of five archetypes using tendency columns.
    player dict keys: overall, age, and optionally tendency_3pt, tendency_drive,
    tendency_pass, ast_tendency, reb_tendency, blk_tendency, stl_tendency, position.
    Returns the archetype string or None when no clear archetype matches.

    Priority order matters — a player can only have one label here:
    interior_big > two_way_wing > playmaker > slasher > shooter
    """
    pos = player.get("position", "")
    t3pt = player.get("tendency_3pt", 50)
    tdrive = player.get("tendency_drive", 50)
    tpass = player.get("tendency_pass", 50)
    tast = player.get("ast_tendency", 50)
    treb = player.get("reb_tendency", 50)
    tblk = player.get("blk_tendency", 50)
    tstl = player.get("stl_tendency", 50)
    tdef = (tblk + tstl) // 2  # composite defensive tendency proxy

    if treb > 70 and pos in ("C", "PF"):
        return "interior_big"
    if tdef > 70 and pos in ("SF", "SG"):
        return "two_way_wing"
    if tpass > 65 or tast > 80:
        return "playmaker"
    if tdrive > 65:
        return "slasher"
    if t3pt > 65:
        return "shooter"
    return None


def _team_primary_scheme(receiving_assets: list[dict]) -> str | None:
    """
    Infer the receiving team's primary scheme from the players they're giving away.
    This gives a rough signal of what archetype they already have/need.
    Returns an archetype string or None.
    """
    archetypes: list[str] = []
    for a in receiving_assets:
        if a.get("asset_type") != "player":
            continue
        arch = _player_archetype(a.get("player", {}))
        if arch:
            archetypes.append(arch)
    if not archetypes:
        return None
    # Most common archetype among what they're giving up is what they have plenty of —
    # the complement is what they need, but we return what they have for scheme match.
    return Counter(archetypes).most_common(1)[0][0]


LEAD_ROLES: frozenset[str] = frozenset({
    "primary_initiator", "iso_scorer", "post_anchor",
    "slashing_lead", "movement_shooter",
    "rim_protector", "two_way_big", "switching_big", "wing_stopper",
})


async def cpu_should_accept(
    cpu_team_mode: str,
    assets_receiving: list,
    assets_giving: list,
    evaluation: dict,
    salary_cap: int,
    current_cap_used: int,
    *,
    giving_role_map: dict[int, str] | None = None,
    pool=None,
    context_kwargs: dict | None = None,
) -> tuple[bool, str]:
    """
    Returns (accept: bool, reason: str).
    Rules:
    - Reject if evaluation['differential'] > 25% of max side
    - Rebuilding CPU: prefers picks > vets. Accepts if receiving picks or youth (age < 26).
    - Contending CPU: prefers proven players. Reluctant to give picks.
    - Reject if accepting would put CPU over salary_cap.

    Mutates `evaluation` in place: adds 'context_signals_per_player' and
    'score_a_after_context' keys for ride-along / ra_reasoning access.
    - Never give up a player with overall >= 88 unless rebuilding AND getting 2+ first-rounders.
    - Buy-low fit bonus: underperforming player (form_modifier < 0.92) gets a value bump
      from the receiving team's perspective when archetype fits their scheme or mode.
    """
    # Normalize mode string defensively — callers may pass un-stripped or mixed-case values.
    cpu_team_mode = (cpu_team_mode or "").lower().strip()

    score_a = evaluation["score_a"]
    score_b = evaluation["score_b"]
    max_side = max(score_a, score_b, 1.0)
    differential = evaluation["differential"]

    # CPU is the counterparty — score_b is what CPU gives, score_a is what CPU receives.
    # assets_receiving = what CPU gets, assets_giving = what CPU gives.

    # ── Context modifier (ride-along-aligned signals) ─────────────────────────
    # Apply per-player context modifiers to score_a (what CPU receives).
    # Each incoming player's contribution is scaled by a modifier in [0.85, 1.15]
    # derived from synergy, role-fit, window-fit, defense quality, overperformance,
    # and scheme-fit detectors in trade_context.  The same signals drive the
    # ride-along narrative bullets so user reads what actually moved the math.
    context_signals_per_player: dict[int, list] = {}
    if pool is not None and context_kwargs is not None:
        _tc = _get_trade_context()
        league_id = context_kwargs.get("league_id")
        season = context_kwargs.get("season")
        perspective_team_id = context_kwargs.get("perspective_team_id")
        plan = context_kwargs.get("plan") or {}
        posture = context_kwargs.get("posture") or {}
        coach_philosophy = context_kwargs.get("coach_philosophy")

        # Build per-player coroutines and gather in parallel; filter exceptions.
        _player_assets = [
            (a, a.get("player", {}), a.get("player", {}).get("id"))
            for a in assets_receiving
            if a.get("asset_type") == "player" and a.get("player", {}).get("id")
        ]

        async def _compute_one(a_item, p_dict, pid):
            form_mod = a_item.get("form_modifier", 1.0)
            player_stats = a_item.get("season_stats") or {}
            player_for_ctx = dict(p_dict)
            modifier, signals = await _tc.compute_context_modifier(
                pool=pool,
                league_id=league_id,
                season=season,
                perspective_team_id=perspective_team_id,
                plan=plan,
                posture=posture,
                coach_philosophy=coach_philosophy,
                incoming_player=player_for_ctx,
                form_mod=form_mod,
                stats=player_stats,
            )
            return pid, modifier, signals, a_item, player_for_ctx

        # return_exceptions=True preserves index order so enumerate(_ctx_results)[_idx] maps correctly to _player_assets[_idx].
        _ctx_results = await asyncio.gather(
            *[_compute_one(a, p, pid) for a, p, pid in _player_assets],
            return_exceptions=True,
        )

        for _idx, _res in enumerate(_ctx_results):
            if isinstance(_res, BaseException):
                _pid = _player_assets[_idx][2]
                log.warning("context modifier failed pid=%d: %s", _pid, _res)
                continue
            pid, modifier, signals, a_item, player_for_ctx = _res
            context_signals_per_player[pid] = [s._asdict() for s in signals]
            # Apply context modifier to player's market-value contribution in score_a.
            # Score adjustment: add (modifier - 1.0) * player_value to score_a.
            form_mod = a_item.get("form_modifier", 1.0)
            player_base = player_trade_value(
                player_for_ctx,
                a_item.get("contract", {}),
                salary_cap,
                a_item.get("season_stats") or None,
                form_mod,
            )
            delta_from_context = player_base * (modifier - 1.0)
            score_a += delta_from_context
            if abs(delta_from_context) >= 0.5:
                log.info(
                    "[CPU] context modifier pid=%d modifier=%.4f delta=%.2f "
                    "codes=%s",
                    pid, modifier, delta_from_context,
                    [s.get("code") for s in context_signals_per_player[pid]],
                )

        # Recompute differential/max_side after context adjustment.
        differential = abs(score_a - score_b)
        max_side = max(score_a, score_b, 1.0)

    # Stash signals in evaluation dict so callers (ride-along, ra_reasoning) can read them.
    evaluation["context_signals_per_player"] = context_signals_per_player
    evaluation["score_a_after_context"] = score_a
    # ── End context modifier ──────────────────────────────────────────────────

    # ── Buy-low fit bonus ────────────────────────────────────────────────────
    # When a player arriving is underperforming (form_modifier < 0.92), apply a
    # bonus to score_a (what CPU receives) based on archetype fit and team mode.
    # This can flip close reject → accept when a team is a good landing spot.
    fit_bonus_total = 0.0
    fit_bonus_notes: list[str] = []
    for a in assets_receiving:
        if a.get("asset_type") != "player":
            continue
        form_mod = a.get("form_modifier", 1.0)
        if form_mod >= 0.92:
            continue  # not underperforming — no buy-low consideration
        arch = _player_archetype(a.get("player", {}))
        if arch is None:
            continue  # no clear archetype — skip

        base_player_value = player_trade_value(
            a.get("player", {}),
            a.get("contract", {}),
            salary_cap,
        )

        bonus_pct = 0.0
        if cpu_team_mode in ("rebuilding", "soft_rebuild", "developing"):
            # Rebuilders buy low regardless of scheme — that's their bread and butter.
            bonus_pct = 0.10
        elif cpu_team_mode in ("contending", "play_in_fringe"):
            # Contenders only want a buy-low if the archetype fills a scheme gap.
            # We compare archetype against what they're giving up (their scheme).
            giving_arch = _team_primary_scheme(assets_giving)
            # A different archetype arriving = scheme complement = good fit.
            if giving_arch and arch != giving_arch:
                bonus_pct = 0.05 if cpu_team_mode == "contending" else 0.04

        if bonus_pct > 0:
            bonus = base_player_value * bonus_pct
            fit_bonus_total += bonus
            fit_bonus_notes.append(
                f"{arch} (form={form_mod:.2f}) buy-low +{bonus:.1f}"
            )

    if fit_bonus_total > 0:
        # Re-evaluate with bonus folded into score_a.
        adjusted_score_a = score_a + fit_bonus_total
        log.info(
            f"[CPU] buy-low fit bonus applied: {'; '.join(fit_bonus_notes)} "
            f"| score_a {score_a:.1f} → {adjusted_score_a:.1f}"
        )
        score_a = adjusted_score_a
        differential = abs(score_a - score_b)
        max_side = max(score_a, score_b, 1.0)
    # ── End buy-low fit bonus ─────────────────────────────────────────────────

    # ── Hard fleecing floor ───────────────────────────────────────────────────
    # After all bonuses: if CPU would receive less than the mode-appropriate floor
    # of what it gives up, force-reject.  The floor is 0.85 by default; for
    # rebuild-adjacent teams receiving a young, quality player it softens to 0.70
    # ("rebuild buys young" exemption — they're buying age, not raw value).
    if score_b > 0:
        _effective_ratio = score_a / score_b
        _floor, _is_softened = _effective_fleecing_floor(cpu_team_mode, assets_receiving)
        if _effective_ratio < _floor:
            _suffix = " [rebuild softened]" if _is_softened else ""
            _reason = (
                f"forced reject: fleecing floor "
                f"(effective ratio {_effective_ratio:.2f} < {_floor:.2f}){_suffix}"
            )
            log.info(f"[CPU] counterparty {_reason}")
            return False, _reason
    # ── End fleecing floor ────────────────────────────────────────────────────

    # ── B5: Asymmetric rejection — tighter threshold for contending/fringe ──────
    # After B1/B3/B6 modifiers: if a win-now or fringe team is receiving materially
    # less value than they're giving up with no compensating strategic gain, reject.
    # "Strategic gain" means: at least one R1 pick incoming, OR significant cap
    # relief (incoming salary < outgoing by >15% of cap).  Without strategic gain,
    # the rejection threshold tightens from 25% → 15% differential.
    _b5_threshold = 0.25  # default differential tolerance
    if cpu_team_mode in ("contending", "play_in_fringe") and score_b > score_a:
        _receiving_r1s = sum(
            1 for a in assets_receiving
            if a.get("asset_type") == "pick" and a.get("round") == 1
        )
        _incoming_sal = sum(
            a.get("contract", {}).get("salary", 0)
            for a in assets_receiving if a.get("asset_type") == "player"
        )
        _outgoing_sal = sum(
            a.get("contract", {}).get("salary", 0)
            for a in assets_giving if a.get("asset_type") == "player"
        )
        _cap_relief = _outgoing_sal - _incoming_sal  # positive = we get cap relief
        _has_strategic_gain = (
            _receiving_r1s >= 1
            or _cap_relief >= salary_cap * 0.15
        )
        if not _has_strategic_gain:
            _b5_threshold = 0.15  # tighter: no picks or cap relief = harder standard
    # ── End B5 setup ─────────────────────────────────────────────────────────────

    if differential > max_side * _b5_threshold:
        losing_side = score_b > score_a
        if losing_side:
            _b5_note = " [B5: no strategic gain, tight threshold]" if _b5_threshold < 0.25 else ""
            return False, f"CPU evaluated the trade as too lopsided against its interests.{_b5_note}"

    giving_players = [a for a in assets_giving if a.get("asset_type") == "player"]
    receiving_picks = [a for a in assets_receiving if a.get("asset_type") == "pick"]
    receiving_players = [a for a in assets_receiving if a.get("asset_type") == "player"]

    # ── B3: High-upside asset guard — don't give away young/pedigree players cheap ──
    # Applies when the CPU is giving away a player whose upside is materially
    # underpriced in the raw OVR/value calculation: young (≤22) + decent OVR (≥74),
    # or very young (≤21) at any OVR.  When such a player is in assets_giving and
    # score_b is meaningfully higher than score_a, reject rather than let the value
    # math slide it through.
    if giving_players:
        for _ga in giving_players:
            _gp = _ga.get("player", {})
            _g_age = _gp.get("age", 99)
            _g_ovr = _gp.get("overall", 0)
            _is_high_upside = (
                (_g_age <= 21)
                or (_g_age <= 22 and _g_ovr >= 74)
                or (_g_age <= 23 and _g_ovr >= 79)
            )
            if _is_high_upside:
                # Upside floor: must receive at least 90% of what we give up
                # (tighter than the standard 85% fleecing floor).
                if score_b > 0 and (score_a / score_b) < 0.90:
                    _gname = f"{_gp.get('first_name', '?')} {_gp.get('last_name', '?')}"
                    return False, (
                        f"hard reject: giving away high-upside asset {_gname} "
                        f"(age {_g_age}, OVR {_g_ovr}) at only {score_a/score_b:.0%} return value"
                    )
    # ── End B3 guard ─────────────────────────────────────────────────────────────

    # ── Guard 1: Age + OVR asymmetry — won't sell low on a younger, better player ──
    # Blocks trades where the CPU gives up a meaningfully better AND younger player
    # without a proportionate return. The value math can miss this when salary tricks
    # make the trade look balanced — this guard fires regardless of score ratios.
    if giving_players and receiving_players:
        max_giving_ovr = max(a["player"].get("overall", 0) for a in giving_players)
        max_receiving_ovr = max(a["player"].get("overall", 0) for a in receiving_players)
        avg_giving_age = sum(a["player"].get("age") or 28 for a in giving_players) / len(giving_players)
        avg_receiving_age = sum(a["player"].get("age") or 28 for a in receiving_players) / len(receiving_players)

        ovr_gap = max_giving_ovr - max_receiving_ovr
        age_gap = avg_receiving_age - avg_giving_age  # positive = receiving older

        if (ovr_gap >= 3 and age_gap >= 2.0) or (ovr_gap >= 2 and age_gap >= 1.0):
            return False, (
                f"hard reject: parting with OVR {max_giving_ovr} (avg age {avg_giving_age:.1f}) "
                f"for OVR {max_receiving_ovr} (avg age {avg_receiving_age:.1f}) — selling low on younger asset"
            )
    # ── End Guard 1 ───────────────────────────────────────────────────────────────

    # ── Guard 2: Lead-role player requires equivalent return ──────────────────────
    # A player in a lead role (primary initiator, iso scorer, etc.) is harder to
    # replace than their OVR alone suggests. Giving one up without receiving either
    # a starter-quality player (OVR 78+) or a 1st-round pick is a bad deal by design.
    # Silently skips when giving_role_map is None — callers that haven't been updated
    # yet aren't penalised.
    if giving_role_map and giving_players:
        giving_lead_players = [
            a for a in giving_players
            if giving_role_map.get(a["player"].get("id")) in LEAD_ROLES
        ]
        if giving_lead_players:
            has_starter_tier = any(
                (a["player"].get("overall") or 0) >= 78
                for a in receiving_players
            )
            has_first_round_pick = any(
                a.get("asset_type") == "pick" and a.get("round") == 1
                for a in assets_receiving
            )
            # Elite lead-role players (OVR≥82, cornerstone tier) require BOTH
            # a starter-quality player AND a 1st-round pick. The starter OR pick
            # alternative is fine for an OVR-78 starter; an OVR-82+ cornerstone
            # needs more.  See module constant _ELITE_OVR_FLOOR.
            elite_giving = [
                p for p in giving_lead_players
                if (p["player"].get("overall") or 0) >= _ELITE_OVR_FLOOR
            ]
            if elite_giving:
                if not (has_starter_tier and has_first_round_pick):
                    elite_names = ", ".join(
                        f"{p['player'].get('first_name', '?')} {p['player'].get('last_name', '?')} "
                        f"(OVR {p['player'].get('overall', '?')}, {giving_role_map.get(p['player'].get('id'))})"
                        for p in elite_giving
                    )
                    return False, (
                        f"hard reject: giving up ELITE lead-role player(s) {elite_names} "
                        f"requires BOTH a starter-quality player (OVR≥78) AND a 1st-round pick"
                    )
            elif not (has_starter_tier or has_first_round_pick):
                lead_names = ", ".join(
                    f"{p['player'].get('first_name', '?')} {p['player'].get('last_name', '?')} "
                    f"({giving_role_map.get(p['player'].get('id'))})"
                    for p in giving_lead_players
                )
                return False, (
                    f"hard reject: giving up lead-role player(s) {lead_names} "
                    f"without receiving a starter-quality player (OVR≥78) or 1st-round pick"
                )
    # ── End Guard 2 ───────────────────────────────────────────────────────────────

    for asset in giving_players:
        player = asset.get("player", {})
        if player.get("overall", 0) >= 88:
            if cpu_team_mode != "rebuilding":
                return False, "CPU refuses to trade away a franchise cornerstone."
            first_rounders_incoming = sum(
                1 for p in receiving_picks
                if p.get("pick", {}).get("round", 2) == 1
            )
            if first_rounders_incoming < 2:
                return False, "CPU won't give up a star player without at least 2 first-round picks in return."

    incoming_salary = sum(
        a.get("contract", {}).get("salary", 0)
        for a in assets_receiving
        if a.get("asset_type") == "player"
    )
    outgoing_salary = sum(
        a.get("contract", {}).get("salary", 0)
        for a in assets_giving
        if a.get("asset_type") == "player"
    )
    net_cap_change = incoming_salary - outgoing_salary
    if current_cap_used + net_cap_change > salary_cap:
        return False, "Accepting this trade would put CPU over the salary cap."

    if cpu_team_mode == "rebuilding":
        has_picks = len(receiving_picks) > 0
        has_youth = any(
            a.get("player", {}).get("age", 30) < 26
            for a in receiving_players
        )
        # Buy-low on a high-OVR underperformer counts as a valid rebuild asset —
        # a player the team can develop/deploy better than their current situation allows.
        has_buy_low_star = any(
            a.get("form_modifier", 1.0) < 0.92 and a.get("player", {}).get("overall", 0) >= 80
            for a in receiving_players
            if a.get("asset_type") == "player"
        )
        if has_picks or has_youth or has_buy_low_star:
            reason = "CPU accepts — acquiring picks and young talent fits the rebuild."
            if has_buy_low_star and not (has_picks or has_youth):
                reason = "CPU accepts — buy-low on underperforming star fits the rebuild."
            if fit_bonus_notes:
                reason += f" [fit bonus: {'; '.join(fit_bonus_notes)}]"
            return True, reason
        return False, "CPU declined — rebuilding teams need picks and youth, not veteran salaries."

    if cpu_team_mode == "soft_rebuild":
        # Sell veterans — accept any deal that returns picks or youth (age < 25).
        # Less strict than rebuilding: will also accept OVR 72+ players under 26.
        has_picks = len(receiving_picks) > 0
        has_youth = any(
            a.get("player", {}).get("age", 30) < 25
            for a in receiving_players
        )
        has_young_talent = any(
            a.get("player", {}).get("age", 30) < 27
            and a.get("player", {}).get("overall", 0) >= 72
            for a in receiving_players
        )
        # "Rebuild buys young" — quality young player (age ≤ 25, OVR ≥ 75) incoming.
        # Mirrors the fleecing-floor exemption: soften the value threshold to 0.70
        # because the CPU is buying age/upside, not raw value.
        has_quality_young = any(
            a.get("player", {}).get("age", 30) <= 25
            and a.get("player", {}).get("overall", 0) >= 75
            for a in receiving_players
        )
        has_first_rounders = any(
            a.get("pick", {}).get("round", 2) == 1
            for a in receiving_picks
        )
        # Accept if we get picks or youth; be extra lenient if first-rounders included.
        if has_first_rounders and score_a >= score_b * 0.92:
            reason = "CPU accepts — first-round pick return fits the soft rebuild."
            if fit_bonus_notes:
                reason += f" [fit bonus: {'; '.join(fit_bonus_notes)}]"
            return True, reason
        # Quality young player incoming: use the softened 0.70 floor (buy age, not value).
        if has_quality_young and score_a >= score_b * REBUILD_BUYS_YOUNG_FLOOR:
            reason = "CPU accepts — acquiring quality young talent fits the soft rebuild [rebuild softened]."
            if fit_bonus_notes:
                reason += f" [fit bonus: {'; '.join(fit_bonus_notes)}]"
            return True, reason
        if (has_picks or has_youth or has_young_talent) and score_a >= score_b * 0.92:
            reason = "CPU accepts — acquiring picks and young talent moves the soft rebuild forward."
            if fit_bonus_notes:
                reason += f" [fit bonus: {'; '.join(fit_bonus_notes)}]"
            return True, reason
        return False, "CPU declined — soft rebuild teams need picks and young players."

    if cpu_team_mode == "contending":
        giving_picks = [a for a in assets_giving if a.get("asset_type") == "pick"]
        if giving_picks and not receiving_players:
            return False, "CPU declines — contending teams protect their future draft capital."
        high_value_incoming = any(
            a.get("player", {}).get("overall", 0) >= 80
            for a in receiving_players
        )
        if high_value_incoming:
            reason = "CPU accepts — acquiring a proven starter strengthens the contending roster."
            if fit_bonus_notes:
                reason += f" [fit bonus: {'; '.join(fit_bonus_notes)}]"
            return True, reason
        # Buy-low bonus may have made score_a competitive even without a high-OVR player.
        if fit_bonus_total > 0 and score_a >= score_b * 0.85:
            return True, f"CPU accepts — buy-low fit flips valuation: {'; '.join(fit_bonus_notes)}"
        return False, "CPU declined — assets don't meaningfully improve the contending roster."

    if cpu_team_mode == "play_in_fringe":
        # Like contending but slightly more flexible — will accept OVR 77+ players.
        giving_picks = [a for a in assets_giving if a.get("asset_type") == "pick"]
        if giving_picks and not receiving_players:
            return False, "CPU declines — fringe teams still protect draft capital."
        solid_incoming = any(
            a.get("player", {}).get("overall", 0) >= 77
            for a in receiving_players
        )
        if solid_incoming and score_a >= score_b * 0.97:
            reason = "CPU accepts — solid player upgrade fits the play-in push."
            if fit_bonus_notes:
                reason += f" [fit bonus: {'; '.join(fit_bonus_notes)}]"
            return True, reason
        if fit_bonus_total > 0 and score_a >= score_b * 0.90:
            return True, f"CPU accepts — buy-low fits the fringe push: {'; '.join(fit_bonus_notes)}"
        return False, "CPU declined — play-in fringe teams need clear upgrades."

    # developing mode: balanced, accept fair trades
    if evaluation["is_fair"] or score_a >= score_b * 0.80:
        reason = "CPU accepts — trade is balanced and fits team development."
        if fit_bonus_notes:
            reason += f" [fit bonus: {'; '.join(fit_bonus_notes)}]"
        return True, reason
    return False, "CPU declined — trade doesn't offer sufficient value."


async def get_ai_reasoning(trade_summary: dict, api_key: str | None) -> dict[str, str]:
    """
    Generate one-paragraph AI reasoning per trade side using Claude Haiku.

    trade_summary keys:
        team_a_name, team_b_name  — full team names
        team_a_record             — e.g. "32-18"
        team_b_record             — e.g. "20-30"
        team_a_players            — list of {full_name, position, overall, age}
        team_b_players            — list of {full_name, position, overall, age}
        grade_a, grade_b          — letter grades already assigned

    Returns {"team_a": "...", "team_b": "..."} or {} on failure.
    Silent fallback — never raises.
    """
    if not api_key:
        return {}

    def _player_line(p: dict) -> str:
        return f"{p.get('full_name', '?')} ({p.get('position', '?')}, OVR {p.get('overall', '?')}, age {p.get('age', '?')})"

    players_a = ", ".join(_player_line(p) for p in trade_summary.get("team_a_players", [])) or "picks only"
    players_b = ", ".join(_player_line(p) for p in trade_summary.get("team_b_players", [])) or "picks only"

    prompt = (
        "Grade this DBA trade. Be direct. No filler. No paragraph blocks.\n\n"
        f"TRADE: {trade_summary['team_a_name']} gives {players_a} | "
        f"{trade_summary['team_b_name']} gives {players_b}\n"
        f"{trade_summary['team_a_name']} record: {trade_summary.get('team_a_record', '?')} | "
        f"{trade_summary['team_b_name']} record: {trade_summary.get('team_b_record', '?')}\n"
        f"Grades: {trade_summary['team_a_name']} = **{trade_summary['grade_a']}** | "
        f"{trade_summary['team_b_name']} = **{trade_summary['grade_b']}**\n\n"
        "MANDATORY FORMAT — each team gets exactly two sentences:\n"
        "Sentence 1: the asymmetry (what one side got vs the other, bolding the key player/number).\n"
        "Sentence 2: the rationale (why this fits or hurts that team's situation).\n"
        "No openers. No conclusions. Two sentences per team, period.\n"
        'Return JSON: {"teamA": "...", "teamB": "..."}'
    )

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=api_key)
        message = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        # Strip markdown code fences if the model wraps its JSON output.
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and "teamA" in parsed and "teamB" in parsed:
            return {"team_a": str(parsed["teamA"]), "team_b": str(parsed["teamB"])}
    except Exception as exc:
        log.warning(f"AI trade reasoning failed, skipping: {exc}")

    return {}
