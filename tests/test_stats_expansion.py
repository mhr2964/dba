"""
Integration tests for the stats expansion features:
career stats, all-time leaderboards, and trade history.

Uses db_pool directly. clean_db (autouse) truncates leagues CASCADE before each test.
"""
from __future__ import annotations

import pytest

from data.repositories import player_repo, trade_repo


# ---------------------------------------------------------------------------
# Shared setup helpers
# ---------------------------------------------------------------------------


async def _setup(pool) -> tuple[int, int, int]:
    """Insert a league and two teams. Returns (league_id, team_a_id, team_b_id)."""
    league_row = await pool.fetchrow(
        """
        INSERT INTO leagues (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES (777001, 'Stats Expansion Test League', 2025, 2025, 99999)
        RETURNING id
        """,
    )
    league_id = league_row["id"]

    team_a = await pool.fetchrow(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'GSW', 'Warriors', 'Golden State', 'West', 'Pacific')
        RETURNING id
        """,
        league_id,
    )
    team_b = await pool.fetchrow(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'MIA', 'Heat', 'Miami', 'East', 'Southeast')
        RETURNING id
        """,
        league_id,
    )
    return league_id, team_a["id"], team_b["id"]


async def _insert_player(pool, league_id: int, team_id: int, overall: int = 80) -> int:
    """Insert a minimal player and return its id."""
    row = await pool.fetchrow(
        """
        INSERT INTO players (
            league_id, first_name, last_name, position, team_id,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq, potential,
            peak_age_start, peak_age_end, loyalty, money_drive, win_drive
        ) VALUES (
            $1, 'Test', 'Player', 'SG', $2,
            $3, 75, 75, 70, 70,
            75, 70, 75, 70, 75, 85,
            24, 30, 50, 50, 60
        )
        RETURNING id
        """,
        league_id, team_id, overall,
    )
    return row["id"]


async def _insert_game(pool, league_id: int, team_id: int, season: int) -> int:
    """Insert a completed game and return its id."""
    row = await pool.fetchrow(
        """
        INSERT INTO games (
            league_id, season, season_type, game_index,
            home_team_id, away_team_id, scheduled_date,
            status, home_score, away_score, winner_team_id
        ) VALUES (
            $1, $2, 'regular', 1,
            $3, $3, '2025-01-01',
            'simmed', 110, 105, $3
        )
        RETURNING id
        """,
        league_id, season, team_id,
    )
    return row["id"]


async def _insert_box_score(
    pool,
    player_id: int,
    team_id: int,
    game_id: int,
    points: int,
    rebounds: int,
    assists: int,
    steals: int = 1,
    blocks: int = 0,
    fgm: int = 5,
    fga: int = 10,
    tpm: int = 1,
    tpa: int = 3,
) -> None:
    """Insert a box score row for testing."""
    await pool.execute(
        """
        INSERT INTO game_box_scores (
            game_id, player_id, team_id,
            points, rebounds_off, rebounds_def,
            assists, steals, blocks,
            fgm, fga, tpm, tpa
        ) VALUES (
            $1, $2, $3,
            $4, $5, $6,
            $7, $8, $9,
            $10, $11, $12, $13
        )
        """,
        game_id, player_id, team_id,
        points,
        rebounds // 2,          # split evenly into off/def for simplicity
        rebounds - rebounds // 2,
        assists, steals, blocks,
        fgm, fga, tpm, tpa,
    )


# ---------------------------------------------------------------------------
# Career stats tests
# ---------------------------------------------------------------------------


async def test_career_stats_aggregates_across_seasons(db_pool):
    """get_career_stats counts games across seasons and PPG matches the inserted data."""
    league_id, team_a, _ = await _setup(db_pool)
    player_id = await _insert_player(db_pool, league_id, team_a)

    game_s1 = await _insert_game(db_pool, league_id, team_a, season=2024)
    game_s2 = await _insert_game(db_pool, league_id, team_a, season=2025)

    await _insert_box_score(db_pool, player_id, team_a, game_s1, points=20, rebounds=5, assists=3)
    await _insert_box_score(db_pool, player_id, team_a, game_s2, points=30, rebounds=8, assists=5)

    career = await player_repo.get_career_stats(db_pool, player_id, league_id)

    assert career["games_played"] == 2
    assert career["ppg"] == pytest.approx(25.0)
    assert career["total_points"] == 50


async def test_career_stats_per_season_breakdown(db_pool):
    """get_career_stats returns a seasons list with one entry per season."""
    league_id, team_a, _ = await _setup(db_pool)
    player_id = await _insert_player(db_pool, league_id, team_a)

    game_s1 = await _insert_game(db_pool, league_id, team_a, season=2024)
    game_s2 = await _insert_game(db_pool, league_id, team_a, season=2025)

    await _insert_box_score(db_pool, player_id, team_a, game_s1, points=20, rebounds=5, assists=3)
    await _insert_box_score(db_pool, player_id, team_a, game_s2, points=30, rebounds=8, assists=5)

    career = await player_repo.get_career_stats(db_pool, player_id, league_id)

    assert career["seasons_played"] == 2
    seasons = career["seasons"]
    assert len(seasons) == 2

    by_season = {s["season"]: s for s in seasons}
    assert by_season[2024]["ppg"] == pytest.approx(20.0)
    assert by_season[2025]["ppg"] == pytest.approx(30.0)
    assert int(by_season[2024]["games_played"]) == 1
    assert int(by_season[2025]["games_played"]) == 1


async def test_career_high_points(db_pool):
    """career_high_pts is the maximum single-game point total."""
    league_id, team_a, _ = await _setup(db_pool)
    player_id = await _insert_player(db_pool, league_id, team_a)

    game1 = await _insert_game(db_pool, league_id, team_a, season=2025)
    game2 = await _insert_game(db_pool, league_id, team_a, season=2025)

    await _insert_box_score(db_pool, player_id, team_a, game1, points=30, rebounds=5, assists=3)
    await _insert_box_score(db_pool, player_id, team_a, game2, points=45, rebounds=7, assists=4)

    career = await player_repo.get_career_stats(db_pool, player_id, league_id)

    assert career["career_high_pts"] == 45


async def test_career_stats_no_games_returns_zeros(db_pool):
    """get_career_stats returns empty-safe defaults when no box scores exist."""
    league_id, team_a, _ = await _setup(db_pool)
    player_id = await _insert_player(db_pool, league_id, team_a)

    career = await player_repo.get_career_stats(db_pool, player_id, league_id)

    assert career["games_played"] == 0
    assert career["ppg"] == 0.0
    assert career["seasons"] == []


# ---------------------------------------------------------------------------
# All-time leaderboard tests
# ---------------------------------------------------------------------------


async def test_all_time_leaders_ordered(db_pool):
    """get_all_time_leaders returns players in descending order with enough games played."""
    league_id, team_a, team_b = await _setup(db_pool)

    # Three players; we insert enough games to clear the 100-game minimum.
    # We bypass the minimum by using pts_total (no minimum required).
    players = []
    expected_pts = [3000, 2000, 1000]
    for pts in expected_pts:
        pid = await _insert_player(db_pool, league_id, team_a)
        players.append(pid)

        for i in range(5):
            game_id = await _insert_game(db_pool, league_id, team_a, season=2025 + i)
            per_game = pts // 5
            await _insert_box_score(db_pool, pid, team_a, game_id, points=per_game, rebounds=5, assists=3)

    leaders = await player_repo.get_all_time_leaders(db_pool, league_id, "pts_total")

    assert len(leaders) >= 3
    assert leaders[0]["player_id"] == players[0]
    assert int(leaders[0]["value"]) == 3000
    assert leaders[1]["player_id"] == players[1]
    assert leaders[2]["player_id"] == players[2]


async def test_all_time_leaders_invalid_stat_raises(db_pool):
    """get_all_time_leaders raises ValueError for a stat not in the allowlist."""
    league_id, _, _ = await _setup(db_pool)

    with pytest.raises(ValueError, match="Invalid career stat"):
        await player_repo.get_all_time_leaders(db_pool, league_id, "DROP TABLE players--")


# ---------------------------------------------------------------------------
# Trade history tests
# ---------------------------------------------------------------------------


async def test_trade_history_returns_approved_only(db_pool):
    """get_team_trade_history only returns trades with status='approved'."""
    league_id, team_a, team_b = await _setup(db_pool)

    approved = await trade_repo.create_trade(db_pool, league_id, 2025, team_a, team_b)
    await trade_repo.update_status(db_pool, approved.id, "approved")

    vetoed = await trade_repo.create_trade(db_pool, league_id, 2025, team_a, team_b)
    await trade_repo.update_status(db_pool, vetoed.id, "vetoed")

    history = await trade_repo.get_team_trade_history(db_pool, league_id, team_a)

    assert len(history) == 1
    assert history[0]["trade"].id == approved.id
    assert history[0]["trade"].status == "approved"


async def test_team_trade_history_visible_from_both_sides(db_pool):
    """A trade between A and B appears in both team A and team B's history."""
    league_id, team_a, team_b = await _setup(db_pool)

    trade = await trade_repo.create_trade(db_pool, league_id, 2025, team_a, team_b)
    await trade_repo.update_status(db_pool, trade.id, "approved")

    history_a = await trade_repo.get_team_trade_history(db_pool, league_id, team_a)
    history_b = await trade_repo.get_team_trade_history(db_pool, league_id, team_b)

    assert len(history_a) == 1
    assert len(history_b) == 1
    assert history_a[0]["trade"].id == trade.id
    assert history_b[0]["trade"].id == trade.id

    # other_team_id is set relative to the querying team
    assert history_a[0]["other_team_id"] == team_b
    assert history_b[0]["other_team_id"] == team_a


async def test_trade_history_includes_assets(db_pool):
    """Assets attached to the trade are included in the history result."""
    league_id, team_a, team_b = await _setup(db_pool)
    player_id = await _insert_player(db_pool, league_id, team_a)

    trade = await trade_repo.create_trade(db_pool, league_id, 2025, team_a, team_b)
    await trade_repo.add_asset(
        db_pool,
        trade_id=trade.id,
        from_team_id=team_a,
        to_team_id=team_b,
        asset_type="player",
        player_id=player_id,
    )
    await trade_repo.update_status(db_pool, trade.id, "approved")

    history = await trade_repo.get_team_trade_history(db_pool, league_id, team_a)

    assert len(history) == 1
    assert len(history[0]["assets"]) == 1
    assert history[0]["assets"][0].player_id == player_id


async def test_get_all_approved_trades_season_filter(db_pool):
    """get_all_approved_trades filters by season when provided."""
    league_id, team_a, team_b = await _setup(db_pool)

    trade_2025 = await trade_repo.create_trade(db_pool, league_id, 2025, team_a, team_b)
    await trade_repo.update_status(db_pool, trade_2025.id, "approved")

    trade_2026 = await trade_repo.create_trade(db_pool, league_id, 2026, team_a, team_b)
    await trade_repo.update_status(db_pool, trade_2026.id, "approved")

    all_trades = await trade_repo.get_all_approved_trades(db_pool, league_id)
    filtered = await trade_repo.get_all_approved_trades(db_pool, league_id, season=2025)

    assert len(all_trades) == 2
    assert len(filtered) == 1
    assert filtered[0]["trade"].season == 2025
