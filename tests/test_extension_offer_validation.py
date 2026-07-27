"""
Cog-layer test for bot.cogs.extension_cog's Round-2 finding #5: `/extension
offer` validated an upper bound on salary (35% of cap) but no floor. A
negative or zero salary passed every check, and because
`projected_cap = cap_used - own_salary + salary` is additive, a negative
value actually REDUCED projected cap usage -- masking real cap violations
instead of triggering them.

Drives the real command against a real seeded Postgres test DB, per this
project's established convention. `bot.cogs.extension_cog.get_pool` is in
conftest.py's autouse patch list.
"""
from __future__ import annotations

import datetime as _dt

import pytest

from bot.cogs.extension_cog import ExtensionGroup

pytestmark = pytest.mark.usefixtures("patch_get_pool")


async def _seed_league_team_player(db_pool, *, guild_id: int, manager_user_id: int) -> int:
    league_row = await db_pool.fetchrow(
        """
        INSERT INTO leagues (
            discord_guild_id, name, start_season_year, current_season,
            commissioner_user_id, current_phase, salary_cap
        )
        VALUES ($1, 'Extension Offer Test League', 2025, 2025, $2, 'REGULAR_SEASON_ACTIVE', 140000000)
        RETURNING id
        """,
        guild_id,
        manager_user_id,
    )
    league_id: int = league_row["id"]

    team_row = await db_pool.fetchrow(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division, manager_user_id)
        VALUES ($1, 'EXT', 'Extenders', 'Testville', 'East', 'Atlantic', $2)
        RETURNING id
        """,
        league_id,
        manager_user_id,
    )
    team_id: int = team_row["id"]

    player_row = await db_pool.fetchrow(
        """
        INSERT INTO players (
            league_id, first_name, last_name, position, team_id, roster_status,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq, potential,
            peak_age_start, peak_age_end, loyalty, money_drive, win_drive
        ) VALUES (
            $1, 'Extend', 'Me', 'PG', $2, 'active',
            85, 80, 80, 75, 75,
            80, 85, 75, 65, 80, 90,
            25, 30, 50, 50, 70
        )
        RETURNING id
        """,
        league_id,
        team_id,
    )
    player_id: int = player_row["id"]

    await db_pool.execute(
        """
        INSERT INTO contracts
            (league_id, player_id, team_id, salary, years_remaining, total_years,
             contract_type, signed_in_season, is_active)
        VALUES ($1, $2, $3, 10000000, 2, 4, 'standard', 2024, TRUE)
        """,
        league_id,
        player_id,
        team_id,
    )
    return player_id


def _fired_call(mock_interaction):
    responded_mocks = [
        mock_interaction.edit_original_response,
        mock_interaction.followup.send,
        mock_interaction.response.send_message,
    ]
    fired = [m for m in responded_mocks if m.await_count > 0]
    assert fired, "/extension offer never sent any response"
    return fired[-1].call_args


async def test_offer_rejects_negative_salary(db_pool, mock_interaction):
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    player_id = await _seed_league_team_player(
        db_pool, guild_id=mock_interaction.guild.id, manager_user_id=mock_interaction.user.id
    )

    group = ExtensionGroup()
    await group.offer.callback(
        group, mock_interaction, player="Extend Me", years=3, salary=-5_000_000
    )

    content = _fired_call(mock_interaction).kwargs.get("content") or ""
    assert "$1,000,000" in content

    row = await db_pool.fetchrow("SELECT id FROM contract_extensions WHERE player_id = $1", player_id)
    assert row is None, "no extension row should be written for a rejected negative salary"


async def test_offer_rejects_zero_salary(db_pool, mock_interaction):
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    await _seed_league_team_player(
        db_pool, guild_id=mock_interaction.guild.id, manager_user_id=mock_interaction.user.id
    )

    group = ExtensionGroup()
    await group.offer.callback(
        group, mock_interaction, player="Extend Me", years=3, salary=0
    )

    content = _fired_call(mock_interaction).kwargs.get("content") or ""
    assert "$1,000,000" in content
