"""
PT5 -- real DB-backed tests for services.sim_batch_hooks.

Zero test coverage existed on these hooks before this sweep: tests/test_phase.py
covers only phase/helpers.require_phase and phase/transitions.is_allowed; no
test exercised _maybe_advance_trade_deadline, _maybe_close_trade_window, or
_maybe_advance_season_complete against a real DB or a real sim call.

Includes PT4's MANDATORY live smoke test: drives the REAL
sim_orchestrator.sim_range entry point across a single call engineered to
cross BOTH the trade-deadline and season-end game-index boundaries, and
asserts the league steps through REGULAR_SEASON_POSTDEADLINE instead of
silently skipping straight from TRADE_DEADLINE_OPEN to
REGULAR_SEASON_COMPLETE (see docs/design/phase-transition-logic-rules.md PT4).
"""
from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, patch

import pytest

from phase.states import Phase
from services import league_service, sim_orchestrator
from services.sim_batch_hooks import (
    _maybe_advance_season_complete,
    _maybe_advance_trade_deadline,
    _maybe_close_trade_window,
)

pytestmark = pytest.mark.asyncio

_POSITIONS = ["PG", "SG", "SF", "PF", "C", "PG", "SF", "C"]


# ---------------------------------------------------------------------------
# Shared seeding helpers
# ---------------------------------------------------------------------------


async def _insert_league(
    db_pool, guild_id: int, name: str, season: int = 2025, phase: str = "REGULAR_SEASON_ACTIVE"
) -> int:
    row = await db_pool.fetchrow(
        """
        INSERT INTO leagues (
            discord_guild_id, name, start_season_year, current_season,
            current_phase, commissioner_user_id
        ) VALUES ($1, $2, $3, $3, $4, 99999)
        RETURNING id
        """,
        guild_id, name, season, phase,
    )
    return row["id"]


async def _insert_team(db_pool, league_id: int, code: str) -> int:
    return await db_pool.fetchval(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, $2, $3, $2, 'East', 'Atlantic')
        RETURNING id
        """,
        league_id, code, f"{code} City",
    )


async def _insert_team_with_roster(db_pool, league_id: int, code: str) -> tuple[int, list[int]]:
    """Insert an 8-player CPU-managed team with a full lineup (needed for a real sim)."""
    team_id = await _insert_team(db_pool, league_id, code)
    player_ids: list[int] = []
    for i, pos in enumerate(_POSITIONS):
        pid = await db_pool.fetchval(
            """
            INSERT INTO players (
                league_id, team_id, first_name, last_name, position,
                birth_date, years_pro, roster_status,
                overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
                finishing, playmaking, defense, rebounding, iq,
                potential, peak_age_start, peak_age_end,
                loyalty, money_drive, win_drive
            ) VALUES (
                $1, $2, $3, $4, $5,
                '1997-01-01', 6, 'active',
                75, 70, 68, 62, 65,
                68, 65, 68, 68, 70,
                75, 26, 31,
                50, 50, 50
            )
            RETURNING id
            """,
            league_id, team_id, code, f"Player{i}", pos,
        )
        player_ids.append(pid)
    for slot, pid in enumerate(player_ids, start=1):
        await db_pool.execute(
            """
            INSERT INTO lineups (league_id, team_id, is_starter, slot, player_id, set_by)
            VALUES ($1, $2, $3, $4, $5, NULL)
            """,
            league_id, team_id, slot <= 5, slot, pid,
        )
    return team_id, player_ids


async def _insert_games(
    db_pool, league_id: int, season: int, home_id: int, away_id: int,
    count: int, status: str = "scheduled",
) -> None:
    base_date = datetime.date(season, 11, 1)
    for i in range(1, count + 1):
        await db_pool.execute(
            """
            INSERT INTO games (
                league_id, season, season_type, game_index,
                home_team_id, away_team_id, scheduled_date,
                status, is_user_matchup, rng_seed
            ) VALUES ($1, $2, 'regular', $3, $4, $5, $6, $7, FALSE, $8)
            """,
            league_id, season, i, home_id, away_id,
            base_date + datetime.timedelta(days=i * 2), status, 9000 + i,
        )


async def _get_phase(db_pool, league_id: int) -> str:
    return await db_pool.fetchval("SELECT current_phase FROM leagues WHERE id = $1", league_id)


# ---------------------------------------------------------------------------
# _maybe_advance_trade_deadline
# ---------------------------------------------------------------------------


async def test_maybe_advance_trade_deadline_fires_when_index_reached(db_pool):
    league_id = await _insert_league(db_pool, 810001, "Deadline League", phase="REGULAR_SEASON_ACTIVE")

    await _maybe_advance_trade_deadline(db_pool, league_id, current_game_index=55, deadline_game_index=55)

    assert await _get_phase(db_pool, league_id) == Phase.TRADE_DEADLINE_OPEN.value


async def test_maybe_advance_trade_deadline_noop_before_index(db_pool):
    league_id = await _insert_league(db_pool, 810002, "Deadline Noop League", phase="REGULAR_SEASON_ACTIVE")

    await _maybe_advance_trade_deadline(db_pool, league_id, current_game_index=10, deadline_game_index=55)

    assert await _get_phase(db_pool, league_id) == "REGULAR_SEASON_ACTIVE"


async def test_maybe_advance_trade_deadline_noop_when_no_deadline_index(db_pool):
    """deadline_game_index=None (e.g. no games scheduled yet) must never fire."""
    league_id = await _insert_league(db_pool, 810003, "No Deadline League", phase="REGULAR_SEASON_ACTIVE")

    await _maybe_advance_trade_deadline(db_pool, league_id, current_game_index=10, deadline_game_index=None)

    assert await _get_phase(db_pool, league_id) == "REGULAR_SEASON_ACTIVE"


# ---------------------------------------------------------------------------
# _maybe_close_trade_window
# ---------------------------------------------------------------------------


async def test_maybe_close_trade_window_closes_when_open(db_pool):
    league_id = await _insert_league(db_pool, 810004, "Close Window League", phase="TRADE_DEADLINE_OPEN")

    await _maybe_close_trade_window(db_pool, league_id)

    assert await _get_phase(db_pool, league_id) == Phase.REGULAR_SEASON_POSTDEADLINE.value


async def test_maybe_close_trade_window_noop_when_not_open(db_pool):
    league_id = await _insert_league(db_pool, 810005, "Close Window Noop League", phase="REGULAR_SEASON_ACTIVE")

    await _maybe_close_trade_window(db_pool, league_id)

    assert await _get_phase(db_pool, league_id) == "REGULAR_SEASON_ACTIVE"


# ---------------------------------------------------------------------------
# _maybe_advance_season_complete -- direct-call contract tests
# ---------------------------------------------------------------------------


async def test_maybe_advance_season_complete_false_when_games_remain(db_pool):
    league_id = await _insert_league(db_pool, 810006, "Games Remain League", phase="REGULAR_SEASON_POSTDEADLINE")
    home_id, _ = await _insert_team_with_roster(db_pool, league_id, "HOM")
    away_id, _ = await _insert_team_with_roster(db_pool, league_id, "AWY")
    await _insert_games(db_pool, league_id, 2025, home_id, away_id, count=5, status="scheduled")

    advanced = await _maybe_advance_season_complete(db_pool, league_id, 2025, news_channel=None)

    assert advanced is False
    assert await _get_phase(db_pool, league_id) == "REGULAR_SEASON_POSTDEADLINE"


async def test_maybe_advance_season_complete_advances_from_postdeadline(db_pool):
    """Normal path: trade window already closed before the season finished."""
    league_id = await _insert_league(db_pool, 810007, "Postdeadline League", phase="REGULAR_SEASON_POSTDEADLINE")
    home_id, _ = await _insert_team_with_roster(db_pool, league_id, "HOM")
    away_id, _ = await _insert_team_with_roster(db_pool, league_id, "AWY")
    await _insert_games(db_pool, league_id, 2025, home_id, away_id, count=5, status="simmed")

    advanced = await _maybe_advance_season_complete(db_pool, league_id, 2025, news_channel=None)

    assert advanced is True
    assert await _get_phase(db_pool, league_id) == Phase.REGULAR_SEASON_COMPLETE.value


async def test_maybe_advance_season_complete_advances_from_active_when_no_deadline_ever_set(db_pool):
    """A league whose schedule never produced a computable deadline index (or
    that simply never crossed it) must still be able to close the season
    directly from REGULAR_SEASON_ACTIVE -- phase/graph.py legalizes this
    exact edge for that reason."""
    league_id = await _insert_league(db_pool, 810008, "No Deadline Ever League", phase="REGULAR_SEASON_ACTIVE")
    home_id, _ = await _insert_team_with_roster(db_pool, league_id, "HOM")
    away_id, _ = await _insert_team_with_roster(db_pool, league_id, "AWY")
    await _insert_games(db_pool, league_id, 2025, home_id, away_id, count=5, status="simmed")

    advanced = await _maybe_advance_season_complete(db_pool, league_id, 2025, news_channel=None)

    assert advanced is True
    assert await _get_phase(db_pool, league_id) == Phase.REGULAR_SEASON_COMPLETE.value


async def test_maybe_advance_season_complete_reentrant_noop_when_already_complete(db_pool):
    """Calling the hook again after the season is already complete must not
    raise (current_phase == REGULAR_SEASON_COMPLETE is not in the allowed
    pre-states) and must not stomp current_phase."""
    league_id = await _insert_league(db_pool, 810009, "Already Complete League", phase="REGULAR_SEASON_COMPLETE")
    home_id, _ = await _insert_team_with_roster(db_pool, league_id, "HOM")
    away_id, _ = await _insert_team_with_roster(db_pool, league_id, "AWY")
    await _insert_games(db_pool, league_id, 2025, home_id, away_id, count=5, status="simmed")

    advanced = await _maybe_advance_season_complete(db_pool, league_id, 2025, news_channel=None)

    assert advanced is False
    assert await _get_phase(db_pool, league_id) == Phase.REGULAR_SEASON_COMPLETE.value


# ---------------------------------------------------------------------------
# PT4 -- MANDATORY live smoke test
# ---------------------------------------------------------------------------


async def test_pt4_season_complete_steps_through_postdeadline_when_boundaries_cross_mid_call(
    db_pool, mock_guild,
):
    """PT4 -- MANDATORY live smoke test.

    Schedules a full 82-game regular season between two real CPU teams and
    drives the REAL sim_orchestrator.sim_range entry point across the WHOLE
    season in a single call (to_game_index=82). The trade deadline
    (~2/3 through the season, ~game 55) is crossed mid-call by
    _maybe_advance_trade_deadline (fired at sub-batch flush points), and the
    season finishes in the SAME call -- so by the time
    _maybe_advance_season_complete runs (once, at the very end of the call),
    current_phase is still TRADE_DEADLINE_OPEN, because _maybe_close_trade_window
    only runs at the *start* of a sim entry-point, not mid-call.

    Post-fix, _maybe_advance_season_complete must step through
    REGULAR_SEASON_POSTDEADLINE before landing on REGULAR_SEASON_COMPLETE --
    proven here by spying on league_service.advance_phase (wrapped, not
    replaced, so the real DB writes still happen) and asserting the phase
    sequence contains TRADE_DEADLINE_OPEN, then REGULAR_SEASON_POSTDEADLINE,
    then REGULAR_SEASON_COMPLETE, in that order.

    MANDATORY pre-fix proof (performed manually via `git stash` on
    services/sim_batch_hooks.py alone, since PT1's phase-graph gate in
    services/league_service.py must stay in place for the two fixes to be
    tested together per this sweep's ordering requirement -- see this repo's
    PR/session notes for the captured pre-fix output): reverting
    _maybe_advance_season_complete's re-check leaves it calling
    advance_phase(..., REGULAR_SEASON_COMPLETE) unconditionally while
    current_phase is still TRADE_DEADLINE_OPEN -- which PT1's new graph gate
    then correctly rejects, since TRADE_DEADLINE_OPEN's only legal next phase
    is REGULAR_SEASON_POSTDEADLINE. Either way (a silent phase-skip with no
    graph gate, or a hard PhaseError with the gate in place), the pre-fix hook
    never reaches REGULAR_SEASON_COMPLETE via REGULAR_SEASON_POSTDEADLINE, so
    this test fails against the pre-fix hook.
    """
    league_id = await _insert_league(db_pool, 810100, "PT4 Smoke League", phase="REGULAR_SEASON_ACTIVE")
    season = 2025

    home_id, _ = await _insert_team_with_roster(db_pool, league_id, "HOM")
    away_id, _ = await _insert_team_with_roster(db_pool, league_id, "AWY")
    await _insert_games(db_pool, league_id, season, home_id, away_id, count=82, status="scheduled")

    with patch("services.sim_orchestrator.get_pool", AsyncMock(return_value=db_pool)):
        with patch(
            "services.league_service.advance_phase",
            new=AsyncMock(wraps=league_service.advance_phase),
        ) as advance_spy:
            summary = await sim_orchestrator.sim_range(
                league_id, mock_guild, season, to_game_index=82, bot=None, force=False,
            )

    assert summary["games_simmed"] == 82

    phase_calls = [call.args[1] for call in advance_spy.await_args_list]
    assert Phase.TRADE_DEADLINE_OPEN.value in phase_calls, (
        f"Expected the trade deadline to open mid-call before the season finished, "
        f"got phase call sequence: {phase_calls!r}"
    )
    assert Phase.REGULAR_SEASON_POSTDEADLINE.value in phase_calls, (
        "Expected the fixed hook to step through REGULAR_SEASON_POSTDEADLINE -- "
        f"this is the PT4 dispositive assertion. Got call sequence: {phase_calls!r}"
    )
    assert Phase.REGULAR_SEASON_COMPLETE.value in phase_calls

    i_deadline = phase_calls.index(Phase.TRADE_DEADLINE_OPEN.value)
    i_postdeadline = phase_calls.index(Phase.REGULAR_SEASON_POSTDEADLINE.value)
    i_complete = phase_calls.index(Phase.REGULAR_SEASON_COMPLETE.value)
    assert i_deadline < i_postdeadline < i_complete, (
        f"Expected TRADE_DEADLINE_OPEN -> REGULAR_SEASON_POSTDEADLINE -> "
        f"REGULAR_SEASON_COMPLETE in that order, got: {phase_calls!r}"
    )

    assert await _get_phase(db_pool, league_id) == Phase.REGULAR_SEASON_COMPLETE.value
