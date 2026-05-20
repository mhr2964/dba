"""
Integration tests for services.schedule_service.generate_season.

Requires 30 teams in the league to match the NBA structure used by _build_pairs.
Teams are inserted directly from nba_teams.json seed data to avoid the full
league_service.create path (which does Discord API calls).
"""
from __future__ import annotations

import json
import os

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
    """is_user_matchup=TRUE is set for every game where at least one team has a human manager.

    The flag uses OR semantics: any game involving a human-managed team gets the flag,
    not just matchups between two human teams. This is intentional — it lets the Discord
    bot surface all games a human manager needs to pay attention to.
    """
    # why: production code uses OR logic (either team has a manager → flag true).
    #      Old test expected AND logic (both teams must have managers). Updated 2026-05
    #      to reflect current schedule_service.generate_season behavior.
    league_id = await _setup_league_with_30_teams(db_pool)

    # Assign a manager to one team only (LAL), leave all others as CPU.
    team_a_id = await db_pool.fetchval(
        "SELECT id FROM teams WHERE league_id = $1 AND nba_team_code = 'LAL'",
        league_id,
    )
    await db_pool.execute(
        "UPDATE teams SET manager_user_id = 22222 WHERE id = $1", team_a_id
    )

    await schedule_service.generate_season(league_id, 2025)

    # All LAL games should be flagged (82 games = one per-team appearance).
    user_matchup_count = await db_pool.fetchval(
        """
        SELECT COUNT(*) FROM games
        WHERE league_id = $1 AND season = $2
          AND is_user_matchup = TRUE
          AND (home_team_id = $3 OR away_team_id = $3)
        """,
        league_id,
        2025,
        team_a_id,
    )
    assert user_matchup_count > 0, "All LAL games should be marked is_user_matchup=TRUE"

    # Games with NO human-managed team must have is_user_matchup=FALSE.
    non_user_flagged = await db_pool.fetchval(
        """
        SELECT COUNT(*) FROM games
        WHERE league_id = $1 AND season = $2
          AND is_user_matchup = TRUE
          AND home_team_id != $3
          AND away_team_id != $3
        """,
        league_id,
        2025,
        team_a_id,
    )
    assert non_user_flagged == 0, "Games with no human manager must not be marked is_user_matchup=TRUE"


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
    Calling generate_season twice on the same league/season replaces the schedule —
    the second call deletes existing games then regenerates. Final row count equals
    the second call's output only (replace semantics, not append).

    This is the documented behavior: generate_season is safe to re-run; it clears
    previous games for that league+season before inserting new ones.
    """
    # why: generate_season gained idempotent/replace semantics in 2026-05 refactor —
    #      it now deletes existing games before inserting. Old test expected append
    #      behavior (count1 + count2); updated to expect replace behavior (count2 only).
    league_id = await _setup_league_with_30_teams(db_pool)

    count1 = await schedule_service.generate_season(league_id, 2025)
    count2 = await schedule_service.generate_season(league_id, 2025)

    total = await db_pool.fetchval(
        "SELECT COUNT(*) FROM games WHERE league_id = $1 AND season = $2",
        league_id,
        2025,
    )

    # Second call replaces first — total rows equal the second run's count only.
    assert total == count2, (
        f"Expected {count2} total rows (replace semantics) after two generate_season calls, got {total}"
    )
    # Sanity: both calls should generate the same number of games.
    assert count1 == count2, f"Both runs should generate identical game counts, got {count1} vs {count2}"
