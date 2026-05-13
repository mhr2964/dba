"""
Cog-layer tests for bot.cogs.setup_cog (LeagueGroup and TeamGroup).

These tests call the command handler functions directly, bypassing the Discord
app-command dispatch machinery.  Service calls are patched so this layer only
verifies routing, argument passing, and response behaviour.

Service-level correctness is covered in test_league_service.py.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot.cogs.setup_cog import LeagueGroup, TeamGroup
from core.errors import DBAError
from data.repositories.league_repo import League
from data.repositories.team_repo import Team


# ---------------------------------------------------------------------------
# Fixtures — shared stubs for service return values
# ---------------------------------------------------------------------------


def _make_fake_league() -> League:
    import datetime

    return League(
        id=1,
        discord_guild_id=999001,
        name="Test League",
        start_season_year=2025,
        current_season=2025,
        current_phase="SETUP",
        phase_data={},
        commissioner_user_id=12345,
        fa_day_count=8,
        salary_cap=140_000_000,
        created_at=datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc),
        archived_at=None,
    )


def _make_fake_team(code: str = "LAL", manager_id: int | None = None) -> Team:
    return Team(
        id=10,
        league_id=1,
        nba_team_code=code,
        name="Lakers",
        city="Los Angeles",
        conference="West",
        division="Pacific",
        manager_user_id=manager_id,
        cpu_mode="developing",
        team_offense_rating=None,
        team_defense_rating=None,
        pace=None,
    )


# ---------------------------------------------------------------------------
# LeagueGroup.create
# ---------------------------------------------------------------------------


async def test_league_create_calls_service_with_correct_args(mock_interaction):
    """
    /league create defers, calls league_service.create with guild/user/name/season,
    then sends a followup embed.
    """
    fake_league = _make_fake_league()

    with patch("bot.cogs.setup_cog.league_service.create", new=AsyncMock(return_value=fake_league)) as mock_create, \
         patch("bot.cogs.setup_cog.league_repo.get_channel", new=AsyncMock(return_value=None)):

        group = LeagueGroup()
        await group.create.callback(group, mock_interaction, name="Test League", season=2025)

    mock_create.assert_awaited_once_with(
        guild=mock_interaction.guild,
        commissioner=mock_interaction.user,
        name="Test League",
        season_year=2025,
    )
    mock_interaction.response.defer.assert_awaited_once()
    mock_interaction.followup.send.assert_awaited_once()


async def test_league_create_dba_error_replies_ephemeral(mock_interaction):
    """
    When league_service.create raises DBAError, the command propagates the
    exception (the app-command error handler converts it to an ephemeral reply).
    We verify the exception escapes the handler — the cog does not swallow it.
    """
    with patch(
        "bot.cogs.setup_cog.league_service.create",
        new=AsyncMock(side_effect=DBAError("already exists")),
    ):
        group = LeagueGroup()
        with pytest.raises(DBAError, match="already exists"):
            await group.create.callback(group, mock_interaction, name="Dupe League", season=2025)


async def test_league_create_posts_to_league_news_when_channel_exists(mock_interaction):
    """
    After a successful create, the command fetches the #league-news channel
    and sends an announcement if the channel is resolvable.
    """
    fake_league = _make_fake_league()
    fake_channel = AsyncMock()
    fake_channel.send = AsyncMock()

    mock_interaction.guild.get_channel = MagicMock(return_value=fake_channel)

    with patch("bot.cogs.setup_cog.league_service.create", new=AsyncMock(return_value=fake_league)), \
         patch("bot.cogs.setup_cog.league_repo.get_channel", new=AsyncMock(return_value=200)):

        group = LeagueGroup()
        await group.create.callback(group, mock_interaction, name="Test League", season=2025)

    fake_channel.send.assert_awaited_once()
    call_args = fake_channel.send.call_args[0][0]
    assert "Test League" in call_args


async def test_league_create_skips_news_when_channel_missing(mock_interaction):
    """
    If get_channel returns None (channel not yet cached), no announcement is
    attempted — no AttributeError is raised.
    """
    fake_league = _make_fake_league()
    mock_interaction.guild.get_channel = MagicMock(return_value=None)

    with patch("bot.cogs.setup_cog.league_service.create", new=AsyncMock(return_value=fake_league)), \
         patch("bot.cogs.setup_cog.league_repo.get_channel", new=AsyncMock(return_value=200)):

        group = LeagueGroup()
        # Must not raise.
        await group.create.callback(group, mock_interaction, name="Test League", season=2025)

    # followup.send was still called for the embed — just no channel.send.
    mock_interaction.followup.send.assert_awaited_once()


# ---------------------------------------------------------------------------
# TeamGroup.assign
# ---------------------------------------------------------------------------


async def test_assign_command_calls_service_and_sends_embed(mock_interaction, mock_manager):
    """
    /team assign defers, calls league_service.assign_manager with correct args,
    and sends a followup embed.
    """
    fake_league = _make_fake_league()
    fake_team = _make_fake_team(manager_id=mock_manager.id)

    with patch("bot.cogs.setup_cog.league_service.get_league", new=AsyncMock(return_value=fake_league)), \
         patch("bot.cogs.setup_cog.league_service.assign_manager", new=AsyncMock(return_value=fake_team)) as mock_assign, \
         patch("bot.cogs.setup_cog.league_repo.get_channel", new=AsyncMock(return_value=None)):

        group = TeamGroup()
        await group.assign.callback(group, mock_interaction, user=mock_manager, team_code="LAL")

    mock_assign.assert_awaited_once_with(
        guild=mock_interaction.guild,
        league=fake_league,
        team_code="LAL",
        user=mock_manager,
    )
    mock_interaction.followup.send.assert_awaited_once()


async def test_assign_command_posts_to_league_news(mock_interaction, mock_manager):
    """
    On successful assign, the command sends an announcement to #league-news
    when the channel can be resolved.
    """
    fake_league = _make_fake_league()
    fake_team = _make_fake_team(code="LAL", manager_id=mock_manager.id)
    fake_channel = AsyncMock()
    fake_channel.send = AsyncMock()
    mock_interaction.guild.get_channel = MagicMock(return_value=fake_channel)

    with patch("bot.cogs.setup_cog.league_service.get_league", new=AsyncMock(return_value=fake_league)), \
         patch("bot.cogs.setup_cog.league_service.assign_manager", new=AsyncMock(return_value=fake_team)), \
         patch("bot.cogs.setup_cog.league_repo.get_channel", new=AsyncMock(return_value=201)):

        group = TeamGroup()
        await group.assign.callback(group, mock_interaction, user=mock_manager, team_code="LAL")

    fake_channel.send.assert_awaited_once()
    announcement = fake_channel.send.call_args[0][0]
    assert "Lakers" in announcement or "LAL" in announcement


async def test_assign_command_no_league_replies_ephemeral(mock_interaction, mock_manager):
    """
    /team assign sends an ephemeral error when there is no active league.
    """
    with patch("bot.cogs.setup_cog.league_service.get_league", new=AsyncMock(return_value=None)):
        group = TeamGroup()
        await group.assign.callback(group, mock_interaction, user=mock_manager, team_code="LAL")

    # followup.send called with ephemeral=True.
    mock_interaction.followup.send.assert_awaited_once()
    _, kwargs = mock_interaction.followup.send.call_args
    assert kwargs.get("ephemeral") is True


async def test_assign_command_propagates_dba_error(mock_interaction, mock_manager):
    """
    DBAError from assign_manager escapes the handler for the global error handler
    to convert to an ephemeral reply.
    """
    fake_league = _make_fake_league()

    with patch("bot.cogs.setup_cog.league_service.get_league", new=AsyncMock(return_value=fake_league)), \
         patch("bot.cogs.setup_cog.league_service.assign_manager", new=AsyncMock(side_effect=DBAError("already manages"))):

        group = TeamGroup()
        with pytest.raises(DBAError, match="already manages"):
            await group.assign.callback(group, mock_interaction, user=mock_manager, team_code="LAL")
