from __future__ import annotations

from typing import Optional

import asyncpg

from core.errors import DBAError
from core.logging import get_logger
from data.db import get_pool
from data.repositories import extension_repo, history_repo, trade_repo
from phase.states import Phase
from services import league_service

log = get_logger(__name__)

# RO6: disclosed placeholder cap-growth rate. Real NBA cap growth runs roughly
# 5-10%/year, but a smaller flat rate here avoids destabilizing existing
# trade-value formulas that assume a roughly-stable cap.
_SALARY_CAP_GROWTH_RATE = 0.03


async def run_rollover(league_id: int) -> dict:
    """
    Full season rollover. Returns summary dict with keys:
      season_archived, next_season, contracts_expired, extensions_activated,
      picks_seeded, new_salary_cap
    Steps run inside a single connection but individual statements are not
    wrapped in one transaction — history and contract aging are idempotent
    enough that partial retries are safe, and progression is an external call.

    Hall of Fame induction (hof_service.check_and_induct) intentionally does
    NOT run here (RO3) — it now runs from the /offseason progression command
    handler, after progression_service.run_progression has updated years_pro/
    retirement state for the season. Running it here evaluated every
    candidate against stale, pre-progression state.
    """
    pool = await get_pool()

    league_row = await pool.fetchrow(
        "SELECT current_season, salary_cap FROM leagues WHERE id = $1", league_id
    )
    if league_row is None:
        raise DBAError(f"League {league_id} not found.")

    season = league_row["current_season"]
    next_season = season + 1

    await _record_history(pool, league_id, season)

    # RO1: age existing contracts BEFORE activating pending extensions. The
    # previous order activated extensions first (inserting the new contract
    # with its full new_years term), then unconditionally decremented every
    # active contract by 1 -- including the one just inserted, silently
    # losing a year of term on every activated extension.
    contracts_expired = await _age_contracts(pool, league_id)

    # Because _age_contracts now runs first, any contract that naturally hit 0
    # this cycle already flipped its player to roster_status='free_agent' /
    # team_id=NULL before this call -- process_extensions_for_season restores
    # the player to active on the new contract's team when their extension
    # activates.
    extensions_activated = await extension_repo.process_extensions_for_season(
        pool, league_id, season, signed_in_season=next_season
    )

    await _reset_game_state(pool, league_id, next_season)

    # RO6: grow the cap alongside the season advance, in the same statement
    # block. Rounded to the nearest $100k so displayed values look like real
    # cap numbers rather than an odd multiplication artifact.
    new_salary_cap = round(league_row["salary_cap"] * (1 + _SALARY_CAP_GROWTH_RATE) / 100_000) * 100_000
    await pool.execute(
        "UPDATE leagues SET current_season = $1, salary_cap = $2 WHERE id = $3",
        next_season,
        new_salary_cap,
        league_id,
    )

    # Stash the pre-increment season — pending_progression_season is what
    # /offseason progression must filter games/injuries on, since current_season
    # above has already moved past the season that just finished (see
    # progression_service._avg_minutes / _has_season_ending_injury callers).
    await pool.execute(
        "UPDATE leagues SET pending_progression_season = $1 WHERE id = $2",
        season,
        league_id,
    )

    # PT3: route the phase write through league_service.advance_phase instead
    # of a second, independent `UPDATE leagues SET current_phase = ...` that
    # bypassed PT1's phase-graph validation entirely. Legal from either
    # OFFSEASON_AWARDS_CLOSED or DRAFT_LOTTERY_DONE — the two phases
    # /offseason rollover's own precondition allows calling run_rollover from
    # (see phase/graph.py).
    await league_service.advance_phase(league_id, Phase.PROGRESSION_PENDING.value)

    # Seed the new frontier draft season so the 7-season pick window rolls forward.
    await trade_repo.seed_picks_for_league(pool, league_id, next_season + 6, num_seasons=1)

    log.info(
        f"Rollover complete: league={league_id} season={season}->{next_season} "
        f"expired={contracts_expired} "
        f"extensions_activated={extensions_activated} picks_seeded=1 "
        f"new_salary_cap={new_salary_cap}"
    )

    return {
        "season_archived": season,
        "next_season": next_season,
        "contracts_expired": contracts_expired,
        "extensions_activated": extensions_activated,
        "picks_seeded": 1,
        "new_salary_cap": new_salary_cap,
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
    Clear standings_cache (scoped to new_season only -- prior seasons' rows
    are retained for /offseason history), ready_status, and trade_block for
    this league. Games and box_scores are kept as historical data.
    """
    # RO2: new_season's rows don't exist yet at this point in rollover (this
    # is defensive-only), but scoping by season stops every prior season's
    # standings_cache rows from being wiped on every rollover.
    await pool.execute(
        "DELETE FROM standings_cache WHERE league_id = $1 AND season = $2",
        league_id,
        new_season,
    )
    await pool.execute(
        "DELETE FROM ready_status WHERE league_id = $1",
        league_id,
    )
    # RO8: trade_block listings are pure "current state" like ready_status --
    # its read paths never filter by season -- so stale listings from the
    # season that just ended must be cleared at rollover.
    await pool.execute(
        "DELETE FROM trade_block WHERE league_id = $1",
        league_id,
    )
