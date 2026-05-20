from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import asyncpg

from core.logging import get_logger

log = get_logger(__name__)


@dataclass
class Series:
    id: int
    league_id: int
    season: int
    round: str
    high_seed_team_id: int
    low_seed_team_id: int
    wins_high: int
    wins_low: int
    games_needed: int
    winner_team_id: Optional[int]
    status: str


def _series_from_record(r: asyncpg.Record) -> Series:
    return Series(
        id=r["id"],
        league_id=r["league_id"],
        season=r["season"],
        round=r["round"],
        high_seed_team_id=r["high_seed_team_id"],
        low_seed_team_id=r["low_seed_team_id"],
        wins_high=r["wins_high"],
        wins_low=r["wins_low"],
        games_needed=r["games_needed"],
        winner_team_id=r["winner_team_id"],
        status=r["status"],
    )


async def create_series(
    pool: asyncpg.Pool,
    league_id: int,
    season: int,
    round: str,
    high_seed_id: int,
    low_seed_id: int,
    games_needed: int = 7,
) -> Series:
    row = await pool.fetchrow(
        """
        INSERT INTO series (
            league_id, season, round,
            high_seed_team_id, low_seed_team_id, games_needed
        ) VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING *
        """,
        league_id,
        season,
        round,
        high_seed_id,
        low_seed_id,
        games_needed,
    )
    return _series_from_record(row)


async def get_series(pool: asyncpg.Pool, series_id: int) -> Optional[Series]:
    row = await pool.fetchrow("SELECT * FROM series WHERE id = $1", series_id)
    return _series_from_record(row) if row else None


async def get_active_series(pool: asyncpg.Pool, league_id: int, season: int) -> List[Series]:
    rows = await pool.fetch(
        """
        SELECT * FROM series
        WHERE league_id = $1 AND season = $2 AND status = 'active'
        ORDER BY id ASC
        """,
        league_id,
        season,
    )
    return [_series_from_record(r) for r in rows]


async def get_series_by_round(
    pool: asyncpg.Pool,
    league_id: int,
    season: int,
    round: str,
) -> List[Series]:
    rows = await pool.fetch(
        """
        SELECT * FROM series
        WHERE league_id = $1 AND season = $2 AND round = $3
        ORDER BY id ASC
        """,
        league_id,
        season,
        round,
    )
    return [_series_from_record(r) for r in rows]


async def record_game_result(
    pool: asyncpg.Pool,
    series_id: int,
    winner_team_id: int,
) -> Series:
    """
    Increments the win counter for whichever side won. Marks the series complete
    when either side reaches the clinch threshold: ceil(games_needed / 2) for
    best-of-N series. For play-in (games_needed == 2) the threshold is 1 win
    because it's a single-elimination game inside the play-in bracket — callers
    handle the second play-in game by creating a separate series row.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM series WHERE id = $1 FOR UPDATE",
                series_id,
            )
            if row is None:
                raise ValueError(f"Series {series_id} not found")

            series = _series_from_record(row)

            is_high = winner_team_id == series.high_seed_team_id
            new_wins_high = series.wins_high + (1 if is_high else 0)
            new_wins_low = series.wins_low + (0 if is_high else 1)

            # Play-in series (games_needed=2) are single-elimination — 1 win completes.
            # All other series: first to ceil(games_needed/2) wins.
            clinch = 1 if series.games_needed == 2 else (series.games_needed // 2) + 1

            winner: Optional[int] = None
            new_status = "active"
            if new_wins_high >= clinch or new_wins_low >= clinch:
                winner = winner_team_id
                new_status = "complete"

            updated = await conn.fetchrow(
                """
                UPDATE series
                SET wins_high      = $2,
                    wins_low       = $3,
                    winner_team_id = $4,
                    status         = $5
                WHERE id = $1
                RETURNING *
                """,
                series_id,
                new_wins_high,
                new_wins_low,
                winner,
                new_status,
            )
            return _series_from_record(updated)


async def get_bracket(pool: asyncpg.Pool, league_id: int, season: int) -> List[Series]:
    rows = await pool.fetch(
        """
        SELECT * FROM series
        WHERE league_id = $1 AND season = $2
        ORDER BY id ASC
        """,
        league_id,
        season,
    )
    return [_series_from_record(r) for r in rows]
