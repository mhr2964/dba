import asyncio
import asyncpg

async def check():
    c = await asyncpg.connect("postgresql://dba:dba@localhost:5434/dba")

    # Jokic minutes each game
    rows = await c.fetch("""
        SELECT g.game_index, bs.minutes, bs.assists, bs.points
        FROM game_box_scores bs
        JOIN games g ON g.id = bs.game_id
        JOIN players p ON p.id = bs.player_id
        WHERE g.league_id = 1
          AND p.first_name = 'Nikola'
          AND p.overall = 92
        ORDER BY g.game_index
        LIMIT 15
    """)
    print("Jokic per-game minutes/assists/points:")
    for r in rows:
        print(f"  Game #{r['game_index']}: {r['minutes']} min | {r['points']} pts | {r['assists']} ast")

    # Check DEN lineup: how many starters/bench
    lineup = await c.fetch("""
        SELECT p.first_name || ' ' || p.last_name as name, l.is_starter, l.slot, p.overall
        FROM lineups l
        JOIN players p ON p.id = l.player_id
        JOIN teams t ON t.id = l.team_id
        WHERE t.nba_team_code = 'DEN' AND t.league_id = 1
        ORDER BY l.slot
    """)
    starters = [r for r in lineup if r['is_starter']]
    bench = [r for r in lineup if not r['is_starter']]
    print(f"\nDEN lineup: {len(starters)} starters, {len(bench)} bench")
    print("Starters:")
    for r in starters:
        name = r['name'].encode('ascii','replace').decode()
        print(f"  Slot {r['slot']}: {name} OVR {r['overall']}")

    # Check if there are any target_minutes for DEN in strategy_repo
    targets = await c.fetchval("""
        SELECT COUNT(*) FROM strategy_targets
        WHERE league_id = 1
    """)
    print(f"\nStrategy targets in DB: {targets}")

    await c.close()

asyncio.run(check())
