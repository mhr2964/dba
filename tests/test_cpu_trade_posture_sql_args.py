"""
Tests for the cpu_trade_posture plan_goal SQL argument order fix (B7 part 2).

Before the fix, the franchise_plans query passed args as:
    (league.id, league.current_season, team_id)
which would match WHERE league_id=$1 AND team_id=$2 AND season=$3 as
    league_id=league.id, team_id=league.current_season, season=team_id
— swapping season and team_id, causing the plan_goal lookup to always return None.

After the fix: args are (league.id, team_id, league.current_season), matching
the placeholder order $1=league_id, $2=team_id, $3=season.
"""
from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

from data.repositories import league_repo
import services.cpu_trade_posture as posture_mod


LEAGUE_ID = 1
TEAM_ID = 42
SEASON = 2025


def _make_league() -> league_repo.League:
    return league_repo.League(
        id=LEAGUE_ID,
        discord_guild_id=999,
        name="Test",
        start_season_year=SEASON,
        current_season=SEASON,
        current_phase="regular_season",
        phase_data={},
        commissioner_user_id=1,
        fa_day_count=0,
        salary_cap=140_000_000,
        created_at=datetime.datetime(2025, 1, 1),
        archived_at=None,
    )


async def test_plan_goal_query_uses_correct_arg_order():
    """franchise_plans query args must be (league.id, team_id, league.current_season).

    The pool is mocked to capture arguments passed to fetchrow().
    We assert the third fetchrow call (franchise_plans) passes args in the
    correct order: league_id=1, team_id=42, season=2025.

    If args were swapped (legacy bug): team_id=2025, season=42 — which would
    never match a real row and always return None, hiding the plan_goal.
    """
    league = _make_league()

    # Track all fetchrow calls and their args.
    fetchrow_calls: list[tuple] = []

    async def _fetchrow(sql: str, *args):
        fetchrow_calls.append((sql.strip(), args))
        sql_lower = sql.lower()
        if "standings_cache" in sql_lower:
            # Minimal standings row so code proceeds past games_played check.
            return {"wins": 30, "losses": 20, "conference": "East"}
        if "franchise_plans" in sql_lower:
            # Return a goal that would only be found with correct args.
            # With wrong args (team_id=2025), no row would match in a real DB.
            return {"goal": "win_now"}
        return None

    async def _fetchval(sql: str, *args):
        sql_lower = sql.lower()
        if "count(*)" in sql_lower and "standings_cache" in sql_lower:
            return 3  # conf_rank = 4
        if "count(*)" in sql_lower and "lineups" in sql_lower:
            return 1  # star_count = 1
        return None

    async def _fetch(sql: str, *args):
        return []  # no age rows → default avg_age=27.0

    pool = MagicMock()
    pool.fetchrow = _fetchrow
    pool.fetchval = _fetchval
    pool.fetch = _fetch

    # Patch trade_evaluator.compute_team_mode to avoid needing full logic.
    with patch.object(posture_mod.trade_evaluator, "compute_team_mode", return_value="contending"):
        await posture_mod._compute_team_posture(pool, league, TEAM_ID)

    # Find the franchise_plans fetchrow call.
    plan_calls = [
        (sql, args) for (sql, args) in fetchrow_calls
        if "franchise_plans" in sql.lower()
    ]
    assert len(plan_calls) == 1, (
        f"Expected exactly 1 franchise_plans query; got {len(plan_calls)}"
    )

    _, actual_args = plan_calls[0]
    assert actual_args == (LEAGUE_ID, TEAM_ID, SEASON), (
        f"franchise_plans query args must be (league_id={LEAGUE_ID}, "
        f"team_id={TEAM_ID}, season={SEASON}); got {actual_args!r}. "
        f"If args are swapped, team_id and season would be transposed."
    )
