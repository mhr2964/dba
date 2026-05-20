from __future__ import annotations

from typing import Optional

import asyncpg


async def get_record(
    pool: asyncpg.Pool,
    league_id: int,
    record_type: str,
) -> Optional[dict]:
    """Return the current all-time record row for (league_id, record_type), or None."""
    row = await pool.fetchrow(
        """
        SELECT id, league_id, record_type, value, player_id, team_id, game_id, season_set, set_at
        FROM all_time_records
        WHERE league_id = $1 AND record_type = $2
        """,
        league_id,
        record_type,
    )
    return dict(row) if row else None


async def set_record(
    pool: asyncpg.Pool,
    league_id: int,
    record_type: str,
    value: float,
    player_id: Optional[int] = None,
    team_id: Optional[int] = None,
    game_id: Optional[int] = None,
    season_set: Optional[int] = None,
) -> None:
    """Upsert an all-time record.  Updates regardless of current value (caller is
    responsible for checking if new_value > old_value before calling)."""
    await pool.execute(
        """
        INSERT INTO all_time_records
            (league_id, record_type, value, player_id, team_id, game_id, season_set, set_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
        ON CONFLICT (league_id, record_type)
        DO UPDATE SET
            value      = EXCLUDED.value,
            player_id  = EXCLUDED.player_id,
            team_id    = EXCLUDED.team_id,
            game_id    = EXCLUDED.game_id,
            season_set = EXCLUDED.season_set,
            set_at     = NOW()
        WHERE all_time_records.value < EXCLUDED.value
        """,
        league_id,
        record_type,
        value,
        player_id,
        team_id,
        game_id,
        season_set,
    )
