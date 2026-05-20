"""Run a small sim batch directly and check Jokic APG + minutes + trades."""
import asyncio
import os
import sys

# Add project root to path
sys.path.insert(0, r"C:\Users\Owner\Desktop\AI\Projects\dba")
os.chdir(r"C:\Users\Owner\Desktop\AI\Projects\dba")

from dotenv import load_dotenv
load_dotenv()

import asyncpg
import data.db as db_mod
from services import batch_sim_runner


async def main():
    # Initialize pool manually
    pool = await asyncpg.create_pool(
        "postgresql://dba:dba@localhost:5434/dba",
        min_size=2, max_size=5
    )
    db_mod._pool = pool

    print("=== Running 20-game sim batch ===")

    # Get current state
    league = await pool.fetchrow("SELECT * FROM leagues WHERE id=1")
    season = league["current_season"]
    simmed_before = await pool.fetchval("SELECT COUNT(*) FROM games WHERE league_id=1 AND status='simmed'")
    trades_before = await pool.fetchval("SELECT COUNT(*) FROM trades WHERE league_id=1")
    print(f"Before: {simmed_before} games simmed, {trades_before} trades")

    # Run sim for a range of games (next 20)
    from data.repositories import game_repo
    current_idx = await game_repo.get_current_index(pool, 1, season)

    # Sim next 20 games directly
    result = await batch_sim_runner.sim_range(
        league_id=1,
        guild=None,
        season=season,
        to_game_index=current_idx + 20,
        bot=None,
    )
    print(f"Simmed: {result.get('games_simmed', 0)} games")

    simmed_after = await pool.fetchval("SELECT COUNT(*) FROM games WHERE league_id=1 AND status='simmed'")
    trades_after = await pool.fetchval("SELECT COUNT(*) FROM trades WHERE league_id=1")
    print(f"After: {simmed_after} games simmed, {trades_after} trades")

    # Check Jokic stats
    print("\n=== Jokic stats (last 10 games) ===")
    jokic_rows = await pool.fetch("""
        SELECT g.game_index, bs.minutes, bs.assists, bs.points, bs.rebounds_off + bs.rebounds_def as reb
        FROM game_box_scores bs
        JOIN games g ON g.id = bs.game_id
        JOIN players p ON p.id = bs.player_id
        WHERE g.league_id = 1 AND p.position='C' AND p.overall=92
        ORDER BY g.game_index DESC
        LIMIT 10
    """)
    total_ast = 0
    total_min = 0
    count = 0
    for r in jokic_rows:
        print(f"  Game #{r['game_index']}: {r['minutes']:.1f} min | {r['points']} pts | {r['reb']} reb | {r['assists']} ast")
        total_ast += r['assists']
        total_min += r['minutes']
        count += 1
    if count > 0:
        print(f"  AVG: {total_min/count:.1f} MPG | {total_ast/count:.1f} APG")

    # Check APG leaders
    print("\n=== APG leaders ===")
    rows = await pool.fetch("""
        SELECT p.first_name || ' ' || p.last_name as name, p.position, t.nba_team_code,
               ROUND(AVG(bs.assists)::numeric,1) as apg, COUNT(*) as gp
        FROM game_box_scores bs
        JOIN games g ON g.id = bs.game_id
        JOIN players p ON p.id = bs.player_id
        JOIN teams t ON t.id = bs.team_id
        WHERE g.league_id = 1
        GROUP BY p.id, p.first_name, p.last_name, p.position, t.nba_team_code
        HAVING COUNT(*) >= 5
        ORDER BY apg DESC
        LIMIT 10
    """)
    for r in rows:
        name = r['name'].encode('ascii','replace').decode()
        print(f"  {name} ({r['nba_team_code']}, {r['position']}): {r['apg']} APG in {r['gp']} GP")

    await pool.close()


asyncio.run(main())
