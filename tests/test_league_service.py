"""
Service-layer tests for services.league_service.

All tests run against dba_test (real asyncpg, no real Discord).
Guild/role/channel interactions use mocks from conftest.
"""
from __future__ import annotations

import pytest

from core.errors import DBAError
from data.repositories import league_repo, team_repo
from services import league_service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_league(guild, commissioner, db_pool, name="Test League", year=2025):
    """Convenience wrapper that calls league_service.create and returns the league."""
    return await league_service.create(
        guild=guild,
        commissioner=commissioner,
        name=name,
        season_year=year,
    )


# ---------------------------------------------------------------------------
# create()
# ---------------------------------------------------------------------------


async def test_create_league_success(db_pool, mock_guild, mock_commissioner):
    """create() inserts a league row with correct guild and commissioner, and seeds 30 teams."""
    league = await _create_league(mock_guild, mock_commissioner, db_pool)

    # League row exists with correct values.
    db_league = await league_repo.get_by_guild(db_pool, mock_guild.id)
    assert db_league is not None
    assert db_league.discord_guild_id == mock_guild.id
    assert db_league.commissioner_user_id == mock_commissioner.id
    assert db_league.name == "Test League"
    assert db_league.start_season_year == 2025

    # Exactly 30 team rows created.
    teams = await team_repo.get_all(db_pool, league.id)
    assert len(teams) == 30


async def test_create_league_returns_league_object(db_pool, mock_guild, mock_commissioner):
    """create() returns a League dataclass with populated id."""
    league = await _create_league(mock_guild, mock_commissioner, db_pool)
    assert isinstance(league, league_repo.League)
    assert league.id > 0


async def test_create_league_duplicate_raises_dba_error(db_pool, mock_guild, mock_commissioner):
    """A second create() call for the same guild raises DBAError."""
    await _create_league(mock_guild, mock_commissioner, db_pool)

    with pytest.raises(DBAError, match="already has an active league"):
        await _create_league(mock_guild, mock_commissioner, db_pool, name="Duplicate League")


async def test_create_league_commissioner_role_added(db_pool, mock_guild, mock_commissioner):
    """commissioner.add_roles is awaited exactly once during league creation."""
    await _create_league(mock_guild, mock_commissioner, db_pool)
    mock_commissioner.add_roles.assert_awaited_once()


async def test_create_league_channels_stored(db_pool, mock_guild, mock_commissioner):
    """All five channel roles are persisted to league_channels."""
    league = await _create_league(mock_guild, mock_commissioner, db_pool)

    for role_name in league_service.CHANNEL_ROLES:
        channel_id = await league_repo.get_channel(db_pool, league.id, role_name)
        assert channel_id is not None, f"Channel row missing for role '{role_name}'"


# ---------------------------------------------------------------------------
# assign_manager()
# ---------------------------------------------------------------------------


async def test_assign_manager_success(db_pool, mock_guild, mock_commissioner, mock_manager):
    """assign_manager() sets manager_user_id and writes a history row."""
    league = await _create_league(mock_guild, mock_commissioner, db_pool)

    team = await league_service.assign_manager(
        guild=mock_guild,
        league=league,
        team_code="LAL",
        user=mock_manager,
    )

    # team object reflects the new manager.
    assert team.manager_user_id == mock_manager.id

    # DB row is updated.
    db_team = await team_repo.get_by_code(db_pool, league.id, "LAL")
    assert db_team.manager_user_id == mock_manager.id

    # History row inserted.
    row = await db_pool.fetchrow(
        "SELECT * FROM team_manager_history WHERE team_id = $1 ORDER BY id DESC LIMIT 1",
        team.id,
    )
    assert row is not None
    assert row["user_id"] == mock_manager.id


async def test_assign_manager_invalid_code_raises_dba_error(
    db_pool, mock_guild, mock_commissioner, mock_manager
):
    """assign_manager() raises DBAError when the team code does not exist."""
    league = await _create_league(mock_guild, mock_commissioner, db_pool)

    with pytest.raises(DBAError, match="No team found"):
        await league_service.assign_manager(
            guild=mock_guild,
            league=league,
            team_code="XXX",
            user=mock_manager,
        )


async def test_assign_manager_already_owns_team_raises_dba_error(
    db_pool, mock_guild, mock_commissioner, mock_manager
):
    """A user who already manages a team cannot be assigned to a second one."""
    league = await _create_league(mock_guild, mock_commissioner, db_pool)

    await league_service.assign_manager(
        guild=mock_guild, league=league, team_code="LAL", user=mock_manager
    )

    with pytest.raises(DBAError, match="already manages"):
        await league_service.assign_manager(
            guild=mock_guild, league=league, team_code="BOS", user=mock_manager
        )


# ---------------------------------------------------------------------------
# remove_manager()
# ---------------------------------------------------------------------------


async def test_remove_manager_success(db_pool, mock_guild, mock_commissioner, mock_manager):
    """remove_manager() sets manager_user_id to NULL."""
    league = await _create_league(mock_guild, mock_commissioner, db_pool)
    team = await league_service.assign_manager(
        guild=mock_guild, league=league, team_code="LAL", user=mock_manager
    )

    await league_service.remove_manager(
        guild=mock_guild,
        league=league,
        team_code="LAL",
        requester=mock_commissioner,
    )

    db_team = await team_repo.get_by_code(db_pool, league.id, "LAL")
    assert db_team.manager_user_id is None


async def test_remove_manager_cpu_team_raises_dba_error(
    db_pool, mock_guild, mock_commissioner
):
    """remove_manager() raises DBAError when the team has no manager."""
    league = await _create_league(mock_guild, mock_commissioner, db_pool)

    with pytest.raises(DBAError, match="no manager"):
        await league_service.remove_manager(
            guild=mock_guild,
            league=league,
            team_code="LAL",
            requester=mock_commissioner,
        )
