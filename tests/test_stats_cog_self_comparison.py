"""
Cog-layer tests for bot.cogs.stats_cog's Round-2 finding #9: `/stats compare`
and `/stats h2h` silently allowed comparing a thing to itself (same player
twice, or same team twice), rendering a degenerate self-comparison / 0-0
head-to-head instead of erroring.

Drives the real commands against a real seeded Postgres test DB, per this
project's established convention. `bot.cogs.stats_cog.get_pool` is in
conftest.py's autouse patch list.
"""
from __future__ import annotations

import datetime as _dt

import pytest

from bot.cogs.stats_cog import StatsGroup

pytestmark = pytest.mark.usefixtures("patch_get_pool")


async def _seed_league_team_player(db_pool, *, guild_id: int, commissioner_id: int) -> tuple[int, int]:
    league_row = await db_pool.fetchrow(
        """
        INSERT INTO leagues (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES ($1, 'Stats Self-Compare Test League', 2025, 2025, $2)
        RETURNING id
        """,
        guild_id,
        commissioner_id,
    )
    league_id: int = league_row["id"]

    team_row = await db_pool.fetchrow(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'SLF', 'Selfers', 'Testville', 'East', 'Atlantic')
        RETURNING id
        """,
        league_id,
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
            $1, 'Same', 'Guy', 'PG', $2, 'active',
            80, 80, 80, 75, 75,
            80, 85, 75, 65, 80, 90,
            25, 30, 50, 50, 70
        )
        RETURNING id
        """,
        league_id,
        team_id,
    )
    player_id: int = player_row["id"]

    return league_id, player_id


def _fired_call(mock_interaction):
    responded_mocks = [
        mock_interaction.edit_original_response,
        mock_interaction.followup.send,
        mock_interaction.response.send_message,
    ]
    fired = [m for m in responded_mocks if m.await_count > 0]
    assert fired, "command never sent any response"
    return fired[-1].call_args


async def test_compare_rejects_same_player_twice(db_pool, mock_interaction):
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    await _seed_league_team_player(
        db_pool, guild_id=mock_interaction.guild.id, commissioner_id=mock_interaction.user.id
    )

    group = StatsGroup()
    await group.compare.callback(
        group, mock_interaction, player1="Same Guy", player2="Same Guy"
    )

    content = _fired_call(mock_interaction).kwargs.get("content") or ""
    assert "different" in content.lower()


async def test_h2h_rejects_same_team_twice(db_pool, mock_interaction):
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    await _seed_league_team_player(
        db_pool, guild_id=mock_interaction.guild.id, commissioner_id=mock_interaction.user.id
    )

    group = StatsGroup()
    await group.h2h.callback(
        group, mock_interaction, team1_code="SLF", team2_code="slf"
    )

    content = _fired_call(mock_interaction).kwargs.get("content") or ""
    assert "different" in content.lower()
