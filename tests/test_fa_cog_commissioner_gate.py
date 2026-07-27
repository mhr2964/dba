"""
Cog-layer tests for bot.cogs.fa_cog's Round-2 finding #11 -- authorization
gap. `fa_open`, `fa_advance`, and `fa_close` were gated ONLY by
`@app_commands.default_permissions(administrator=True)`, a Discord-side
default suggestion, not real enforcement -- any guild member holding the
Administrator permission (not just the recorded league commissioner) could
run these. Fix adds a real `require_commissioner(interaction, league)` check,
matching every other commissioner-gated cog in this codebase.

Heavy downstream business logic (FA day processing) is patched out via
`services.fa_service` so each test isolates the auth check -- same
technique test_trade_propose_unified.py uses to patch
`cpu_trade_evaluation._cpu_evaluate`.
"""
from __future__ import annotations

import datetime as _dt
from unittest.mock import AsyncMock, patch

import pytest

from bot.cogs.fa_cog import FAGroup
from core.errors import DBAError
from services import fa_service

pytestmark = pytest.mark.usefixtures("patch_get_pool")

_COMMISSIONER_ID = 556001
_NON_COMMISSIONER_ADMIN_ID = 556002


async def _seed_league(db_pool, *, guild_id: int) -> int:
    row = await db_pool.fetchrow(
        """
        INSERT INTO leagues (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES ($1, 'FA Commissioner Gate Test League', 2025, 2025, $2)
        RETURNING id
        """,
        guild_id,
        _COMMISSIONER_ID,
    )
    return row["id"]


def _fired(mock_interaction):
    return [
        m for m in [
            mock_interaction.edit_original_response,
            mock_interaction.followup.send,
            mock_interaction.response.send_message,
        ] if m.await_count > 0
    ]


@pytest.fixture
def group() -> FAGroup:
    return FAGroup(bot=AsyncMock())


async def test_fa_open_rejects_non_commissioner_admin(db_pool, mock_guild, mock_interaction, group):
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    mock_interaction.user.id = _NON_COMMISSIONER_ADMIN_ID
    await _seed_league(db_pool, guild_id=mock_guild.id)

    with pytest.raises(DBAError, match="commissioner"):
        await group.fa_open.callback(group, mock_interaction)


async def test_fa_open_allows_actual_commissioner(db_pool, mock_guild, mock_interaction, group):
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    mock_interaction.user.id = _COMMISSIONER_ID
    await _seed_league(db_pool, guild_id=mock_guild.id)

    with patch.object(
        fa_service, "open_fa", AsyncMock(return_value={"current_day": 1, "total_days": 8})
    ):
        await group.fa_open.callback(group, mock_interaction)

    assert _fired(mock_interaction), "commissioner's /fa open should have produced a response, not a permission error"


async def test_fa_advance_rejects_non_commissioner_admin(db_pool, mock_guild, mock_interaction, group):
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    mock_interaction.user.id = _NON_COMMISSIONER_ADMIN_ID
    await _seed_league(db_pool, guild_id=mock_guild.id)

    with pytest.raises(DBAError, match="commissioner"):
        await group.fa_advance.callback(group, mock_interaction)


async def test_fa_advance_allows_actual_commissioner(db_pool, mock_guild, mock_interaction, group):
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    mock_interaction.user.id = _COMMISSIONER_ID
    await _seed_league(db_pool, guild_id=mock_guild.id)

    with patch.object(fa_service, "advance_to_responses", AsyncMock(return_value=[])), \
         patch.object(
             fa_service, "advance_day",
             AsyncMock(return_value={"phase": "complete", "waived_count": 0, "retired_count": 0}),
         ):
        await group.fa_advance.callback(group, mock_interaction)

    assert _fired(mock_interaction), "commissioner's /fa advance should have produced a response, not a permission error"


async def test_fa_close_rejects_non_commissioner_admin(db_pool, mock_guild, mock_interaction, group):
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    mock_interaction.user.id = _NON_COMMISSIONER_ADMIN_ID
    await _seed_league(db_pool, guild_id=mock_guild.id)

    with pytest.raises(DBAError, match="commissioner"):
        await group.fa_close.callback(group, mock_interaction)


async def test_fa_close_allows_actual_commissioner(db_pool, mock_guild, mock_interaction, group):
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    mock_interaction.user.id = _COMMISSIONER_ID
    await _seed_league(db_pool, guild_id=mock_guild.id)

    with patch.object(
        fa_service, "close_fa",
        AsyncMock(return_value={"signed_count": 0, "waived_count": 0, "retired_count": 0}),
    ):
        await group.fa_close.callback(group, mock_interaction)

    assert _fired(mock_interaction), "commissioner's /fa close should have produced a response, not a permission error"
