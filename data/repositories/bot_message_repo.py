"""Anchors every non-ephemeral bot post to the league/sim/entity context it was
generated against, keyed by Discord message_id so an on_message reply handler
can join replies back to "what was the bot referring to" in one indexed lookup.
"""
from __future__ import annotations

import datetime
import json
from typing import Optional

import asyncpg


async def insert(
    pool: asyncpg.Pool,
    *,
    message_id: int,
    channel_id: int,
    guild_id: int,
    kind: str,
    league_id: Optional[int] = None,
    game_index: Optional[int] = None,
    sim_date: Optional[datetime.date] = None,
    season: Optional[int] = None,
    subject_team_ids: Optional[list[int]] = None,
    subject_player_ids: Optional[list[int]] = None,
    subject_trade_id: Optional[int] = None,
    context_blob: Optional[dict] = None,
    content_preview: Optional[str] = None,
) -> int:
    """Register a bot post and return its bigserial id."""
    return await pool.fetchval(
        """
        INSERT INTO bot_message_log (
            message_id, channel_id, guild_id, league_id, kind,
            game_index, sim_date, season,
            subject_team_ids, subject_player_ids, subject_trade_id,
            context_blob, content_preview
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
        ON CONFLICT (message_id) DO UPDATE SET
            kind = EXCLUDED.kind,
            league_id = EXCLUDED.league_id,
            game_index = EXCLUDED.game_index,
            sim_date = EXCLUDED.sim_date,
            season = EXCLUDED.season,
            subject_team_ids = EXCLUDED.subject_team_ids,
            subject_player_ids = EXCLUDED.subject_player_ids,
            subject_trade_id = EXCLUDED.subject_trade_id,
            context_blob = EXCLUDED.context_blob,
            content_preview = EXCLUDED.content_preview
        RETURNING id
        """,
        message_id,
        channel_id,
        guild_id,
        league_id,
        kind,
        game_index,
        sim_date,
        season,
        subject_team_ids or [],
        subject_player_ids or [],
        subject_trade_id,
        json.dumps(context_blob or {}),
        content_preview,
    )


async def get_by_message_id(
    pool: asyncpg.Pool, message_id: int
) -> Optional[dict]:
    """Look up a tracked bot post by Discord message_id. Returns None if untracked."""
    row = await pool.fetchrow(
        "SELECT * FROM bot_message_log WHERE message_id = $1",
        message_id,
    )
    if row is None:
        return None
    out = dict(row)
    # context_blob comes back as a JSON string under asyncpg's default codec;
    # decode to a dict so callers don't have to think about it.
    if isinstance(out.get("context_blob"), str):
        try:
            out["context_blob"] = json.loads(out["context_blob"])
        except (TypeError, ValueError):
            out["context_blob"] = {}
    return out


async def list_recent_for_league(
    pool: asyncpg.Pool, league_id: int, limit: int = 50
) -> list[dict]:
    """Recent tracked posts in a league, newest first — for `/feedback list` etc."""
    rows = await pool.fetch(
        """
        SELECT * FROM bot_message_log
        WHERE league_id = $1
        ORDER BY posted_at DESC
        LIMIT $2
        """,
        league_id,
        limit,
    )
    return [dict(r) for r in rows]
