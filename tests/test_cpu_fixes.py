"""
Tests for CPU gameplay fixes:
  1. _ensure_lineup auto-generates rows for a team with no lineup.
  2. _ensure_lineup is idempotent when rows already exist.
  3. all_regular_season_games_complete returns True when no scheduled games remain.
  4. all_regular_season_games_complete returns False when a scheduled game remains.
"""
from __future__ import annotations

from data.repositories import game_repo
from services.batch_sim_runner import _ensure_lineup


# ---------------------------------------------------------------------------
# Shared DB helpers
# ---------------------------------------------------------------------------


async def _seed_league(pool) -> int:
    return await pool.fetchval(
        """
        INSERT INTO leagues
            (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES (777001, 'CPU Fix Test League', 2025, 2025, 10001)
        RETURNING id
        """
    )


async def _seed_team(pool, league_id: int, code: str) -> int:
    return await pool.fetchval(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, $2, 'Test Team', 'Testville', 'East', 'Atlantic')
        RETURNING id
        """,
        league_id,
        code,
    )


async def _seed_player(pool, league_id: int, team_id: int, overall: int = 75) -> int:
    return await pool.fetchval(
        """
        INSERT INTO players
            (league_id, first_name, last_name, position, years_pro, team_id, roster_status,
             overall, speed, shooting_2pt, shooting_3pt, shooting_mid, finishing,
             playmaking, defense, rebounding, iq,
             potential, peak_age_start, peak_age_end,
             loyalty, money_drive, win_drive, star_leverage)
        VALUES ($1, 'CPU', 'Player', 'SF', 3, $2, 'active',
                $3, $3, $3, $3, $3, $3, $3, $3, $3, $3,
                80, 26, 31, 60, 60, 60, 40)
        RETURNING id
        """,
        league_id,
        team_id,
        overall,
    )


async def _seed_game(
    pool,
    league_id: int,
    home_id: int,
    away_id: int,
    status: str,
    game_index: int = 1,
) -> int:
    import datetime
    return await pool.fetchval(
        """
        INSERT INTO games
            (league_id, season, game_index, home_team_id, away_team_id,
             scheduled_date, status, is_user_matchup, rng_seed, season_type)
        VALUES ($1, 2025, $2, $3, $4, $5, $6, FALSE, 42, 'regular')
        RETURNING id
        """,
        league_id,
        game_index,
        home_id,
        away_id,
        datetime.date(2025, 10, game_index),
        status,
    )


# ---------------------------------------------------------------------------
# 1. _ensure_lineup auto-generates for a team with no lineup
# ---------------------------------------------------------------------------


async def test_ensure_lineup_auto_generates_for_empty_team(db_pool):
    """
    Given a team with 10 players and zero lineup rows, _ensure_lineup should
    insert 10 lineup rows with correct is_starter flags (slots 1-5 = starter).
    """
    league_id = await _seed_league(db_pool)
    team_id = await _seed_team(db_pool, league_id, "CPU")

    # Insert 10 players with descending OVR so ordering is deterministic.
    for i in range(10):
        await _seed_player(db_pool, team_id=team_id, league_id=league_id, overall=90 - i)

    # Precondition: no lineup rows.
    before = await db_pool.fetchval(
        "SELECT COUNT(*) FROM lineups WHERE league_id=$1 AND team_id=$2",
        league_id, team_id,
    )
    assert before == 0

    await _ensure_lineup(db_pool, league_id, team_id)

    rows = await db_pool.fetch(
        "SELECT slot, is_starter FROM lineups WHERE league_id=$1 AND team_id=$2 ORDER BY slot",
        league_id, team_id,
    )
    assert len(rows) == 10

    for row in rows:
        expected_starter = row["slot"] <= 5
        assert row["is_starter"] == expected_starter, (
            f"Slot {row['slot']}: expected is_starter={expected_starter}, got {row['is_starter']}"
        )


# ---------------------------------------------------------------------------
# 2. _ensure_lineup skips when rows already exist
# ---------------------------------------------------------------------------


async def test_ensure_lineup_skips_existing(db_pool):
    """
    When lineup rows already exist, _ensure_lineup must not modify them.
    Row count stays the same after the second call.
    """
    league_id = await _seed_league(db_pool)
    team_id = await _seed_team(db_pool, league_id, "CPU")

    player_id = await _seed_player(db_pool, league_id=league_id, team_id=team_id)

    # Insert a single lineup row manually.
    await db_pool.execute(
        """
        INSERT INTO lineups (league_id, team_id, is_starter, slot, player_id)
        VALUES ($1, $2, TRUE, 1, $3)
        """,
        league_id, team_id, player_id,
    )

    before = await db_pool.fetchval(
        "SELECT COUNT(*) FROM lineups WHERE league_id=$1 AND team_id=$2",
        league_id, team_id,
    )
    assert before == 1

    await _ensure_lineup(db_pool, league_id, team_id)

    after = await db_pool.fetchval(
        "SELECT COUNT(*) FROM lineups WHERE league_id=$1 AND team_id=$2",
        league_id, team_id,
    )
    assert after == 1, "Row count should not change when lineup already exists"


# ---------------------------------------------------------------------------
# 3. all_regular_season_games_complete returns True with no scheduled games
# ---------------------------------------------------------------------------


async def test_all_regular_season_games_complete_true(db_pool):
    """
    With only simmed regular-season games, all_regular_season_games_complete
    must return True.
    """
    league_id = await _seed_league(db_pool)
    home_id = await _seed_team(db_pool, league_id, "HM1")
    away_id = await _seed_team(db_pool, league_id, "AW1")

    await _seed_game(db_pool, league_id, home_id, away_id, "simmed", game_index=1)
    await _seed_game(db_pool, league_id, home_id, away_id, "simmed", game_index=2)

    result = await game_repo.all_regular_season_games_complete(db_pool, league_id, 2025)
    assert result is True


# ---------------------------------------------------------------------------
# 4. all_regular_season_games_complete returns False when a scheduled game exists
# ---------------------------------------------------------------------------


async def test_all_regular_season_games_complete_false(db_pool):
    """
    With one simmed and one scheduled regular-season game,
    all_regular_season_games_complete must return False.
    """
    league_id = await _seed_league(db_pool)
    home_id = await _seed_team(db_pool, league_id, "HM2")
    away_id = await _seed_team(db_pool, league_id, "AW2")

    await _seed_game(db_pool, league_id, home_id, away_id, "simmed", game_index=1)
    await _seed_game(db_pool, league_id, home_id, away_id, "scheduled", game_index=2)

    result = await game_repo.all_regular_season_games_complete(db_pool, league_id, 2025)
    assert result is False
