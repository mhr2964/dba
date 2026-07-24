"""
Tests for gameplan_repo.get_scheme_history (Phase 2 fix C3) -- aggregates a
team's CPU-selected offensive/defensive schemes across the season's simmed
games into "most-used scheme + W-L record while running it," feeding Coach
Beat's scheme-history awareness.

Uses db_pool directly, matching test_cpu_fixes.py's seeding convention.
clean_db (autouse) truncates leagues CASCADE before each test.
"""
from __future__ import annotations

import datetime

from data.repositories import gameplan_repo


async def _seed_league(pool) -> int:
    return await pool.fetchval(
        """
        INSERT INTO leagues
            (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES (777002, 'Scheme History Test League', 2025, 2025, 10002)
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


async def _seed_game(
    pool, league_id: int, season: int, home_id: int, away_id: int,
    winner_id: int, status: str = "simmed", game_index: int = 1,
) -> int:
    return await pool.fetchval(
        """
        INSERT INTO games
            (league_id, season, game_index, home_team_id, away_team_id,
             scheduled_date, status, is_user_matchup, rng_seed, winner_team_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, FALSE, 1, $8)
        RETURNING id
        """,
        league_id, season, game_index, home_id, away_id,
        datetime.date(2025, 10, game_index), status, winner_id,
    )


async def _seed_gameplan(
    pool, game_id: int, team_id: int, offensive_scheme: str, defensive_scheme: str,
) -> None:
    await pool.execute(
        """
        INSERT INTO game_cpu_gameplans (
            game_id, team_id, source,
            offensive_pace, offensive_scheme, defensive_scheme,
            defensive_intensity, star_usage
        )
        VALUES ($1, $2, 'cpu', 'balanced', $3, $4, 'normal', 50)
        """,
        game_id, team_id, offensive_scheme, defensive_scheme,
    )


async def test_get_scheme_history_empty_for_no_games(db_pool):
    if db_pool is None:
        return
    league_id = await _seed_league(db_pool)
    team_id = await _seed_team(db_pool, league_id, "LAL")
    result = await gameplan_repo.get_scheme_history(db_pool, league_id, 2025, team_id)
    assert result == {}


async def test_get_scheme_history_aggregates_most_used_scheme_and_record(db_pool):
    if db_pool is None:
        return
    league_id = await _seed_league(db_pool)
    team_id = await _seed_team(db_pool, league_id, "LAL")
    opp_id = await _seed_team(db_pool, league_id, "BOS")

    # 3 games running zone defense: 2 wins, 1 loss. 1 game running man_to_man: 1 win.
    game1 = await _seed_game(db_pool, league_id, 2025, team_id, opp_id, winner_id=team_id, game_index=1)
    await _seed_gameplan(db_pool, game1, team_id, "ball_movement", "zone")
    game2 = await _seed_game(db_pool, league_id, 2025, team_id, opp_id, winner_id=team_id, game_index=2)
    await _seed_gameplan(db_pool, game2, team_id, "ball_movement", "zone")
    game3 = await _seed_game(db_pool, league_id, 2025, team_id, opp_id, winner_id=opp_id, game_index=3)
    await _seed_gameplan(db_pool, game3, team_id, "isolation", "zone")
    game4 = await _seed_game(db_pool, league_id, 2025, team_id, opp_id, winner_id=team_id, game_index=4)
    await _seed_gameplan(db_pool, game4, team_id, "isolation", "man_to_man")

    result = await gameplan_repo.get_scheme_history(db_pool, league_id, 2025, team_id)

    assert result["defensive_scheme"] == {"scheme": "zone", "games": 3, "wins": 2, "losses": 1}
    # offensive_scheme is tied 2-2 between ball_movement and isolation -- max()
    # picks a stable winner (first-seen on ties), just assert the shape is sane.
    assert result["offensive_scheme"]["games"] == 2
    assert result["offensive_scheme"]["wins"] + result["offensive_scheme"]["losses"] == 2


async def test_get_scheme_history_ignores_other_seasons(db_pool):
    if db_pool is None:
        return
    league_id = await _seed_league(db_pool)
    team_id = await _seed_team(db_pool, league_id, "LAL")
    opp_id = await _seed_team(db_pool, league_id, "BOS")
    game = await _seed_game(db_pool, league_id, 2024, team_id, opp_id, winner_id=team_id, game_index=1)
    await _seed_gameplan(db_pool, game, team_id, "ball_movement", "zone")

    result = await gameplan_repo.get_scheme_history(db_pool, league_id, 2025, team_id)
    assert result == {}


async def test_get_scheme_history_ignores_unsimmed_games(db_pool):
    if db_pool is None:
        return
    league_id = await _seed_league(db_pool)
    team_id = await _seed_team(db_pool, league_id, "LAL")
    opp_id = await _seed_team(db_pool, league_id, "BOS")
    game = await _seed_game(
        db_pool, league_id, 2025, team_id, opp_id, winner_id=None,
        status="scheduled", game_index=1,
    )
    await _seed_gameplan(db_pool, game, team_id, "ball_movement", "zone")

    result = await gameplan_repo.get_scheme_history(db_pool, league_id, 2025, team_id)
    assert result == {}
