from __future__ import annotations

from typing import List, Optional

import asyncpg


async def record_season(
    pool: asyncpg.Pool,
    league_id: int,
    season: int,
    champion_team_id: Optional[int] = None,
    mvp_player_id: Optional[int] = None,
    finals_mvp_player_id: Optional[int] = None,
    regular_season_wins_leader_id: Optional[int] = None,
    regular_season_losses_leader_id: Optional[int] = None,
) -> None:
    await pool.execute(
        """
        INSERT INTO history_seasons (
            league_id, season,
            champion_team_id, mvp_player_id, finals_mvp_player_id,
            regular_season_wins_leader_id, regular_season_losses_leader_id
        ) VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (league_id, season) DO UPDATE SET
            champion_team_id                = EXCLUDED.champion_team_id,
            mvp_player_id                   = EXCLUDED.mvp_player_id,
            finals_mvp_player_id            = EXCLUDED.finals_mvp_player_id,
            regular_season_wins_leader_id   = EXCLUDED.regular_season_wins_leader_id,
            regular_season_losses_leader_id = EXCLUDED.regular_season_losses_leader_id,
            completed_at                    = NOW()
        """,
        league_id,
        season,
        champion_team_id,
        mvp_player_id,
        finals_mvp_player_id,
        regular_season_wins_leader_id,
        regular_season_losses_leader_id,
    )


async def get_season(
    pool: asyncpg.Pool,
    league_id: int,
    season: int,
) -> Optional[dict]:
    row = await pool.fetchrow(
        "SELECT * FROM history_seasons WHERE league_id = $1 AND season = $2",
        league_id,
        season,
    )
    return dict(row) if row else None


async def get_all_seasons(
    pool: asyncpg.Pool,
    league_id: int,
) -> List[dict]:
    rows = await pool.fetch(
        "SELECT * FROM history_seasons WHERE league_id = $1 ORDER BY season DESC",
        league_id,
    )
    return [dict(r) for r in rows]
