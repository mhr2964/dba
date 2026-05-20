"""Repository helpers for the roster-year reconciler (Slice 3).

All functions operate directly on the asyncpg pool — no ORM.  Results are
returned as plain dicts so the reconciler can manipulate them without coupling
to Player dataclass field ordering.

seed_source domain:
    'seed'  — row came from a season seed CSV/JSON file
    'api'   — row was fetched from an external stats API
    'draft' — row was created by the draft system
    'fa'    — row was signed in a free-agent signing
    'trade' — row arrived via a trade (rare; usually the player already exists)
    NULL    — pre-021 row; treat as 'api' at read time
"""
from __future__ import annotations

from typing import Optional

import asyncpg


async def find_player_by_external_id(
    pool: asyncpg.Pool, league_id: int, external_id: int
) -> Optional[dict]:
    """Return the player row for this external_id in this league, or None.

    external_id is stored as TEXT in the schema; the int parameter is cast
    so callers can pass the raw numeric id from the seed file without
    pre-converting.
    """
    row = await pool.fetchrow(
        """
        SELECT * FROM players
        WHERE league_id = $1 AND external_id = $2::TEXT
        LIMIT 1
        """,
        league_id,
        external_id,
    )
    return dict(row) if row else None


async def find_player_by_name(
    pool: asyncpg.Pool, league_id: int, normalized_name: str
) -> Optional[dict]:
    """Fallback lookup by normalized (lowercase, accent-stripped) full name.

    normalized_name should already be lowercased and unaccented by the caller.
    The query applies unaccent() on the DB side as well so both paths agree.
    Returns the first match ordered by id (lowest id wins on duplicates) or None.
    """
    row = await pool.fetchrow(
        """
        SELECT * FROM players
        WHERE league_id = $1
          AND lower(unaccent(first_name || ' ' || last_name)) = lower(unaccent($2))
        ORDER BY id
        LIMIT 1
        """,
        league_id,
        normalized_name,
    )
    return dict(row) if row else None


async def bulk_set_team(
    pool: asyncpg.Pool, league_id: int, player_ids: list[int], team_id: int
) -> None:
    """Move all listed players to team_id in the players table.

    Scoped to league_id so a stale player_id from another league cannot
    accidentally be reassigned.  No-ops gracefully when player_ids is empty.
    """
    if not player_ids:
        return
    await pool.execute(
        """
        UPDATE players
        SET team_id = $1
        WHERE league_id = $2 AND id = ANY($3::INT[])
        """,
        team_id,
        league_id,
        player_ids,
    )


async def mark_players_seeded(
    pool: asyncpg.Pool, player_ids: list[int], season: int
) -> None:
    """Set seed_season and seed_source='seed' for the given player IDs.

    Not scoped by league_id because player IDs are globally unique (SERIAL PK).
    No-ops gracefully when player_ids is empty.
    """
    if not player_ids:
        return
    await pool.execute(
        """
        UPDATE players
        SET seed_season = $1,
            seed_source = 'seed'
        WHERE id = ANY($2::INT[])
        """,
        season,
        player_ids,
    )


async def get_all_players_for_league(
    pool: asyncpg.Pool, league_id: int
) -> list[dict]:
    """Return all player rows for this league as dicts.

    Only the columns the reconciler needs are selected to keep the payload
    small.  seed_source may be NULL for pre-021 rows; callers should treat
    NULL as 'api'.
    """
    rows = await pool.fetch(
        """
        SELECT id,
               external_id,
               first_name,
               last_name,
               team_id,
               seed_source
        FROM players
        WHERE league_id = $1
        ORDER BY id
        """,
        league_id,
    )
    return [dict(r) for r in rows]


async def terminate_contract(
    pool: asyncpg.Pool, league_id: int, player_id: int
) -> None:
    """Soft-deactivate the active contract for this player in this league.

    Uses the same soft-delete pattern as every other contract termination path
    in the codebase (is_active=FALSE + terminated_reason) so audit history is
    preserved.  The reconciler calls this before inserting a replacement
    contract when a seed file supplies a corrected salary/years figure.

    Hard-DELETE is intentionally avoided: contracts are an audit trail.
    """
    await pool.execute(
        """
        UPDATE contracts
        SET is_active = FALSE,
            terminated_reason = 'seed_reconcile'
        WHERE player_id = $1
          AND league_id = $2
          AND is_active = TRUE
        """,
        player_id,
        league_id,
    )
