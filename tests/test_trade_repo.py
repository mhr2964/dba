"""
Integration tests for data.repositories.trade_repo.

Uses db_pool directly. clean_db (autouse) truncates leagues CASCADE before each test.
"""
from __future__ import annotations

from data.repositories import trade_repo


# ---------------------------------------------------------------------------
# Helper — insert league + two teams
# ---------------------------------------------------------------------------


async def _setup(pool) -> tuple[int, int, int]:
    """Insert a league and two teams. Returns (league_id, team_a_id, team_b_id)."""
    league_row = await pool.fetchrow(
        """
        INSERT INTO leagues (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES (666001, 'Trade Repo Test League', 2025, 2025, 11111)
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


async def _insert_player(pool, league_id: int, team_id: int) -> int:
    """Insert a minimal player and return its id."""
    row = await pool.fetchrow(
        """
        INSERT INTO players (
            league_id, first_name, last_name, position, team_id,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq, potential,
            peak_age_start, peak_age_end, loyalty, money_drive, win_drive
        ) VALUES (
            $1, 'Trade', 'Guy', 'SF', $2,
            80, 75, 75, 70, 70,
            75, 70, 75, 70, 75, 85,
            24, 30, 50, 50, 60
        )
        RETURNING id
        """,
        league_id, team_id,
    )
    return row["id"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_create_trade(db_pool):
    """create_trade returns a Trade with correct proposer, counterparty, and default status."""
    league_id, team_a, team_b = await _setup(db_pool)

    trade = await trade_repo.create_trade(
        db_pool,
        league_id=league_id,
        season=2025,
        proposer_id=team_a,
        counterparty_id=team_b,
    )

    assert trade.id is not None
    assert trade.proposer_team_id == team_a
    assert trade.counterparty_team_id == team_b
    assert trade.league_id == league_id
    assert trade.status == "pending_counterparty"
    assert trade.season == 2025


async def test_add_player_asset(db_pool):
    """add_asset with asset_type='player' inserts a row in trade_assets."""
    league_id, team_a, team_b = await _setup(db_pool)
    player_id = await _insert_player(db_pool, league_id, team_a)

    trade = await trade_repo.create_trade(
        db_pool,
        league_id=league_id,
        season=2025,
        proposer_id=team_a,
        counterparty_id=team_b,
    )

    asset = await trade_repo.add_asset(
        db_pool,
        trade_id=trade.id,
        from_team_id=team_a,
        to_team_id=team_b,
        asset_type="player",
        player_id=player_id,
    )

    assert asset.trade_id == trade.id
    assert asset.from_team_id == team_a
    assert asset.to_team_id == team_b
    assert asset.asset_type == "player"
    assert asset.player_id == player_id

    # Verify via raw query
    row = await db_pool.fetchrow(
        "SELECT * FROM trade_assets WHERE id = $1", asset.id
    )
    assert row is not None
    assert row["player_id"] == player_id


async def test_update_status(db_pool):
    """update_status changes trade status in the DB."""
    league_id, team_a, team_b = await _setup(db_pool)

    trade = await trade_repo.create_trade(
        db_pool,
        league_id=league_id,
        season=2025,
        proposer_id=team_a,
        counterparty_id=team_b,
    )

    await trade_repo.update_status(db_pool, trade.id, "approved")

    refreshed = await trade_repo.get_trade(db_pool, trade.id)
    assert refreshed.status == "approved"
    assert refreshed.resolved_at is not None  # approved is a resolved status


async def test_count_pending_commissioner(db_pool):
    """count_pending_commissioner returns the count of trades awaiting commissioner review."""
    league_id, team_a, team_b = await _setup(db_pool)

    for _ in range(2):
        trade = await trade_repo.create_trade(
            db_pool,
            league_id=league_id,
            season=2025,
            proposer_id=team_a,
            counterparty_id=team_b,
        )
        await trade_repo.update_status(db_pool, trade.id, "pending_commissioner")

    # One extra trade that stays pending_counterparty — should not be counted
    await trade_repo.create_trade(
        db_pool,
        league_id=league_id,
        season=2025,
        proposer_id=team_a,
        counterparty_id=team_b,
    )

    count = await trade_repo.count_pending_commissioner(db_pool, league_id)
    assert count == 2


async def test_seed_picks_for_league(db_pool):
    """seed_picks_for_league creates draft_picks for both rounds across multiple seasons."""
    league_id, team_a, team_b = await _setup(db_pool)

    await trade_repo.seed_picks_for_league(
        db_pool,
        league_id=league_id,
        from_season=2025,
        num_seasons=3,
    )

    rows = await db_pool.fetch(
        "SELECT * FROM draft_picks WHERE league_id = $1 ORDER BY season, round",
        league_id,
    )

    # 2 teams * 3 seasons * 2 rounds = 12 rows
    assert len(rows) == 12, f"Expected 12 picks, got {len(rows)}"

    rounds = {r["round"] for r in rows}
    assert 1 in rounds, "Round 1 picks should be present"
    assert 2 in rounds, "Round 2 picks should be present"

    seasons = {r["season"] for r in rows}
    assert seasons == {2025, 2026, 2027}, f"Unexpected seasons: {seasons}"

    team_ids_in_picks = {r["current_team_id"] for r in rows}
    assert team_a in team_ids_in_picks
    assert team_b in team_ids_in_picks
