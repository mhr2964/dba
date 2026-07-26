"""
Cog-layer test for bot.cogs.awards_cog's /awards vote rank-required fix.

Before this fix, omitting `rank` on an all_nba vote silently recorded a
First-Team vote (default rank=1) instead of asking the voter which team
they meant -- the `rank not in (1,2,3)` guard was satisfied by the
unrequested default, so nothing ever errored. This drives the real
command against a real seeded league/team/player (project convention:
DB-touching behavior gets a real Postgres test DB, not mocks).
"""
from __future__ import annotations

import datetime as _dt

import pytest
from discord import app_commands

from bot.cogs.awards_cog import AwardsGroup
from core.errors import DBAError


async def _seed_league_team_player(db_pool, *, guild_id: int, manager_user_id: int) -> None:
    league_row = await db_pool.fetchrow(
        """
        INSERT INTO leagues (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES ($1, 'Awards Vote Cog Test League', 2025, 2025, $2)
        RETURNING id
        """,
        guild_id,
        manager_user_id,
    )
    league_id: int = league_row["id"]

    team_row = await db_pool.fetchrow(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division, manager_user_id)
        VALUES ($1, 'AVT', 'Voters', 'Testville', 'East', 'Atlantic', $2)
        RETURNING id
        """,
        league_id,
        manager_user_id,
    )
    team_id: int = team_row["id"]

    await db_pool.execute(
        """
        INSERT INTO players (
            league_id, first_name, last_name, position, team_id,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq, potential,
            peak_age_start, peak_age_end, loyalty, money_drive, win_drive
        ) VALUES (
            $1, 'Rank', 'Voteme', 'PG', $2,
            85, 80, 80, 75, 75,
            80, 85, 75, 65, 80, 90,
            25, 30, 50, 50, 70
        )
        """,
        league_id,
        team_id,
    )


async def test_vote_all_nba_without_rank_raises_clear_error(db_pool, mock_guild, mock_interaction):
    """/awards vote award_type=all_nba with no rank must reject with a clear
    message telling the voter to specify rank, instead of silently defaulting
    to First Team."""
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    await _seed_league_team_player(
        db_pool, guild_id=mock_guild.id, manager_user_id=mock_interaction.user.id
    )

    group = AwardsGroup()
    all_nba_choice = app_commands.Choice(name="All-NBA", value="all_nba")

    with pytest.raises(DBAError, match="rank"):
        await group.vote.callback(
            group, mock_interaction, award_type=all_nba_choice, player="Voteme", rank=None
        )


async def test_vote_all_nba_with_invalid_rank_still_rejected(db_pool, mock_guild, mock_interaction):
    """Existing behavior preserved: an explicit but out-of-range rank is still rejected."""
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    await _seed_league_team_player(
        db_pool, guild_id=mock_guild.id, manager_user_id=mock_interaction.user.id
    )

    group = AwardsGroup()
    all_nba_choice = app_commands.Choice(name="All-NBA", value="all_nba")

    with pytest.raises(DBAError, match="1 \\(First\\)"):
        await group.vote.callback(
            group, mock_interaction, award_type=all_nba_choice, player="Voteme", rank=5
        )


async def test_vote_non_all_nba_rank_still_optional(db_pool, mock_guild, mock_interaction):
    """Non-all_nba award types are unaffected: omitting rank does not trigger the
    all_nba-only rank-required error (it should fail later, for the unrelated
    reason that no voting session is open)."""
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    await _seed_league_team_player(
        db_pool, guild_id=mock_guild.id, manager_user_id=mock_interaction.user.id
    )

    group = AwardsGroup()
    mvp_choice = app_commands.Choice(name="MVP", value="mvp")

    with pytest.raises(DBAError, match="No open"):
        await group.vote.callback(
            group, mock_interaction, award_type=mvp_choice, player="Voteme", rank=None
        )
