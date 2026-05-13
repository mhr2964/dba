from __future__ import annotations

from typing import List

import asyncpg


async def get_recent_commissioner_actions(
    pool: asyncpg.Pool,
    league_id: int,
    limit: int = 20,
) -> List[dict]:
    rows = await pool.fetch(
        """
        SELECT id, user_id, action_type, target_ref, detail, created_at
        FROM commissioner_actions
        WHERE league_id = $1
        ORDER BY created_at DESC
        LIMIT $2
        """,
        league_id,
        limit,
    )
    return [dict(r) for r in rows]


async def log_commissioner_action(
    pool: asyncpg.Pool,
    league_id: int,
    user_id: int,
    action_type: str,
    target_ref: str | None = None,
    detail: str | None = None,
) -> None:
    await pool.execute(
        """
        INSERT INTO commissioner_actions (league_id, user_id, action_type, target_ref, detail)
        VALUES ($1, $2, $3, $4, $5)
        """,
        league_id,
        user_id,
        action_type,
        target_ref,
        detail,
    )
