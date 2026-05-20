"""Quick stat reader for the most recent league. Throwaway script."""
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
    rows = await c.fetch(
        """
        SELECT p.first_name || ' ' || p.last_name AS name, p.position, p.overall,
               ROUND(AVG(b.points)::numeric, 1) AS ppg,
               ROUND(AVG(b.assists)::numeric, 1) AS apg,
               ROUND(AVG(b.rebounds_off + b.rebounds_def)::numeric, 1) AS rpg,
               ROUND(AVG(b.tpm)::numeric, 1) AS three_pg
        FROM game_box_scores b
        JOIN games g ON g.id = b.game_id
        JOIN players p ON p.id = b.player_id
        WHERE g.league_id = $1
        GROUP BY p.id, p.first_name, p.last_name, p.position, p.overall
        HAVING COUNT(*) >= 40
        ORDER BY AVG(b.points) DESC
        LIMIT 15
        """,
        lg,
    )
    print(f"{'player':<24}{'pos':<5}{'ovr':<5}{'ppg':<6}{'apg':<6}{'rpg':<6}{'3pg':<5}")
    print("-" * 60)
    for r in rows:
        print(
            f"{r['name']:<24}{r['position']:<5}{r['overall']!s:<5}"
            f"{r['ppg']!s:<6}{r['apg']!s:<6}{r['rpg']!s:<6}{r['three_pg']!s:<5}"
        )
    await c.close()


asyncio.run(go())
