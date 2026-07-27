"""
Cog-layer test for bot.cogs.team_cog's /team assign `user_id` crash fix, and
Round-2 fixes: both user+user_id given, and a cache-miss-but-valid user_id
must be resolved via a real API lookup (guild.fetch_member) instead of
silently self-assigning to the caller.

Before the round-2 fix, `interaction.guild.get_member(int(user_id))` is a
cache-only lookup -- a valid-but-uncached Discord ID returned None with no
exception, which fell through to `member = interaction.user`, silently
assigning the team to whoever ran the command instead of the intended
target. None of these three cases touch the DB (the guard fires, or the
Discord lookup fails, before any league/service lookup), so no seeding is
needed.
"""
from __future__ import annotations

import datetime as _dt
from unittest.mock import AsyncMock, MagicMock

import discord

from bot.cogs.team_cog import TeamGroup


def _fired_call(mock_interaction):
    responded_mocks = [
        mock_interaction.edit_original_response,
        mock_interaction.followup.send,
        mock_interaction.response.send_message,
    ]
    fired = [m for m in responded_mocks if m.await_count > 0]
    assert fired, "/team assign never sent any response"
    return fired[0].call_args


async def test_assign_with_non_numeric_user_id_replies_cleanly(mock_interaction):
    """A non-numeric user_id must produce a clean ephemeral error message,
    not an unhandled ValueError, and must not attempt any league lookup."""
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)

    group = TeamGroup()
    # Must not raise -- this call would previously blow up on int("not-a-number").
    await group.assign.callback(
        group, mock_interaction, team_code="LAL", user=None, user_id="not-a-number"
    )

    call_kwargs = _fired_call(mock_interaction).kwargs
    content = call_kwargs.get("content") or ""
    assert "valid Discord user ID" in content


async def test_assign_rejects_both_user_and_user_id_given(mock_interaction, mock_manager):
    """Providing both `user` and `user_id` must be rejected -- not silently
    resolved by letting `user` win."""
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)

    group = TeamGroup()
    await group.assign.callback(
        group, mock_interaction, team_code="LAL", user=mock_manager, user_id="67890"
    )

    content = _fired_call(mock_interaction).kwargs.get("content") or ""
    assert "not both" in content.lower()
    assert "not neither" in content.lower()


async def test_assign_cache_miss_valid_user_id_rejects_instead_of_self_assigning(mock_interaction):
    """A syntactically valid but not-yet-cached user_id must be resolved via a
    real fetch_member API call. When that lookup 404s, the command must
    reject cleanly -- NOT silently fall through to self-assigning the team
    to the commissioner running the command."""
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    fake_response = MagicMock(status=404, reason="Not Found")
    mock_interaction.guild.get_member = MagicMock(return_value=None)
    mock_interaction.guild.fetch_member = AsyncMock(
        side_effect=discord.NotFound(fake_response, "Unknown Member")
    )

    group = TeamGroup()
    await group.assign.callback(
        group, mock_interaction, team_code="LAL", user=None, user_id="999999999999999999"
    )

    content = _fired_call(mock_interaction).kwargs.get("content") or ""
    assert "999999999999999999" in content
    assert "no member" in content.lower()
