"""Force re-derive franchise plans for all CPU teams in a league.

Usage:
    python scripts/_rederive_all_plans.py [--league LEAGUE_ID] [--force]

LEAGUE_ID defaults to the most-recent league in the DB.

--force bypasses Phase 4 stickiness checks and re-derives every CPU team's
plan unconditionally.  Use this after a data correction (e.g. defensive_archetype
backfill) where you need all 30 plans rebuilt against current DB state.
Without --force the normal checkpoint/pivot rules apply.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys

import asyncpg
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

load_dotenv()

from services import franchise_plan_service  # noqa: E402
from data.repositories import team_repo  # noqa: E402


async def _force_rederive_all(pool, league_id: int, season: int) -> int:
    """Derive + persist every CPU team's plan with no stickiness checks.

    Calls derive_plan + persist_plan directly, bypassing derive_and_persist_all
    (which honours Phase 4 checkpoint/pivot gates).  Use only for one-shot
    corrections where every plan must reflect current DB state regardless of
    where in the season the league is.
    """
    teams = await team_repo.get_all(pool, league_id)
    cpu_teams = [t for t in teams if t.manager_user_id is None]

    count = 0
    for team in cpu_teams:
        try:
            plan = await franchise_plan_service.derive_plan(pool, league_id, team.id, season)
            await franchise_plan_service.persist_plan(pool, plan)
            count += 1
            print(f"  [{count:2d}/{len(cpu_teams)}] {team.nba_team_code}  goal={plan['goal']}"
                  f"  core={len(plan['core_player_ids'])}"
                  f"  flex={len(plan['flex_player_ids'])}"
                  f"  surplus={len(plan['surplus_player_ids'])}")
        except Exception as exc:
            print(f"  [WARN] {team.nba_team_code} failed: {exc}")

    return count


async def main(league_id_arg: int | None, force: bool) -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)

    pool = await asyncpg.create_pool(db_url)

    if league_id_arg:
        league_row = await pool.fetchrow(
            "SELECT id, name, current_season FROM leagues WHERE id = $1", league_id_arg
        )
    else:
        league_row = await pool.fetchrow(
            "SELECT id, name, current_season FROM leagues ORDER BY id DESC LIMIT 1"
        )

    if not league_row:
        print("No league found.")
        await pool.close()
        return

    league_id: int = league_row["id"]
    season: int = league_row["current_season"]
    print(f"League: {league_row['name']}  (id={league_id}, season={season})")

    if force:
        print("--force: bypassing stickiness checks — re-deriving ALL CPU teams unconditionally")
        count = await _force_rederive_all(pool, league_id, season)
    else:
        print("Re-deriving plans for all CPU teams (checkpoint/pivot rules apply)…")
        count = await franchise_plan_service.derive_and_persist_all(pool, league_id, season)

    print(f"Done — {count} plans derived/updated.")

    await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Re-derive all franchise plans")
    parser.add_argument("--league", type=int, default=None, dest="league_id")
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Bypass Phase 4 stickiness: call derive_plan+persist_plan directly "
            "for every CPU team regardless of checkpoint windows or pivot gates."
        ),
    )
    args = parser.parse_args()
    asyncio.run(main(args.league_id, args.force))
