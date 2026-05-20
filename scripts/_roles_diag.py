"""Quick diagnostic — show assigned roles for CLE / HOU / MIN / DEN starting 5.

Output columns: name | position | ovr | role | touch_share | rationale
Mirrors the format of _starters_diag.py.
"""
import asyncio
import io
import os
import sys

import asyncpg
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.role_service import derive_roles

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
load_dotenv()


async def go() -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    lg = await conn.fetchval("SELECT id FROM leagues ORDER BY id DESC LIMIT 1")
    season = await conn.fetchval(
        "SELECT MAX(season) FROM games WHERE league_id = $1", lg
    )
    if season is None:
        season = 2024

    for team_code in ("CLE", "HOU", "MIN", "DEN"):
        team_id = await conn.fetchval(
            "SELECT id FROM teams WHERE nba_team_code = $1 AND league_id = $2",
            team_code,
            lg,
        )
        if not team_id:
            print(f"=== {team_code} — not found ===\n")
            continue

        # Fetch assignments fresh (not persisted yet — diagnostic only)
        assignments = await derive_roles(conn, lg, team_id, season)

        # Pull player names and positions for display
        player_ids = [a["player_id"] for a in assignments]
        if not player_ids:
            print(f"=== {team_code} — no roster ===\n")
            continue

        # Fetch name + position + ovr + lineup slot for the top-5 starters
        player_rows = await conn.fetch(
            """
            SELECT p.id, p.first_name || ' ' || p.last_name AS name,
                   p.position, p.overall, l.slot
            FROM players p
            JOIN lineups l ON l.player_id = p.id
            WHERE l.league_id = $1 AND l.team_id = $2 AND l.slot <= 5
            ORDER BY l.slot
            """,
            lg,
            team_id,
        )
        starter_ids = {r["id"] for r in player_rows}
        info_map = {r["id"]: r for r in player_rows}

        # Also pull bench info for players in assignments that are starters
        print(f"=== {team_code} starting 5 roles (season {season}) ===")
        print(
            f"{'name':<24}{'pos':<5}{'ovr':<5}{'role':<22}{'touch':<8}rationale"
        )
        print("-" * 90)

        for a in assignments:
            pid = a["player_id"]
            if pid not in starter_ids:
                continue
            r = info_map[pid]
            touch_pct = f"{a['touch_share']*100:.1f}%"
            print(
                f"{r['name']:<24}{r['position']:<5}{r['overall']!s:<5}"
                f"{a['role']:<22}{touch_pct:<8}{a['rationale']}"
            )
        print()

    await conn.close()


asyncio.run(go())
