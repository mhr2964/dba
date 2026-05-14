"""Populate players for a league from the 2024-25 NBA rosters via nba_api.

Usage:
    python scripts/import_players.py --league-id 1 --season 2024
    python scripts/import_players.py --league-id 1 --season 2024 --dry-run

Player overall ratings are derived entirely from NBA stats via nba_api:
  - Veterans: best composite score across the target season + 2 prior seasons
    (min 10 GP), using absolute stat thresholds so a Luka injury year doesn't
    crater his rating.
  - Rookies: draft pick position → OVR range (pick 1-3 ≈ 82-86, etc.)
  - Undrafted / data gaps: experience-based estimate.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import time
from pathlib import Path
from datetime import date
from typing import Optional

import asyncpg
from dotenv import load_dotenv
from nba_api.stats.endpoints.commonteamroster import CommonTeamRoster
from nba_api.stats.static import teams as nba_teams_static

load_dotenv()

# NBA team codes as used in the DBA teams table (matches nba_api abbreviations).
NBA_TEAM_CODES = [
    "ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW",
    "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NOP", "NYK",
    "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS",
]

# Per-position base adjustments relative to overall rating.
# Values are (mean_delta, max_noise). Each attribute = clamp(overall + delta ± noise, floor, ceil).
_POSITION_PROFILE: dict[str, dict[str, int]] = {
    "PG": {
        "speed": 8, "playmaking": 10, "shooting_3pt": 5,
        "shooting_2pt": 2, "shooting_mid": 2,
        "finishing": -5, "defense": -2, "rebounding": -12, "iq": 5,
    },
    "SG": {
        "speed": 4, "shooting_3pt": 8, "shooting_2pt": 8, "shooting_mid": 6,
        "playmaking": 2, "finishing": 0, "defense": 0, "rebounding": -8, "iq": 2,
    },
    "SF": {
        "speed": 0, "shooting_3pt": 2, "shooting_2pt": 2, "shooting_mid": 2,
        "finishing": 2, "playmaking": 0, "defense": 2, "rebounding": 0, "iq": 0,
    },
    "PF": {
        "speed": -4, "rebounding": 8, "finishing": 6, "shooting_mid": 4,
        "shooting_3pt": -8, "shooting_2pt": 0, "defense": 4, "playmaking": -4, "iq": 0,
    },
    "C": {
        "speed": -10, "rebounding": 14, "finishing": 10, "defense": 8,
        "shooting_3pt": -18, "shooting_2pt": -4, "shooting_mid": -6,
        "playmaking": -10, "iq": 0,
    },
}

_ALL_ATTRS = [
    "speed", "shooting_2pt", "shooting_3pt", "shooting_mid",
    "finishing", "playmaking", "defense", "rebounding", "iq",
]

# Map nba_api position strings to our five-position schema.
_POSITION_MAP = {
    "G": "PG", "G-F": "SG", "F-G": "SG",
    "F": "SF", "F-C": "PF", "C-F": "PF",
    "C": "C",
}

# Headers required by stats.nba.com to avoid 403/timeout responses.
_NBA_HEADERS = {
    "Host": "stats.nba.com",
    "Connection": "keep-alive",
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
}

_STATS_RATINGS_DIR = Path(__file__).parent.parent / "data" / "stats_ratings"


def _load_stats_ratings(season: int) -> dict[int, int]:
    """Load pre-computed stats ratings from data/stats_ratings/{season}.json if present."""
    path = _STATS_RATINGS_DIR / f"{season}.json"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        result = {int(k): int(v) for k, v in raw.items()}
        print(f"[INFO] Loaded pre-built stats ratings for {len(result)} players ({season}).")
        return result
    except Exception as exc:
        print(f"[WARN] Failed to load stats ratings: {exc}")
        return {}


def _normalize_position(raw: str) -> str:
    raw = (raw or "").strip()
    return _POSITION_MAP.get(raw, "SF")


# ---------------------------------------------------------------------------
# Absolute-threshold OVR formula
# ---------------------------------------------------------------------------

# Breakpoints: list of (threshold, cumulative_score). Ascending.
# The highest threshold the player clears wins — we walk all entries and keep
# updating, so the final score is the pts for the last threshold they cleared.
_PPG_BREAKS:  list[tuple[float, int]] = [(5,5),(10,10),(15,14),(20,18),(25,21),(30,24),(35,26)]
_RPG_BREAKS:  list[tuple[float, int]] = [(3,3),(5,5),(7,7),(10,9),(13,11),(15,12)]
_APG_BREAKS:  list[tuple[float, int]] = [(2,3),(4,5),(6,7),(8,9),(10,11),(12,13)]
_SPG_BREAKS:  list[tuple[float, int]] = [(0.5,2),(1.0,4),(1.5,6),(2.0,8)]
_BPG_BREAKS:  list[tuple[float, int]] = [(0.5,2),(1.0,4),(1.5,6),(2.0,8),(3.0,10)]
_FG3_BREAKS:  list[tuple[float, int]] = [(0.30,2),(0.35,4),(0.38,6),(0.40,8),(0.43,10)]  # 3PT%
_FGA3_BREAKS: list[tuple[float, int]] = [(2,1),(4,2),(6,3),(8,4)]                         # 3PA volume
_USG_BREAKS:  list[tuple[float, int]] = [(0.15,2),(0.20,4),(0.25,6),(0.30,8),(0.35,10)]   # usage rate
_EFF_BREAKS:  list[tuple[float, int]] = [(0.50,2),(0.53,4),(0.55,6),(0.58,8),(0.60,10)]   # TS%

# Theoretical raw max: 26+12+13+8+10+10+4+10+10+8(def) = 111, but unreachable.
# Real elite players top out ~80-88; formula is scaled to that practical range.
_RAW_SUM_MAX = 88
_DEF_RTG_BASELINE = 115.0  # lenient league avg; below = good defense


def _stat_to_score(val: float, breakpoints: list[tuple[float, int]]) -> int:
    """Return the score for the highest threshold the value clears."""
    score = 0
    for threshold, pts in breakpoints:
        if val >= threshold:
            score = pts
    return score


def _composite_from_row(row: dict) -> float:
    """
    Compute composite raw score from per-game stats. Returns value used by _overall_from_raw_sum.
    Accepts optional DEF_RTG (defensive rating); lower = better defense, ~112-115 is league avg.
    """
    pts  = float(row.get("PTS", 0) or 0)
    reb  = float(row.get("REB", 0) or 0)
    ast  = float(row.get("AST", 0) or 0)
    stl  = float(row.get("STL", 0) or 0)
    blk  = float(row.get("BLK", 0) or 0)
    fg3  = float(row.get("FG3_PCT", 0) or 0)
    fga3 = float(row.get("FG3A", 0) or 0)
    usg  = float(row.get("USG_PCT", 0) or 0)

    # True Shooting % = PTS / (2 * (FGA + 0.44 * FTA))
    fga = float(row.get("FGA", 0) or 0)
    fta = float(row.get("FTA", 0) or 0)
    denom = 2 * (fga + 0.44 * fta)
    ts = pts / denom if denom > 0 else 0.0

    # Defensive rating: 0-8 pts; league avg ~112-115, elite ~104-108.
    # Only applied when DEF_RTG is present (advanced stats cached).
    def_rtg = row.get("DEF_RTG")
    if def_rtg is not None:
        def_score = max(0, min(8, int(_DEF_RTG_BASELINE - float(def_rtg))))
    else:
        def_score = 0

    raw_sum = (
        _stat_to_score(pts,  _PPG_BREAKS)
        + _stat_to_score(reb,  _RPG_BREAKS)
        + _stat_to_score(ast,  _APG_BREAKS)
        + _stat_to_score(stl,  _SPG_BREAKS)
        + _stat_to_score(blk,  _BPG_BREAKS)
        + _stat_to_score(fg3,  _FG3_BREAKS)
        + _stat_to_score(fga3, _FGA3_BREAKS)
        + _stat_to_score(usg,  _USG_BREAKS)
        + _stat_to_score(ts,   _EFF_BREAKS)
        + def_score
    )
    return float(raw_sum)


def _overall_from_raw_sum(raw_sum: float, noise: int = 0) -> int:
    """Scale raw_sum to overall (50–99), optionally add noise, then clamp."""
    overall = 55 + int(raw_sum * 44 / _RAW_SUM_MAX)
    return max(50, min(99, overall + noise))


# ---------------------------------------------------------------------------
# Draft-class OVR for rookies
# ---------------------------------------------------------------------------

def _overall_for_draft_pick(round_num: int, pick: int) -> int:
    """
    Estimate rookie OVR from draft round + pick number using absolute ranges.
    Adds small noise ±3 to avoid identical ratings for adjacent picks.
    """
    noise = random.randint(-3, 3)
    if round_num == 1:
        if pick <= 3:
            base = random.randint(82, 86)
        elif pick <= 10:
            base = random.randint(76, 82)
        elif pick <= 20:
            base = random.randint(71, 77)
        else:
            base = random.randint(68, 74)
    else:
        # Second round: picks 31-45 and beyond
        if pick <= 45:
            base = random.randint(63, 69)
        else:
            base = random.randint(60, 66)
    return max(50, min(99, base + noise))


def _overall_for_exp(exp: int) -> int:
    """
    Fallback OVR estimate from years of experience when stats and draft info
    are both unavailable. Only reached for undrafted players or data gaps.
    """
    if exp >= 8:
        base = random.randint(82, 95)
    elif exp >= 3:
        base = random.randint(72, 84)
    elif exp >= 1:
        base = random.randint(62, 74)
    else:
        # Undrafted rookie — no draft position available
        base = random.randint(60, 65)
    noise = random.randint(-5, 5)
    return max(50, min(99, base + noise))


# ---------------------------------------------------------------------------
# Stats fetching
# ---------------------------------------------------------------------------

def _fetch_league_stats_sync(season: int) -> dict[int, dict]:
    """
    Fetch per-game stats for all players in `season` from LeagueDashPlayerStats.
    Returns a dict keyed by PLAYER_ID (int) containing raw per-game stat columns
    plus 'gp'. The composite/OVR calculation is deferred to _overall_from_stats
    so that multi-season peak logic can compare apples-to-apples raw_sums.
    Raises on network or API failure — caller wraps in try/except.
    """
    from nba_api.stats.endpoints import leaguedashplayerstats

    season_str = f"{season}-{str(season + 1)[2:]}"
    stats = leaguedashplayerstats.LeagueDashPlayerStats(
        season=season_str,
        per_mode_detailed="PerGame",
        headers=_NBA_HEADERS,
        timeout=60,
    )
    df = stats.get_data_frames()[0]

    lookup: dict[int, dict] = {}
    for _, row in df.iterrows():
        pid = int(row["PLAYER_ID"])
        gp = int(row.get("GP", 0) or 0)
        lookup[pid] = {
            "gp": gp,
            "PTS":    float(row.get("PTS", 0) or 0),
            "REB":    float(row.get("REB", 0) or 0),
            "AST":    float(row.get("AST", 0) or 0),
            "STL":    float(row.get("STL", 0) or 0),
            "BLK":    float(row.get("BLK", 0) or 0),
            "FG3_PCT":float(row.get("FG3_PCT", 0) or 0),
            "FG3A":   float(row.get("FG3A", 0) or 0),
            "USG_PCT":float(row.get("USG_PCT", 0) or 0),
            "FGA":    float(row.get("FGA", 0) or 0),
            "FTA":    float(row.get("FTA", 0) or 0),
        }
    return lookup


def _fetch_multi_season_peak(season: int) -> dict[int, dict]:
    """
    Fetch per-game stats for `season`, `season-1`, and `season-2`, then return
    the row with the highest composite raw_sum per player (min 10 GP required).

    Returns the same shape as _fetch_league_stats_sync but only includes the
    player's best single-season row, so _overall_from_stats correctly reflects
    peak performance rather than an injury-shortened year.
    """
    from nba_api.stats.endpoints import leaguedashplayerstats

    seasons_to_try = [season, season - 1, season - 2]
    # player_id → best raw_sum and corresponding stat row
    best: dict[int, tuple[float, dict]] = {}

    for yr in seasons_to_try:
        season_str = f"{yr}-{str(yr + 1)[2:]}"
        try:
            print(f"  Fetching stats for {season_str}...")
            stats = leaguedashplayerstats.LeagueDashPlayerStats(
                season=season_str,
                per_mode_detailed="PerGame",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            df = stats.get_data_frames()[0]
            time.sleep(0.6)  # be polite between calls
        except Exception as exc:
            print(f"  [WARN] Could not fetch stats for {season_str}: {exc}")
            continue

        for _, row in df.iterrows():
            pid = int(row["PLAYER_ID"])
            gp = int(row.get("GP", 0) or 0)
            if gp < 10:
                continue

            stat_row = {
                "gp": gp,
                "PTS":    float(row.get("PTS", 0) or 0),
                "REB":    float(row.get("REB", 0) or 0),
                "AST":    float(row.get("AST", 0) or 0),
                "STL":    float(row.get("STL", 0) or 0),
                "BLK":    float(row.get("BLK", 0) or 0),
                "FG3_PCT":float(row.get("FG3_PCT", 0) or 0),
                "FG3A":   float(row.get("FG3A", 0) or 0),
                "USG_PCT":float(row.get("USG_PCT", 0) or 0),
                "FGA":    float(row.get("FGA", 0) or 0),
                "FTA":    float(row.get("FTA", 0) or 0),
            }
            raw_sum = _composite_from_row(stat_row)

            if pid not in best or raw_sum > best[pid][0]:
                best[pid] = (raw_sum, stat_row)

    return {pid: row for pid, (_, row) in best.items()}


def _fetch_draft_class_sync(season: int) -> dict[int, dict]:
    """
    Fetch the draft class for `season` using DraftHistory.
    Returns {player_id: {"round": int, "pick": int}} for all drafted players.
    Raises on network or API failure — caller wraps in try/except.
    """
    from nba_api.stats.endpoints.drafthistory import DraftHistory

    df = DraftHistory(
        league_id="00",
        season_year_nullable=str(season),
    ).get_data_frames()[0]

    result: dict[int, dict] = {}
    for _, row in df.iterrows():
        pid = int(row["PERSON_ID"])
        result[pid] = {
            "round": int(row["ROUND_NUMBER"]),
            "pick":  int(row["ROUND_PICK"]),
        }
    return result


def _overall_from_stats(
    player_id: int,
    peak_lookup: dict[int, dict],
    exp: int,
    draft_info: Optional[dict] = None,
) -> int:
    """
    Derive overall from multi-season peak stats when available (min 10 GP).
    For rookies (exp == 0), falls back to draft position OVR if draft_info is
    provided, then to _overall_for_exp for undrafted / data-gap cases.
    """
    entry = peak_lookup.get(player_id)
    if entry is not None and entry.get("gp", 0) >= 10:
        raw_sum = _composite_from_row(entry)
        return _overall_from_raw_sum(raw_sum)

    # No qualifying stats found — use draft position for rookies.
    if exp == 0 and draft_info is not None:
        pick_info = draft_info.get(player_id)
        if pick_info is not None:
            return _overall_for_draft_pick(pick_info["round"], pick_info["pick"])

    return _overall_for_exp(exp)


def _clamp(val: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, val))


def _generate_attributes(overall: int, position: str) -> dict[str, int]:
    profile = _POSITION_PROFILE.get(position, _POSITION_PROFILE["SF"])
    attrs: dict[str, int] = {}
    for attr in _ALL_ATTRS:
        delta = profile.get(attr, 0)
        noise = random.randint(-5, 5)
        attrs[attr] = _clamp(overall + delta + noise, 40, 99)
    return attrs


def _generate_hidden(overall: int, exp: int) -> dict[str, int]:
    if exp <= 3:
        potential = _clamp(overall + random.randint(0, 15), 50, 99)
    else:
        potential = _clamp(overall + random.randint(-5, 5), 50, 99)
    peak_start = random.randint(24, 27)
    peak_end = peak_start + random.randint(4, 6)
    return {
        "potential": potential,
        "peak_age_start": peak_start,
        "peak_age_end": peak_end,
        "loyalty": random.randint(0, 100),
        "money_drive": random.randint(0, 100),
        "win_drive": random.randint(0, 100),
    }


def _star_leverage(overall: int) -> int:
    if overall >= 88:
        return random.randint(80, 95)
    if overall >= 80:
        return random.randint(50, 70)
    return random.randint(10, 40)


def _market_pref() -> str:
    return random.choice(["big_market", "neutral", "indifferent", "neutral", "neutral"])


def _contract_salary(overall: int, exp: int, salary_cap: int) -> tuple[int, str, int]:
    max_salary = salary_cap // 4
    min_salary = 1_100_000

    raw = (overall - 60) * 800_000
    salary = _clamp(raw, min_salary, max_salary)

    if salary >= salary_cap * 0.25:
        ctype = "max"
    elif salary <= min_salary:
        ctype = "minimum"
    else:
        ctype = "standard"

    years = random.choices([1, 2, 3, 4], weights=[15, 30, 35, 20])[0]
    return salary, ctype, years


async def _fetch_rookie_scale(conn: asyncpg.Connection) -> dict[int, int]:
    rows = await conn.fetch("SELECT pick_number, year_1_salary FROM rookie_scale ORDER BY pick_number")
    return {r["pick_number"]: r["year_1_salary"] for r in rows}


async def _get_team_id(conn: asyncpg.Connection, league_id: int, nba_code: str) -> Optional[int]:
    row = await conn.fetchrow(
        "SELECT id FROM teams WHERE league_id = $1 AND nba_team_code = $2",
        league_id, nba_code,
    )
    return row["id"] if row else None


async def _insert_player(
    conn: asyncpg.Connection,
    league_id: int,
    team_id: int,
    player_data: dict,
    dry_run: bool,
) -> Optional[int]:
    if dry_run:
        print(
            f"  [DRY] {player_data['first_name']} {player_data['last_name']} "
            f"({player_data['position']}) OVR={player_data['overall']} "
            f"SAL=${player_data['_salary']:,}"
        )
        return None

    player_id = await conn.fetchval(
        """
        INSERT INTO players (
            league_id, external_id, first_name, last_name, position,
            height_in, weight_lb, birth_date, years_pro, is_rookie,
            team_id, roster_status,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq,
            potential, peak_age_start, peak_age_end,
            loyalty, money_drive, win_drive,
            market_pref, star_leverage
        ) VALUES (
            $1, $2, $3, $4, $5,
            $6, $7, $8, $9, $10,
            $11, 'active',
            $12, $13, $14, $15, $16,
            $17, $18, $19, $20, $21,
            $22, $23, $24,
            $25, $26, $27,
            $28, $29
        )
        RETURNING id
        """,
        league_id,
        player_data["external_id"],
        player_data["first_name"],
        player_data["last_name"],
        player_data["position"],
        player_data.get("height_in"),
        player_data.get("weight_lb"),
        player_data.get("birth_date"),
        player_data["years_pro"],
        player_data["is_rookie"],
        team_id,
        player_data["overall"],
        player_data["speed"],
        player_data["shooting_2pt"],
        player_data["shooting_3pt"],
        player_data["shooting_mid"],
        player_data["finishing"],
        player_data["playmaking"],
        player_data["defense"],
        player_data["rebounding"],
        player_data["iq"],
        player_data["potential"],
        player_data["peak_age_start"],
        player_data["peak_age_end"],
        player_data["loyalty"],
        player_data["money_drive"],
        player_data["win_drive"],
        player_data["market_pref"],
        player_data["star_leverage"],
    )
    return player_id


async def _insert_contract(
    conn: asyncpg.Connection,
    league_id: int,
    team_id: int,
    player_id: int,
    salary: int,
    contract_type: str,
    years: int,
    current_season: int,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    await conn.execute(
        """
        INSERT INTO contracts (
            league_id, player_id, team_id, salary, years_remaining,
            total_years, contract_type, signed_in_season, is_active
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, TRUE)
        """,
        league_id, player_id, team_id, salary, years, years, contract_type, current_season,
    )


def _parse_height(height_str: str) -> Optional[int]:
    try:
        ft, inches = height_str.replace('"', "").split("-")
        return int(ft) * 12 + int(inches)
    except Exception:
        return None


def _parse_birthdate(birth_str: str) -> Optional[date]:
    try:
        return date.fromisoformat(birth_str[:10])
    except Exception:
        return None


async def _generate_lineups(
    conn: asyncpg.Connection,
    league_id: int,
    team_id: int,
    player_ids_by_overall: list[tuple[int, int]],
    dry_run: bool,
) -> None:
    """Auto-assign lineup slots: top 5 OVR = starters (1-5), next 8 = bench (6-13), up to 15."""
    for i, (pid, _ovr) in enumerate(player_ids_by_overall[:15]):
        slot = i + 1
        is_starter = slot <= 5
        if dry_run:
            print(f"  [DRY] Lineup slot {slot} ({'starter' if is_starter else 'bench'}) -> player_id={pid}")
            continue
        await conn.execute(
            """
            INSERT INTO lineups (league_id, team_id, is_starter, slot, player_id)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (league_id, team_id, slot) DO UPDATE
                SET player_id = EXCLUDED.player_id,
                    is_starter = EXCLUDED.is_starter
            """,
            league_id, team_id, is_starter, slot, pid,
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Import NBA players into a DBA league.")
    parser.add_argument("--league-id", type=int, required=True)
    parser.add_argument("--season", type=int, required=True, help="Season start year (e.g. 2024)")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing to DB")
    args = parser.parse_args()

    # Validate that we have data for this season before hitting the DB.
    _data_root = Path(__file__).parent.parent / "data"
    ratings_file = _data_root / "stats_ratings" / f"{args.season}.json"
    cache_ok = all(
        (_data_root / "bdl_cache" / f"season_{s}_{t}.json").exists()
        for s in [args.season, args.season - 1, args.season - 2]
        for t in ("base", "usage")
    )
    if not ratings_file.exists() and not cache_ok:
        raise SystemExit(
            f"No stats data for season {args.season}. "
            "Run scripts/fetch_bdl_cache.py then scripts/build_stats_ratings.py --season "
            f"{args.season} first."
        )

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise SystemExit("DATABASE_URL environment variable is not set.")

    conn = await asyncpg.connect(db_url)
    try:
        league_row = await conn.fetchrow(
            "SELECT id, salary_cap, current_season FROM leagues WHERE id = $1",
            args.league_id,
        )
        if league_row is None:
            raise SystemExit(f"League id={args.league_id} not found.")

        salary_cap: int = league_row["salary_cap"]
        current_season: int = league_row["current_season"]
        rookie_scale = await _fetch_rookie_scale(conn)

        nba_teams_list = nba_teams_static.get_teams()
        abbrev_to_id: dict[str, int] = {t["abbreviation"]: t["id"] for t in nba_teams_list}

        # Pre-built stats ratings file is the primary OVR source (keyed by nba player_id).
        # If absent, fall back to live multi-season API fetch.
        prebuilt: dict[int, int] = _load_stats_ratings(args.season)
        peak_lookup: dict[int, dict] = {}
        if not prebuilt:
            try:
                print(f"Fetching multi-season peak stats (seasons {args.season-2}–{args.season})...")
                peak_lookup = _fetch_multi_season_peak(args.season)
                print(f"  Peak stats resolved for {len(peak_lookup)} players.")
            except Exception as exc:
                print(f"[WARN] Multi-season stats fetch failed, using exp fallback: {exc}")

        # Fetch draft history for the target season so rookies get pick-based OVR.
        draft_info: dict[int, dict] = {}
        try:
            print(f"Fetching draft class for {args.season}...")
            draft_info = _fetch_draft_class_sync(args.season)
            print(f"  Draft info loaded for {len(draft_info)} players.")
            time.sleep(0.6)
        except Exception as exc:
            print(f"[WARN] DraftHistory fetch failed, rookies will use exp fallback: {exc}")

        for code in NBA_TEAM_CODES:
            nba_team_id = abbrev_to_id.get(code)
            if nba_team_id is None:
                print(f"[WARN] No nba_api team ID for code {code}, skipping.")
                continue

            team_id = await _get_team_id(conn, args.league_id, code)
            if team_id is None:
                print(f"[WARN] Team {code} not in league {args.league_id}, skipping.")
                continue

            print(f"Fetching roster for {code}...")
            try:
                roster_endpoint = CommonTeamRoster(
                    team_id=nba_team_id,
                    season=f"{args.season}-{str(args.season + 1)[2:]}",
                )
                roster_df = roster_endpoint.get_data_frames()[0]
            except Exception as exc:
                print(f"[ERROR] Failed to fetch {code}: {exc}")
                time.sleep(1)
                continue

            time.sleep(0.5)

            team_players: list[tuple[int, int]] = []  # (player_id, overall)

            for _, row in roster_df.iterrows():
                try:
                    exp_raw = row.get("EXP", "0") or "0"
                    exp = int(exp_raw) if str(exp_raw).isdigit() else 0
                    is_rookie = exp == 0

                    position = _normalize_position(str(row.get("POSITION", "")))
                    player_id_raw = int(row.get("PLAYER_ID", 0) or 0)

                    if player_id_raw in prebuilt:
                        overall = prebuilt[player_id_raw]
                    else:
                        overall = _overall_from_stats(
                            player_id_raw, peak_lookup, exp,
                            draft_info=draft_info if is_rookie else None,
                        )
                    attrs = _generate_attributes(overall, position)
                    hidden = _generate_hidden(overall, exp)

                    height_raw = str(row.get("HEIGHT", ""))
                    weight_raw = row.get("WEIGHT", None)

                    player_data = {
                        "external_id": str(row.get("PLAYER_ID", "")),
                        "first_name": str(row.get("PLAYER", "")).split(" ")[0],
                        "last_name": " ".join(str(row.get("PLAYER", "")).split(" ")[1:]) or "Unknown",
                        "position": position,
                        "height_in": _parse_height(height_raw),
                        "weight_lb": int(weight_raw) if weight_raw and str(weight_raw).isdigit() else None,
                        "birth_date": _parse_birthdate(str(row.get("BIRTH_DATE", ""))),
                        "years_pro": exp,
                        "is_rookie": is_rookie,
                        "overall": overall,
                        **attrs,
                        **hidden,
                        "market_pref": _market_pref(),
                        "star_leverage": _star_leverage(overall),
                    }

                    if is_rookie:
                        # Use the actual draft pick salary when available; fall
                        # back to pick 15 if the rookie wasn't in the draft table
                        # (undrafted players on rookie minimums).
                        pick_info = draft_info.get(player_id_raw)
                        pick_num = pick_info["pick"] if pick_info else 15
                        salary = rookie_scale.get(pick_num, rookie_scale.get(15, 2_500_000))
                        contract_type = "rookie_scale"
                        years = 4
                    else:
                        salary, contract_type, years = _contract_salary(overall, exp, salary_cap)

                    player_data["_salary"] = salary

                    player_id = await _insert_player(conn, args.league_id, team_id, player_data, args.dry_run)

                    if player_id is not None:
                        await _insert_contract(
                            conn, args.league_id, team_id, player_id,
                            salary, contract_type, years, current_season, args.dry_run,
                        )
                        team_players.append((player_id, overall))

                except Exception as exc:
                    player_name = row.get("PLAYER", "unknown")
                    print(f"[ERROR] Skipping {player_name} on {code}: {exc}")
                    continue

            team_players.sort(key=lambda x: x[1], reverse=True)
            print(f"  Generating lineup for {code} ({len(team_players)} players)...")
            await _generate_lineups(conn, args.league_id, team_id, team_players, args.dry_run)

        if args.dry_run:
            print("\n[DRY RUN] No data was written to the database.")
        else:
            print(f"\nDone. Imported players for league {args.league_id}.")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
