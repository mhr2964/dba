"""
Integration tests for data.repositories.series_repo.

Uses db_pool directly. clean_db (autouse) truncates leagues CASCADE before each test.
"""
from __future__ import annotations

import pytest

from data.repositories import series_repo


# ---------------------------------------------------------------------------
# Helper — insert minimal league + two teams
# ---------------------------------------------------------------------------


async def _create_test_league_and_teams(pool) -> tuple[int, int, int]:
    """Insert a league and two teams. Returns (league_id, team_a_id, team_b_id)."""
    league_row = await pool.fetchrow(
        """
        INSERT INTO leagues (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES (777001, 'Series Test League', 2025, 2025, 11111)
        RETURNING id
        """,
    )
    league_id = league_row["id"]

    team_a = await pool.fetchrow(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'LAL', 'Lakers', 'Los Angeles', 'West', 'Pacific')
        RETURNING id
        """,
        league_id,
    )
    team_b = await pool.fetchrow(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'BOS', 'Celtics', 'Boston', 'East', 'Atlantic')
        RETURNING id
        """,
        league_id,
    )
    return league_id, team_a["id"], team_b["id"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_create_series(db_pool):
    """create_series inserts a row with correct teams and status='active'."""
    league_id, team_a, team_b = await _create_test_league_and_teams(db_pool)

    series = await series_repo.create_series(
        db_pool,
        league_id=league_id,
        season=2025,
        round="first_round",
        high_seed_id=team_a,
        low_seed_id=team_b,
        games_needed=7,
    )

    assert series.id is not None
    assert series.high_seed_team_id == team_a
    assert series.low_seed_team_id == team_b
    assert series.status == "active"
    assert series.wins_high == 0
    assert series.wins_low == 0
    assert series.winner_team_id is None


async def test_record_game_result_increments_wins(db_pool):
    """Recording one win for the high seed increments wins_high by 1; series stays active."""
    league_id, team_a, team_b = await _create_test_league_and_teams(db_pool)

    series = await series_repo.create_series(
        db_pool,
        league_id=league_id,
        season=2025,
        round="first_round",
        high_seed_id=team_a,
        low_seed_id=team_b,
        games_needed=7,
    )

    updated = await series_repo.record_game_result(db_pool, series.id, winner_team_id=team_a)

    assert updated.wins_high == 1
    assert updated.wins_low == 0
    assert updated.status == "active"
    assert updated.winner_team_id is None


async def test_series_completes_at_four_wins(db_pool):
    """Recording 4 wins for the high seed in a best-of-7 sets status='complete'."""
    league_id, team_a, team_b = await _create_test_league_and_teams(db_pool)

    series = await series_repo.create_series(
        db_pool,
        league_id=league_id,
        season=2025,
        round="first_round",
        high_seed_id=team_a,
        low_seed_id=team_b,
        games_needed=7,
    )

    updated = series
    for _ in range(4):
        updated = await series_repo.record_game_result(db_pool, series.id, winner_team_id=team_a)

    assert updated.wins_high == 4
    assert updated.status == "complete"
    assert updated.winner_team_id == team_a


async def test_play_in_completes_at_one_win(db_pool):
    """A games_needed=1 series (play-in single game) completes after a single result."""
    league_id, team_a, team_b = await _create_test_league_and_teams(db_pool)

    series = await series_repo.create_series(
        db_pool,
        league_id=league_id,
        season=2025,
        round="play_in",
        high_seed_id=team_a,
        low_seed_id=team_b,
        games_needed=1,
    )

    updated = await series_repo.record_game_result(db_pool, series.id, winner_team_id=team_b)

    assert updated.status == "complete"
    assert updated.winner_team_id == team_b


async def test_get_active_series_returns_only_active(db_pool):
    """get_active_series excludes complete series and returns only active ones."""
    league_id, team_a, team_b = await _create_test_league_and_teams(db_pool)

    # Active series
    active = await series_repo.create_series(
        db_pool,
        league_id=league_id,
        season=2025,
        round="first_round",
        high_seed_id=team_a,
        low_seed_id=team_b,
        games_needed=7,
    )

    # Completed series (record 4 wins immediately)
    completed = await series_repo.create_series(
        db_pool,
        league_id=league_id,
        season=2025,
        round="second_round",
        high_seed_id=team_a,
        low_seed_id=team_b,
        games_needed=7,
    )
    for _ in range(4):
        await series_repo.record_game_result(db_pool, completed.id, winner_team_id=team_a)

    results = await series_repo.get_active_series(db_pool, league_id, season=2025)

    assert len(results) == 1
    assert results[0].id == active.id
    assert results[0].status == "active"
