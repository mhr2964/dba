"""
Unit and integration tests for notifier_service and dm_repo.

DM tests mock the Discord API entirely — no real bot connection.
dm_outbox tests use db_pool directly.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from data.repositories import dm_repo
from services import notifier_service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bot(user: MagicMock | None = None) -> MagicMock:
    bot = MagicMock(spec=discord.Client)
    bot.get_user = MagicMock(return_value=user)
    bot.fetch_user = AsyncMock(return_value=user)
    bot.get_channel = MagicMock(return_value=None)
    return bot


def _make_user(*, forbidden: bool = False) -> MagicMock:
    user = MagicMock(spec=discord.User)
    user.id = 42
    if forbidden:
        user.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "Cannot send messages to this user"))
    else:
        user.send = AsyncMock()
    return user


def _make_embed() -> discord.Embed:
    return discord.Embed(title="Test", description="Test embed")


async def _setup_league(pool) -> int:
    row = await pool.fetchrow(
        """
        INSERT INTO leagues (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES (777001, 'Notifier Test League', 2025, 2025, 99999)
        RETURNING id
        """
    )
    return row["id"]


# ---------------------------------------------------------------------------
# send_dm — mock-only (no real Discord, no DB for DM-succeeds path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_dm_success():
    """DM delivered successfully → returns True, no channel post."""
    user = _make_user()
    bot = _make_bot(user=user)
    embed = _make_embed()

    pool_mock = AsyncMock()
    with patch("services.notifier_service.get_pool", AsyncMock(return_value=pool_mock)):
        result = await notifier_service.send_dm(bot, league_id=1, user_id=42, embed=embed, fallback_message="fallback")

    assert result is True
    user.send.assert_awaited_once_with(embed=embed)
    # get_pool should not have been called — DM succeeded before any DB access
    pool_mock.fetchval.assert_not_called()


@pytest.mark.asyncio
async def test_send_dm_forbidden_fallback():
    """DM blocked → falls back to channel, returns False."""
    user = _make_user(forbidden=True)
    bot = _make_bot(user=user)

    fallback_channel = AsyncMock(spec=discord.TextChannel)
    fallback_channel.send = AsyncMock()
    bot.get_channel = MagicMock(return_value=fallback_channel)

    pool_mock = AsyncMock()
    pool_mock.fetchval = AsyncMock(return_value=888)  # discord_channel_id for 'league-news'

    embed = _make_embed()
    with patch("services.notifier_service.get_pool", AsyncMock(return_value=pool_mock)):
        result = await notifier_service.send_dm(
            bot,
            league_id=1,
            user_id=42,
            embed=embed,
            fallback_message="Check the channel.",
            fallback_channel_role="league-news",
        )

    assert result is False
    fallback_channel.send.assert_awaited_once()
    sent_text: str = fallback_channel.send.call_args[0][0]
    assert "<@42>" in sent_text
    assert "Check the channel." in sent_text


@pytest.mark.asyncio
async def test_send_dm_no_channel_fallback():
    """DM fails and fallback channel not configured → logs warning, returns False without crashing."""
    user = _make_user(forbidden=True)
    bot = _make_bot(user=user)
    bot.get_channel = MagicMock(return_value=None)

    pool_mock = AsyncMock()
    pool_mock.fetchval = AsyncMock(return_value=None)  # no channel configured

    embed = _make_embed()
    with patch("services.notifier_service.get_pool", AsyncMock(return_value=pool_mock)):
        result = await notifier_service.send_dm(
            bot,
            league_id=1,
            user_id=42,
            embed=embed,
            fallback_message="fallback",
        )

    assert result is False


# ---------------------------------------------------------------------------
# dm_repo — integration tests (require db_pool)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_dm_inserts_row(db_pool):
    """queue_dm inserts a row and returns a positive integer ID."""
    league_id = await _setup_league(db_pool)

    embed_data = {"title": "Trade Proposed", "description": "Team A offered 2 players"}
    dm_id = await dm_repo.queue_dm(
        db_pool,
        league_id=league_id,
        user_id=55555,
        embed_data=embed_data,
        fallback_message="A trade was proposed.",
        fallback_channel_role="transactions",
    )

    assert isinstance(dm_id, int)
    assert dm_id > 0

    row = await db_pool.fetchrow("SELECT * FROM dm_outbox WHERE id = $1", dm_id)
    assert row is not None
    assert row["user_id"] == 55555
    assert row["delivered"] is False
    assert row["attempts"] == 0
    assert row["fallback_channel_role"] == "transactions"


@pytest.mark.asyncio
async def test_mark_delivered_updates_row(db_pool):
    """queue_dm then mark_delivered → delivered=True."""
    league_id = await _setup_league(db_pool)

    dm_id = await dm_repo.queue_dm(
        db_pool,
        league_id=league_id,
        user_id=66666,
        embed_data={"title": "Game tonight"},
        fallback_message="Upcoming game reminder.",
    )

    await dm_repo.mark_delivered(db_pool, dm_id)

    row = await db_pool.fetchrow("SELECT delivered FROM dm_outbox WHERE id = $1", dm_id)
    assert row["delivered"] is True


@pytest.mark.asyncio
async def test_mark_failed_increments_attempts(db_pool):
    """mark_failed increments attempts and records last_error."""
    league_id = await _setup_league(db_pool)

    dm_id = await dm_repo.queue_dm(
        db_pool,
        league_id=league_id,
        user_id=77777,
        embed_data={"title": "FA decision"},
        fallback_message="FA offer resolved.",
    )

    await dm_repo.mark_failed(db_pool, dm_id, "Forbidden: Cannot send messages to this user")

    row = await db_pool.fetchrow("SELECT attempts, last_error FROM dm_outbox WHERE id = $1", dm_id)
    assert row["attempts"] == 1
    assert "Forbidden" in row["last_error"]


@pytest.mark.asyncio
async def test_get_pending_excludes_delivered(db_pool):
    """get_pending only returns rows where delivered=FALSE and attempts < 3."""
    league_id = await _setup_league(db_pool)

    id_pending = await dm_repo.queue_dm(
        db_pool, league_id=league_id, user_id=1, embed_data={}, fallback_message="p"
    )
    id_delivered = await dm_repo.queue_dm(
        db_pool, league_id=league_id, user_id=2, embed_data={}, fallback_message="d"
    )
    await dm_repo.mark_delivered(db_pool, id_delivered)

    # Exhaust attempts on a third row
    id_exhausted = await dm_repo.queue_dm(
        db_pool, league_id=league_id, user_id=3, embed_data={}, fallback_message="e"
    )
    for _ in range(3):
        await dm_repo.mark_failed(db_pool, id_exhausted, "err")

    pending = await dm_repo.get_pending(db_pool, league_id)
    pending_ids = {r["id"] for r in pending}
    assert id_pending in pending_ids
    assert id_delivered not in pending_ids
    assert id_exhausted not in pending_ids
