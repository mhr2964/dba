from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from typing import Optional

import asyncpg


@dataclass
class League:
    id: int
    discord_guild_id: int
    name: str
    start_season_year: int
    current_season: int
    current_phase: str
    phase_data: dict
    commissioner_user_id: int
    fa_day_count: int
    salary_cap: int
    created_at: datetime.datetime
    archived_at: Optional[datetime.datetime]
    pending_progression_season: Optional[int] = None


def _league_from_record(r: asyncpg.Record) -> League:
    return League(
        id=r["id"],
        discord_guild_id=r["discord_guild_id"],
        name=r["name"],
        start_season_year=r["start_season_year"],
        current_season=r["current_season"],
        current_phase=r["current_phase"],
        phase_data=r["phase_data"] if isinstance(r["phase_data"], dict) else json.loads(r["phase_data"]),
        commissioner_user_id=r["commissioner_user_id"],
        fa_day_count=r["fa_day_count"],
        salary_cap=r["salary_cap"],
        created_at=r["created_at"],
        archived_at=r["archived_at"],
        pending_progression_season=r["pending_progression_season"],
    )


async def get_by_guild(pool: asyncpg.Pool, guild_id: int) -> Optional[League]:
    row = await pool.fetchrow(
        "SELECT * FROM leagues WHERE discord_guild_id = $1 AND archived_at IS NULL",
        guild_id,
    )
    return _league_from_record(row) if row else None


async def create(
    pool: asyncpg.Pool,
    guild_id: int,
    name: str,
    season_year: int,
    commissioner_id: int,
) -> League:
    row = await pool.fetchrow(
        """
        INSERT INTO leagues
            (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES ($1, $2, $3, $3, $4)
        RETURNING *
        """,
        guild_id,
        name,
        season_year,
        commissioner_id,
    )
    return _league_from_record(row)


async def add_channel(
    pool: asyncpg.Pool,
    league_id: int,
    role: str,
    channel_id: int,
) -> None:
    await pool.execute(
        """
        INSERT INTO league_channels (league_id, channel_role, discord_channel_id)
        VALUES ($1, $2, $3)
        ON CONFLICT (league_id, channel_role) DO UPDATE SET discord_channel_id = EXCLUDED.discord_channel_id
        """,
        league_id,
        role,
        channel_id,
    )


async def add_role(
    pool: asyncpg.Pool,
    league_id: int,
    role_type: str,
    team_id: Optional[int],
    discord_role_id: int,
) -> None:
    await pool.execute(
        """
        INSERT INTO league_roles (league_id, role_type, team_id, discord_role_id)
        VALUES ($1, $2, $3, $4)
        """,
        league_id,
        role_type,
        team_id,
        discord_role_id,
    )


async def get_channel(pool: asyncpg.Pool, league_id: int, role: str) -> Optional[int]:
    return await pool.fetchval(
        "SELECT discord_channel_id FROM league_channels WHERE league_id = $1 AND channel_role = $2",
        league_id,
        role,
    )


async def get_team_role(pool: asyncpg.Pool, league_id: int, team_id: int) -> Optional[int]:
    return await pool.fetchval(
        "SELECT discord_role_id FROM league_roles WHERE league_id = $1 AND team_id = $2 AND role_type = 'team'",
        league_id,
        team_id,
    )


async def get_commissioner_role(pool: asyncpg.Pool, league_id: int) -> Optional[int]:
    return await pool.fetchval(
        "SELECT discord_role_id FROM league_roles WHERE league_id = $1 AND role_type = 'commissioner'",
        league_id,
    )
