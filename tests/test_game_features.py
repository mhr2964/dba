"""
Integration tests for game feature additions:
- Trade block repo (add, remove, is_on_block, get_team_block, clear_team_block)
- Team rename (team_repo.rename_team)

Uses db_pool directly. clean_db (autouse) truncates leagues CASCADE before each test.
"""
from __future__ import annotations

import pytest

from data.repositories import team_repo, trade_block_repo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _setup(pool) -> tuple[int, int, int]:
    """Insert a league and two teams. Returns (league_id, team_a_id, team_b_id)."""
    league_row = await pool.fetchrow(
        """
        INSERT INTO leagues (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES (777001, 'Game Features Test League', 2025, 2025, 11111)
        RETURNING id
        """
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


async def _insert_player(pool, league_id: int, team_id: int, first: str = "Trade") -> int:
    row = await pool.fetchrow(
        """
        INSERT INTO players (
            league_id, first_name, last_name, position, team_id,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq, potential,
            peak_age_start, peak_age_end, loyalty, money_drive, win_drive
        ) VALUES (
            $1, $2, 'Guy', 'SF', $3,
            80, 75, 75, 70, 70,
            75, 70, 75, 70, 75, 85,
            26, 30, 50, 50, 50
        )
        RETURNING id
        """,
        league_id,
        first,
        team_id,
    )
    return row["id"]


# ---------------------------------------------------------------------------
# Trade block tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_to_trade_block(db_pool):
    league_id, team_a_id, _ = await _setup(db_pool)
    player_id = await _insert_player(db_pool, league_id, team_a_id)

    await trade_block_repo.add_to_block(
        db_pool, league_id, team_a_id, player_id, asking_price=10_000_000, note="Looking for picks"
    )

    row = await db_pool.fetchrow(
        "SELECT * FROM trade_block WHERE league_id = $1 AND player_id = $2",
        league_id,
        player_id,
    )
    assert row is not None
    assert row["team_id"] == team_a_id
    assert row["asking_price"] == 10_000_000
    assert row["note"] == "Looking for picks"


@pytest.mark.asyncio
async def test_remove_from_trade_block(db_pool):
    league_id, team_a_id, _ = await _setup(db_pool)
    player_id = await _insert_player(db_pool, league_id, team_a_id)

    await trade_block_repo.add_to_block(db_pool, league_id, team_a_id, player_id)
    await trade_block_repo.remove_from_block(db_pool, league_id, player_id)

    row = await db_pool.fetchrow(
        "SELECT 1 FROM trade_block WHERE league_id = $1 AND player_id = $2",
        league_id,
        player_id,
    )
    assert row is None


@pytest.mark.asyncio
async def test_is_on_block_true_and_false(db_pool):
    league_id, team_a_id, _ = await _setup(db_pool)
    player_id = await _insert_player(db_pool, league_id, team_a_id)

    assert await trade_block_repo.is_on_block(db_pool, league_id, player_id) is False

    await trade_block_repo.add_to_block(db_pool, league_id, team_a_id, player_id)

    assert await trade_block_repo.is_on_block(db_pool, league_id, player_id) is True


@pytest.mark.asyncio
async def test_get_team_block_returns_entries(db_pool):
    league_id, team_a_id, _ = await _setup(db_pool)
    player1 = await _insert_player(db_pool, league_id, team_a_id, first="Alpha")
    player2 = await _insert_player(db_pool, league_id, team_a_id, first="Beta")

    await trade_block_repo.add_to_block(db_pool, league_id, team_a_id, player1)
    await trade_block_repo.add_to_block(db_pool, league_id, team_a_id, player2, note="Need shooter")

    entries = await trade_block_repo.get_team_block(db_pool, league_id, team_a_id)
    player_ids = {e["player_id"] for e in entries}

    assert len(entries) == 2
    assert player1 in player_ids
    assert player2 in player_ids


@pytest.mark.asyncio
async def test_clear_team_block(db_pool):
    league_id, team_a_id, _ = await _setup(db_pool)
    player1 = await _insert_player(db_pool, league_id, team_a_id, first="Gamma")
    player2 = await _insert_player(db_pool, league_id, team_a_id, first="Delta")

    await trade_block_repo.add_to_block(db_pool, league_id, team_a_id, player1)
    await trade_block_repo.add_to_block(db_pool, league_id, team_a_id, player2)

    removed = await trade_block_repo.clear_team_block(db_pool, league_id, team_a_id)
    assert removed == 2

    entries = await trade_block_repo.get_team_block(db_pool, league_id, team_a_id)
    assert entries == []


# ---------------------------------------------------------------------------
# Team rename test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_team_rename_updates_db(db_pool):
    league_id, team_a_id, _ = await _setup(db_pool)

    await team_repo.rename_team(db_pool, team_a_id, "Legends", "Brooklyn")

    row = await db_pool.fetchrow("SELECT name, city FROM teams WHERE id = $1", team_a_id)
    assert row["name"] == "Legends"
    assert row["city"] == "Brooklyn"
