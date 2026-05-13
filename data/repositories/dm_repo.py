from __future__ import annotations

import json
from typing import Any

import asyncpg


async def queue_dm(
    pool: asyncpg.Pool,
    league_id: int,
    user_id: int,
    embed_data: dict[str, Any],
    fallback_message: str,
    fallback_channel_role: str = "league-news",
) -> int:
    return await pool.fetchval(
        """
        INSERT INTO dm_outbox
            (league_id, user_id, embed_data, fallback_message, fallback_channel_role)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id
        """,
        league_id,
        user_id,
        json.dumps(embed_data),
        fallback_message,
        fallback_channel_role,
    )


async def get_pending(
    pool: asyncpg.Pool,
    league_id: int,
    limit: int = 20,
) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        SELECT id, league_id, user_id, embed_data, fallback_channel_role,
               fallback_message, attempts, last_error, delivered, created_at
        FROM dm_outbox
        WHERE league_id = $1
          AND delivered = FALSE
          AND attempts < 3
        ORDER BY created_at
        LIMIT $2
        """,
        league_id,
        limit,
    )
    return [dict(r) for r in rows]


async def mark_delivered(pool: asyncpg.Pool, dm_id: int) -> None:
    await pool.execute(
        "UPDATE dm_outbox SET delivered = TRUE WHERE id = $1",
        dm_id,
    )


async def mark_failed(pool: asyncpg.Pool, dm_id: int, error: str) -> None:
    await pool.execute(
        """
        UPDATE dm_outbox
        SET attempts = attempts + 1,
            last_error = $2
        WHERE id = $1
        """,
        dm_id,
        error,
    )
