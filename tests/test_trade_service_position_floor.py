"""
Integration tests for the #3 hard positional-floor check in
trade_service.propose (docs/design/trade-logic-rules.md).

Uses db_pool directly, following tests/test_trade_repo.py's pattern.
Both teams are human-managed (manager_user_id set) so propose() returns at
'pending_counterparty' without invoking the CPU-evaluation pipeline — keeps
these tests focused on the positional-floor gate itself.
"""
from __future__ import annotations

import pytest

from core.errors import DBAError
from data.repositories import league_repo, team_repo
from services import trade_service

pytestmark = pytest.mark.usefixtures("patch_get_pool")


async def _setup_league_and_teams(pool) -> tuple[league_repo.League, team_repo.Team, team_repo.Team]:
    league_row = await pool.fetchrow(
        """
        INSERT INTO leagues (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES (777001, 'Position Floor Test League', 2025, 2025, 22222)
        RETURNING *
        """,
    )
    league = league_repo._league_from_record(league_row)

    team_a_row = await pool.fetchrow(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division, manager_user_id)
        VALUES ($1, 'LAL', 'Lakers', 'Los Angeles', 'West', 'Pacific', 501)
        RETURNING *
        """,
        league.id,
    )
    team_b_row = await pool.fetchrow(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division, manager_user_id)
        VALUES ($1, 'BOS', 'Celtics', 'Boston', 'East', 'Atlantic', 502)
        RETURNING *
        """,
        league.id,
    )
    return league, team_repo._team_from_record(team_a_row), team_repo._team_from_record(team_b_row)


async def _insert_player(pool, league_id: int, team_id: int, position: str, overall: int = 75) -> int:
    row = await pool.fetchrow(
        """
        INSERT INTO players (
            league_id, first_name, last_name, position, team_id,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq, potential,
            peak_age_start, peak_age_end, loyalty, money_drive, win_drive
        ) VALUES (
            $1, 'Trade', 'Guy', $3, $2,
            $4, 75, 75, 70, 70,
            75, 70, 75, 70, 75, 85,
            24, 30, 50, 50, 60
        )
        RETURNING id
        """,
        league_id, team_id, position, overall,
    )
    return row["id"]


async def test_propose_rejects_trade_leaving_zero_at_a_position(db_pool):
    """Team A has exactly one center; trading it away with nothing at C coming
    back must be rejected before the trade record is even created."""
    league, team_a, team_b = await _setup_league_and_teams(db_pool)

    # Team A: one C (the one being traded), plus enough at other positions.
    only_center = await _insert_player(db_pool, league.id, team_a.id, "C")
    for pos in ("PG", "SG", "SF", "PF"):
        await _insert_player(db_pool, league.id, team_a.id, pos)

    # Team B sends back a non-center (SF) — doesn't fill A's vacated C slot.
    b_sf = await _insert_player(db_pool, league.id, team_b.id, "SF")
    # Team B needs enough roster depth of its own so it isn't also emptied out.
    for pos in ("PG", "SG", "PF", "C"):
        await _insert_player(db_pool, league.id, team_b.id, pos)

    with pytest.raises(DBAError, match="core position"):
        await trade_service.propose(
            league=league,
            proposer_team=team_a,
            counterparty_team=team_b,
            proposer_player_ids=[only_center],
            proposer_pick_ids=[],
            counterparty_player_ids=[b_sf],
            counterparty_pick_ids=[],
        )


async def test_propose_allows_trade_when_incoming_player_fills_the_position(db_pool):
    """Same shape, but team B sends back a center — A's post-trade C count
    stays at 1 (the incoming player), so the floor check must NOT block this."""
    league, team_a, team_b = await _setup_league_and_teams(db_pool)

    only_center = await _insert_player(db_pool, league.id, team_a.id, "C")
    for pos in ("PG", "SG", "SF", "PF"):
        await _insert_player(db_pool, league.id, team_a.id, pos)

    b_center = await _insert_player(db_pool, league.id, team_b.id, "C")
    for pos in ("PG", "SG", "SF", "PF"):
        await _insert_player(db_pool, league.id, team_b.id, pos)

    trade = await trade_service.propose(
        league=league,
        proposer_team=team_a,
        counterparty_team=team_b,
        proposer_player_ids=[only_center],
        proposer_pick_ids=[],
        counterparty_player_ids=[b_center],
        counterparty_pick_ids=[],
    )

    assert trade.status == "pending_counterparty", (
        f"Trade should be created normally when the position is backfilled; got status={trade.status}"
    )


async def test_propose_allows_trade_with_surplus_at_the_position(db_pool):
    """Team A has 2 centers and trades one away — 1 remains, so the floor
    check (which only blocks reaching ZERO) must not fire."""
    league, team_a, team_b = await _setup_league_and_teams(db_pool)

    center_1 = await _insert_player(db_pool, league.id, team_a.id, "C")
    await _insert_player(db_pool, league.id, team_a.id, "C")  # stays on roster
    for pos in ("PG", "SG", "SF", "PF"):
        await _insert_player(db_pool, league.id, team_a.id, pos)

    b_sf = await _insert_player(db_pool, league.id, team_b.id, "SF")
    await _insert_player(db_pool, league.id, team_b.id, "SF")  # stays on roster
    for pos in ("PG", "SG", "PF", "C"):
        await _insert_player(db_pool, league.id, team_b.id, pos)

    trade = await trade_service.propose(
        league=league,
        proposer_team=team_a,
        counterparty_team=team_b,
        proposer_player_ids=[center_1],
        proposer_pick_ids=[],
        counterparty_player_ids=[b_sf],
        counterparty_pick_ids=[],
    )

    assert trade.status == "pending_counterparty", (
        f"Trade should be created when the traded position has surplus; got status={trade.status}"
    )
