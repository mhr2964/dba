"""Diagnostic: show franchise plan categorization with production tiers.

For each team (default: HOU, CHI, BOS, DEN — or all if --all flag given),
prints the per-bucket player list with:
  name | pos | OVR | prod-tier | stats line | bucket

Usage:
    python scripts/_plan_production_diag.py
    python scripts/_plan_production_diag.py --teams HOU CHI BOS DEN
    python scripts/_plan_production_diag.py --all
    python scripts/_plan_production_diag.py --league 112
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

from services.franchise_plan_service import (  # noqa: E402
    _fetch_season_production,
    _production_tier,
    _defensive_tier,
    _combined_tier,
    derive_plan,
)


async def main(teams: list[str], all_teams: bool, league_id_arg: int | None) -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)

    pool = await asyncpg.create_pool(db_url)

    # Resolve league and season.
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
    print(f"League: {league_row['name']}  (id={league_id}, season={season})\n")

    # Resolve target team codes.
    if all_teams:
        team_rows = await pool.fetch(
            "SELECT id, nba_team_code FROM teams WHERE league_id = $1 ORDER BY nba_team_code",
            league_id,
        )
    else:
        target_codes = teams if teams else ["HOU", "CHI", "BOS", "DEN"]
        team_rows = await pool.fetch(
            "SELECT id, nba_team_code FROM teams "
            "WHERE league_id = $1 AND nba_team_code = ANY($2::text[])",
            league_id,
            target_codes,
        )

    if not team_rows:
        print("No matching teams found.")
        await pool.close()
        return

    for team_row in team_rows:
        team_id: int = team_row["id"]
        team_code: str = team_row["nba_team_code"]

        # Fresh derive (does not persist) so we always see current state.
        plan = await derive_plan(pool, league_id, team_id, season)
        goal: str = plan["goal"]
        horizon: int = plan["horizon_seasons"]

        # Build player lookup for this roster (includes defensive_archetype).
        player_rows = await pool.fetch(
            """
            SELECT p.id,
                   p.first_name,
                   p.last_name,
                   p.first_name || ' ' || p.last_name AS full_name,
                   p.position,
                   p.overall,
                   p.defensive_archetype
            FROM players p
            WHERE p.league_id = $1
              AND p.team_id   = $2
              AND p.roster_status = 'active'
            """,
            league_id,
            team_id,
        )
        player_map: dict[int, dict] = {r["id"]: dict(r) for r in player_rows}

        # Fetch production — pass full rows so the BDL fallback can name-match.
        prod_rows = [dict(r) for r in player_rows]
        prod_map = await _fetch_season_production(pool, league_id, season, prod_rows)

        core_ids: list[int] = plan["core_player_ids"] or []
        flex_ids: list[int] = plan["flex_player_ids"] or []
        surplus_ids: list[int] = plan["surplus_player_ids"] or []

        print(f"=== {team_code} (plan: {goal} h:{horizon}) ===")

        def fmt_player(pid: int) -> str:
            p = player_map.get(pid)
            if p is None:
                return f"  [unknown player id={pid}]"
            stats = prod_map.get(pid, {})
            archetype = p.get("defensive_archetype")
            off_tier = _production_tier(stats)
            def_tier = _defensive_tier(stats)
            comb_tier = _combined_tier(off_tier, def_tier, archetype)
            gp = stats.get("gp", 0)
            is_fallback = "_source" in stats and str(stats["_source"]).startswith("bdl_")
            fallback_tag = f"  [bdl-fallback:{stats['_source']}]" if is_fallback else ""
            if gp >= 10:
                off_line = (
                    f"{stats['ppg']:.1f} PPG, {stats['apg']:.1f} APG, {stats['rpg']:.1f} RPG"
                )
                def_line = (
                    f"{stats.get('bpg', 0):.1f} BPG, {stats.get('spg', 0):.1f} SPG, "
                    f"{stats.get('drpg', 0):.1f} DRPG, {stats.get('mpg', 0):.0f} MPG"
                )
                stats_line = f"[{off_line} / {def_line}, {gp}GP]{fallback_tag}"
            else:
                stats_line = f"[<{gp}GP — no prod data]"

            tier_label = f"off={off_tier}/def={def_tier}/comb={comb_tier}"
            arch_label = f"  arch:{archetype}" if archetype else ""
            return (
                f"  {p['full_name']:<22} {p['position']:<3} OVR {p['overall']:>2}  "
                f"prod:{tier_label}{arch_label}  {stats_line}"
            )

        for label, ids in [("CORE", core_ids), ("FLEX", flex_ids), ("SURPLUS", surplus_ids)]:
            if ids:
                print(f"  {label}:")
                for pid in ids:
                    print(fmt_player(pid))
            else:
                print(f"  {label}: (empty)")

        print()

    await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Plan production diagnostic")
    parser.add_argument(
        "--teams",
        nargs="+",
        default=[],
        help="Team codes to show (default: HOU CHI BOS DEN)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="all_teams",
        help="Show all teams",
    )
    parser.add_argument(
        "--league",
        type=int,
        default=None,
        dest="league_id",
        help="League ID (default: most recent)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.teams, args.all_teams, args.league_id))
