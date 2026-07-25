from __future__ import annotations

from typing import Optional

import asyncpg


async def get_or_create_fa_state(
    pool: asyncpg.Pool, league_id: int, season: int
) -> dict:
    row = await pool.fetchrow(
        "SELECT * FROM fa_state WHERE league_id = $1",
        league_id,
    )
    if row:
        return dict(row)

    row = await pool.fetchrow(
        """
        INSERT INTO fa_state (league_id, season, current_day, phase, total_days)
        VALUES ($1, $2, 0, 'closed', 8)
        RETURNING *
        """,
        league_id,
        season,
    )
    return dict(row)


async def update_fa_state(
    pool: asyncpg.Pool,
    league_id: int,
    phase: str,
    current_day: Optional[int] = None,
) -> None:
    if current_day is not None:
        await pool.execute(
            "UPDATE fa_state SET phase = $1, current_day = $2 WHERE league_id = $3",
            phase,
            current_day,
            league_id,
        )
    else:
        await pool.execute(
            "UPDATE fa_state SET phase = $1 WHERE league_id = $2",
            phase,
            league_id,
        )


async def submit_offer(
    pool: asyncpg.Pool,
    league_id: int,
    season: int,
    fa_day: int,
    team_id: int,
    player_id: int,
    salary: int,
    years: int,
) -> int:
    offer_id = await pool.fetchval(
        """
        INSERT INTO fa_offers
            (league_id, season, fa_day, team_id, player_id, salary_per_year, years, status)
        VALUES ($1, $2, $3, $4, $5, $6, $7, 'submitted')
        RETURNING id
        """,
        league_id,
        season,
        fa_day,
        team_id,
        player_id,
        salary,
        years,
    )
    return int(offer_id)


async def get_offers_for_player(
    pool: asyncpg.Pool,
    league_id: int,
    season: int,
    fa_day: int,
    player_id: int,
) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT * FROM fa_offers
        WHERE league_id = $1 AND season = $2 AND fa_day = $3 AND player_id = $4
        ORDER BY salary_per_year DESC
        """,
        league_id,
        season,
        fa_day,
        player_id,
    )
    return [dict(r) for r in rows]


async def get_offers_for_team(
    pool: asyncpg.Pool,
    league_id: int,
    season: int,
    team_id: int,
) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT * FROM fa_offers
        WHERE league_id = $1 AND season = $2 AND team_id = $3
        ORDER BY submitted_at DESC
        """,
        league_id,
        season,
        team_id,
    )
    return [dict(r) for r in rows]


async def count_offers_today(
    pool: asyncpg.Pool,
    league_id: int,
    season: int,
    fa_day: int,
    team_id: int,
) -> int:
    count = await pool.fetchval(
        """
        SELECT COUNT(*) FROM fa_offers
        WHERE league_id = $1 AND season = $2 AND fa_day = $3 AND team_id = $4
        """,
        league_id,
        season,
        fa_day,
        team_id,
    )
    return int(count)


async def update_offer_status(
    pool: asyncpg.Pool, offer_id: int, status: str
) -> None:
    await pool.execute(
        "UPDATE fa_offers SET status = $1 WHERE id = $2",
        status,
        offer_id,
    )


async def create_counter(
    pool: asyncpg.Pool, offer_id: int, salary: int, years: int
) -> int:
    counter_id = await pool.fetchval(
        """
        INSERT INTO fa_counters (original_offer_id, salary_per_year, years, team_response)
        VALUES ($1, $2, $3, 'pending')
        RETURNING id
        """,
        offer_id,
        salary,
        years,
    )
    return int(counter_id)


async def get_pending_counter(
    pool: asyncpg.Pool, offer_id: int
) -> Optional[dict]:
    row = await pool.fetchrow(
        """
        SELECT * FROM fa_counters
        WHERE original_offer_id = $1 AND team_response = 'pending'
        LIMIT 1
        """,
        offer_id,
    )
    return dict(row) if row else None


async def update_counter_response(
    pool: asyncpg.Pool, counter_id: int, response: str
) -> None:
    await pool.execute(
        "UPDATE fa_counters SET team_response = $1, responded_at = NOW() WHERE id = $2",
        response,
        counter_id,
    )


async def get_unsigned_players(
    pool: asyncpg.Pool, league_id: int
) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT * FROM players
        WHERE league_id = $1 AND roster_status = 'free_agent'
        ORDER BY overall DESC
        """,
        league_id,
    )
    return [dict(r) for r in rows]


async def get_waiver_claims_for_player(
    pool: asyncpg.Pool, league_id: int, player_id: int
) -> list[dict]:
    """
    Returns existing waiver claims for this player, with the claiming team's
    current record, sorted by worst record first (lowest wins = highest priority).
    """
    rows = await pool.fetch(
        """
        SELECT
            wc.team_id,
            COALESCE(sc.wins, 0)   AS wins,
            COALESCE(sc.losses, 0) AS losses
        FROM waiver_claims wc
        LEFT JOIN standings_cache sc
            ON sc.team_id = wc.team_id AND sc.league_id = wc.league_id
        WHERE wc.league_id = $1 AND wc.player_id = $2 AND wc.status = 'pending'
        ORDER BY COALESCE(sc.wins, 0) ASC, COALESCE(sc.losses, 0) DESC
        """,
        league_id,
        player_id,
    )
    return [dict(r) for r in rows]


async def add_waiver_claim(
    pool: asyncpg.Pool, league_id: int, player_id: int, team_id: int
) -> None:
    """Record a team's waiver claim for a player. No-op if the claim already exists."""
    await pool.execute(
        """
        INSERT INTO waiver_claims (league_id, player_id, team_id, status)
        VALUES ($1, $2, $3, 'pending')
        ON CONFLICT (league_id, player_id, team_id) DO NOTHING
        """,
        league_id,
        player_id,
        team_id,
    )


async def get_cpu_teams(pool: asyncpg.Pool, league_id: int) -> list[dict]:
    """Returns CPU-controlled teams (manager_user_id IS NULL) with current cap usage.

    Real-bug fix (found during the FA1-FA4 realism pass, 2026-07-25): the
    previous version LEFT JOINed both `contracts` and `players` in the same
    query, matched only on team_id/league_id (not on a shared key between the
    two joined tables). That's a classic join fan-out — N active contracts x
    M active players produces N*M joined rows per team before the GROUP BY,
    so both COALESCE(SUM(c.salary)) and COUNT(p.id) were inflated by the
    *other* table's row count whenever a team had more than one of each
    (i.e. any normal, non-trivial CPU roster). In practice this meant
    active_player_count usually exceeded _ROSTER_MAX well before a real
    15-player roster filled up, and cap_used was wildly overcounted, so CPU
    teams silently stopped submitting offers long before they should have.
    Independent correlated subqueries avoid the fan-out entirely.
    """
    rows = await pool.fetch(
        """
        SELECT
            t.id,
            t.cpu_mode,
            COALESCE(
                (SELECT SUM(c.salary) FROM contracts c
                 WHERE c.team_id = t.id AND c.league_id = t.league_id AND c.is_active = TRUE),
                0
            ) AS cap_used,
            COALESCE(
                (SELECT COUNT(*) FROM players p
                 WHERE p.team_id = t.id AND p.league_id = t.league_id AND p.roster_status = 'active'),
                0
            ) AS active_player_count
        FROM teams t
        WHERE t.league_id = $1
          AND t.manager_user_id IS NULL
        """,
        league_id,
    )
    return [dict(r) for r in rows]
