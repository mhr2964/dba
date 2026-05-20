from __future__ import annotations

from typing import List

import asyncpg


async def insert_award(
    pool: asyncpg.Pool,
    player_id: int,
    league_id: int,
    season: int,
    award_type: str,
    is_winner: bool = True,
) -> None:
    """Insert a player award row. Silently no-ops on duplicate via ON CONFLICT DO NOTHING."""
    await pool.execute(
        """
        INSERT INTO player_awards (player_id, league_id, season, award_type, is_winner)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (player_id, league_id, season, award_type) DO NOTHING
        """,
        player_id,
        league_id,
        season,
        award_type,
        is_winner,
    )


async def get_career_awards(
    pool: asyncpg.Pool,
    player_id: int,
) -> List[dict]:
    """Return all award rows for a player across all leagues and seasons."""
    rows = await pool.fetch(
        """
        SELECT season, award_type, is_winner
        FROM player_awards
        WHERE player_id = $1
        ORDER BY season DESC, award_type
        """,
        player_id,
    )
    return [dict(r) for r in rows]


async def get_season_awards(
    pool: asyncpg.Pool,
    league_id: int,
    season: int,
) -> List[dict]:
    """Return all award rows for a league season."""
    rows = await pool.fetch(
        """
        SELECT player_id, award_type, is_winner
        FROM player_awards
        WHERE league_id = $1 AND season = $2
        ORDER BY award_type, player_id
        """,
        league_id,
        season,
    )
    return [dict(r) for r in rows]
