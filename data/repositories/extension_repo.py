from __future__ import annotations

from typing import Optional

import asyncpg

from core.logging import get_logger

log = get_logger(__name__)


async def get_extension(
    pool: asyncpg.Pool, league_id: int, player_id: int
) -> Optional[dict]:
    row = await pool.fetchrow(
        """
        SELECT * FROM contract_extensions
        WHERE league_id = $1 AND player_id = $2
        """,
        league_id,
        player_id,
    )
    return dict(row) if row else None


async def create_extension(
    pool: asyncpg.Pool,
    league_id: int,
    player_id: int,
    team_id: int,
    salary: int,
    years: int,
    season: int,
    activates_after: int,
) -> int:
    """Insert a pending extension and return its id."""
    row = await pool.fetchrow(
        """
        INSERT INTO contract_extensions
            (league_id, player_id, team_id, new_salary, new_years,
             signed_in_season, activates_after_season)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id
        """,
        league_id,
        player_id,
        team_id,
        salary,
        years,
        season,
        activates_after,
    )
    return row["id"]


async def get_team_extensions(
    pool: asyncpg.Pool, league_id: int, team_id: int
) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT * FROM contract_extensions
        WHERE league_id = $1 AND team_id = $2
        ORDER BY activates_after_season
        """,
        league_id,
        team_id,
    )
    return [dict(r) for r in rows]


async def cancel_extension(
    pool: asyncpg.Pool, league_id: int, player_id: int
) -> None:
    await pool.execute(
        "DELETE FROM contract_extensions WHERE league_id = $1 AND player_id = $2",
        league_id,
        player_id,
    )


async def process_extensions_for_season(
    pool: asyncpg.Pool, league_id: int, season: int, signed_in_season: int
) -> int:
    """
    Convert pending extensions whose activates_after_season == season into active
    contracts. Deactivates the old contract and inserts the new one.
    Called during rollover, after contracts are aged, before the season counter
    advances -- `season` matches against activates_after_season (the season
    that just ended), while `signed_in_season` anchors the new contract row to
    the season it actually starts in (next_season), since these two are no
    longer the same value now that rollover ages contracts first (RO1).
    Also restores the player to active on the extended contract's team: since
    _age_contracts now runs before this call, a contract that naturally hit
    years_remaining=0 this cycle has already flipped the player to
    roster_status='free_agent'/team_id=NULL -- without this restore, a player
    whose extension just activated would incorrectly remain a free agent.
    Returns the count of extensions processed.
    """
    rows = await pool.fetch(
        """
        SELECT * FROM contract_extensions
        WHERE league_id = $1 AND activates_after_season = $2
        """,
        league_id,
        season,
    )
    if not rows:
        return 0

    processed = 0
    for row in rows:
        player_id: int = row["player_id"]
        team_id: int = row["team_id"]
        new_salary: int = row["new_salary"]
        new_years: int = row["new_years"]

        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE contracts
                    SET is_active = FALSE, terminated_reason = 'extension_superseded'
                    WHERE player_id = $1 AND league_id = $2 AND is_active = TRUE
                    """,
                    player_id,
                    league_id,
                )
                await conn.execute(
                    """
                    INSERT INTO contracts
                        (league_id, player_id, team_id, salary, years_remaining,
                         total_years, contract_type, signed_in_season, is_active, signed_at)
                    VALUES ($1, $2, $3, $4, $5, $5, 'extension', $6, TRUE, NOW())
                    """,
                    league_id,
                    player_id,
                    team_id,
                    new_salary,
                    new_years,
                    signed_in_season,
                )
                await conn.execute(
                    "DELETE FROM contract_extensions WHERE id = $1",
                    row["id"],
                )
                await conn.execute(
                    """
                    UPDATE players
                    SET roster_status = 'active', team_id = $1
                    WHERE id = $2
                    """,
                    team_id,
                    player_id,
                )

        processed += 1
        log.info(
            f"Extension activated: player={player_id} team={team_id} "
            f"salary={new_salary} years={new_years} signed_in_season={signed_in_season}"
        )

    return processed
