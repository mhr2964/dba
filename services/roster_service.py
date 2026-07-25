from __future__ import annotations

from typing import List

import asyncpg

from data.db import get_pool
from data.repositories import player_repo
from core.errors import DBAError

# RO5: disclosed judgment call, not derived from a formula — low enough that a
# normally functioning league never trips it, high enough to catch a genuinely
# roster-decimated team before an 82-game season locks it in as unplayable.
ROSTER_FLOOR_DEFAULT = 8


async def get_teams_below_roster_floor(
    pool: asyncpg.Pool, league_id: int, floor: int = ROSTER_FLOOR_DEFAULT
) -> List[dict]:
    """Return teams whose active-roster player count is below `floor`.

    Nothing upstream (rollover contract expirations, free agency) enforces a
    minimum roster size, so a team can enter season-start unable to reasonably
    field a lineup. This is a read-only guard-rail check — no auto-signing or
    backfill happens here, callers decide what to do with the result (see
    schedule_service.generate_season).
    """
    rows = await pool.fetch(
        """
        SELECT
            t.id AS team_id,
            t.city,
            t.name,
            COUNT(p.id) FILTER (WHERE p.roster_status = 'active') AS active_count
        FROM teams t
        LEFT JOIN players p
            ON p.team_id = t.id AND p.league_id = t.league_id
        WHERE t.league_id = $1
        GROUP BY t.id, t.city, t.name
        HAVING COUNT(p.id) FILTER (WHERE p.roster_status = 'active') < $2
        """,
        league_id,
        floor,
    )
    return [
        {
            "team_id": r["team_id"],
            "team_name": f"{r['city']} {r['name']}",
            "active_count": r["active_count"],
        }
        for r in rows
    ]


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
