"""Verifies the send-side registration + reply-side recording pipeline.

DB-backed tests so the repo + Postgres round-trip is part of the contract being
verified — mocking the repos here would leave the JSONB / array marshalling
untested, which is exactly the surface most likely to drift silently.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import discord
import pytest

from data.repositories import bot_message_repo
from services import feedback_log


def _make_sent_message(
    message_id: int = 1_000_000_000_000_000_001,
    channel_id: int = 555,
    guild_id: int = 999,
) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.id = message_id
    channel = MagicMock()
    channel.id = channel_id
    msg.channel = channel
    guild = MagicMock()
    guild.id = guild_id
    msg.guild = guild
    return msg


def _make_reply_message(
    reply_id: int,
    author_id: int = 100,
    author_name: str = "TesterUser#0001",
    text: str = "this trade looks weird",
) -> MagicMock:
    reply = MagicMock(spec=discord.Message)
    reply.id = reply_id
    reply.content = text
    author = MagicMock()
    author.id = author_id
    author.__str__ = lambda _self: author_name
    reply.author = author
    reply.attachments = []
    return reply


async def _make_league(pool) -> int:
    """Minimal league row so bot_message_log.league_id FK is satisfied."""
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


async def test_register_bot_post_writes_row_with_full_context(db_pool):
    """Happy path — every anchor field round-trips through Postgres."""
    league_id = await _make_league(db_pool)
    sent = _make_sent_message()
    inserted_id = await feedback_log.register_bot_post(
        db_pool,
        sent,
        {
            "kind": "columnist_article",
            "league_id": league_id,
            "season": 2026,
            "game_index": 412,
            "subject_team_ids": [3, 17],
            "subject_player_ids": [287],
            "subject_trade_id": 1042,
            "context_blob": {"persona_id": "marcus_cole", "headline": "Denver's Quiet Drift"},
            "content_preview": "DENVER — there's a feeling around the Pepsi Center this week that...",
        },
    )
    assert inserted_id is not None

    row = await bot_message_repo.get_by_message_id(db_pool, sent.id)
    assert row is not None
    assert row["kind"] == "columnist_article"
    assert row["league_id"] == league_id
    assert row["season"] == 2026
    assert row["game_index"] == 412
    assert list(row["subject_team_ids"]) == [3, 17]
    assert list(row["subject_player_ids"]) == [287]
    assert row["subject_trade_id"] == 1042
    assert row["context_blob"]["persona_id"] == "marcus_cole"
    assert "Pepsi Center" in row["content_preview"]


async def test_register_bot_post_with_none_message_is_noop(db_pool):
    """When the upstream send failed, registration is a no-op (no row, no raise)."""
    result = await feedback_log.register_bot_post(
        db_pool, None, {"kind": "columnist_article", "league_id": None}
    )
    assert result is None
    count = await db_pool.fetchval("SELECT count(*) FROM bot_message_log")
    assert count == 0


async def test_register_bot_post_idempotent_on_duplicate_message_id(db_pool):
    """ON CONFLICT keeps redelivery from inflating rows — second call updates context."""
    league_id = await _make_league(db_pool)
    sent = _make_sent_message(message_id=42_42_42)
    await feedback_log.register_bot_post(
        db_pool, sent,
        {"kind": "columnist_article", "league_id": league_id, "context_blob": {"v": 1}},
    )
    await feedback_log.register_bot_post(
        db_pool, sent,
        {"kind": "columnist_article", "league_id": league_id, "context_blob": {"v": 2}},
    )
    rows = await db_pool.fetch(
        "SELECT * FROM bot_message_log WHERE message_id = $1", sent.id
    )
    assert len(rows) == 1
    blob = rows[0]["context_blob"]
    if isinstance(blob, str):
        blob = json.loads(blob)
    assert blob == {"v": 2}


async def test_record_reply_writes_db_row_and_jsonl_line(db_pool, tmp_path, monkeypatch):
    """End-to-end — registered post + reply produces both a DB row and a self-contained JSONL line."""
    session_path = tmp_path / "feedback_test.jsonl"
    monkeypatch.setattr(feedback_log, "_SESSION_PATH", session_path)
    monkeypatch.setattr(feedback_log, "_LOG_DIR", tmp_path)

    league_id = await _make_league(db_pool)
    sent = _make_sent_message(message_id=7777)
    await feedback_log.register_bot_post(
        db_pool, sent,
        {
            "kind": "trade_announcement",
            "league_id": league_id,
            "season": 2026,
            "subject_team_ids": [3, 17],
            "subject_trade_id": 1042,
            "context_blob": {"status": "approved"},
            "content_preview": "DEN/LAC trade approved",
        },
    )
    bot_row = await bot_message_repo.get_by_message_id(db_pool, sent.id)
    assert bot_row is not None

    reply = _make_reply_message(reply_id=8888, text="den shouldn't have traded gordon")
    reply_id = await feedback_log.record_reply(db_pool, bot_row, reply)
    assert reply_id is not None

    db_count = await db_pool.fetchval(
        "SELECT count(*) FROM feedback_replies WHERE reply_message_id = $1", reply.id
    )
    assert db_count == 1

    lines = session_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["reply"]["text"] == "den shouldn't have traded gordon"
    assert parsed["reply"]["message_id"] == 8888
    assert parsed["bot_post"]["message_id"] == 7777
    assert parsed["bot_post"]["kind"] == "trade_announcement"
    assert parsed["bot_post"]["league_id"] == league_id
    assert parsed["bot_post"]["subject_team_ids"] == [3, 17]
    assert parsed["bot_post"]["subject_trade_id"] == 1042
    assert parsed["bot_post"]["context"]["status"] == "approved"


async def test_record_reply_dedupes_on_duplicate_reply_message_id(db_pool, tmp_path, monkeypatch):
    """Discord can redeliver the same on_message event; we must not double-write."""
    session_path = tmp_path / "feedback_dedup.jsonl"
    monkeypatch.setattr(feedback_log, "_SESSION_PATH", session_path)
    monkeypatch.setattr(feedback_log, "_LOG_DIR", tmp_path)

    league_id = await _make_league(db_pool)
    sent = _make_sent_message(message_id=11)
    await feedback_log.register_bot_post(
        db_pool, sent,
        {"kind": "columnist_article", "league_id": league_id, "content_preview": "x"},
    )
    bot_row = await bot_message_repo.get_by_message_id(db_pool, sent.id)
    reply = _make_reply_message(reply_id=22, text="first")

    first = await feedback_log.record_reply(db_pool, bot_row, reply)
    second = await feedback_log.record_reply(db_pool, bot_row, reply)
    assert first is not None
    assert second is None  # dedup signal

    lines = session_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1  # second call must NOT have appended


async def test_export_session_returns_zero_when_no_captures(db_pool, tmp_path, monkeypatch):
    """No captures yet → (path, 0); cog turns this into the friendly empty response."""
    session_path = tmp_path / "feedback_empty.jsonl"
    monkeypatch.setattr(feedback_log, "_SESSION_PATH", session_path)
    monkeypatch.setattr(feedback_log, "_LOG_DIR", tmp_path)

    path, count = await feedback_log.export_session(db_pool)
    assert path == session_path
    assert count == 0


async def test_export_session_rebuilds_jsonl_from_db_when_file_missing(db_pool, tmp_path, monkeypatch):
    """Disk loss / mid-session restart shouldn't strand DB rows — file rebuilds from Postgres."""
    session_path = tmp_path / "feedback_rebuild.jsonl"
    monkeypatch.setattr(feedback_log, "_SESSION_PATH", session_path)
    monkeypatch.setattr(feedback_log, "_LOG_DIR", tmp_path)

    league_id = await _make_league(db_pool)
    sent = _make_sent_message(message_id=33)
    await feedback_log.register_bot_post(
        db_pool, sent,
        {"kind": "columnist_article", "league_id": league_id, "content_preview": "preview"},
    )
    bot_row = await bot_message_repo.get_by_message_id(db_pool, sent.id)
    await feedback_log.record_reply(db_pool, bot_row, _make_reply_message(reply_id=44, text="hi"))

    # Wipe the file but keep the DB rows.
    session_path.unlink()

    path, count = await feedback_log.export_session(db_pool)
    assert count == 1
    assert path.exists()
    parsed = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert parsed["reply"]["text"] == "hi"
    assert parsed["bot_post"]["league_id"] == league_id
