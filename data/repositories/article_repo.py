from __future__ import annotations

from typing import Optional

import asyncpg


async def insert(
    pool: asyncpg.Pool,
    league_id: int,
    season: int,
    persona_id: str,
    category: str,
    headline: str,
    body: str,
    subject_team_ids: Optional[list[int]] = None,
    subject_player_ids: Optional[list[int]] = None,
    posted_channel_role: Optional[str] = None,
    posted_message_id: Optional[int] = None,
) -> int:
    """Insert a new article and return its id."""
    return await pool.fetchval(
        """
        INSERT INTO league_articles (
            league_id, season, persona_id, category,
            headline, body,
            subject_team_ids, subject_player_ids,
            posted_channel_role, posted_message_id
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        RETURNING id
        """,
        league_id,
        season,
        persona_id,
        category,
        headline,
        body,
        subject_team_ids or [],
        subject_player_ids or [],
        posted_channel_role,
        posted_message_id,
    )


async def recent_by_persona(
    pool: asyncpg.Pool,
    league_id: int,
    persona_id: str,
    limit: int = 5,
    season: Optional[int] = None,
) -> list[dict]:
    """Return the most recent N articles by this persona in this league.

    Pass season to restrict results to a single season — useful when building
    rank_deltas so a new season's first Power List doesn't compare against the
    prior season's final ranking.
    """
    if season is not None:
        rows = await pool.fetch(
            """
            SELECT *
            FROM league_articles
            WHERE league_id = $1
              AND persona_id = $2
              AND season = $3
            ORDER BY created_at DESC
            LIMIT $4
            """,
            league_id,
            persona_id,
            season,
            limit,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT *
            FROM league_articles
            WHERE league_id = $1
              AND persona_id = $2
            ORDER BY created_at DESC
            LIMIT $3
            """,
            league_id,
            persona_id,
            limit,
        )
    return [dict(r) for r in rows]


async def recent_about_team(
    pool: asyncpg.Pool,
    league_id: int,
    team_id: int,
    limit: int = 5,
) -> list[dict]:
    """Return the most recent N articles mentioning this team (via GIN index on subject_team_ids)."""
    rows = await pool.fetch(
        """
        SELECT *
        FROM league_articles
        WHERE league_id = $1
          AND subject_team_ids @> ARRAY[$2]::integer[]
        ORDER BY created_at DESC
        LIMIT $3
        """,
        league_id,
        team_id,
        limit,
    )
    return [dict(r) for r in rows]


async def update_message_id(
    pool: asyncpg.Pool,
    article_id: int,
    message_id: int,
) -> None:
    """Set posted_message_id after the Discord message is sent."""
    await pool.execute(
        """
        UPDATE league_articles
        SET posted_message_id = $2
        WHERE id = $1
        """,
        article_id,
        message_id,
    )
