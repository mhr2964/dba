"""Diagnostic for services/team_intel — Phase 1 verification.

Prints per-team: mode/urgency, plan goal, philosophy, recent pivots,
recent role changes.

Usage:
    python scripts/_team_intel_diag.py [team_codes...]
    python scripts/_team_intel_diag.py              # defaults: CLE HOU MIN DEN
    python scripts/_team_intel_diag.py CLE LAL BOS
"""
from __future__ import annotations

import asyncio
import io
import os
import sys

import asyncpg
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
load_dotenv()

from data.repositories import league_repo  # noqa: E402
from services import team_intel  # noqa: E402


async def main(team_codes: list[str]) -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)

    pool = await asyncpg.create_pool(db_url)

    # Resolve league.
    raw = await pool.fetchrow(
        "SELECT * FROM leagues WHERE archived_at IS NULL ORDER BY id DESC LIMIT 1"
    )
    if not raw:
        print("No active league found.")
        await pool.close()
        return

    # Build League dataclass the same way league_repo does.
    league = league_repo._league_from_record(raw)
    season = league.current_season
    print(f"League: {league.name}  (id={league.id}, season={season})\n")

    # Resolve team IDs.
    target_codes = team_codes if team_codes else ["CLE", "HOU", "MIN", "DEN"]
    team_rows = await pool.fetch(
        """
        SELECT id, nba_team_code
        FROM teams
        WHERE league_id = $1 AND nba_team_code = ANY($2::text[])
        ORDER BY nba_team_code
        """,
        league.id,
        target_codes,
    )
    if not team_rows:
        print(f"No teams found for codes: {target_codes}")
        await pool.close()
        return

    team_ids = [r["id"] for r in team_rows]
    code_by_id = {r["id"]: r["nba_team_code"] for r in team_rows}

    # Single bulk call — all intel for all teams.
    intel = await team_intel.build_team_intel(pool, league, season, team_ids)

    for tid, code in sorted(code_by_id.items(), key=lambda kv: kv[1]):
        data = intel.get(tid, {})

        posture = data.get("posture", {})
        plan = data.get("plan")
        philosophy = data.get("philosophy", "?")
        pivots = data.get("recent_pivots", [])
        role_changes = data.get("recent_role_changes", [])

        mode = posture.get("mode", "?")
        urgency = posture.get("urgency", "?")
        proj_w = posture.get("projected_wins")
        conf_rank = posture.get("conf_rank")
        wins = posture.get("wins", 0)
        losses = posture.get("losses", 0)

        plan_str = "no plan stored"
        if plan:
            goal = plan.get("goal", "?")
            horizon = plan.get("horizon_seasons", "?")
            core_size = plan.get("core_size", 0)
            core_names = plan.get("core_names") or []
            core_str = ", ".join(core_names) if core_names else "(none)"
            plan_str = f"goal={goal} h:{horizon}  core({core_size}): {core_str}"

        rank_str = f" #{conf_rank} conf" if conf_rank else ""
        proj_str = f" proj {proj_w}W" if proj_w is not None else f" {wins}-{losses}"

        print(f"{code} — {mode}/{urgency}{proj_str}{rank_str}")
        print(f"  plan     : {plan_str}")
        print(f"  philosophy: {philosophy}")

        if pivots:
            print(f"  Recent pivots ({len(pivots)}):")
            for p in pivots:
                print(
                    f"    [{p.get('pivot_date', '?')[:10]}] "
                    f"{p['old_goal']} → {p['new_goal']}  \"{p['reason']}\""
                )
        else:
            print("  Recent pivots: none")

        if role_changes:
            print(f"  Recent role changes ({len(role_changes)}, last 14d):")
            for rc in role_changes[:5]:
                old = rc.get("old_role") or "(unknown)"
                print(
                    f"    {rc['player_name']:<22} {old} → {rc['new_role']}  "
                    f"by:{rc['assigned_by']}  at:{(rc.get('assigned_at') or '')[:10]}"
                )
        else:
            print("  Recent role changes: none in last 14 days")

        print()

    await pool.close()


if __name__ == "__main__":
    codes = [c.upper() for c in sys.argv[1:]] if len(sys.argv) > 1 else []
    asyncio.run(main(codes))
