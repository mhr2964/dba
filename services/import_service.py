from __future__ import annotations

import asyncio
import random
from datetime import date
from typing import Optional

import asyncpg
import discord

from core.logging import get_logger
from data.db import get_pool
from data.repositories import league_repo

log = get_logger(__name__)

# Same team codes as scripts/import_players.py
_NBA_TEAM_CODES = [
    "ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW",
    "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NOP", "NYK",
    "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS",
]

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

_POSITION_MAP = {
    "G": "PG", "G-F": "SG", "F-G": "SG",
    "F": "SF", "F-C": "PF", "C-F": "PF",
    "C": "C",
}


def _normalize_position(raw: str) -> str:
    return _POSITION_MAP.get((raw or "").strip(), "SF")


def _overall_for_exp(exp: int) -> int:
    if exp >= 8:
        base = random.randint(82, 95)
    elif exp >= 3:
        base = random.randint(72, 84)
    elif exp >= 1:
        base = random.randint(62, 74)
    else:
        base = random.randint(58, 70)
    return max(50, min(99, base + random.randint(-5, 5)))


def _clamp(val: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, val))


def _generate_attributes(overall: int, position: str) -> dict[str, int]:
    profile = _POSITION_PROFILE.get(position, _POSITION_PROFILE["SF"])
    return {
        attr: _clamp(overall + profile.get(attr, 0) + random.randint(-15, 15), 40, 99)
        for attr in _ALL_ATTRS
    }


def _generate_hidden(overall: int, exp: int) -> dict[str, int]:
    potential = _clamp(
        overall + (random.randint(0, 15) if exp <= 3 else random.randint(-5, 5)),
        50, 99,
    )
    peak_start = random.randint(24, 27)
    return {
        "potential": potential,
        "peak_age_start": peak_start,
        "peak_age_end": peak_start + random.randint(4, 6),
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


def _contract_salary(overall: int, salary_cap: int) -> tuple[int, str, int]:
    raw = (overall - 60) * 800_000
    salary = _clamp(raw, 1_100_000, salary_cap // 4)
    if salary >= salary_cap * 0.25:
        ctype = "max"
    elif salary <= 1_100_000:
        ctype = "minimum"
    else:
        ctype = "standard"
    years = random.choices([1, 2, 3, 4], weights=[15, 30, 35, 20])[0]
    return salary, ctype, years


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


async def _team_has_players(pool: asyncpg.Pool, league_id: int, team_id: int) -> bool:
    count = await pool.fetchval(
        "SELECT COUNT(*) FROM players WHERE league_id = $1 AND team_id = $2",
        league_id,
        team_id,
    )
    return (count or 0) > 0


async def _insert_player_row(
    pool: asyncpg.Pool,
    league_id: int,
    team_id: int,
    p: dict,
) -> int:
    return await pool.fetchval(
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
        league_id, p["external_id"], p["first_name"], p["last_name"], p["position"],
        p.get("height_in"), p.get("weight_lb"), p.get("birth_date"),
        p["years_pro"], p["is_rookie"],
        team_id,
        p["overall"], p["speed"], p["shooting_2pt"], p["shooting_3pt"], p["shooting_mid"],
        p["finishing"], p["playmaking"], p["defense"], p["rebounding"], p["iq"],
        p["potential"], p["peak_age_start"], p["peak_age_end"],
        p["loyalty"], p["money_drive"], p["win_drive"],
        p["market_pref"], p["star_leverage"],
    )


async def _insert_contract_row(
    pool: asyncpg.Pool,
    league_id: int,
    team_id: int,
    player_id: int,
    salary: int,
    contract_type: str,
    years: int,
    current_season: int,
) -> None:
    await pool.execute(
        """
        INSERT INTO contracts (
            league_id, player_id, team_id, salary, years_remaining,
            total_years, contract_type, signed_in_season, is_active
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, TRUE)
        """,
        league_id, player_id, team_id, salary, years, years, contract_type, current_season,
    )


async def _generate_lineup(
    pool: asyncpg.Pool,
    league_id: int,
    team_id: int,
    player_ids_by_overall: list[tuple[int, int]],
) -> None:
    for i, (pid, _ovr) in enumerate(player_ids_by_overall[:15]):
        slot = i + 1
        await pool.execute(
            """
            INSERT INTO lineups (league_id, team_id, is_starter, slot, player_id)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (league_id, team_id, slot) DO UPDATE
                SET player_id = EXCLUDED.player_id,
                    is_starter = EXCLUDED.is_starter
            """,
            league_id, team_id, slot <= 5, slot, pid,
        )


async def _update_team_ratings(
    pool: asyncpg.Pool,
    team_id: int,
    players: list[tuple[int, int]],
) -> None:
    """Set team offense/defense/pace to the roster-average OVR."""
    if not players:
        return
    avg_ovr = sum(ovr for _, ovr in players) // len(players)
    await pool.execute(
        """
        UPDATE teams
        SET team_offense_rating = $2,
            team_defense_rating = $3,
            pace = $4
        WHERE id = $1
        """,
        team_id, avg_ovr, avg_ovr, 100.0,
    )


async def import_players_from_api(
    league_id: int,
    season: int,
    guild: discord.Guild,
) -> dict:
    """
    Imports real NBA rosters from nba_api for the given season.
    Returns {"teams_imported": int, "players_imported": int, "errors": list[str]}

    For each of the 30 teams:
    1. Fetch roster from nba_api CommonTeamRoster
    2. Generate ratings
    3. Insert players + contracts
    4. Auto-generate lineup (top 5 OVR = starters)
    5. Update team offense_rating, defense_rating, pace

    Skips teams that already have players (idempotent).
    Rate-limits to 0.6s between team API calls.
    """
    try:
        from nba_api.stats.endpoints.commonteamroster import CommonTeamRoster
        from nba_api.stats.static import teams as nba_teams_static
    except ImportError as exc:
        return {"teams_imported": 0, "players_imported": 0, "errors": [f"nba_api not installed: {exc}"]}

    pool = await get_pool()

    league_row = await pool.fetchrow(
        "SELECT id, salary_cap, current_season FROM leagues WHERE id = $1",
        league_id,
    )
    if not league_row:
        return {"teams_imported": 0, "players_imported": 0, "errors": [f"League {league_id} not found."]}

    salary_cap: int = league_row["salary_cap"]
    current_season: int = league_row["current_season"]

    rookie_rows = await pool.fetch(
        "SELECT pick_number, year_1_salary FROM rookie_scale ORDER BY pick_number"
    )
    rookie_scale: dict[int, int] = {r["pick_number"]: r["year_1_salary"] for r in rookie_rows}

    nba_teams_list = nba_teams_static.get_teams()
    abbrev_to_nba_id: dict[str, int] = {t["abbreviation"]: t["id"] for t in nba_teams_list}

    news_channel_id = await league_repo.get_channel(pool, league_id, "league-news")
    news_channel: Optional[discord.TextChannel] = None
    if news_channel_id:
        news_channel = guild.get_channel(news_channel_id)

    teams_imported = 0
    players_imported = 0
    errors: list[str] = []

    for i, code in enumerate(_NBA_TEAM_CODES):
        nba_team_id = abbrev_to_nba_id.get(code)
        if nba_team_id is None:
            errors.append(f"No nba_api team ID for {code}")
            continue

        team_row = await pool.fetchrow(
            "SELECT id FROM teams WHERE league_id = $1 AND nba_team_code = $2",
            league_id, code,
        )
        if not team_row:
            errors.append(f"Team {code} not found in league")
            continue

        team_id: int = team_row["id"]

        if await _team_has_players(pool, league_id, team_id):
            log.info(f"import_players: {code} already has players, skipping")
            continue

        try:
            endpoint = CommonTeamRoster(
                team_id=nba_team_id,
                season=f"{season}-{str(season + 1)[2:]}",
            )
            roster_df = endpoint.get_data_frames()[0]
        except Exception as exc:
            errors.append(f"{code}: API error — {exc}")
            await asyncio.sleep(1.0)
            continue

        await asyncio.sleep(0.6)

        team_players: list[tuple[int, int]] = []

        for _, row in roster_df.iterrows():
            try:
                exp_raw = row.get("EXP", "0") or "0"
                exp = int(exp_raw) if str(exp_raw).isdigit() else 0
                is_rookie = exp == 0

                position = _normalize_position(str(row.get("POSITION", "")))
                overall = _overall_for_exp(exp)
                attrs = _generate_attributes(overall, position)
                hidden = _generate_hidden(overall, exp)

                player_data = {
                    "external_id": str(row.get("PLAYER_ID", "")),
                    "first_name": str(row.get("PLAYER", "")).split(" ")[0],
                    "last_name": " ".join(str(row.get("PLAYER", "")).split(" ")[1:]) or "Unknown",
                    "position": position,
                    "height_in": _parse_height(str(row.get("HEIGHT", ""))),
                    "weight_lb": int(w) if (w := row.get("WEIGHT")) and str(w).isdigit() else None,
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
                    salary = rookie_scale.get(15, 2_500_000)
                    contract_type = "rookie_scale"
                    years = 4
                else:
                    salary, contract_type, years = _contract_salary(overall, salary_cap)

                player_id = await _insert_player_row(pool, league_id, team_id, player_data)
                await _insert_contract_row(
                    pool, league_id, team_id, player_id,
                    salary, contract_type, years, current_season,
                )
                team_players.append((player_id, overall))
                players_imported += 1

            except Exception as exc:
                player_name = row.get("PLAYER", "unknown")
                errors.append(f"{code}/{player_name}: {exc}")
                continue

        if team_players:
            team_players.sort(key=lambda x: x[1], reverse=True)
            await _generate_lineup(pool, league_id, team_id, team_players)
            await _update_team_ratings(pool, team_id, team_players)
            teams_imported += 1

        if news_channel and (i + 1) % 5 == 0:
            await news_channel.send(f"Importing... {i + 1}/30 teams complete")

    log.info(
        f"import_players_from_api: league={league_id} season={season} "
        f"teams={teams_imported} players={players_imported} errors={len(errors)}"
    )
    return {
        "teams_imported": teams_imported,
        "players_imported": players_imported,
        "errors": errors,
    }
