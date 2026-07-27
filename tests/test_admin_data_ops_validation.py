"""
Cog-layer tests for bot.cogs.admin_data_ops's Round-2 validation gaps.

Finding #3 -- force-fa-sign had zero bounds validation on salary/years,
letting an admin write a contract row (years=0/negative salary) that
downstream cap/contract-lifecycle logic doesn't expect. Fix mirrors
/fa offer's exact thresholds (salary >= $1,000,000; 1 <= years <= 5).

Finding #4 -- edit-player's `value` had no range/allowlist validation:
numeric rating fields accepted any int, and `position` accepted any
string verbatim. Fix clamps ratings to [0, 99] and position to the
5 real position codes, rejecting (not silently coercing) bad input.

Drives the real command functions directly against a real seeded
Postgres test DB, per this project's established convention.
`bot.cogs.admin_data_ops.get_pool` is in conftest.py's autouse patch list.
"""
from __future__ import annotations

import datetime as _dt

import pytest

from bot.cogs import admin_data_ops
from data.repositories import league_repo, team_repo

pytestmark = pytest.mark.usefixtures("patch_get_pool")


async def _seed_league_team_player(
    db_pool, *, guild_id: int, commissioner_id: int
) -> tuple[league_repo.League, team_repo.Team, int]:
    league_row = await db_pool.fetchrow(
        """
        INSERT INTO leagues (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES ($1, 'Admin Data Ops Test League', 2025, 2025, $2)
        RETURNING *
        """,
        guild_id,
        commissioner_id,
    )
    league = league_repo._league_from_record(league_row)

    team_row = await db_pool.fetchrow(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'ADO', 'Editors', 'Testville', 'East', 'Atlantic')
        RETURNING *
        """,
        league.id,
    )
    team = team_repo._team_from_record(team_row)

    player_row = await db_pool.fetchrow(
        """
        INSERT INTO players (
            league_id, first_name, last_name, position, team_id, roster_status,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq, potential,
            peak_age_start, peak_age_end, loyalty, money_drive, win_drive
        ) VALUES (
            $1, 'Edit', 'Target', 'PG', $2, 'active',
            75, 75, 75, 70, 70,
            75, 75, 70, 65, 75, 85,
            25, 30, 50, 50, 60
        )
        RETURNING id
        """,
        league.id,
        team.id,
    )
    player_id: int = player_row["id"]

    return league, team, player_id


async def _seed_free_agent(db_pool, league_id: int) -> int:
    row = await db_pool.fetchrow(
        """
        INSERT INTO players (
            league_id, first_name, last_name, position, roster_status,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq, potential,
            peak_age_start, peak_age_end, loyalty, money_drive, win_drive
        ) VALUES (
            $1, 'Free', 'Agent', 'SF', 'free_agent',
            70, 70, 70, 65, 65,
            70, 70, 65, 60, 70, 80,
            24, 30, 50, 50, 60
        )
        RETURNING id
        """,
        league_id,
    )
    return row["id"]


def _fired_call(mock_interaction):
    responded_mocks = [
        mock_interaction.edit_original_response,
        mock_interaction.followup.send,
        mock_interaction.response.send_message,
    ]
    fired = [m for m in responded_mocks if m.await_count > 0]
    assert fired, "command never sent any response"
    return fired[-1].call_args


# ---------------------------------------------------------------------------
# Finding #3 -- force-fa-sign bounds
# ---------------------------------------------------------------------------


async def test_force_fa_sign_rejects_zero_years(db_pool, mock_interaction):
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    league, team, _ = await _seed_league_team_player(
        db_pool, guild_id=mock_interaction.guild.id, commissioner_id=mock_interaction.user.id
    )
    fa_player_id = await _seed_free_agent(db_pool, league.id)

    await admin_data_ops.force_fa_sign.callback(
        mock_interaction, player="Free Agent", team_code="ADO", salary=5_000_000, years=0
    )

    content = _fired_call(mock_interaction).kwargs.get("content") or ""
    assert "1 and 5 years" in content

    row = await db_pool.fetchrow("SELECT * FROM contracts WHERE player_id = $1", fa_player_id)
    assert row is None, "no contract row should be written for a rejected years=0 sign"


async def test_force_fa_sign_rejects_negative_salary(db_pool, mock_interaction):
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    league, team, _ = await _seed_league_team_player(
        db_pool, guild_id=mock_interaction.guild.id, commissioner_id=mock_interaction.user.id
    )
    fa_player_id = await _seed_free_agent(db_pool, league.id)

    await admin_data_ops.force_fa_sign.callback(
        mock_interaction, player="Free Agent", team_code="ADO", salary=-1_000_000, years=2
    )

    content = _fired_call(mock_interaction).kwargs.get("content") or ""
    assert "$1,000,000" in content

    row = await db_pool.fetchrow("SELECT * FROM contracts WHERE player_id = $1", fa_player_id)
    assert row is None, "no contract row should be written for a rejected negative salary"


# ---------------------------------------------------------------------------
# Finding #4 -- edit-player value validation
# ---------------------------------------------------------------------------


async def test_edit_player_rejects_out_of_range_high(db_pool, mock_interaction):
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    league, _, player_id = await _seed_league_team_player(
        db_pool, guild_id=mock_interaction.guild.id, commissioner_id=mock_interaction.user.id
    )

    await admin_data_ops.edit_player.callback(
        mock_interaction, player="Edit Target", field="ovr", value="150"
    )

    content = _fired_call(mock_interaction).kwargs.get("content") or ""
    assert "between 0 and 99" in content

    row = await db_pool.fetchrow("SELECT overall FROM players WHERE id = $1", player_id)
    assert row["overall"] == 75, "ovr must be unchanged after a rejected out-of-range edit"


async def test_edit_player_rejects_out_of_range_low(db_pool, mock_interaction):
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    league, _, player_id = await _seed_league_team_player(
        db_pool, guild_id=mock_interaction.guild.id, commissioner_id=mock_interaction.user.id
    )

    await admin_data_ops.edit_player.callback(
        mock_interaction, player="Edit Target", field="ovr", value="-10"
    )

    content = _fired_call(mock_interaction).kwargs.get("content") or ""
    assert "between 0 and 99" in content

    row = await db_pool.fetchrow("SELECT overall FROM players WHERE id = $1", player_id)
    assert row["overall"] == 75


async def test_edit_player_rejects_invalid_position(db_pool, mock_interaction):
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    league, _, player_id = await _seed_league_team_player(
        db_pool, guild_id=mock_interaction.guild.id, commissioner_id=mock_interaction.user.id
    )

    await admin_data_ops.edit_player.callback(
        mock_interaction, player="Edit Target", field="position", value="XX"
    )

    content = _fired_call(mock_interaction).kwargs.get("content") or ""
    assert "Invalid position" in content

    row = await db_pool.fetchrow("SELECT position FROM players WHERE id = $1", player_id)
    assert row["position"] == "PG", "position must be unchanged after a rejected invalid value"


async def test_edit_player_valid_edit_still_succeeds(db_pool, mock_interaction):
    """Regression guard: a normal in-range edit still works after tightening validation."""
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    league, _, player_id = await _seed_league_team_player(
        db_pool, guild_id=mock_interaction.guild.id, commissioner_id=mock_interaction.user.id
    )

    await admin_data_ops.edit_player.callback(
        mock_interaction, player="Edit Target", field="ovr", value="88"
    )

    content = _fired_call(mock_interaction).kwargs.get("content") or ""
    assert "88" in content

    row = await db_pool.fetchrow("SELECT overall FROM players WHERE id = $1", player_id)
    assert row["overall"] == 88


async def test_edit_player_valid_lowercase_position_normalized(db_pool, mock_interaction):
    """Regression guard: a valid lowercase position is normalized to uppercase, not rejected."""
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    league, _, player_id = await _seed_league_team_player(
        db_pool, guild_id=mock_interaction.guild.id, commissioner_id=mock_interaction.user.id
    )

    await admin_data_ops.edit_player.callback(
        mock_interaction, player="Edit Target", field="position", value="sg"
    )

    row = await db_pool.fetchrow("SELECT position FROM players WHERE id = $1", player_id)
    assert row["position"] == "SG"
