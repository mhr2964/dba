"""
Cog-layer test for bot.cogs.team_cog's /team assign `user_id` crash fix.

Before this fix, `int(user_id)` had no try/except -- a non-numeric value
raised an unhandled ValueError after safe_defer already ran, producing a
raw Discord interaction failure instead of a clean message. This drives
the real command handler directly (no DB access happens on this path --
the ValueError is raised, and the fix must short-circuit, before any
league/service lookup).
"""
from __future__ import annotations

import datetime as _dt

from bot.cogs.team_cog import TeamGroup


async def test_assign_with_non_numeric_user_id_replies_cleanly(mock_interaction):
    """A non-numeric user_id must produce a clean ephemeral error message,
    not an unhandled ValueError, and must not attempt any league lookup."""
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)

    group = TeamGroup()
    # Must not raise -- this call would previously blow up on int("not-a-number").
    await group.assign.callback(
        group, mock_interaction, team_code="LAL", user=None, user_id="not-a-number"
    )

    responded_mocks = [
        mock_interaction.edit_original_response,
        mock_interaction.followup.send,
        mock_interaction.response.send_message,
    ]
    fired = [m for m in responded_mocks if m.await_count > 0]
    assert fired, "/team assign never sent any response for a bad user_id"

    call_kwargs = fired[0].call_args.kwargs
    content = call_kwargs.get("content") or ""
    assert "valid Discord user ID" in content
