"""Listener semantics: react only to user replies that target a tracked post.

The cog must self-gate — silently skipping non-tracked replies — so it's safe
to register before all send sites are wired.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot.cogs.feedback_cog import FeedbackCog
from services import feedback_log


def _make_listener_message(
    *,
    is_bot: bool = False,
    reference_message_id: int | None = 1234,
    text: str = "feedback text",
    reply_id: int = 9999,
    author_id: int = 100,
    author_name: str = "Owner#0001",
) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.id = reply_id
    msg.content = text
    author = MagicMock()
    author.bot = is_bot
    author.id = author_id
    author.__str__ = lambda _self: author_name
    msg.author = author
    msg.attachments = []
    if reference_message_id is None:
        msg.reference = None
    else:
        ref = MagicMock()
        ref.message_id = reference_message_id
        msg.reference = ref
    msg.add_reaction = AsyncMock()
    return msg


async def _make_league(pool) -> int:
    return await pool.fetchval(
        """
        INSERT INTO leagues (
            discord_guild_id, name, start_season_year, current_season,
            current_phase, phase_data, commissioner_user_id, fa_day_count,
            salary_cap
        ) VALUES ($1, $2, $3, $3, 'SETUP', '{}'::jsonb, $4, 8, 140000000)
        RETURNING id
        """,
        999001, "Test League", 2026, 12345,
    )


def _make_sent(msg_id: int = 12345) -> MagicMock:
    sent = MagicMock(spec=discord.Message)
    sent.id = msg_id
    channel = MagicMock()
    channel.id = 555
    sent.channel = channel
    guild = MagicMock()
    guild.id = 999
    sent.guild = guild
    return sent


async def test_on_message_ignores_bot_authored_messages(db_pool, tmp_path, monkeypatch):
    """Bot's own messages must never trigger lookup — would recursively spam."""
    monkeypatch.setattr(feedback_log, "_SESSION_PATH", tmp_path / "feedback_ignore_bot.jsonl")
    monkeypatch.setattr(feedback_log, "_LOG_DIR", tmp_path)
    cog = FeedbackCog(MagicMock())
    msg = _make_listener_message(is_bot=True)
    await cog.on_message(msg)
    msg.add_reaction.assert_not_called()


async def test_on_message_ignores_messages_without_reference(db_pool, tmp_path, monkeypatch):
    """A normal channel message (not a reply) is left alone."""
    monkeypatch.setattr(feedback_log, "_SESSION_PATH", tmp_path / "feedback_no_ref.jsonl")
    monkeypatch.setattr(feedback_log, "_LOG_DIR", tmp_path)
    cog = FeedbackCog(MagicMock())
    msg = _make_listener_message(reference_message_id=None)
    await cog.on_message(msg)
    msg.add_reaction.assert_not_called()


async def test_on_message_silently_skips_reply_to_untracked_message(
    db_pool, tmp_path, monkeypatch
):
    """Reply to an unknown message_id → no DB row, no reaction, no JSONL line."""
    session_path = tmp_path / "feedback_untracked.jsonl"
    monkeypatch.setattr(feedback_log, "_SESSION_PATH", session_path)
    monkeypatch.setattr(feedback_log, "_LOG_DIR", tmp_path)

    cog = FeedbackCog(MagicMock())
    msg = _make_listener_message(reference_message_id=987654321)  # never registered
    await cog.on_message(msg)

    msg.add_reaction.assert_not_called()
    db_count = await db_pool.fetchval("SELECT count(*) FROM feedback_replies")
    assert db_count == 0
    assert not session_path.exists() or session_path.read_text() == ""


async def test_on_message_records_reply_to_tracked_post_and_reacts(
    db_pool, tmp_path, monkeypatch
):
    """End-to-end — registered post → reply → DB row + JSONL line + 💬 reaction."""
    session_path = tmp_path / "feedback_happy.jsonl"
    monkeypatch.setattr(feedback_log, "_SESSION_PATH", session_path)
    monkeypatch.setattr(feedback_log, "_LOG_DIR", tmp_path)

    league_id = await _make_league(db_pool)
    sent = _make_sent(msg_id=5_5_5_5)
    await feedback_log.register_bot_post(
        db_pool, sent,
        {
            "kind": "columnist_article",
            "league_id": league_id,
            "season": 2026,
            "subject_team_ids": [3],
            "content_preview": "Marcus Cole on Denver's slide",
        },
    )

    cog = FeedbackCog(MagicMock())
    reply = _make_listener_message(
        reference_message_id=5_5_5_5,
        reply_id=6_6_6_6,
        text="agreed — denver looks done",
    )
    await cog.on_message(reply)

    reply.add_reaction.assert_awaited_once()
    db_count = await db_pool.fetchval(
        "SELECT count(*) FROM feedback_replies WHERE reply_message_id = $1", reply.id
    )
    assert db_count == 1
    assert session_path.exists()
    line = session_path.read_text(encoding="utf-8").splitlines()[0]
    assert "denver looks done" in line
    assert str(league_id) in line
