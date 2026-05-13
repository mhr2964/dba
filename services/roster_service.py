from __future__ import annotations

from typing import List

from data.db import get_pool
from data.repositories import player_repo
from core.errors import DBAError


async def get_roster(league_id: int, team_id: int) -> List[player_repo.Player]:
    pool = await get_pool()
    return await player_repo.get_roster(pool, league_id, team_id)


async def get_lineup(league_id: int, team_id: int) -> List[tuple]:
    pool = await get_pool()
    return await player_repo.get_lineup(pool, league_id, team_id)


async def get_cap_summary(league_id: int, team_id: int) -> dict:
    pool = await get_pool()

    cap_row = await pool.fetchrow(
        "SELECT salary_cap FROM leagues WHERE id = $1",
        league_id,
    )
    if cap_row is None:
        raise DBAError("League not found.")

    cap: int = cap_row["salary_cap"]
    used: int = await player_repo.get_team_cap_usage(pool, league_id, team_id)
    remaining: int = cap - used
    pct: float = round((used / cap * 100), 1) if cap > 0 else 0.0

    return {
        "used": used,
        "cap": cap,
        "remaining": remaining,
        "pct": pct,
    }
