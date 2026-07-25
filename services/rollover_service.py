from __future__ import annotations

from typing import Optional

import asyncpg

from core.errors import DBAError
from core.logging import get_logger
from data.db import get_pool
from data.repositories import extension_repo, history_repo, trade_repo
from phase.states import Phase
from services import hof_service

log = get_logger(__name__)


async def run_rollover(league_id: int) -> dict:
    """
    Full season rollover. Returns summary dict with keys:
      season_archived, next_season, players_progressed, contracts_expired
    Steps run inside a single connection but individual statements are not
    wrapped in one transaction — history and contract aging are idempotent
    enough that partial retries are safe, and progression is an external call.
    """
    pool = await get_pool()

    league_row = await pool.fetchrow(
        "SELECT current_season FROM leagues WHERE id = $1", league_id
    )
    if league_row is None:
        raise DBAError(f"League {league_id} not found.")

    season = league_row["current_season"]
    next_season = season + 1

    await _record_history(pool, league_id, season)

    # Process pending extensions whose current contract ends this season before aging contracts.
    extensions_activated = await extension_repo.process_extensions_for_season(pool, league_id, season)

    contracts_expired = await _age_contracts(pool, league_id)

    await _reset_game_state(pool, league_id, next_season)

    await pool.execute(
        "UPDATE leagues SET current_season = $1 WHERE id = $2",
        next_season,
        league_id,
    )

    # Set phase to PROGRESSION_PENDING and stash the pre-increment season in the
    # same statement — pending_progression_season is what /offseason progression
    # must filter games/injuries on, since current_season above has already moved
    # past the season that just finished (see progression_service._avg_minutes /
    # _has_season_ending_injury callers).
    await pool.execute(
        "UPDATE leagues SET current_phase = $1, pending_progression_season = $2 WHERE id = $3",
        Phase.PROGRESSION_PENDING.value,
        season,
        league_id,
    )

    # Seed the new frontier draft season so the 7-season pick window rolls forward.
    await trade_repo.seed_picks_for_league(pool, league_id, next_season + 6, num_seasons=1)

    inducted = await hof_service.check_and_induct(pool, league_id, next_season)

    log.info(
        f"Rollover complete: league={league_id} season={season}->{next_season} "
        f"expired={contracts_expired} "
        f"extensions_activated={extensions_activated} picks_seeded=1 hof_inducted={len(inducted)}"
    )

    return {
        "season_archived": season,
        "next_season": next_season,
        "contracts_expired": contracts_expired,
        "extensions_activated": extensions_activated,
        "picks_seeded": 1,
        "hof_inducted": inducted,
    }


async def _record_history(pool: asyncpg.Pool, league_id: int, season: int) -> None:
    """
    Read award_results to find MVP (place=1 for award_type='mvp').
    Read series to find champion (NBA Finals winner_team_id).
    Insert or update history_seasons row.
    """
    mvp_player_id: Optional[int] = await pool.fetchval(
        """
        SELECT ar.player_id
        FROM award_results ar
        JOIN award_votings av ON av.id = ar.voting_id
        WHERE av.league_id = $1
          AND av.season    = $2
          AND av.award_type = 'mvp'
          AND ar.place = 1
        LIMIT 1
        """,
        league_id,
        season,
    )

    finals_mvp_player_id: Optional[int] = await pool.fetchval(
        """
        SELECT ar.player_id
        FROM award_results ar
        JOIN award_votings av ON av.id = ar.voting_id
        WHERE av.league_id = $1
          AND av.season    = $2
          AND av.award_type = 'finals_mvp'
          AND ar.place = 1
        LIMIT 1
        """,
        league_id,
        season,
    )

    champion_team_id: Optional[int] = await pool.fetchval(
        """
        SELECT winner_team_id
        FROM series
        WHERE league_id = $1
          AND season    = $2
          AND round     = 'nba_finals'
          AND status    = 'complete'
        LIMIT 1
        """,
        league_id,
        season,
    )

    # Best regular-season record: most wins, most losses
    wins_leader_id: Optional[int] = await pool.fetchval(
        """
        SELECT team_id
        FROM standings_cache
        WHERE league_id = $1 AND season = $2
        ORDER BY wins DESC
        LIMIT 1
        """,
        league_id,
        season,
    )

    losses_leader_id: Optional[int] = await pool.fetchval(
        """
        SELECT team_id
        FROM standings_cache
        WHERE league_id = $1 AND season = $2
        ORDER BY losses DESC
        LIMIT 1
        """,
        league_id,
        season,
    )

    await history_repo.record_season(
        pool,
        league_id=league_id,
        season=season,
        champion_team_id=champion_team_id,
        mvp_player_id=mvp_player_id,
        finals_mvp_player_id=finals_mvp_player_id,
        regular_season_wins_leader_id=wins_leader_id,
        regular_season_losses_leader_id=losses_leader_id,
    )


async def _age_contracts(pool: asyncpg.Pool, league_id: int) -> int:
    """
    Decrement years_remaining for every active contract in this league.
    Contracts that reach 0 are deactivated and the player becomes a free agent.
    Returns count of expired contracts.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Decrement all active contracts by 1 year, capture which hit 0.
            expired_rows = await conn.fetch(
                """
                UPDATE contracts
                SET years_remaining = years_remaining - 1
                WHERE league_id = $1
                  AND is_active = TRUE
                RETURNING id, player_id, years_remaining
                """,
                league_id,
            )

            expired_player_ids = [
                r["player_id"] for r in expired_rows if r["years_remaining"] == 0
            ]

            if expired_player_ids:
                await conn.execute(
                    """
                    UPDATE contracts
                    SET is_active = FALSE
                    WHERE league_id = $1
                      AND player_id = ANY($2::int[])
                      AND years_remaining = 0
                    """,
                    league_id,
                    expired_player_ids,
                )

                await conn.execute(
                    """
                    UPDATE players
                    SET roster_status = 'free_agent',
                        team_id       = NULL
                    WHERE league_id = $1
                      AND id = ANY($2::int[])
                    """,
                    league_id,
                    expired_player_ids,
                )

    return len(expired_player_ids)


async def _reset_game_state(
    pool: asyncpg.Pool, league_id: int, new_season: int
) -> None:
    """
    Clear standings_cache and ready_status for this league.
    Games and box_scores are kept as historical data.
    """
    await pool.execute(
        "DELETE FROM standings_cache WHERE league_id = $1",
        league_id,
    )
    await pool.execute(
        "DELETE FROM ready_status WHERE league_id = $1",
        league_id,
    )
