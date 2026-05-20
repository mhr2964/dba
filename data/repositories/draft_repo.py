from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import List, Optional

import asyncpg


@dataclass
class Draft:
    id: int
    league_id: int
    season: int
    lottery_completed_at: Optional[datetime.datetime]
    current_pick_number: int
    status: str


@dataclass
class DraftSelection:
    id: int
    draft_id: int
    pick_number: int
    round: int
    team_id: int
    player_id: int
    pick_asset_id: Optional[int]
    selected_at: datetime.datetime


@dataclass
class DraftClass:
    id: int
    league_id: int
    class_year: int
    player_id: int
    mock_rank: Optional[int]
    is_generated: bool
    source: Optional[str]


def _draft_from_record(r: asyncpg.Record) -> Draft:
    return Draft(
        id=r["id"],
        league_id=r["league_id"],
        season=r["season"],
        lottery_completed_at=r["lottery_completed_at"],
        current_pick_number=r["current_pick_number"],
        status=r["status"],
    )


def _selection_from_record(r: asyncpg.Record) -> DraftSelection:
    return DraftSelection(
        id=r["id"],
        draft_id=r["draft_id"],
        pick_number=r["pick_number"],
        round=r["round"],
        team_id=r["team_id"],
        player_id=r["player_id"],
        pick_asset_id=r["pick_asset_id"],
        selected_at=r["selected_at"],
    )


async def create_draft(pool: asyncpg.Pool, league_id: int, season: int) -> Draft:
    row = await pool.fetchrow(
        """
        INSERT INTO drafts (league_id, season)
        VALUES ($1, $2)
        ON CONFLICT (league_id, season) DO UPDATE
            SET status = drafts.status
        RETURNING *
        """,
        league_id,
        season,
    )
    return _draft_from_record(row)


async def get_draft(pool: asyncpg.Pool, league_id: int, season: int) -> Optional[Draft]:
    row = await pool.fetchrow(
        "SELECT * FROM drafts WHERE league_id = $1 AND season = $2",
        league_id,
        season,
    )
    return _draft_from_record(row) if row else None


async def update_draft_status(
    pool: asyncpg.Pool,
    draft_id: int,
    status: str,
    current_pick: Optional[int] = None,
) -> None:
    if current_pick is not None:
        await pool.execute(
            "UPDATE drafts SET status = $1, current_pick_number = $2 WHERE id = $3",
            status,
            current_pick,
            draft_id,
        )
    else:
        await pool.execute(
            "UPDATE drafts SET status = $1 WHERE id = $2",
            status,
            draft_id,
        )


async def get_available_prospects(
    pool: asyncpg.Pool, league_id: int, class_year: int
) -> List[dict]:
    """Returns players in draft_classes not yet selected in any draft for this league/year."""
    rows = await pool.fetch(
        """
        SELECT p.id, p.first_name, p.last_name, p.position, p.overall,
               p.speed, p.shooting_2pt, p.shooting_3pt, p.shooting_mid,
               p.finishing, p.playmaking, p.defense, p.rebounding, p.iq,
               dc.mock_rank
        FROM draft_classes dc
        JOIN players p ON p.id = dc.player_id
        WHERE dc.league_id = $1
          AND dc.class_year = $2
          AND NOT EXISTS (
              SELECT 1
              FROM draft_selections ds
              JOIN drafts d ON d.id = ds.draft_id
              WHERE d.league_id = $1
                AND d.season = $2
                AND ds.player_id = p.id
          )
        ORDER BY p.overall DESC, dc.mock_rank ASC NULLS LAST
        """,
        league_id,
        class_year,
    )
    return [dict(r) for r in rows]


async def record_selection(
    pool: asyncpg.Pool,
    draft_id: int,
    pick_number: int,
    round: int,
    team_id: int,
    player_id: int,
    pick_asset_id: Optional[int] = None,
) -> DraftSelection:
    row = await pool.fetchrow(
        """
        INSERT INTO draft_selections
            (draft_id, pick_number, round, team_id, player_id, pick_asset_id)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING *
        """,
        draft_id,
        pick_number,
        round,
        team_id,
        player_id,
        pick_asset_id,
    )
    return _selection_from_record(row)


async def get_selections(pool: asyncpg.Pool, draft_id: int) -> List[DraftSelection]:
    rows = await pool.fetch(
        "SELECT * FROM draft_selections WHERE draft_id = $1 ORDER BY pick_number",
        draft_id,
    )
    return [_selection_from_record(r) for r in rows]


async def get_on_the_clock_team(
    pool: asyncpg.Pool, draft_id: int, pick_number: int
) -> Optional[int]:
    """
    Resolves which team owns this pick slot by looking at draft_picks.current_team_id,
    ordered by pick_number for the draft's league and season.
    Returns the team_id that should be making this pick.
    """
    row = await pool.fetchrow(
        """
        SELECT dp.current_team_id
        FROM draft_picks dp
        JOIN drafts d ON d.league_id = dp.league_id AND d.season = dp.season
        WHERE d.id = $1
          AND dp.pick_number = $2
        LIMIT 1
        """,
        draft_id,
        pick_number,
    )
    return row["current_team_id"] if row else None


async def seed_draft_class(
    pool: asyncpg.Pool,
    league_id: int,
    class_year: int,
    prospects: list[dict],
    source: str = "generated",
) -> int:
    """
    Inserts each prospect into players (roster_status='prospect') then into draft_classes.
    Skips prospects that would violate the UNIQUE(league_id, class_year, player_id) constraint.
    Returns count of newly inserted entries.

    source: value written to draft_classes.source — callers pass 'nba_real',
            'projected_consensus', or leave default 'generated'.
    """
    inserted = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for p in prospects:
                player_id = await conn.fetchval(
                    """
                    INSERT INTO players (
                        league_id, external_id, first_name, last_name, position,
                        height_in, weight_lb, birth_date, years_pro, is_rookie,
                        team_id, roster_status,
                        overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
                        finishing, playmaking, defense, rebounding, iq,
                        potential, peak_age_start, peak_age_end,
                        loyalty, money_drive, win_drive,
                        market_pref, star_leverage
                    ) VALUES (
                        $1, $2, $3, $4, $5,
                        $6, $7, $8, $9, $10,
                        NULL, 'prospect',
                        $11, $12, $13, $14, $15,
                        $16, $17, $18, $19, $20,
                        $21, $22, $23,
                        $24, $25, $26,
                        $27, $28
                    )
                    RETURNING id
                    """,
                    league_id,
                    p.get("external_id"),
                    p["first_name"],
                    p["last_name"],
                    p["position"],
                    p.get("height_in"),
                    p.get("weight_lb"),
                    p.get("birth_date"),
                    p.get("years_pro", 0),
                    p.get("is_rookie", True),
                    p["overall"],
                    p["speed"],
                    p["shooting_2pt"],
                    p["shooting_3pt"],
                    p["shooting_mid"],
                    p["finishing"],
                    p["playmaking"],
                    p["defense"],
                    p["rebounding"],
                    p["iq"],
                    p["potential"],
                    p["peak_age_start"],
                    p["peak_age_end"],
                    p["loyalty"],
                    p["money_drive"],
                    p["win_drive"],
                    p.get("market_pref", "neutral"),
                    p.get("star_leverage", 30),
                )

                await conn.execute(
                    """
                    INSERT INTO draft_classes (league_id, class_year, player_id, mock_rank, is_generated, source)
                    VALUES ($1, $2, $3, $4, TRUE, $5)
                    ON CONFLICT (league_id, class_year, player_id) DO NOTHING
                    """,
                    league_id,
                    class_year,
                    player_id,
                    p.get("_mock_rank"),
                    source,
                )
                inserted += 1

    return inserted
