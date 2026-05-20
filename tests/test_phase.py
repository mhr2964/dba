"""
Tests for phase guard functions and phase transition logic.

All tests use the real test DB (db_pool fixture) and the autouse
clean_db + patch_get_pool fixtures from conftest.
"""
from __future__ import annotations

import pytest

from core.errors import DBAError, PhaseError
from data.repositories import league_repo
from phase.helpers import require_commissioner, require_phase
from phase.transitions import allowed_phases_for, is_allowed


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


async def _create_league_in_phase(pool, phase: str) -> league_repo.League:
    """Insert a minimal league row with the given phase and return it via the public repo getter."""
    await pool.execute(
        """
        INSERT INTO leagues
            (discord_guild_id, name, start_season_year, current_season,
             commissioner_user_id, current_phase)
        VALUES (777001, 'Phase Test League', 2025, 2025, 12345, $1)
        """,
        phase,
    )
    league = await league_repo.get_by_guild(pool, 777001)
    assert league is not None
    return league


# ---------------------------------------------------------------------------
# require_phase
# ---------------------------------------------------------------------------


async def test_require_phase_allows_correct_phase(db_pool):
    """require_phase does not raise when the league is in an allowed phase for the command."""
    league = await _create_league_in_phase(db_pool, "REGULAR_SEASON_ACTIVE")
    # sim_rivalry is allowed in REGULAR_SEASON_ACTIVE — must not raise
    await require_phase(league, "sim_rivalry")


async def test_require_phase_blocks_wrong_phase(db_pool):
    """require_phase raises PhaseError when the league phase does not permit the command."""
    league = await _create_league_in_phase(db_pool, "SETUP")
    with pytest.raises(PhaseError):
        await require_phase(league, "sim_rivalry")


# ---------------------------------------------------------------------------
# require_commissioner
# ---------------------------------------------------------------------------


async def test_require_commissioner_blocks_non_commissioner(db_pool, mock_interaction):
    """require_commissioner raises DBAError when invoking user is not the commissioner."""
    league = await _create_league_in_phase(db_pool, "SETUP")
    # mock_interaction.user.id == 12345 == league.commissioner_user_id — adjust to a different id
    mock_interaction.user.id = 99999
    with pytest.raises(DBAError):
        await require_commissioner(mock_interaction, league)


async def test_require_commissioner_allows_commissioner(db_pool, mock_interaction):
    """require_commissioner does not raise when invoking user is the commissioner."""
    league = await _create_league_in_phase(db_pool, "SETUP")
    # commissioner_user_id was inserted as 12345; mock_interaction.user.id == 12345
    mock_interaction.user.id = 12345
    await require_commissioner(mock_interaction, league)


# ---------------------------------------------------------------------------
# transitions.is_allowed
# ---------------------------------------------------------------------------


def test_is_allowed_returns_true_for_valid():
    """is_allowed returns True for a command/phase pair that is in ALLOWED."""
    assert is_allowed("sim_rivalry", "REGULAR_SEASON_ACTIVE") is True


def test_is_allowed_returns_false_for_invalid():
    """is_allowed returns False when the phase is not in the command's allowed set."""
    assert is_allowed("sim_rivalry", "SETUP") is False


def test_sim_games_allowed_same_phases_as_sim_rivalry():
    """sim_games is allowed in the same phases as sim_rivalry (REGULAR_SEASON_ACTIVE and REGULAR_SEASON_POSTDEADLINE)."""
    assert is_allowed("sim_games", "REGULAR_SEASON_ACTIVE") is True
    assert is_allowed("sim_games", "REGULAR_SEASON_POSTDEADLINE") is True


def test_sim_games_blocked_in_setup():
    """sim_games is NOT allowed in SETUP — regression for the missing key bug."""
    assert is_allowed("sim_games", "SETUP") is False


# ---------------------------------------------------------------------------
# transitions.allowed_phases_for
# ---------------------------------------------------------------------------


def test_allowed_phases_for_returns_list():
    """allowed_phases_for returns a non-empty list of phase strings for a known command."""
    result = allowed_phases_for("sim_rivalry")
    assert isinstance(result, list)
    assert len(result) > 0
    assert all(isinstance(p, str) for p in result)
