from __future__ import annotations

from typing import Optional

import asyncpg


async def add_to_block(
    pool: asyncpg.Pool,
    league_id: int,
    team_id: int,
    player_id: int,
    asking_price: Optional[int] = None,
    note: Optional[str] = None,
) -> None:
    await pool.execute(
        """
        INSERT INTO trade_block (league_id, team_id, player_id, asking_price, note)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (league_id, player_id) DO UPDATE
            SET asking_price = EXCLUDED.asking_price,
                note = EXCLUDED.note,
                added_at = NOW()
        """,
        league_id,
        team_id,
        player_id,
        asking_price,
        note,
    )


async def remove_from_block(pool: asyncpg.Pool, league_id: int, player_id: int) -> None:
    await pool.execute(
        "DELETE FROM trade_block WHERE league_id = $1 AND player_id = $2",
        league_id,
        player_id,
    )


async def get_team_block(pool: asyncpg.Pool, league_id: int, team_id: int) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT player_id, asking_price, note, added_at
        FROM trade_block
        WHERE league_id = $1 AND team_id = $2
        ORDER BY added_at DESC
        """,
        league_id,
        team_id,
    )
    return [dict(r) for r in rows]


async def get_league_block(pool: asyncpg.Pool, league_id: int) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT team_id, player_id, asking_price, note, added_at
        FROM trade_block
        WHERE league_id = $1
        ORDER BY team_id, added_at DESC
        """,
        league_id,
    )
    return [dict(r) for r in rows]


async def is_on_block(pool: asyncpg.Pool, league_id: int, player_id: int) -> bool:
    result = await pool.fetchval(
        "SELECT 1 FROM trade_block WHERE league_id = $1 AND player_id = $2",
        league_id,
        player_id,
    )
    return result is not None


async def clear_team_block(pool: asyncpg.Pool, league_id: int, team_id: int) -> int:
    result = await pool.execute(
        "DELETE FROM trade_block WHERE league_id = $1 AND team_id = $2",
        league_id,
        team_id,
    )
    # asyncpg returns "DELETE N" as a string
    parts = result.split()
    return int(parts[1]) if len(parts) == 2 else 0


async def remove_players_from_block(
    pool: asyncpg.Pool, league_id: int, player_ids: list[int]
) -> None:
    """Remove multiple players from the trade block at once — used after trade approval."""
    if not player_ids:
        return
    await pool.execute(
        "DELETE FROM trade_block WHERE league_id = $1 AND player_id = ANY($2::int[])",
        league_id,
        player_ids,
    )
