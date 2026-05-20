"""
Repository-layer tests for data.repositories.team_repo.

These tests use db_pool directly — no service or Discord involvement.
A league row is inserted manually before each test that needs one, since
team rows have a FK on leagues(id).
"""
from __future__ import annotations

from data.repositories import team_repo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_TEAM = {
    "code": "LAL",
    "name": "Lakers",
    "city": "Los Angeles",
    "conference": "West",
    "division": "Pacific",
}

_SECOND_TEAM = {
    "code": "BOS",
    "name": "Celtics",
    "city": "Boston",
    "conference": "East",
    "division": "Atlantic",
}

_THIRD_TEAM = {
    "code": "MIA",
    "name": "Heat",
    "city": "Miami",
    "conference": "East",
    "division": "Southeast",
}


async def _insert_league(pool, guild_id=888001) -> int:
    """Insert a bare-minimum league row and return its id."""
    row = await pool.fetchrow(
        """
        INSERT INTO leagues (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES ($1, 'Repo Test League', 2025, 2025, 11111)
        RETURNING id
        """,
        guild_id,
    )
    return row["id"]


# ---------------------------------------------------------------------------
# get_by_code — case insensitivity
# ---------------------------------------------------------------------------


async def test_get_by_code_case_insensitive(db_pool):
    """get_by_code finds a team regardless of the case supplied by the caller."""
    league_id = await _insert_league(db_pool)
    await team_repo.create(db_pool, league_id, _MINIMAL_TEAM)

    lower = await team_repo.get_by_code(db_pool, league_id, "lal")
    upper = await team_repo.get_by_code(db_pool, league_id, "LAL")
    mixed = await team_repo.get_by_code(db_pool, league_id, "LaL")

    assert lower is not None
    assert upper is not None
    assert mixed is not None
    assert lower.nba_team_code == upper.nba_team_code == mixed.nba_team_code


async def test_get_by_code_returns_none_for_missing_team(db_pool):
    """get_by_code returns None when no team matches."""
    league_id = await _insert_league(db_pool)
    result = await team_repo.get_by_code(db_pool, league_id, "XXX")
    assert result is None


# ---------------------------------------------------------------------------
# get_all — ordering
# ---------------------------------------------------------------------------


async def test_get_all_orders_by_conference_then_division_then_name(db_pool):
    """get_all returns teams ordered conference → division → name (all ASC)."""
    league_id = await _insert_league(db_pool)

    # Insert in intentionally scrambled order.
    for data in [_MINIMAL_TEAM, _SECOND_TEAM, _THIRD_TEAM]:
        await team_repo.create(db_pool, league_id, data)

    teams = await team_repo.get_all(db_pool, league_id)

    # East comes before West alphabetically.
    conferences = [t.conference for t in teams]
    assert conferences == sorted(conferences), "Teams not grouped by conference"

    # Within each conference, ordered by division then name.
    for i in range(len(teams) - 1):
        a, b = teams[i], teams[i + 1]
        if a.conference == b.conference:
            key_a = (a.division, a.name)
            key_b = (b.division, b.name)
            assert key_a <= key_b, (
                f"Out of order: {a.full_name} ({a.division}) before {b.full_name} ({b.division})"
            )


async def test_get_all_scoped_to_league(db_pool):
    """get_all only returns teams belonging to the requested league."""
    league_a = await _insert_league(db_pool, guild_id=888001)
    league_b = await _insert_league(db_pool, guild_id=888002)

    await team_repo.create(db_pool, league_a, _MINIMAL_TEAM)
    await team_repo.create(db_pool, league_b, _SECOND_TEAM)

    teams_a = await team_repo.get_all(db_pool, league_a)
    assert len(teams_a) == 1
    assert teams_a[0].nba_team_code == "LAL"


# ---------------------------------------------------------------------------
# set_manager and log_manager_change
# ---------------------------------------------------------------------------


async def test_set_manager_updates_db(db_pool):
    """set_manager persists manager_user_id to the teams row."""
    league_id = await _insert_league(db_pool)
    team = await team_repo.create(db_pool, league_id, _MINIMAL_TEAM)

    await team_repo.set_manager(db_pool, team.id, 99999)

    refreshed = await team_repo.get_by_id(db_pool, team.id)
    assert refreshed.manager_user_id == 99999


async def test_set_manager_to_none_clears_manager(db_pool):
    """set_manager(None) sets manager_user_id back to NULL."""
    league_id = await _insert_league(db_pool)
    team = await team_repo.create(db_pool, league_id, _MINIMAL_TEAM)

    await team_repo.set_manager(db_pool, team.id, 99999)
    await team_repo.set_manager(db_pool, team.id, None)

    refreshed = await team_repo.get_by_id(db_pool, team.id)
    assert refreshed.manager_user_id is None


async def test_log_manager_change_inserts_history_row(db_pool):
    """log_manager_change inserts a team_manager_history row with correct values."""
    league_id = await _insert_league(db_pool)
    team = await team_repo.create(db_pool, league_id, _MINIMAL_TEAM)

    await team_repo.set_manager(db_pool, team.id, 77777)
    await team_repo.log_manager_change(db_pool, team.id, user_id=77777, assigned_by=11111)

    row = await db_pool.fetchrow(
        "SELECT * FROM team_manager_history WHERE team_id = $1 ORDER BY id DESC LIMIT 1",
        team.id,
    )
    assert row is not None
    assert row["user_id"] == 77777
    assert row["assigned_by"] == 11111


async def test_log_manager_change_accepts_null_user_id(db_pool):
    """log_manager_change with user_id=None records the removal correctly."""
    league_id = await _insert_league(db_pool)
    team = await team_repo.create(db_pool, league_id, _MINIMAL_TEAM)

    await team_repo.log_manager_change(db_pool, team.id, user_id=None, assigned_by=11111)

    row = await db_pool.fetchrow(
        "SELECT * FROM team_manager_history WHERE team_id = $1",
        team.id,
    )
    assert row["user_id"] is None
