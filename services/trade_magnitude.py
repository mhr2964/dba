"""Trade magnitude comparator (Phase 1 fix D2).

Answers "how big is this trade, ranked against history" so a persona (Marcus
Cole, Phase 2's job to wire in) can truthfully say "this is the biggest trade
in franchise history" or "the third-largest deal since season 1" instead of
guessing.

Magnitude = sum of trade_value_math.player_trade_value for every player asset
in the trade (using the player's CURRENT contract state, or a neutral 1-year
league-average-ish contract when no active contract can be resolved — e.g.
the player has since been waived or re-signed) + trade_value_math.
pick_trade_value for every pick asset. This is deliberately an approximation
using CURRENT state, not a snapshot of value-at-trade-time: good enough for
"biggest trade ever" bragging-rights comparisons, not for auditing whether a
historical trade was fair when it actually happened.

Not wired into any persona's prompt here — that's Phase 2's job. This module
just makes "how big was this trade, ranked" reliably computable.
"""
from __future__ import annotations

import datetime
from typing import Optional

import asyncpg

from data.repositories import trade_repo
from services import trade_value_math

# Substituted when a player asset's contract can no longer be resolved (e.g.
# waived, re-signed on a new deal since the trade). A modest 1-year deal so
# an unresolvable contract doesn't swing the magnitude score wildly either
# direction — it's a neutral placeholder, not a real historical value.
_NEUTRAL_SALARY = 4_000_000
_NEUTRAL_YEARS_REMAINING = 1
_NEUTRAL_AGE = 28


def _age_from_birth_date(birth_date: Optional[datetime.date]) -> int:
    if birth_date is None:
        return _NEUTRAL_AGE
    today = datetime.date.today()
    return today.year - birth_date.year - ((today.month, today.day) < (birth_date.month, birth_date.day))


async def _player_asset_value(pool: asyncpg.Pool, player_id: int, salary_cap: int) -> float:
    """Current market value of one player asset (see module docstring for the
    current-state-not-snapshot caveat). Returns 0.0 if the player no longer
    exists (deleted/bad data) rather than raising."""
    row = await pool.fetchrow(
        """
        SELECT p.overall, p.birth_date, p.position, p.star_leverage, p.defense,
               c.salary, c.years_remaining
        FROM players p
        LEFT JOIN contracts c ON c.player_id = p.id AND c.is_active = true
        WHERE p.id = $1
        """,
        player_id,
    )
    if row is None:
        return 0.0

    player = {
        "overall": row["overall"] or 70,
        "age": _age_from_birth_date(row["birth_date"]),
        "position": row["position"] or "",
        "star_leverage": row["star_leverage"],
        "defense": row["defense"],
    }
    contract = {
        "salary": row["salary"] if row["salary"] is not None else _NEUTRAL_SALARY,
        "years_remaining": (
            row["years_remaining"] if row["years_remaining"] is not None else _NEUTRAL_YEARS_REMAINING
        ),
    }
    return trade_value_math.player_trade_value(player, contract, salary_cap)


async def _pick_asset_value(pool: asyncpg.Pool, pick_id: int, current_season: int) -> float:
    """Current value of one draft-pick asset. Returns 0.0 if the pick no
    longer exists rather than raising."""
    row = await pool.fetchrow(
        "SELECT season, round FROM draft_picks WHERE id = $1", pick_id,
    )
    if row is None:
        return 0.0
    return trade_value_math.pick_trade_value(row["season"], row["round"], current_season)


async def compute_trade_magnitude(
    pool: asyncpg.Pool,
    trade_id: int,
    salary_cap: int,
    current_season: int,
) -> float:
    """Total asset value moved in one trade, both sides combined."""
    assets = await trade_repo.get_assets(pool, trade_id)
    total = 0.0
    for asset in assets:
        if asset.asset_type == "player" and asset.player_id:
            total += await _player_asset_value(pool, asset.player_id, salary_cap)
        elif asset.asset_type == "pick" and asset.pick_id:
            total += await _pick_asset_value(pool, asset.pick_id, current_season)
    return round(total, 2)


async def _rank_within(
    pool: asyncpg.Pool,
    trade_id: int,
    history: list[dict],
    salary_cap: int,
    current_season: int,
) -> Optional[dict]:
    """Shared ranking logic for both team- and league-scoped comparisons.

    `history` is a list of {"trade": Trade, ...} dicts, as returned by
    trade_repo.get_team_trade_history / get_all_approved_trades.
    Returns None if trade_id isn't present in `history`.
    """
    trade_ids = [entry["trade"].id for entry in history]
    if trade_id not in trade_ids:
        return None

    magnitudes: dict[int, float] = {}
    for tid in trade_ids:
        magnitudes[tid] = await compute_trade_magnitude(pool, tid, salary_cap, current_season)

    ordered = sorted(magnitudes.items(), key=lambda kv: kv[1], reverse=True)
    rank = next(i for i, (tid, _) in enumerate(ordered, start=1) if tid == trade_id)

    return {
        "rank": rank,
        "total_trades": len(ordered),
        "magnitude": magnitudes[trade_id],
        "is_biggest": rank == 1,
    }


async def rank_trade_in_team_history(
    pool: asyncpg.Pool,
    league_id: int,
    team_id: int,
    trade_id: int,
    salary_cap: int,
    current_season: int,
) -> Optional[dict]:
    """Rank trade_id by magnitude among team_id's approved trade history.

    Returns None if trade_id isn't in the team's approved trade history (e.g.
    a bad trade_id, or the trade hasn't been approved yet). Otherwise returns
    {"rank": int, "total_trades": int, "magnitude": float, "is_biggest": bool}
    — rank=1 is the biggest trade in the team's history.
    """
    history = await trade_repo.get_team_trade_history(pool, league_id, team_id, limit=1000)
    return await _rank_within(pool, trade_id, history, salary_cap, current_season)


async def rank_trade_in_league_history(
    pool: asyncpg.Pool,
    league_id: int,
    trade_id: int,
    salary_cap: int,
    current_season: int,
    season: Optional[int] = None,
) -> Optional[dict]:
    """Rank trade_id by magnitude among the whole league's approved trade
    history (optionally scoped to a single `season`).

    Same return shape as rank_trade_in_team_history; None if trade_id isn't
    found in the scoped history.
    """
    history = await trade_repo.get_all_approved_trades(pool, league_id, season=season)
    return await _rank_within(pool, trade_id, history, salary_cap, current_season)
