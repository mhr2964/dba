"""Thin CRUD wrappers for the franchise_plans table.

The service layer owns all derivation logic.  This file handles only
parameterised SQL so the service stays readable.
"""
from __future__ import annotations

import json
from typing import Optional

_JSONB_COLS = ("core_player_ids", "flex_player_ids", "surplus_player_ids",
               "asset_targets", "derived_from_record")

# Columns that are integers but might arrive as None from the DB.
_INT_COLS = ("last_derived_game_index",)


def _parse_plan(raw: dict) -> dict:
    """asyncpg returns JSONB columns as raw strings; decode them to Python objects."""
    out = dict(raw)
    for col in _JSONB_COLS:
        if col in out and isinstance(out[col], str):
            out[col] = json.loads(out[col])
    # Integer columns: normalise None → None (already correct; explicit for clarity).
    for col in _INT_COLS:
        if col in out and out[col] is not None:
            out[col] = int(out[col])
    return out


async def get_plan(pool, league_id: int, team_id: int, season: int) -> Optional[dict]:
    """Return the stored plan or None if it has not been derived yet."""
    row = await pool.fetchrow(
        """
        SELECT * FROM franchise_plans
        WHERE league_id = $1 AND team_id = $2 AND season = $3
        """,
        league_id,
        team_id,
        season,
    )
    return _parse_plan(dict(row)) if row else None


async def upsert_plan(pool, plan: dict) -> int:
    """Insert or update the plan.  Returns the persisted plan id."""
    row = await pool.fetchrow(
        """
        INSERT INTO franchise_plans
            (league_id, team_id, season, goal, horizon_seasons,
             core_player_ids, flex_player_ids, surplus_player_ids,
             asset_targets, rationale, derived_at, derived_from_record,
             last_derived_game_index)
        VALUES ($1, $2, $3, $4, $5,
                $6::jsonb, $7::jsonb, $8::jsonb,
                $9::jsonb, $10, NOW(), $11::jsonb,
                $12)
        ON CONFLICT (league_id, team_id, season) DO UPDATE SET
            goal                     = EXCLUDED.goal,
            horizon_seasons          = EXCLUDED.horizon_seasons,
            core_player_ids          = EXCLUDED.core_player_ids,
            flex_player_ids          = EXCLUDED.flex_player_ids,
            surplus_player_ids       = EXCLUDED.surplus_player_ids,
            asset_targets            = EXCLUDED.asset_targets,
            rationale                = EXCLUDED.rationale,
            derived_at               = NOW(),
            derived_from_record      = EXCLUDED.derived_from_record,
            last_derived_game_index  = EXCLUDED.last_derived_game_index
        RETURNING id
        """,
        plan["league_id"],
        plan["team_id"],
        plan["season"],
        plan["goal"],
        plan["horizon_seasons"],
        json.dumps(plan["core_player_ids"]),
        json.dumps(plan["flex_player_ids"]),
        json.dumps(plan["surplus_player_ids"]),
        json.dumps(plan["asset_targets"]),
        plan["rationale"],
        json.dumps(plan.get("derived_from_record", {})),
        plan.get("last_derived_game_index"),  # may be None for offseason derives
    )
    return row["id"]


async def get_all_for_season(pool, league_id: int, season: int) -> list[dict]:
    """Return all plans for every team in a league for one season."""
    rows = await pool.fetch(
        """
        SELECT fp.*, t.nba_team_code
        FROM franchise_plans fp
        JOIN teams t ON t.id = fp.team_id
        WHERE fp.league_id = $1 AND fp.season = $2
        ORDER BY t.nba_team_code
        """,
        league_id,
        season,
    )
    return [_parse_plan(dict(r)) for r in rows]
