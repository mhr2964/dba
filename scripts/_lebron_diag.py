"""Diagnose why LeBron is scoring 36 PPG."""
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
    print(f"=== League {lg} ===\n")

    # LeBron's raw attributes
    lj = await c.fetchrow(
        """
        SELECT id, position, overall, finishing, shooting_2pt, shooting_3pt, playmaking,
               tendency_3pt, tendency_drive, tendency_pass, ast_tendency,
               usage_weight, clutch_rating
        FROM players WHERE first_name = 'LeBron' AND last_name = 'James'
        LIMIT 1
        """
    )
    print("LeBron attributes:")
    for k, v in lj.items():
        print(f"  {k:<18} {v}")

    # LAL roster context — who else is on the team, what's the OVR distribution
    print("\n=== LAL ROSTER (active) ===")
    roster = await c.fetch(
        """
        SELECT p.first_name || ' ' || p.last_name AS name, p.position, p.overall,
               p.usage_weight, p.tendency_3pt, p.tendency_drive
        FROM players p
        JOIN teams t ON t.id = p.team_id
        WHERE t.nba_team_code = 'LAL' AND t.league_id = $1
              AND p.roster_status = 'active'
        ORDER BY p.overall DESC
        LIMIT 12
        """,
        lg,
    )
    for r in roster:
        print(f"  {r['name']:<24} {r['position']:<3} OVR {r['overall']:<3} usage {r['usage_weight']:<3} t3 {r['tendency_3pt']:<3} tdr {r['tendency_drive']}")

    # LeBron's game distribution
    print("\n=== LeBron game log distribution ===")
    dist = await c.fetch(
        """
        SELECT
          width_bucket(b.points, 0, 60, 12) AS bucket,
          MIN(b.points) AS lo, MAX(b.points) AS hi, COUNT(*) AS games
        FROM game_box_scores b
        JOIN games g ON g.id = b.game_id
        WHERE g.league_id = $1 AND b.player_id = $2
        GROUP BY bucket ORDER BY bucket
        """,
        lg, lj["id"],
    )
    for d in dist:
        print(f"  {d['lo']:<3}-{d['hi']:<3} PPG: {'#' * d['games']} ({d['games']} games)")

    # LAL team scheme distribution — does LAL run iso every game?
    print("\n=== Has any team scheme info in DB? ===")
    has_strategy = await c.fetchval(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'team_strategies'"
    )
    if has_strategy:
        lal_strat = await c.fetch(
            """
            SELECT offensive_scheme, defensive_scheme, offensive_pace, star_usage
            FROM team_strategies ts
            JOIN teams t ON t.id = ts.team_id
            WHERE t.nba_team_code = 'LAL' AND ts.league_id = $1
            """,
            lg,
        )
        for s in lal_strat:
            print(f"  Strategy: {dict(s)}")

    # LeBron's points distribution by stat — how does he score his 36?
    print("\n=== LeBron scoring breakdown ===")
    breakdown = await c.fetchrow(
        """
        SELECT
          ROUND(AVG(b.points)::numeric, 1) AS ppg,
          ROUND(AVG(b.fga)::numeric, 1) AS fga,
          ROUND(AVG(b.fgm)::numeric, 1) AS fgm,
          ROUND(AVG(b.tpa)::numeric, 1) AS tpa,
          ROUND(AVG(b.tpm)::numeric, 1) AS tpm,
          ROUND(AVG(b.fta)::numeric, 1) AS fta,
          ROUND(AVG(b.ftm)::numeric, 1) AS ftm,
          ROUND(AVG(b.minutes)::numeric, 1) AS mpg
        FROM game_box_scores b
        JOIN games g ON g.id = b.game_id
        WHERE g.league_id = $1 AND b.player_id = $2
        """,
        lg, lj["id"],
    )
    print(f"  {dict(breakdown)}")

    await c.close()


asyncio.run(go())
