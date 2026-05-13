from __future__ import annotations

from typing import Optional

import asyncpg


async def get_record(
    pool: asyncpg.Pool,
    league_id: int,
    season: int,
    record_type: str,
) -> Optional[dict]:
    row = await pool.fetchrow(
        """
        SELECT * FROM season_records
        WHERE league_id = $1 AND season = $2 AND record_type = $3
        """,
        league_id,
        season,
        record_type,
    )
    return dict(row) if row else None


async def set_record(
    pool: asyncpg.Pool,
    league_id: int,
    season: int,
    record_type: str,
    value: float,
    player_id: Optional[int] = None,
    team_id: Optional[int] = None,
    game_id: Optional[int] = None,
) -> None:
    """Upsert a season record. Only updates if value is strictly greater than existing."""
    await pool.execute(
        """
        INSERT INTO season_records
            (league_id, season, record_type, value, player_id, team_id, game_id, set_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
        ON CONFLICT (league_id, season, record_type)
        DO UPDATE SET
            value     = EXCLUDED.value,
            player_id = EXCLUDED.player_id,
            team_id   = EXCLUDED.team_id,
            game_id   = EXCLUDED.game_id,
            set_at    = NOW()
        WHERE season_records.value < EXCLUDED.value
        """,
        league_id,
        season,
        record_type,
        value,
        player_id,
        team_id,
        game_id,
    )


async def get_all_records(
    pool: asyncpg.Pool,
    league_id: int,
    season: int,
) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT * FROM season_records
        WHERE league_id = $1 AND season = $2
        ORDER BY record_type
        """,
        league_id,
        season,
    )
    return [dict(r) for r in rows]
