"""Philosophy diagnostic — show all 30 teams' role assignments alongside their
coach philosophy.  Allows quick visual confirmation that different philosophies
produce different role patterns.

Output per team:
    === LAL (philosophy: star_maxer) ===
    name              role               share  rationale
    ...

Usage:
    .venv/Scripts/python.exe scripts/_philosophy_diag.py

Pass --team CODE to restrict output to one team (e.g. --team CLE).
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

from services.role_service import derive_roles  # noqa: E402


async def go(team_filter: str | None = None) -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])

    lg = await conn.fetchval("SELECT id FROM leagues ORDER BY id DESC LIMIT 1")
    if lg is None:
        print("No leagues found.")
        await conn.close()
        return

    season = await conn.fetchval(
        "SELECT COALESCE(MAX(season), 2024) FROM games WHERE league_id = $1", lg
    )

    team_rows = await conn.fetch(
        """
        SELECT id, nba_team_code, coach_philosophy
        FROM teams
        WHERE league_id = $1
        ORDER BY nba_team_code
        """,
        lg,
    )

    # Collect per-team player name/position/ovr for display
    for team in team_rows:
        code = team["nba_team_code"]
        if team_filter and code.upper() != team_filter.upper():
            continue

        team_id = team["id"]
        philosophy = team["coach_philosophy"] or "tendency_respecter"

        # Derive fresh (not persisted) so we see what the current philosophy produces
        assignments = await derive_roles(conn, lg, team_id, season, philosophy=philosophy)
        if not assignments:
            print(f"=== {code} (philosophy: {philosophy}) — no roster ===\n")
            continue

        # Fetch player names, position, ovr, slot for display
        player_ids = [a["player_id"] for a in assignments]
        player_rows = await conn.fetch(
            """
            SELECT p.id,
                   p.first_name || ' ' || p.last_name AS name,
                   p.position,
                   p.overall,
                   l.slot
            FROM players p
            JOIN lineups l ON l.player_id = p.id
            WHERE l.league_id = $1
              AND l.team_id   = $2
              AND p.id = ANY($3::int[])
            ORDER BY l.slot
            """,
            lg,
            team_id,
            player_ids,
        )
        info_map = {r["id"]: r for r in player_rows}

        print(f"=== {code} (philosophy: {philosophy}) ===")
        print(f"{'name':<22}{'pos':<5}{'ovr':<5}{'role':<22}{'share':<7}rationale")
        print("-" * 100)

        # Sort by touch_share descending so top consumers appear first
        for a in sorted(assignments, key=lambda x: x["touch_share"], reverse=True):
            pid = a["player_id"]
            info = info_map.get(pid)
            if info is None:
                name = f"player#{pid}"
                pos = "?"
                ovr = "?"
            else:
                name = info["name"]
                pos = info["position"]
                ovr = info["overall"]
            share_str = f"{a['touch_share'] * 100:.1f}%"
            # Truncate rationale to keep lines readable
            rationale = a.get("rationale") or ""
            if len(rationale) > 70:
                rationale = rationale[:67] + "…"
            print(f"{name:<22}{pos!s:<5}{ovr!s:<5}{a['role']:<22}{share_str:<7}{rationale}")
        print()

    await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Show role assignments per team with philosophy")
    parser.add_argument("--team", default=None, help="Filter to a single team code (e.g. CLE)")
    args = parser.parse_args()
    asyncio.run(go(team_filter=args.team))
