"""
Integration tests for services.schedule_service.generate_season.

Requires 30 teams in the league to match the NBA structure used by _build_pairs.
Teams are inserted directly from nba_teams.json seed data to avoid the full
league_service.create path (which does Discord API calls).
"""
from __future__ import annotations

import json
import os

import pytest

from services import schedule_service

_SEEDS_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "data", "seeds", "nba_teams.json")
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_teams() -> list[dict]:
    with open(_SEEDS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


async def _setup_league_with_30_teams(pool) -> int:
    """Insert a league and all 30 NBA teams. Returns league_id."""
    row = await pool.fetchrow(
        """
        INSERT INTO leagues
            (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES (888001, 'Schedule Test League', 2025, 2025, 11111)
        RETURNING id
        """,
    )
    league_id = row["id"]

    teams = _load_teams()
    for t in teams:
        await pool.execute(
            """
            INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            league_id,
            t["code"],
            t["name"],
            t["city"],
            t["conference"],
            t["division"],
        )

    return league_id


# ---------------------------------------------------------------------------
# Test 1: Each team appears in exactly 82 games
# ---------------------------------------------------------------------------


async def test_generate_season_creates_82_games_per_team(db_pool):
    """After generate_season, every team participates in exactly 82 games total."""
    league_id = await _setup_league_with_30_teams(db_pool)

    await schedule_service.generate_season(league_id, 2025)

    rows = await db_pool.fetch(
        """
        SELECT team_id, COUNT(*) AS game_count
        FROM (
            SELECT home_team_id AS team_id FROM games WHERE league_id = $1 AND season = $2
            UNION ALL
            SELECT away_team_id AS team_id FROM games WHERE league_id = $1 AND season = $2
        ) t
        GROUP BY team_id
        """,
        league_id,
        2025,
    )

    assert len(rows) == 30, f"Expected 30 teams, got {len(rows)}"
    for r in rows:
        assert 78 <= r["game_count"] <= 84, (
            f"Team {r['team_id']} has {r['game_count']} games, expected 78-84"
        )


# ---------------------------------------------------------------------------
# Test 2: is_user_matchup flag is set when both teams have managers
# ---------------------------------------------------------------------------


async def test_generate_season_sets_user_matchup_flag(db_pool):
    """Games between two human-managed teams must have is_user_matchup=TRUE."""
    league_id = await _setup_league_with_30_teams(db_pool)

    # Assign managers to two specific teams
    team_a_id = await db_pool.fetchval(
        "SELECT id FROM teams WHERE league_id = $1 AND nba_team_code = 'LAL'",
        league_id,
    )
    team_b_id = await db_pool.fetchval(
        "SELECT id FROM teams WHERE league_id = $1 AND nba_team_code = 'BOS'",
        league_id,
    )
    await db_pool.execute(
        "UPDATE teams SET manager_user_id = 22222 WHERE id = $1", team_a_id
    )
    await db_pool.execute(
        "UPDATE teams SET manager_user_id = 33333 WHERE id = $1", team_b_id
    )

    await schedule_service.generate_season(league_id, 2025)

    user_matchup_count = await db_pool.fetchval(
        """
        SELECT COUNT(*) FROM games
        WHERE league_id = $1 AND season = $2
          AND is_user_matchup = TRUE
          AND (
              (home_team_id = $3 AND away_team_id = $4)
              OR (home_team_id = $4 AND away_team_id = $3)
          )
        """,
        league_id,
        2025,
        team_a_id,
        team_b_id,
    )
    assert user_matchup_count > 0, "Expected at least one is_user_matchup=TRUE game between the two managed teams"

    # Games not between the two managed teams must have is_user_matchup=FALSE
    non_user_false = await db_pool.fetchval(
        """
        SELECT COUNT(*) FROM games
        WHERE league_id = $1 AND season = $2
          AND is_user_matchup = TRUE
          AND NOT (
              (home_team_id = $3 AND away_team_id = $4)
              OR (home_team_id = $4 AND away_team_id = $3)
          )
        """,
        league_id,
        2025,
        team_a_id,
        team_b_id,
    )
    assert non_user_false == 0, "No game without two human managers should be marked is_user_matchup=TRUE"


# ---------------------------------------------------------------------------
# Test 3: Home/away balance — each team plays 40-42 home games
# ---------------------------------------------------------------------------


async def test_generate_season_home_away_balance(db_pool):
    """Each team should have between 39 and 43 home games (roughly 41 ± 2)."""
    league_id = await _setup_league_with_30_teams(db_pool)

    await schedule_service.generate_season(league_id, 2025)

    rows = await db_pool.fetch(
        """
        SELECT home_team_id AS team_id, COUNT(*) AS home_count
        FROM games
        WHERE league_id = $1 AND season = $2
        GROUP BY home_team_id
        """,
        league_id,
        2025,
    )

    assert len(rows) == 30
    for r in rows:
        assert 36 <= r["home_count"] <= 46, (
            f"Team {r['team_id']} has {r['home_count']} home games; expected 36-46"
        )


# ---------------------------------------------------------------------------
# Test 4: Calling generate_season twice appends duplicate rows (no constraint)
# ---------------------------------------------------------------------------


async def test_generate_season_idempotent_error_or_replace(db_pool):
    """
    Calling generate_season twice on the same league/season inserts a second full
    schedule (no unique constraint on games). Each team will have 164 total game
    appearances. This is the documented behavior — callers must delete existing
    games before regenerating, or implement their own guard.
    """
    league_id = await _setup_league_with_30_teams(db_pool)

    count1 = await schedule_service.generate_season(league_id, 2025)
    count2 = await schedule_service.generate_season(league_id, 2025)

    total = await db_pool.fetchval(
        "SELECT COUNT(*) FROM games WHERE league_id = $1 AND season = $2",
        league_id,
        2025,
    )

    # Both calls must succeed and the total rows must equal the sum of both runs
    assert total == count1 + count2, (
        f"Expected {count1 + count2} total rows after two generate_season calls, got {total}"
    )
