from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import List, Optional

import asyncpg


@dataclass
class Trade:
    id: int
    league_id: int
    season: int
    proposer_team_id: int
    counterparty_team_id: int
    status: str
    cpu_evaluator_score: Optional[float]
    cpu_rationale: Optional[str]
    commissioner_action_by: Optional[int]
    commissioner_reason: Optional[str]
    proposed_at: datetime.datetime
    resolved_at: Optional[datetime.datetime]


@dataclass
class TradeAsset:
    id: int
    trade_id: int
    from_team_id: int
    to_team_id: int
    asset_type: str
    player_id: Optional[int]
    pick_id: Optional[int]


def _trade_from_record(r: asyncpg.Record) -> Trade:
    return Trade(
        id=r["id"],
        league_id=r["league_id"],
        season=r["season"],
        proposer_team_id=r["proposer_team_id"],
        counterparty_team_id=r["counterparty_team_id"],
        status=r["status"],
        cpu_evaluator_score=r["cpu_evaluator_score"],
        cpu_rationale=r["cpu_rationale"],
        commissioner_action_by=r["commissioner_action_by"],
        commissioner_reason=r["commissioner_reason"],
        proposed_at=r["proposed_at"],
        resolved_at=r["resolved_at"],
    )


def _asset_from_record(r: asyncpg.Record) -> TradeAsset:
    return TradeAsset(
        id=r["id"],
        trade_id=r["trade_id"],
        from_team_id=r["from_team_id"],
        to_team_id=r["to_team_id"],
        asset_type=r["asset_type"],
        player_id=r["player_id"],
        pick_id=r["pick_id"],
    )


async def create_trade(
    pool: asyncpg.Pool,
    league_id: int,
    season: int,
    proposer_id: int,
    counterparty_id: int,
) -> Trade:
    row = await pool.fetchrow(
        """
        INSERT INTO trades (league_id, season, proposer_team_id, counterparty_team_id)
        VALUES ($1, $2, $3, $4)
        RETURNING *
        """,
        league_id,
        season,
        proposer_id,
        counterparty_id,
    )
    return _trade_from_record(row)


async def add_asset(
    pool: asyncpg.Pool,
    trade_id: int,
    from_team_id: int,
    to_team_id: int,
    asset_type: str,
    player_id: Optional[int] = None,
    pick_id: Optional[int] = None,
) -> TradeAsset:
    row = await pool.fetchrow(
        """
        INSERT INTO trade_assets (trade_id, from_team_id, to_team_id, asset_type, player_id, pick_id)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING *
        """,
        trade_id,
        from_team_id,
        to_team_id,
        asset_type,
        player_id,
        pick_id,
    )
    return _asset_from_record(row)


async def get_trade(pool: asyncpg.Pool, trade_id: int) -> Optional[Trade]:
    row = await pool.fetchrow("SELECT * FROM trades WHERE id = $1", trade_id)
    return _trade_from_record(row) if row else None


async def get_pending_for_league(pool: asyncpg.Pool, league_id: int) -> List[Trade]:
    rows = await pool.fetch(
        "SELECT * FROM trades WHERE league_id = $1 AND status = 'pending_commissioner' ORDER BY proposed_at",
        league_id,
    )
    return [_trade_from_record(r) for r in rows]


async def get_active_outgoing(pool: asyncpg.Pool, team_id: int) -> List[Trade]:
    rows = await pool.fetch(
        """
        SELECT * FROM trades
        WHERE proposer_team_id = $1
          AND status IN ('pending_counterparty', 'pending_commissioner')
        ORDER BY proposed_at
        """,
        team_id,
    )
    return [_trade_from_record(r) for r in rows]


async def get_pending_for_team(pool: asyncpg.Pool, team_id: int) -> List[Trade]:
    rows = await pool.fetch(
        """
        SELECT * FROM trades
        WHERE counterparty_team_id = $1 AND status = 'pending_counterparty'
        ORDER BY proposed_at
        """,
        team_id,
    )
    return [_trade_from_record(r) for r in rows]


async def get_assets(pool: asyncpg.Pool, trade_id: int) -> List[TradeAsset]:
    rows = await pool.fetch(
        "SELECT * FROM trade_assets WHERE trade_id = $1",
        trade_id,
    )
    return [_asset_from_record(r) for r in rows]


async def update_status(
    pool: asyncpg.Pool,
    trade_id: int,
    status: str,
    commissioner_by: Optional[int] = None,
    reason: Optional[str] = None,
) -> None:
    resolved_statuses = {"approved", "vetoed", "declined", "rejected", "expired"}
    if status in resolved_statuses:
        await pool.execute(
            """
            UPDATE trades
            SET status = $1,
                commissioner_action_by = COALESCE($2, commissioner_action_by),
                commissioner_reason = COALESCE($3, commissioner_reason),
                resolved_at = NOW()
            WHERE id = $4
            """,
            status,
            commissioner_by,
            reason,
            trade_id,
        )
    else:
        await pool.execute(
            """
            UPDATE trades
            SET status = $1,
                commissioner_action_by = COALESCE($2, commissioner_action_by),
                commissioner_reason = COALESCE($3, commissioner_reason)
            WHERE id = $4
            """,
            status,
            commissioner_by,
            reason,
            trade_id,
        )


async def set_cpu_evaluation(
    pool: asyncpg.Pool,
    trade_id: int,
    score: float,
    rationale: str,
) -> None:
    await pool.execute(
        "UPDATE trades SET cpu_evaluator_score = $1, cpu_rationale = $2 WHERE id = $3",
        score,
        rationale,
        trade_id,
    )


async def count_pending_commissioner(pool: asyncpg.Pool, league_id: int) -> int:
    result = await pool.fetchval(
        "SELECT COUNT(*) FROM trades WHERE league_id = $1 AND status = 'pending_commissioner'",
        league_id,
    )
    return int(result)


async def get_team_picks(pool: asyncpg.Pool, league_id: int, team_id: int) -> List[dict]:
    rows = await pool.fetch(
        """
        SELECT * FROM draft_picks
        WHERE league_id = $1 AND current_team_id = $2 AND used_for_player_id IS NULL
        ORDER BY season, round
        """,
        league_id,
        team_id,
    )
    return [dict(r) for r in rows]


async def transfer_pick(pool: asyncpg.Pool, pick_id: int, to_team_id: int) -> None:
    await pool.execute(
        "UPDATE draft_picks SET current_team_id = $1 WHERE id = $2",
        to_team_id,
        pick_id,
    )


async def get_team_trade_history(
    pool: asyncpg.Pool, league_id: int, team_id: int, limit: int = 10
) -> list[dict]:
    """
    Approved trades where this team was either proposer or counterparty,
    newest first. Each item carries the trade, its assets, and the other team's id.
    """
    rows = await pool.fetch(
        """
        SELECT *
        FROM trades
        WHERE league_id = $1
          AND status = 'approved'
          AND (proposer_team_id = $2 OR counterparty_team_id = $2)
        ORDER BY resolved_at DESC
        LIMIT $3
        """,
        league_id,
        team_id,
        limit,
    )
    result: list[dict] = []
    for row in rows:
        trade = _trade_from_record(row)
        assets = await pool.fetch(
            "SELECT * FROM trade_assets WHERE trade_id = $1", trade.id
        )
        other_team_id = (
            trade.counterparty_team_id
            if trade.proposer_team_id == team_id
            else trade.proposer_team_id
        )
        result.append(
            {
                "trade": trade,
                "assets": [_asset_from_record(a) for a in assets],
                "other_team_id": other_team_id,
            }
        )
    return result


async def get_all_approved_trades(
    pool: asyncpg.Pool, league_id: int, season: Optional[int] = None
) -> list[dict]:
    """All approved trades for a league, optionally filtered to one season."""
    if season is not None:
        rows = await pool.fetch(
            """
            SELECT * FROM trades
            WHERE league_id = $1 AND status = 'approved' AND season = $2
            ORDER BY resolved_at DESC
            """,
            league_id,
            season,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT * FROM trades
            WHERE league_id = $1 AND status = 'approved'
            ORDER BY resolved_at DESC
            """,
            league_id,
        )
    result: list[dict] = []
    for row in rows:
        trade = _trade_from_record(row)
        assets = await pool.fetch(
            "SELECT * FROM trade_assets WHERE trade_id = $1", trade.id
        )
        result.append(
            {
                "trade": trade,
                "assets": [_asset_from_record(a) for a in assets],
            }
        )
    return result


async def seed_picks_for_league(
    pool: asyncpg.Pool,
    league_id: int,
    from_season: int,
    num_seasons: int = 7,
) -> None:
    team_ids = await pool.fetch(
        "SELECT id FROM teams WHERE league_id = $1",
        league_id,
    )
    records = []
    for row in team_ids:
        team_id = row["id"]
        for season_offset in range(num_seasons):
            season = from_season + season_offset
            for round_num in (1, 2):
                records.append((league_id, season, round_num, team_id, team_id))

    await pool.executemany(
        """
        INSERT INTO draft_picks (league_id, season, round, original_team_id, current_team_id)
        VALUES ($1, $2, $3, $4, $5)
        """,
        records,
    )
