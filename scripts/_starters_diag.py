"""Quick diagnostic — see CLE + HOU starting 5 with usage and per-game production."""
import asyncio
import io
import os
import sys

import asyncpg
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from dotenv import load_dotenv

load_dotenv()


async def go():
    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    lg = await c.fetchval("SELECT id FROM leagues ORDER BY id DESC LIMIT 1")
    for team_code in ("CLE", "HOU", "MIN", "DEN"):
        team_id = await c.fetchval(
            "SELECT id FROM teams WHERE nba_team_code = $1 AND league_id = $2",
            team_code, lg,
        )
        if not team_id:
            continue
        rows = await c.fetch(
            """
            SELECT p.first_name || ' ' || p.last_name AS name,
                   p.position, p.overall, p.usage_weight, l.slot,
                   COUNT(b.id) AS gp,
                   ROUND(AVG(b.points)::numeric, 1) AS ppg,
                   ROUND(AVG(b.fga)::numeric, 1) AS fga,
                   ROUND(AVG(b.minutes)::numeric, 1) AS mpg
            FROM lineups l
            JOIN players p ON p.id = l.player_id
            LEFT JOIN game_box_scores b ON b.player_id = p.id
            LEFT JOIN games g ON g.id = b.game_id AND g.league_id = $1
            WHERE l.league_id = $1 AND l.team_id = $2 AND l.slot <= 5
            GROUP BY p.id, p.first_name, p.last_name, p.position, p.overall,
                     p.usage_weight, l.slot
            ORDER BY l.slot
            """,
            lg, team_id,
        )
        print(f"=== {team_code} starting 5 ===")
        print(f"{'name':<24}{'pos':<4}{'ovr':<5}{'use':<5}{'slot':<5}{'mpg':<7}{'ppg':<7}{'fga':<5}")
        print("-" * 65)
        for r in rows:
            print(
                f"{r['name']:<24}{r['position']:<4}{r['overall']!s:<5}"
                f"{r['usage_weight']!s:<5}{r['slot']!s:<5}{r['mpg']!s:<7}"
                f"{r['ppg']!s:<7}{r['fga']!s:<5}"
            )
        print()
    await c.close()


asyncio.run(go())
