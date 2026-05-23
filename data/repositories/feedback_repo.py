"""User replies on Discord, joined back to the tracked bot post via the post's
message_id. Insert is idempotent on reply_message_id so a duplicate event
delivery (Discord retries) doesn't double-record.
"""
from __future__ import annotations

import json
from typing import Optional

import asyncpg


async def insert_reply(
    pool: asyncpg.Pool,
    *,
    bot_message_id: int,
    reply_message_id: int,
    author_id: int,
    author_name: str,
    reply_text: str,
    attachments: Optional[list[dict]] = None,
    session_log_path: Optional[str] = None,
) -> Optional[int]:
    """Insert a reply; returns its id, or None if the reply_message_id was already recorded."""
    return await pool.fetchval(
        """
        INSERT INTO feedback_replies (
            bot_message_id, reply_message_id,
            author_id, author_name, reply_text,
            attachments, session_log_path
        ) VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (reply_message_id) DO NOTHING
        RETURNING id
        """,
        bot_message_id,
        reply_message_id,
        author_id,
        author_name,
        reply_text,
        json.dumps(attachments or []),
        session_log_path,
    )


async def list_recent(
    pool: asyncpg.Pool, limit: int = 10
) -> list[dict]:
    """Recent replies across all leagues, newest first."""
    rows = await pool.fetch(
        """
        SELECT
            fr.id,
            fr.reply_text,
            fr.author_name,
            fr.created_at,
            bml.kind,
            bml.league_id,
            bml.game_index,
            bml.sim_date,
            bml.content_preview
        FROM feedback_replies fr
        JOIN bot_message_log bml ON bml.message_id = fr.bot_message_id
        ORDER BY fr.created_at DESC
        LIMIT $1
        """,
        limit,
    )
    return [dict(r) for r in rows]


async def list_for_session(
    pool: asyncpg.Pool, session_log_path: str
) -> list[dict]:
    """All replies recorded against a given session JSONL — used by /feedback export."""
    rows = await pool.fetch(
        """
        SELECT * FROM feedback_replies
        WHERE session_log_path = $1
        ORDER BY created_at ASC
        """,
        session_log_path,
    )
    return [dict(r) for r in rows]
