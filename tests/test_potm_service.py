"""
Tests for services.potm_service, covering PA13 (Player of the Month composite
scoring) from the playoffs/awards/HOF realism audit
(docs/design/playoffs-awards-hof-logic-rules.md).

Zero dedicated test coverage existed for this module before this file.
"""
from __future__ import annotations

import datetime

import pytest

from services.potm_service import check_and_get_potm_awards

pytestmark = pytest.mark.asyncio


async def _seed_league(pool, guild_id: int) -> int:
    league_id: int = await pool.fetchval(
        """
        INSERT INTO leagues (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES ($1, 'POTM Test League', 2025, 2025, 11111)
        RETURNING id
        """,
        guild_id,
    )
    return league_id


async def _insert_team(pool, league_id: int, code: str, conference: str) -> int:
    return await pool.fetchval(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, $2, $2, $2, $3, 'Atlantic')
        RETURNING id
        """,
        league_id, code, conference,
    )


async def _insert_player(pool, league_id: int, team_id: int, first_name: str) -> int:
    return await pool.fetchval(
        """
        INSERT INTO players (
            league_id, first_name, last_name, position, team_id,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq, potential,
            peak_age_start, peak_age_end, loyalty, money_drive, win_drive
        ) VALUES (
            $1, $2, 'POTM', 'SF', $3,
            85, 80, 80, 75, 75, 80, 80, 75, 70, 80, 85, 25, 30, 50, 50, 60
        ) RETURNING id
        """,
        league_id, first_name, team_id,
    )


async def _insert_team_schedule_games(
    pool, league_id: int, team_id: int, opp_id: int, count: int, team_wins: bool, start_index: int
) -> list[int]:
    """Insert `count` regular-season, simmed games in Feb 2025 where team_id
    plays opp_id at home, winning or losing every game per `team_wins`."""
    game_ids = []
    for i in range(count):
        home_score, away_score = (110, 90) if team_wins else (90, 110)
        game_id = await pool.fetchval(
            """
            INSERT INTO games (league_id, season, season_type, game_index,
                                home_team_id, away_team_id, scheduled_date, status,
                                home_score, away_score)
            VALUES ($1, 2025, 'regular', $2, $3, $4, $5, 'simmed', $6, $7)
            RETURNING id
            """,
            league_id, start_index + i, team_id, opp_id,
            datetime.date(2025, 2, 1 + i), home_score, away_score,
        )
        game_ids.append(game_id)
    return game_ids


async def _insert_box_score(pool, game_id: int, player_id: int, team_id: int, points: int, fga: int, fta: int) -> None:
    await pool.execute(
        """
        INSERT INTO game_box_scores
            (game_id, player_id, team_id, started, minutes, points, rebounds_off, rebounds_def, assists, fga, fta)
        VALUES ($1, $2, $3, TRUE, 36, $4, 2, 3, 5, $5, $6)
        """,
        game_id, player_id, team_id, points, fga, fta,
    )


async def test_potm_composite_prefers_efficient_winner_over_inefficient_high_scorer(db_pool):
    """PA13: a lower-ppg, more efficient player on a winning team must beat a
    higher-ppg, inefficient player on a losing team -- pre-fix, the flat
    max(ppg, apg) sort would have picked the inefficient high-volume scorer
    on the losing team every time, with no efficiency or team-success term.
    """
    league_id = await _seed_league(db_pool, 646301)
    team_high = await _insert_team(db_pool, league_id, "HGH", "East")
    team_low = await _insert_team(db_pool, league_id, "LOW", "East")
    opponent = await _insert_team(db_pool, league_id, "OPP", "West")

    # Player H: 30 ppg but poor efficiency (30/30 FGA, 0 FTA -> ts_pct = 0.5),
    # team loses every game in the window.
    player_h = await _insert_player(db_pool, league_id, team_high, "Volume")
    h_games = await _insert_team_schedule_games(
        db_pool, league_id, team_high, opponent, count=10, team_wins=False, start_index=1
    )
    for gid in h_games:
        await _insert_box_score(db_pool, gid, player_h, team_high, points=30, fga=30, fta=0)

    # Player L: 25 ppg but efficient (15 FGA/5 FTA -> ts_pct ~0.73), team wins
    # every game in the window.
    player_l = await _insert_player(db_pool, league_id, team_low, "Efficient")
    l_games = await _insert_team_schedule_games(
        db_pool, league_id, team_low, opponent, count=10, team_wins=True, start_index=11
    )
    for gid in l_games:
        await _insert_box_score(db_pool, gid, player_l, team_low, points=25, fga=15, fta=5)

    awards = await check_and_get_potm_awards(db_pool, league_id, 2025, current_game_date="2025-03-05")

    assert awards, "Expected at least one POTM award to be produced"
    east_award = next((a for a in awards if a["conference"] == "East"), None)
    assert east_award is not None
    assert east_award["player_id"] == player_l, (
        "Composite score (efficiency + team win_pct) should favor the efficient "
        "winning-team player over the higher-ppg inefficient losing-team player"
    )


async def test_potm_still_awards_higher_scorer_when_efficiency_and_team_success_agree(db_pool):
    """Sanity check: when the high-ppg player is ALSO efficient and winning,
    they should still win -- the composite isn't just inverting the old sort."""
    league_id = await _seed_league(db_pool, 646302)
    team_a = await _insert_team(db_pool, league_id, "BST", "West")
    team_b = await _insert_team(db_pool, league_id, "WRS", "West")
    opponent = await _insert_team(db_pool, league_id, "OP2", "East")

    player_best = await _insert_player(db_pool, league_id, team_a, "Best")
    best_games = await _insert_team_schedule_games(
        db_pool, league_id, team_a, opponent, count=10, team_wins=True, start_index=1
    )
    for gid in best_games:
        await _insert_box_score(db_pool, gid, player_best, team_a, points=32, fga=18, fta=8)

    player_worst = await _insert_player(db_pool, league_id, team_b, "Worst")
    worst_games = await _insert_team_schedule_games(
        db_pool, league_id, team_b, opponent, count=10, team_wins=False, start_index=11
    )
    for gid in worst_games:
        await _insert_box_score(db_pool, gid, player_worst, team_b, points=10, fga=15, fta=0)

    awards = await check_and_get_potm_awards(db_pool, league_id, 2025, current_game_date="2025-03-05")

    west_award = next((a for a in awards if a["conference"] == "West"), None)
    assert west_award is not None
    assert west_award["player_id"] == player_best
