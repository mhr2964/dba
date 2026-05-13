"""
Service-layer tests for services.roster_service.

Test data is inserted directly into dba_test via db_pool before calling
the service. get_pool is already patched by conftest.patch_get_pool.
"""
from __future__ import annotations

import pytest

from core.errors import DBAError
from services import roster_service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_league_and_team(pool) -> tuple[int, int]:
    """Insert a league + one team; return (league_id, team_id)."""
    league_row = await pool.fetchrow(
        """
        INSERT INTO leagues
            (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES (777001, 'Roster Test League', 2025, 2025, 11111)
        RETURNING id
        """
    )
    league_id: int = league_row["id"]

    team_row = await pool.fetchrow(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'LAL', 'Lakers', 'Los Angeles', 'West', 'Pacific')
        RETURNING id
        """,
        league_id,
    )
    team_id: int = team_row["id"]
    return league_id, team_id


async def _insert_player(pool, league_id: int, team_id: int, overall: int = 75) -> int:
    """Insert a minimal player row; return player id."""
    row = await pool.fetchrow(
        """
        INSERT INTO players (
            league_id, first_name, last_name, position,
            years_pro, overall, speed, shooting_2pt, shooting_3pt,
            shooting_mid, finishing, playmaking, defense, rebounding,
            iq, potential, peak_age_start, peak_age_end,
            loyalty, money_drive, win_drive, team_id
        )
        VALUES ($1, 'Test', 'Player', 'PG',
                3, $2, 70, 70, 70,
                70, 70, 70, 70, 70,
                70, 80, 25, 29,
                50, 50, 50, $3)
        RETURNING id
        """,
        league_id,
        overall,
        team_id,
    )
    return row["id"]


async def _insert_contract(
    pool, league_id: int, player_id: int, team_id: int, salary: int
) -> None:
    """Insert an active contract for a player."""
    await pool.execute(
        """
        INSERT INTO contracts
            (league_id, player_id, team_id, salary, years_remaining,
             total_years, contract_type, signed_in_season, is_active)
        VALUES ($1, $2, $3, $4, 2, 4, 'max', 2025, TRUE)
        """,
        league_id,
        player_id,
        team_id,
        salary,
    )


# ---------------------------------------------------------------------------
# get_cap_summary()
# ---------------------------------------------------------------------------


async def test_cap_summary_empty_roster(db_pool):
    """Cap summary with no players shows used=0 and remaining equals full cap."""
    league_id, team_id = await _seed_league_and_team(db_pool)

    summary = await roster_service.get_cap_summary(league_id, team_id)

    assert summary["used"] == 0
    assert summary["cap"] == 140_000_000
    assert summary["remaining"] == 140_000_000
    assert summary["pct"] == 0.0


async def test_cap_summary_with_contracts(db_pool):
    """Cap summary correctly sums active contract salaries."""
    league_id, team_id = await _seed_league_and_team(db_pool)

    salaries = [30_000_000, 20_000_000, 15_000_000]
    for salary in salaries:
        pid = await _insert_player(db_pool, league_id, team_id)
        await _insert_contract(db_pool, league_id, pid, team_id, salary)

    summary = await roster_service.get_cap_summary(league_id, team_id)

    expected_used = sum(salaries)
    assert summary["used"] == expected_used
    assert summary["remaining"] == 140_000_000 - expected_used
    assert summary["pct"] == round(expected_used / 140_000_000 * 100, 1)


async def test_cap_summary_inactive_contracts_excluded(db_pool):
    """Inactive contracts are not counted toward cap usage."""
    league_id, team_id = await _seed_league_and_team(db_pool)

    pid = await _insert_player(db_pool, league_id, team_id)
    # Insert an inactive contract — should not count.
    await db_pool.execute(
        """
        INSERT INTO contracts
            (league_id, player_id, team_id, salary, years_remaining,
             total_years, contract_type, signed_in_season, is_active)
        VALUES ($1, $2, $3, 25000000, 0, 2, 'max', 2024, FALSE)
        """,
        league_id,
        pid,
        team_id,
    )

    summary = await roster_service.get_cap_summary(league_id, team_id)
    assert summary["used"] == 0


async def test_cap_summary_league_not_found_raises_dba_error(db_pool):
    """get_cap_summary raises DBAError when the league id does not exist."""
    with pytest.raises(DBAError, match="League not found"):
        await roster_service.get_cap_summary(league_id=999999, team_id=1)


# ---------------------------------------------------------------------------
# get_roster()
# ---------------------------------------------------------------------------


async def test_get_roster_ordered_by_overall_descending(db_pool):
    """get_roster returns players sorted highest overall first."""
    league_id, team_id = await _seed_league_and_team(db_pool)

    overalls = [65, 90, 75, 80, 70]
    for ovr in overalls:
        await _insert_player(db_pool, league_id, team_id, overall=ovr)

    players = await roster_service.get_roster(league_id, team_id)

    assert len(players) == 5
    player_overalls = [p.overall for p in players]
    assert player_overalls == sorted(player_overalls, reverse=True)


async def test_get_roster_empty_returns_empty_list(db_pool):
    """get_roster returns an empty list when the team has no players."""
    league_id, team_id = await _seed_league_and_team(db_pool)

    players = await roster_service.get_roster(league_id, team_id)
    assert players == []


async def test_get_roster_scoped_to_team(db_pool):
    """get_roster does not include players from other teams."""
    league_id, team_id = await _seed_league_and_team(db_pool)

    # Insert a second team.
    other_team_row = await db_pool.fetchrow(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'BOS', 'Celtics', 'Boston', 'East', 'Atlantic')
        RETURNING id
        """,
        league_id,
    )
    other_team_id: int = other_team_row["id"]

    # One player on each team.
    await _insert_player(db_pool, league_id, team_id, overall=85)
    await _insert_player(db_pool, league_id, other_team_id, overall=88)

    players = await roster_service.get_roster(league_id, team_id)
    assert len(players) == 1
    assert players[0].team_id == team_id
