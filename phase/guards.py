from __future__ import annotations

import asyncpg


async def is_commissioner(pool: asyncpg.Pool, league_id: int, user_id: int) -> bool:
    """Returns True if user_id is the league commissioner."""
    val = await pool.fetchval(
        "SELECT commissioner_user_id FROM leagues WHERE id = $1",
        league_id,
    )
    return val == user_id


async def has_team(pool: asyncpg.Pool, league_id: int, user_id: int) -> bool:
    """Returns True if user_id manages a team in this league."""
    val = await pool.fetchval(
        "SELECT id FROM teams WHERE league_id = $1 AND manager_user_id = $2",
        league_id,
        user_id,
    )
    return val is not None


async def all_humans_ready(pool: asyncpg.Pool, league_id: int) -> tuple[bool, list[int]]:
    """Returns (all_ready, list_of_unready_team_ids).

    Compares human-managed teams against rows in ready_status where is_ready = TRUE.
    """
    human_rows = await pool.fetch(
        "SELECT id FROM teams WHERE league_id = $1 AND manager_user_id IS NOT NULL",
        league_id,
    )
    human_team_ids = {r["id"] for r in human_rows}

    if not human_team_ids:
        return True, []

    ready_rows = await pool.fetch(
        "SELECT team_id FROM ready_status WHERE league_id = $1 AND is_ready = TRUE",
        league_id,
    )
    ready_team_ids = {r["team_id"] for r in ready_rows}

    unready = sorted(human_team_ids - ready_team_ids)
    return len(unready) == 0, unready


async def no_pending_trades(pool: asyncpg.Pool, league_id: int) -> tuple[bool, int]:
    """Returns (no_pending, count_pending). Pending = status='pending_commissioner'."""
    count = await pool.fetchval(
        "SELECT COUNT(*) FROM trades WHERE league_id = $1 AND status = 'pending_commissioner'",
        league_id,
    )
    count = count or 0
    return count == 0, count
