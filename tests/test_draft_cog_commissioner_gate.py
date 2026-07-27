"""
Cog-layer tests for bot.cogs.draft_cog's Round-2 finding #11 -- authorization
gap. `lottery`, `advance`, and `draft_class` were gated ONLY by
`@app_commands.default_permissions(administrator=True)`, a Discord-side
default suggestion, not real enforcement -- any guild member holding the
Administrator permission (not just the recorded league commissioner) could
run these. Fix adds a real `require_commissioner(interaction, league)` check,
matching every other commissioner-gated cog in this codebase.

Heavy downstream business logic (lottery draw, pick advancement) is patched
out via `services.draft_service` so each test isolates the auth check --
same technique test_trade_propose_unified.py uses to patch
`cpu_trade_evaluation._cpu_evaluate` to isolate one behavior while keeping
everything else (the real league lookup + require_commissioner check) real.
"""
from __future__ import annotations

import datetime as _dt
from unittest.mock import AsyncMock, patch

import pytest

from bot.cogs.draft_cog import DraftGroup
from core.errors import DBAError
from services import draft_service

pytestmark = pytest.mark.usefixtures("patch_get_pool")

_COMMISSIONER_ID = 555001
_NON_COMMISSIONER_ADMIN_ID = 555002


async def _seed_league(db_pool, *, guild_id: int) -> int:
    row = await db_pool.fetchrow(
        """
        INSERT INTO leagues (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES ($1, 'Draft Commissioner Gate Test League', 2025, 2025, $2)
        RETURNING id
        """,
        guild_id,
        _COMMISSIONER_ID,
    )
    return row["id"]


async def _seed_draft(db_pool, league_id: int, season: int) -> None:
    await db_pool.execute(
        "INSERT INTO drafts (league_id, season, status) VALUES ($1, $2, 'in_progress')",
        league_id,
        season,
    )


@pytest.fixture
def group() -> DraftGroup:
    return DraftGroup(bot=AsyncMock())


async def test_lottery_rejects_non_commissioner_admin(db_pool, mock_guild, mock_interaction, group):
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    mock_interaction.user.id = _NON_COMMISSIONER_ADMIN_ID
    await _seed_league(db_pool, guild_id=mock_guild.id)

    with pytest.raises(DBAError, match="commissioner"):
        await group.lottery.callback(group, mock_interaction)


async def test_lottery_allows_actual_commissioner(db_pool, mock_guild, mock_interaction, group):
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    mock_interaction.user.id = _COMMISSIONER_ID
    await _seed_league(db_pool, guild_id=mock_guild.id)

    with patch.object(draft_service, "run_lottery", AsyncMock(return_value=[])):
        await group.lottery.callback(group, mock_interaction)

    fired = [
        m for m in [
            mock_interaction.edit_original_response,
            mock_interaction.followup.send,
            mock_interaction.response.send_message,
        ] if m.await_count > 0
    ]
    assert fired, "commissioner's /draft lottery should have produced a response, not a permission error"


async def test_advance_rejects_non_commissioner_admin(db_pool, mock_guild, mock_interaction, group):
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    mock_interaction.user.id = _NON_COMMISSIONER_ADMIN_ID
    await _seed_league(db_pool, guild_id=mock_guild.id)

    with pytest.raises(DBAError, match="commissioner"):
        await group.advance.callback(group, mock_interaction)


async def test_advance_allows_actual_commissioner(db_pool, mock_guild, mock_interaction, group):
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    mock_interaction.user.id = _COMMISSIONER_ID
    league_id = await _seed_league(db_pool, guild_id=mock_guild.id)
    await _seed_draft(db_pool, league_id, 2025)

    with patch.object(draft_service, "ensure_draft_class", AsyncMock(return_value=0)), \
         patch.object(draft_service, "advance_pick", AsyncMock(return_value={"status": "complete"})):
        await group.advance.callback(group, mock_interaction)

    fired = [
        m for m in [
            mock_interaction.edit_original_response,
            mock_interaction.followup.send,
            mock_interaction.response.send_message,
        ] if m.await_count > 0
    ]
    assert fired, "commissioner's /draft advance should have produced a response, not a permission error"


async def test_draft_class_rejects_non_commissioner_admin(db_pool, mock_guild, mock_interaction, group):
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    mock_interaction.user.id = _NON_COMMISSIONER_ADMIN_ID
    await _seed_league(db_pool, guild_id=mock_guild.id)

    with pytest.raises(DBAError, match="commissioner"):
        await group.draft_class.callback(group, mock_interaction, year=2029)


async def test_draft_class_allows_actual_commissioner(db_pool, mock_guild, mock_interaction, group):
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    mock_interaction.user.id = _COMMISSIONER_ID
    await _seed_league(db_pool, guild_id=mock_guild.id)

    with patch.object(draft_service, "ensure_draft_class", AsyncMock(return_value=0)):
        await group.draft_class.callback(group, mock_interaction, year=2029)

    fired = [
        m for m in [
            mock_interaction.edit_original_response,
            mock_interaction.followup.send,
            mock_interaction.response.send_message,
        ] if m.await_count > 0
    ]
    assert fired, "commissioner's /draft class should have produced a response, not a permission error"
