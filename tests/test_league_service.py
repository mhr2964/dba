"""
Service-layer tests for services.league_service.

All tests run against dba_test (real asyncpg, no real Discord).
Guild/role/channel interactions use mocks from conftest.
"""
from __future__ import annotations

import pytest

from core.errors import DBAError, PhaseError
from data.repositories import league_repo, team_repo
from phase.states import Phase
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
    await league_service.assign_manager(
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


# ---------------------------------------------------------------------------
# PT1 -- advance_phase phase-graph validation
#
# Before this sweep, advance_phase() performed zero validation that a
# transition was a legal *move* between phases -- it accepted any string that
# parsed as a valid Phase enum member and blindly wrote it, regardless of the
# league's actual current phase. See docs/design/phase-transition-logic-rules.md.
# ---------------------------------------------------------------------------


async def _create_league_in_phase(pool, phase: str, guild_id: int = 780001) -> int:
    """Insert a minimal league row at the given phase; returns its id."""
    row = await pool.fetchrow(
        """
        INSERT INTO leagues
            (discord_guild_id, name, start_season_year, current_season,
             commissioner_user_id, current_phase)
        VALUES ($1, 'PT1 Phase Test League', 2025, 2025, 99999, $2)
        RETURNING id
        """,
        guild_id, phase,
    )
    return row["id"]


async def test_advance_phase_allows_legal_forward_transition(db_pool):
    """The ordinary, expected case: one legal hop must succeed and persist."""
    league_id = await _create_league_in_phase(db_pool, "REGULAR_SEASON_ACTIVE")

    await league_service.advance_phase(league_id, "TRADE_DEADLINE_OPEN")

    phase = await db_pool.fetchval("SELECT current_phase FROM leagues WHERE id = $1", league_id)
    assert phase == "TRADE_DEADLINE_OPEN"


async def test_advance_phase_rejects_illegal_forward_skip(db_pool):
    """PT1 -- MANDATORY live smoke test, case 2.

    REGULAR_SEASON_ACTIVE -> FA_OPEN skips 13 intermediate phases
    (TRADE_DEADLINE_OPEN through WAIVERS_OPEN/PROGRESSION_PENDING). Nothing
    stopped this pre-fix -- advance_phase only checked that "FA_OPEN" parsed
    as a valid Phase member, not that it was reachable from
    REGULAR_SEASON_ACTIVE. Proven to silently succeed pre-fix via `git stash`
    on services/league_service.py (see session verification notes for the
    captured pre-fix output); must raise PhaseError post-fix and must NOT
    have written the new phase to the DB.
    """
    league_id = await _create_league_in_phase(db_pool, "REGULAR_SEASON_ACTIVE")

    with pytest.raises(PhaseError):
        await league_service.advance_phase(league_id, "FA_OPEN")

    phase = await db_pool.fetchval("SELECT current_phase FROM leagues WHERE id = $1", league_id)
    assert phase == "REGULAR_SEASON_ACTIVE", "Rejected transition must not have written the DB"


async def test_advance_phase_rejects_backward_jump(db_pool):
    """NBA_FINALS -> SETUP is a full-cycle backward jump -- must be rejected."""
    league_id = await _create_league_in_phase(db_pool, "NBA_FINALS")

    with pytest.raises(PhaseError):
        await league_service.advance_phase(league_id, "SETUP")

    phase = await db_pool.fetchval("SELECT current_phase FROM leagues WHERE id = $1", league_id)
    assert phase == "NBA_FINALS"


async def test_advance_phase_rejects_same_phase_noop(db_pool):
    """A league can't "advance" to the phase it's already in."""
    league_id = await _create_league_in_phase(db_pool, "REGULAR_SEASON_ACTIVE")

    with pytest.raises(PhaseError):
        await league_service.advance_phase(league_id, "REGULAR_SEASON_ACTIVE")


async def test_advance_phase_error_message_lists_legal_next_phases(db_pool):
    """PT2 rides on this message -- it must name the current phase, the
    rejected target, and what IS legal, not a bare/generic failure."""
    league_id = await _create_league_in_phase(db_pool, "FA_OPEN")

    with pytest.raises(PhaseError) as exc_info:
        await league_service.advance_phase(league_id, "WAIVERS_OPEN")

    msg = str(exc_info.value)
    assert "FA_OPEN" in msg
    assert "WAIVERS_OPEN" in msg
    assert "FA_CLOSED" in msg, "FA_OPEN's one legal next phase (FA_CLOSED) should be named in the message"


async def test_advance_phase_unknown_phase_string_raises_value_error(db_pool):
    """A string that isn't a real Phase member at all is a caller bug, not a
    phase-graph rejection -- still raises, just via the enum lookup."""
    league_id = await _create_league_in_phase(db_pool, "REGULAR_SEASON_ACTIVE")

    with pytest.raises(ValueError):
        await league_service.advance_phase(league_id, "NOT_A_REAL_PHASE")


# ---------------------------------------------------------------------------
# PT1 -- deliberate graph branch points (see phase/graph.py's module docstring)
# ---------------------------------------------------------------------------


async def test_advance_phase_allows_rollover_skip_from_awards_closed(db_pool):
    """OFFSEASON_AWARDS_CLOSED -> PROGRESSION_PENDING: the real rollover skip
    that bypasses the draft phases entirely (RO9 / PT1's disclosed deviation
    from the enum's declared order)."""
    league_id = await _create_league_in_phase(db_pool, "OFFSEASON_AWARDS_CLOSED")

    await league_service.advance_phase(league_id, Phase.PROGRESSION_PENDING.value)

    phase = await db_pool.fetchval("SELECT current_phase FROM leagues WHERE id = $1", league_id)
    assert phase == Phase.PROGRESSION_PENDING.value


async def test_advance_phase_allows_rollover_skip_from_draft_lottery_done(db_pool):
    """DRAFT_LOTTERY_DONE -> PROGRESSION_PENDING: same rollover skip, the
    other phase /offseason rollover's precondition allows calling from."""
    league_id = await _create_league_in_phase(db_pool, "DRAFT_LOTTERY_DONE")

    await league_service.advance_phase(league_id, Phase.PROGRESSION_PENDING.value)

    phase = await db_pool.fetchval("SELECT current_phase FROM leagues WHERE id = $1", league_id)
    assert phase == Phase.PROGRESSION_PENDING.value


async def test_advance_phase_allows_champion_decided_skip_to_awards_closed(db_pool):
    """NBA_FINALS -> OFFSEASON_AWARDS_CLOSED: playoff_cog's champion-decided
    auto-advance, which skips the OFFSEASON_AWARDS_OPEN voting phase."""
    league_id = await _create_league_in_phase(db_pool, "NBA_FINALS")

    await league_service.advance_phase(league_id, Phase.OFFSEASON_AWARDS_CLOSED.value)

    phase = await db_pool.fetchval("SELECT current_phase FROM leagues WHERE id = $1", league_id)
    assert phase == Phase.OFFSEASON_AWARDS_CLOSED.value


async def test_advance_phase_allows_progression_pending_to_fa_open(db_pool):
    """PROGRESSION_PENDING -> FA_OPEN: the real /offseason progression call
    site -- PROGRESSION_PENDING is declared LAST in the enum, but this is a
    real mid-cycle transition, not a wraparound."""
    league_id = await _create_league_in_phase(db_pool, "PROGRESSION_PENDING")

    await league_service.advance_phase(league_id, Phase.FA_OPEN.value)

    phase = await db_pool.fetchval("SELECT current_phase FROM leagues WHERE id = $1", league_id)
    assert phase == Phase.FA_OPEN.value


async def test_advance_phase_allows_waivers_open_to_preseason_ready(db_pool):
    """WAIVERS_OPEN -> PRESEASON_READY: closes the season loop into next
    season via the manual /league advance path (phase/helpers.py's own hint
    text points here)."""
    league_id = await _create_league_in_phase(db_pool, "WAIVERS_OPEN")

    await league_service.advance_phase(league_id, Phase.PRESEASON_READY.value)

    phase = await db_pool.fetchval("SELECT current_phase FROM leagues WHERE id = $1", league_id)
    assert phase == Phase.PRESEASON_READY.value


async def test_advance_phase_allows_setup_to_skip_preseason_ready(db_pool):
    """SETUP -> REGULAR_SEASON_ACTIVE directly: a league's very first season
    can skip PRESEASON_READY entirely (/season start accepts either phase)."""
    league_id = await _create_league_in_phase(db_pool, "SETUP")

    await league_service.advance_phase(league_id, Phase.REGULAR_SEASON_ACTIVE.value)

    phase = await db_pool.fetchval("SELECT current_phase FROM leagues WHERE id = $1", league_id)
    assert phase == Phase.REGULAR_SEASON_ACTIVE.value


async def test_advance_phase_nonexistent_league_raises_dba_error(db_pool):
    with pytest.raises(DBAError, match="not found"):
        await league_service.advance_phase(999_999_999, "REGULAR_SEASON_ACTIVE")
