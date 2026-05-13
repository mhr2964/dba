from __future__ import annotations

from typing import Optional

import asyncpg


async def get_inducted(pool: asyncpg.Pool, league_id: int, player_id: int) -> Optional[dict]:
    """Returns the HOF row for this player in this league, or None if not yet inducted."""
    row = await pool.fetchrow(
        "SELECT * FROM hall_of_fame WHERE league_id = $1 AND player_id = $2",
        league_id,
        player_id,
    )
    return dict(row) if row else None


async def induct(
    pool: asyncpg.Pool,
    league_id: int,
    player_id: int,
    inducted_season: int,
    career_seasons: int,
    career_championships: int,
    career_mvp_votes: int,
    career_overall_peak: int,
    induction_reason: str,
) -> dict:
    """Insert a hall_of_fame row. Caller must verify player is not already inducted."""
    row = await pool.fetchrow(
        """
        INSERT INTO hall_of_fame
            (league_id, player_id, inducted_season, career_seasons,
             career_championships, career_mvp_votes, career_overall_peak, induction_reason)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING *
        """,
        league_id,
        player_id,
        inducted_season,
        career_seasons,
        career_championships,
        career_mvp_votes,
        career_overall_peak,
        induction_reason,
    )
    return dict(row)


async def get_all(pool: asyncpg.Pool, league_id: int) -> list[dict]:
    """All HOF inductees for a league, sorted by induction season then player_id."""
    rows = await pool.fetch(
        """
        SELECT * FROM hall_of_fame
        WHERE league_id = $1
        ORDER BY inducted_season, player_id
        """,
        league_id,
    )
    return [dict(r) for r in rows]
