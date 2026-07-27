"""
Cog-layer test for bot.cogs.awards_cog's Round-2 finding #6 -- MANDATORY,
a real feature fix (not just validation-tightening).

Before this fix, `/awards vote award_type:All-Star` always hit
`elif value == "all_star": raise DBAError(...use award_type: all_star_east
or all_star_west...)` -- but neither of those two values exists anywhere in
`_ALL_AWARD_CHOICES`, so Discord's choice-constrained dropdown could never
let a human select them. No All-Star vote could ever be cast. The fix
derives the conference from the voted-for player's own team (a player
belongs to exactly one conference, so there's no real choice for the voter
to make) and removes the dead-end raise entirely.

This test seeds two players on teams in different conferences, casts a
real vote for each via the actual `/awards vote` command (award_type:
All-Star for both), and asserts each landed in the correct
all_star_east / all_star_west voting bucket in the DB.
"""
from __future__ import annotations

import datetime as _dt
from unittest.mock import AsyncMock, MagicMock

import pytest
from discord import app_commands

from bot.cogs.awards_cog import AwardsGroup
from services import awards_service

pytestmark = pytest.mark.usefixtures("patch_get_pool")


def _make_interaction(mock_guild, user_id: int):
    interaction = MagicMock()
    interaction.guild = mock_guild
    interaction.guild_id = mock_guild.id
    user = MagicMock()
    user.id = user_id
    interaction.user = user
    interaction.response = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    return interaction


async def _seed_league_two_conf_teams_players(db_pool, *, guild_id: int, commissioner_id: int):
    league_row = await db_pool.fetchrow(
        """
        INSERT INTO leagues (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES ($1, 'All-Star Vote Test League', 2025, 2025, $2)
        RETURNING id
        """,
        guild_id,
        commissioner_id,
    )
    league_id: int = league_row["id"]

    east_manager_id = 700001
    west_manager_id = 700002

    east_team_row = await db_pool.fetchrow(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division, manager_user_id)
        VALUES ($1, 'ASE', 'Easters', 'Eastville', 'East', 'Atlantic', $2)
        RETURNING id
        """,
        league_id,
        east_manager_id,
    )
    east_team_id: int = east_team_row["id"]

    west_team_row = await db_pool.fetchrow(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division, manager_user_id)
        VALUES ($1, 'ASW', 'Westers', 'Westville', 'West', 'Pacific', $2)
        RETURNING id
        """,
        league_id,
        west_manager_id,
    )
    west_team_id: int = west_team_row["id"]

    async def _insert_player(team_id, first, last):
        row = await db_pool.fetchrow(
            """
            INSERT INTO players (
                league_id, first_name, last_name, position, team_id,
                overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
                finishing, playmaking, defense, rebounding, iq, potential,
                peak_age_start, peak_age_end, loyalty, money_drive, win_drive
            ) VALUES (
                $1, $2, $3, 'PG', $4,
                90, 85, 85, 80, 80,
                85, 90, 80, 70, 85, 95,
                26, 31, 50, 50, 80
            )
            RETURNING id
            """,
            league_id,
            first,
            last,
            team_id,
        )
        return row["id"]

    east_player_id = await _insert_player(east_team_id, "Easty", "Player")
    west_player_id = await _insert_player(west_team_id, "Westy", "Player")

    return league_id, east_manager_id, west_manager_id, east_player_id, west_player_id


async def test_all_star_vote_lands_in_correct_conference_bucket(db_pool, mock_guild, mock_interaction):
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    (
        league_id,
        east_manager_id,
        west_manager_id,
        east_player_id,
        west_player_id,
    ) = await _seed_league_two_conf_teams_players(
        db_pool, guild_id=mock_guild.id, commissioner_id=mock_interaction.user.id
    )

    await awards_service.open_all_star_voting(league_id, 2025)

    group = AwardsGroup()
    all_star_choice = app_commands.Choice(name="All-Star", value="all_star")

    east_interaction = _make_interaction(mock_guild, east_manager_id)
    await group.vote.callback(
        group, east_interaction, award_type=all_star_choice, player="Easty Player", rank=None
    )
    west_interaction = _make_interaction(mock_guild, west_manager_id)
    await group.vote.callback(
        group, west_interaction, award_type=all_star_choice, player="Westy Player", rank=None
    )

    east_vote = await db_pool.fetchrow(
        """
        SELECT av.player_id FROM award_votes av
        JOIN award_votings avn ON avn.id = av.voting_id
        WHERE avn.league_id = $1 AND avn.award_type = 'all_star_east'
        """,
        league_id,
    )
    west_vote = await db_pool.fetchrow(
        """
        SELECT av.player_id FROM award_votes av
        JOIN award_votings avn ON avn.id = av.voting_id
        WHERE avn.league_id = $1 AND avn.award_type = 'all_star_west'
        """,
        league_id,
    )

    assert east_vote is not None, "no vote recorded in all_star_east bucket"
    assert east_vote["player_id"] == east_player_id
    assert west_vote is not None, "no vote recorded in all_star_west bucket"
    assert west_vote["player_id"] == west_player_id
