"""Populate players for a league from the 2024-25 NBA rosters via nba_api.

Usage:
    python scripts/import_players.py --league-id 1 --season 2024
    python scripts/import_players.py --league-id 1 --season 2024 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import os
import random
import time
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


def _normalize_position(raw: str) -> str:
    raw = (raw or "").strip()
    return _POSITION_MAP.get(raw, "SF")


def _overall_for_exp(exp: int) -> int:
    """Fallback: estimate overall from years of experience when stats are unavailable."""
    if exp >= 8:
        base = random.randint(82, 95)
    elif exp >= 3:
        base = random.randint(72, 84)
    elif exp >= 1:
        base = random.randint(62, 74)
    else:
        base = random.randint(58, 70)
    noise = random.randint(-5, 5)
    return max(50, min(99, base + noise))


def _fetch_league_stats_sync(season: int) -> dict[int, dict]:
    """
    Fetch per-game stats for all players in `season` from LeagueDashPlayerStats.
    Returns a dict keyed by PLAYER_ID (int).
    Raises on network or API failure — caller wraps in try/except.
    """
    from nba_api.stats.endpoints import leaguedashplayerstats

    season_str = f"{season}-{str(season + 1)[2:]}"
    stats = leaguedashplayerstats.LeagueDashPlayerStats(
        season=season_str,
        per_mode_simple="PerGame",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    df = stats.get_data_frames()[0]

    max_pts = df["PTS"].max() or 1.0
    max_reb = df["REB"].max() or 1.0
    max_ast = df["AST"].max() or 1.0
    max_stl = df["STL"].max() or 1.0
    max_blk = df["BLK"].max() or 1.0
    max_usg = df["USG_PCT"].max() or 1.0
    max_min = df["MIN"].max() or 1.0

    lookup: dict[int, dict] = {}
    for _, row in df.iterrows():
        pid = int(row["PLAYER_ID"])
        gp = int(row.get("GP", 0) or 0)

        pts_n  = float(row["PTS"])    / max_pts
        reb_n  = float(row["REB"])    / max_reb
        ast_n  = float(row["AST"])    / max_ast
        stl_n  = float(row["STL"])    / max_stl
        blk_n  = float(row["BLK"])    / max_blk
        fg_n   = float(row["FG_PCT"] or 0)
        usg_n  = float(row["USG_PCT"] or 0) / max_usg
        min_n  = float(row["MIN"])    / max_min

        composite = (
            pts_n  * 0.35 +
            reb_n  * 0.15 +
            ast_n  * 0.15 +
            stl_n  * 0.07 +
            blk_n  * 0.05 +
            fg_n   * 0.10 +
            usg_n  * 0.08 +
            min_n  * 0.05
        )
        overall = int(round(50 + composite * 49))
        overall = max(50, min(99, overall))

        lookup[pid] = {"overall": overall, "gp": gp}

    return lookup


def _overall_from_stats(player_id: int, stats_lookup: dict[int, dict], exp: int) -> int:
    """
    Derive overall from real stats when available and games played >= 10.
    Falls back to _overall_for_exp when the player is absent or has <10 GP.
    """
    entry = stats_lookup.get(player_id)
    if entry is None or entry["gp"] < 10:
        return _overall_for_exp(exp)
    return entry["overall"]


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

        # Fetch per-game stats for all players in one call before the team loop.
        stats_lookup: dict[int, dict] = {}
        try:
            print(f"Fetching league-wide stats for {args.season}-{str(args.season + 1)[2:]}...")
            stats_lookup = _fetch_league_stats_sync(args.season)
            print(f"  Loaded stats for {len(stats_lookup)} players.")
        except Exception as exc:
            print(f"[WARN] LeagueDashPlayerStats failed, using exp fallback: {exc}")

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
                    overall = _overall_from_stats(player_id_raw, stats_lookup, exp)
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
                        # Use pick 15 salary as default when actual pick is unknown.
                        salary = rookie_scale.get(15, 2_500_000)
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
