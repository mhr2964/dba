"""
Player of the Month service.

Checks whether one or more calendar months have elapsed since the last POTM
award and, if so, aggregates per-player stats across those months to determine
East/West winners.  Fires at most once per simulated month per league.
"""
from __future__ import annotations

import calendar
import datetime
from typing import Optional

import asyncpg

from core.logging import get_logger

log = get_logger(__name__)

# Minimum games played within the month to be eligible.
# 3 games is a low bar intentionally: eligibility is determined by the
# schedule spreading games evenly, not by an artificial floor.  The real
# NBA uses ~10 games but our sim awards are commissioner-facing and the
# schedule guarantees ~12-13 games per team per month.
_MIN_GAMES = 3


def _prev_month(year_month: str) -> str:
    """Return the month before 'YYYY-MM' as 'YYYY-MM'."""
    year, month = int(year_month[:4]), int(year_month[5:7])
    if month == 1:
        return f"{year - 1}-12"
    return f"{year}-{month - 1:02d}"


def _months_between_exclusive_inclusive(start: str, end: str) -> list[str]:
    """
    Return the list of months strictly after `start` up through and including `end`.
    Both are 'YYYY-MM' strings.  Returns [] when start >= end.
    """
    months: list[str] = []
    year, month = int(start[:4]), int(start[5:7])
    end_year, end_month = int(end[:4]), int(end[5:7])
    while True:
        month += 1
        if month > 12:
            month = 1
            year += 1
        ym = f"{year}-{month:02d}"
        months.append(ym)
        if year == end_year and month == end_month:
            break
        if year > end_year or (year == end_year and month > end_month):
            break
    return months


async def check_and_get_potm_awards(
    pool: asyncpg.Pool,
    league_id: int,
    season: int,
    current_game_date: str,  # "YYYY-MM-DD" from the most recently simmed game
) -> Optional[list[dict]]:
    """
    Determine whether any new Player of the Month awards should be given.

    Returns:
    - None  — already awarded for current_year_month (no action needed)
    - []    — months elapsed but no eligible players found
    - list  — one or two award dicts (East, West) per elapsed month
    """
    current_year_month = current_game_date[:7]  # e.g. "2024-11"

    log.info(
        f"POTM check: league={league_id} season={season} "
        f"current_game_date={current_game_date} current_year_month={current_year_month}"
    )

    last_ym: Optional[str] = await pool.fetchval(
        "SELECT last_potm_year_month FROM leagues WHERE id = $1",
        league_id,
    )

    log.info(f"POTM check: last_potm_year_month={last_ym!r}")

    if last_ym == current_year_month:
        log.info("POTM check: already awarded for current month, skipping")
        return None  # already awarded this month

    # First-ever run: seed last_ym to the month before the earliest simmed game in
    # the season so that all elapsed months are eligible.  Using _prev_month(current)
    # would skip every month except the last one when simming crosses multiple months.
    if last_ym is None:
        earliest_date: Optional[str] = await pool.fetchval(
            """
            SELECT MIN(scheduled_date)::text FROM games
            WHERE league_id = $1 AND season = $2 AND status = 'simmed'
            """,
            league_id,
            season,
        )
        if earliest_date:
            last_ym = _prev_month(earliest_date[:7])
        else:
            last_ym = _prev_month(current_year_month)
        log.info(f"POTM check: first run, seeding last_ym={last_ym!r} from earliest simmed date={earliest_date!r}")

    months_to_award = _months_between_exclusive_inclusive(last_ym, current_year_month)
    log.info(f"POTM check: months_to_award={months_to_award}")
    if not months_to_award:
        return None

    awards: list[dict] = []

    for ym in months_to_award:
        year_part, month_part = int(ym[:4]), int(ym[5:7])
        _, last_day = calendar.monthrange(year_part, month_part)
        month_start = datetime.date(year_part, month_part, 1)
        month_end = datetime.date(year_part, month_part, last_day)

        log.info(
            f"POTM {ym}: querying games between {month_start} and {month_end} "
            f"for league={league_id} season={season}"
        )

        # Count how many regular-season simmed games exist in this window for debugging.
        game_count = await pool.fetchval(
            """
            SELECT COUNT(*) FROM games
            WHERE league_id = $1 AND season = $2
              AND scheduled_date BETWEEN $3 AND $4
              AND status = 'simmed'
              AND season_type = 'regular'
            """,
            league_id, season, month_start, month_end,
        )
        log.info(
            f"POTM {ym}: found {game_count} regular-season simmed games in window "
            f"({month_start} to {month_end})"
        )

        if game_count == 0:
            # Log date range of actual simmed games so we can diagnose year mismatch.
            date_range = await pool.fetchrow(
                """
                SELECT MIN(scheduled_date)::text AS earliest, MAX(scheduled_date)::text AS latest
                FROM games
                WHERE league_id = $1 AND season = $2 AND status = 'simmed'
                """,
                league_id, season,
            )
            log.warning(
                f"POTM {ym}: no games in window — actual simmed date range is "
                f"{date_range['earliest']!r} to {date_range['latest']!r}. "
                f"Possible year mismatch in scheduled_date values."
            )

        rows = await pool.fetch(
            """
            SELECT
                p.id          AS player_id,
                p.first_name || ' ' || p.last_name AS player_name,
                t.nba_team_code AS team_code,
                t.conference,
                AVG(b.points)                               AS ppg,
                AVG(b.rebounds_off + b.rebounds_def)        AS rpg,
                AVG(b.assists)                              AS apg,
                COUNT(b.game_id)                            AS games_played
            FROM game_box_scores b
            JOIN games g  ON g.id = b.game_id
            JOIN players p ON p.id = b.player_id
            JOIN teams t   ON t.id = b.team_id
            WHERE g.league_id = $1
              AND g.season = $2
              AND g.scheduled_date BETWEEN $3 AND $4
              AND g.status = 'simmed'
              AND g.season_type = 'regular'
            GROUP BY p.id, p.first_name, p.last_name, t.nba_team_code, t.conference
            HAVING COUNT(b.game_id) >= $5
            ORDER BY AVG(b.points) DESC
            """,
            league_id, season, month_start, month_end, _MIN_GAMES,
        )

        log.info(
            f"POTM {ym}: eligible players found: {len(rows)} "
            f"(min {_MIN_GAMES} games, regular season only)"
        )
        if rows:
            log.info(
                f"POTM {ym}: top-5 candidates: "
                + ", ".join(
                    f"{r['player_name']} ({r['games_played']}gp {float(r['ppg']):.1f}ppg)"
                    for r in rows[:5]
                )
            )
        else:
            # Check if non-regular games would have produced results (diagnose season_type filter).
            any_rows = await pool.fetchval(
                """
                SELECT COUNT(DISTINCT b.player_id)
                FROM game_box_scores b
                JOIN games g ON g.id = b.game_id
                WHERE g.league_id = $1 AND g.season = $2
                  AND g.scheduled_date BETWEEN $3 AND $4
                  AND g.status = 'simmed'
                """,
                league_id, season, month_start, month_end,
            )
            log.warning(
                f"POTM {ym}: 0 eligible players with season_type='regular' filter. "
                f"Without the filter, {any_rows} distinct players have box scores in window. "
                f"Check that games.season_type is set to 'regular' for regular season games."
            )
            continue

        month_label = datetime.date(year_part, month_part, 1).strftime("%B %Y")

        east_candidates = [r for r in rows if (r["conference"] or "").lower() == "east"]
        west_candidates = [r for r in rows if (r["conference"] or "").lower() == "west"]
        log.info(
            f"POTM {ym}: East POTM eligible: {len(east_candidates)}, "
            f"West POTM eligible: {len(west_candidates)}"
        )

        for conference in ("East", "West"):
            conf_players = east_candidates if conference == "East" else west_candidates
            if not conf_players:
                continue
            # Primary sort: ppg; tiebreaker: apg
            winner = max(conf_players, key=lambda r: (float(r["ppg"]), float(r["apg"])))
            awards.append({
                "month_label": month_label,
                "conference": conference,
                "player_id": winner["player_id"],
                "player_name": winner["player_name"],
                "team_code": winner["team_code"],
                "ppg": round(float(winner["ppg"]), 1),
                "rpg": round(float(winner["rpg"]), 1),
                "apg": round(float(winner["apg"]), 1),
                "games_played": int(winner["games_played"]),
            })

    # Only advance the tracker when at least one award was actually produced.
    # If every month in the window returned no eligible players we leave
    # last_potm_year_month unchanged so the next sim batch can retry.
    if awards:
        await pool.execute(
            "UPDATE leagues SET last_potm_year_month = $1 WHERE id = $2",
            current_year_month,
            league_id,
        )
    else:
        log.info(
            f"POTM check: no awards produced for league {league_id} — "
            "leaving last_potm_year_month unchanged so retry is possible"
        )

    return awards


def get_potm_context(awards: list[dict]) -> dict:
    """
    Build the context dict passed to Pat Chen when generating the POTM blurb.

    Expects awards to be the list returned by check_and_get_potm_awards for a
    single month (East + West entries).
    """
    east = next((a for a in awards if a["conference"] == "East"), None)
    west = next((a for a in awards if a["conference"] == "West"), None)
    month_label = awards[0]["month_label"] if awards else "Unknown Month"

    def _fmt(a: Optional[dict]) -> Optional[dict]:
        if a is None:
            return None
        return {
            "player": a["player_name"],
            "team": a["team_code"],
            "ppg": a["ppg"],
            "rpg": a["rpg"],
            "apg": a["apg"],
            "games": a["games_played"],
        }

    return {
        "month_label": month_label,
        "east_winner": _fmt(east),
        "west_winner": _fmt(west),
    }
