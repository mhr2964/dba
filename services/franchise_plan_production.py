"""Current-season production stat fetching for franchise plan derivation,
with a prior-season BDL cache fallback for early-season / fresh-league
players who don't yet have enough sim games to classify reliably.

Extracted from franchise_plan_service.py (Phase 3 opportunistic split, see
HANDOFF.md) along with franchise_plan_math.py.
"""
from __future__ import annotations

import json
import unicodedata
from pathlib import Path

from core.logging import get_logger

log = get_logger(__name__)


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
