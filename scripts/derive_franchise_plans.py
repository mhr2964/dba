"""Derive franchise plans for all CPU teams in the most recent league.

Usage:
    cd Projects/dba
    ./.venv/Scripts/python.exe scripts/derive_franchise_plans.py

Prints a summary table: team_code | goal | horizon | core | flex | surplus | rationale
Then asserts that all 4 goal types appear at least once, and that no player
appears in more than one bucket per team.
"""
import asyncio
import asyncpg
import os
import sys
import io
from dotenv import load_dotenv

# Force UTF-8 output on Windows so player names with diacritics don't crash.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

load_dotenv()

# Make repo-relative imports work when run from any cwd.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import franchise_plan_service
from data.repositories import franchise_plan_repo


async def main() -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)

    pool = await asyncpg.create_pool(db_url)

    # Find most-recent league and season.
    league_row = await pool.fetchrow(
        "SELECT id, name, current_season FROM leagues ORDER BY id DESC LIMIT 1"
    )
    if not league_row:
        print("No leagues found in database.")
        await pool.close()
        return

    league_id: int = league_row["id"]
    season: int = league_row["current_season"]
    print(f"League: {league_row['name']}  (id={league_id}, season={season})")
    print()

    # Derive (or update) plans for all CPU teams.
    derived_count = await franchise_plan_service.derive_and_persist_all(pool, league_id, season)
    print(f"Derived/updated {derived_count} plans.\n")

    # Fetch all plans for the summary table.
    plans = await franchise_plan_repo.get_all_for_season(pool, league_id, season)

    if not plans:
        print("No plans stored — something went wrong.")
        await pool.close()
        return

    # Print summary table.
    header = f"{'TEAM':<5} {'GOAL':<12} {'HORIZ':>5} {'CORE':>5} {'FLEX':>5} {'SURP':>5}  RATIONALE"
    print(header)
    print("-" * len(header))

    goals_seen: set[str] = set()
    overlap_errors: list[str] = []

    for p in plans:
        code = p.get("nba_team_code", "???")
        goal = p["goal"]
        horizon = p["horizon_seasons"]
        core = p["core_player_ids"] or []
        flex = p["flex_player_ids"] or []
        surplus = p["surplus_player_ids"] or []
        rationale = p["rationale"]

        goals_seen.add(goal)

        # Overlap check.
        core_s = set(core)
        flex_s = set(flex)
        surp_s = set(surplus)
        core_flex = core_s & flex_s
        core_surp = core_s & surp_s
        flex_surp = flex_s & surp_s
        if core_flex or core_surp or flex_surp:
            overlap_errors.append(
                f"{code}: overlaps core∩flex={core_flex} core∩surp={core_surp} flex∩surp={flex_surp}"
            )

        rat_short = rationale[:72] + ("..." if len(rationale) > 72 else "")
        print(f"{code:<5} {goal:<12} {horizon:>5} {len(core):>5} {len(flex):>5} {len(surplus):>5}  {rat_short}")

    print()

    # Assertions.
    all_goals = {"win_now", "transition", "rebuild", "tank"}
    missing_goals = all_goals - goals_seen
    if missing_goals:
        print(f"WARNING: these goals never appeared: {missing_goals}")
        print("  (This can happen for unusual league states. Check is informational.)")
    else:
        print("OK: all 4 goal types appeared at least once.")

    if overlap_errors:
        print("\nERROR: player bucket overlaps found:")
        for err in overlap_errors:
            print(f"  {err}")
    else:
        print("OK: no player appears in multiple buckets for any team.")

    # Second-run idempotency check.
    await franchise_plan_service.derive_and_persist_all(pool, league_id, season)
    plans2 = await franchise_plan_repo.get_all_for_season(pool, league_id, season)
    if len(plans2) == len(plans):
        print("OK: second run produced no new rows (ON CONFLICT UPDATE worked).")
    else:
        print(f"WARNING: row count changed on second run ({len(plans)} -> {len(plans2)}).")

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
