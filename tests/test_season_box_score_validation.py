"""
Cog-layer test for bot.cogs.season_cog's Round-2 finding #8: `/season
box-score` only rejected "neither `game` nor `game_id` given" -- it silently
preferred `game_id` when BOTH were given, with no warning to the caller.

The guard for this case fires before any league/DB lookup, so no seeding
is required.
"""
from __future__ import annotations

import datetime as _dt

from bot.cogs.season_cog import SeasonGroup


def _fired_call(mock_interaction):
    responded_mocks = [
        mock_interaction.edit_original_response,
        mock_interaction.followup.send,
        mock_interaction.response.send_message,
    ]
    fired = [m for m in responded_mocks if m.await_count > 0]
    assert fired, "/season box-score never sent any response"
    return fired[-1].call_args


async def test_box_score_rejects_both_game_and_game_id_given(mock_interaction):
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)

    group = SeasonGroup()
    await group.box_score.callback(group, mock_interaction, game=5, game_id=42)

    content = _fired_call(mock_interaction).kwargs.get("content") or ""
    assert "either" in content.lower()
    assert "not both" in content.lower()


async def test_box_score_still_rejects_neither_given(mock_interaction):
    """Regression guard: the pre-existing "neither given" rejection still works."""
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)

    group = SeasonGroup()
    await group.box_score.callback(group, mock_interaction, game=None, game_id=None)

    content = _fired_call(mock_interaction).kwargs.get("content") or ""
    assert "either" in content.lower()
