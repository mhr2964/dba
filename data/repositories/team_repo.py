from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import asyncpg


@dataclass
class Team:
    id: int
    league_id: int
    nba_team_code: str
    name: str
    city: str
    conference: str
    division: str
    manager_user_id: Optional[int]
    cpu_mode: str
    team_offense_rating: Optional[int]
    team_defense_rating: Optional[int]
    pace: Optional[float]

    @property
    def full_name(self) -> str:
        return f"{self.city} {self.name}"


def _team_from_record(r: asyncpg.Record) -> Team:
    return Team(
        id=r["id"],
        league_id=r["league_id"],
        nba_team_code=r["nba_team_code"],
        name=r["name"],
        city=r["city"],
        conference=r["conference"],
        division=r["division"],
        manager_user_id=r["manager_user_id"],
        cpu_mode=r["cpu_mode"],
        team_offense_rating=r["team_offense_rating"],
        team_defense_rating=r["team_defense_rating"],
        pace=r["pace"],
    )


async def create(pool: asyncpg.Pool, league_id: int, data: dict) -> Team:
    row = await pool.fetchrow(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING *
        """,
        league_id,
        data["code"],
        data["name"],
        data["city"],
        data["conference"],
        data["division"],
    )
    return _team_from_record(row)


async def get_all(pool: asyncpg.Pool, league_id: int) -> List[Team]:
    rows = await pool.fetch(
        """
        SELECT * FROM teams
        WHERE league_id = $1
        ORDER BY conference, division, name
        """,
        league_id,
    )
    return [_team_from_record(r) for r in rows]


async def get_by_code(pool: asyncpg.Pool, league_id: int, code: str) -> Optional[Team]:
    row = await pool.fetchrow(
        "SELECT * FROM teams WHERE league_id = $1 AND UPPER(nba_team_code) = UPPER($2)",
        league_id,
        code,
    )
    return _team_from_record(row) if row else None


async def get_by_id(pool: asyncpg.Pool, team_id: int) -> Optional[Team]:
    row = await pool.fetchrow("SELECT * FROM teams WHERE id = $1", team_id)
    return _team_from_record(row) if row else None


async def get_by_manager(pool: asyncpg.Pool, league_id: int, user_id: int) -> Optional[Team]:
    row = await pool.fetchrow(
        "SELECT * FROM teams WHERE league_id = $1 AND manager_user_id = $2",
        league_id,
        user_id,
    )
    return _team_from_record(row) if row else None


async def set_manager(pool: asyncpg.Pool, team_id: int, user_id: Optional[int]) -> None:
    await pool.execute(
        "UPDATE teams SET manager_user_id = $1 WHERE id = $2",
        user_id,
        team_id,
    )


async def rename_team(pool: asyncpg.Pool, team_id: int, new_name: str, new_city: str) -> None:
    await pool.execute(
        "UPDATE teams SET name = $1, city = $2 WHERE id = $3",
        new_name,
        new_city,
        team_id,
    )


async def log_manager_change(
    pool: asyncpg.Pool,
    team_id: int,
    user_id: Optional[int],
    assigned_by: int,
) -> None:
    await pool.execute(
        """
        INSERT INTO team_manager_history (team_id, user_id, assigned_by)
        VALUES ($1, $2, $3)
        """,
        team_id,
        user_id,
        assigned_by,
    )
