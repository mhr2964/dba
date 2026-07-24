"""
Integration tests for trade_repo.get_cooldown_team_pairs (finding #6: season-long
trade-partner cooldown). Follows tests/test_trade_repo.py's db_pool pattern.
"""
from __future__ import annotations

import datetime

from data.repositories import trade_repo


async def _setup(pool) -> tuple[int, int, int, int]:
    """Insert a league and three teams. Returns (league_id, team_a, team_b, team_c)."""
    league_row = await pool.fetchrow(
        """
        INSERT INTO leagues (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES (666002, 'Cooldown Test League', 2025, 2025, 11112)
        RETURNING id
        """,
    )
    league_id = league_row["id"]

    async def _team(code: str) -> int:
        row = await pool.fetchrow(
            """
            INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
            VALUES ($1, $2, $2, $2, 'West', 'Pacific')
            RETURNING id
            """,
            league_id, code,
        )
        return row["id"]

    team_a = await _team("AAA")
    team_b = await _team("BBB")
    team_c = await _team("CCC")
    return league_id, team_a, team_b, team_c


async def _insert_simmed_game(pool, league_id: int, game_index: int, scheduled_date: datetime.date) -> None:
    """Insert a minimal simmed game so cutoff-date anchoring has data to read."""
    # Need two distinct team ids for home/away — reuse the helper's own teams.
    team_rows = await pool.fetch("SELECT id FROM teams WHERE league_id = $1 ORDER BY id", league_id)
    home_id, away_id = team_rows[0]["id"], team_rows[1]["id"]
    await pool.execute(
        """
        INSERT INTO games
            (league_id, season, game_index, home_team_id, away_team_id,
             scheduled_date, status, is_user_matchup, rng_seed)
        VALUES ($1, 2025, $2, $3, $4, $5, 'simmed', FALSE, 1)
        """,
        league_id, game_index, home_id, away_id, scheduled_date,
    )


async def _insert_approved_trade(pool, league_id: int, team_a: int, team_b: int, resolved_at: datetime.datetime) -> None:
    trade = await trade_repo.create_trade(
        pool, league_id=league_id, season=2025, proposer_id=team_a, counterparty_id=team_b,
    )
    await pool.execute(
        "UPDATE trades SET status = 'approved', resolved_at = $1 WHERE id = $2",
        resolved_at, trade.id,
    )


async def test_recent_completed_trade_pair_is_in_cooldown(db_pool):
    """A trade resolved 5 days before the last simmed game (well within a
    30-day cooldown window) must return that team pair."""
    league_id, team_a, team_b, _team_c = await _setup(db_pool)

    last_game_date = datetime.date(2025, 3, 1)
    await _insert_simmed_game(db_pool, league_id, 40, last_game_date)
    await _insert_approved_trade(
        db_pool, league_id, team_a, team_b,
        resolved_at=datetime.datetime(2025, 2, 24, tzinfo=datetime.timezone.utc),
    )

    pairs = await trade_repo.get_cooldown_team_pairs(db_pool, league_id, cooldown_days=30.0)

    expected = (min(team_a, team_b), max(team_a, team_b))
    assert expected in pairs, f"Expected {expected} in cooldown pairs; got {pairs}"


async def test_old_completed_trade_pair_is_not_in_cooldown(db_pool):
    """A trade resolved well before the cooldown window (60 days out, vs a
    30-day window) must NOT be returned — the pair is free to re-trade."""
    league_id, team_a, team_b, _team_c = await _setup(db_pool)

    last_game_date = datetime.date(2025, 3, 1)
    await _insert_simmed_game(db_pool, league_id, 40, last_game_date)
    await _insert_approved_trade(
        db_pool, league_id, team_a, team_b,
        resolved_at=datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc),
    )

    pairs = await trade_repo.get_cooldown_team_pairs(db_pool, league_id, cooldown_days=30.0)

    expected = (min(team_a, team_b), max(team_a, team_b))
    assert expected not in pairs, f"Did not expect {expected} in cooldown pairs; got {pairs}"


async def test_unrelated_pair_unaffected(db_pool):
    """A completed trade between A/B must not put C into any cooldown pair."""
    league_id, team_a, team_b, team_c = await _setup(db_pool)

    last_game_date = datetime.date(2025, 3, 1)
    await _insert_simmed_game(db_pool, league_id, 40, last_game_date)
    await _insert_approved_trade(
        db_pool, league_id, team_a, team_b,
        resolved_at=datetime.datetime(2025, 2, 24, tzinfo=datetime.timezone.utc),
    )

    pairs = await trade_repo.get_cooldown_team_pairs(db_pool, league_id, cooldown_days=30.0)

    for pair in pairs:
        assert team_c not in pair, f"Team C should not appear in any cooldown pair; got {pairs}"


async def test_non_approved_trade_does_not_trigger_cooldown(db_pool):
    """A pending (not approved) trade between two teams must not cool them down."""
    league_id, team_a, team_b, _team_c = await _setup(db_pool)

    last_game_date = datetime.date(2025, 3, 1)
    await _insert_simmed_game(db_pool, league_id, 40, last_game_date)
    await trade_repo.create_trade(
        db_pool, league_id=league_id, season=2025, proposer_id=team_a, counterparty_id=team_b,
    )  # left at default 'pending_counterparty' status

    pairs = await trade_repo.get_cooldown_team_pairs(db_pool, league_id, cooldown_days=30.0)

    expected = (min(team_a, team_b), max(team_a, team_b))
    assert expected not in pairs, f"Pending trade should not trigger cooldown; got {pairs}"


async def test_no_simmed_games_returns_empty(db_pool):
    """No simmed-game data yet (season hasn't started) — safe empty list, no crash."""
    league_id, team_a, team_b, _team_c = await _setup(db_pool)
    await _insert_approved_trade(
        db_pool, league_id, team_a, team_b,
        resolved_at=datetime.datetime(2025, 2, 24, tzinfo=datetime.timezone.utc),
    )

    pairs = await trade_repo.get_cooldown_team_pairs(db_pool, league_id, cooldown_days=30.0)

    assert pairs == [], f"Expected empty cooldown list with no simmed games; got {pairs}"
