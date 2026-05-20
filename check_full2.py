import asyncio
import asyncpg

async def check():
    c = await asyncpg.connect("postgresql://dba:dba@localhost:5434/dba")

    # LAL lineup
    print("=== LAL Starters ===")
    rows = await c.fetch("""
        SELECT p.first_name || ' ' || p.last_name as name, p.position, p.overall, l.is_starter, l.slot
        FROM lineups l
        JOIN players p ON p.id = l.player_id
        JOIN teams t ON t.id = l.team_id
        WHERE t.league_id = 1 AND t.nba_team_code = 'LAL'
        ORDER BY l.slot
    """)
    for r in rows:
        starter = "START" if r['is_starter'] else "bench"
        name = r['name'].encode('ascii','replace').decode()
        print(f"  [{starter}] Slot {r['slot']}: {name} | {r['position']} | OVR {r['overall']}")

    # Jokic stats (search by first name only to handle encoding)
    print("\n=== Jokic stats ===")
    rows = await c.fetch("""
        SELECT p.first_name, p.last_name, p.overall,
               ROUND(AVG(bs.points)::numeric,1) as ppg,
               ROUND(AVG(bs.assists)::numeric,1) as apg,
               ROUND(AVG(bs.rebounds_off + bs.rebounds_def)::numeric,1) as rpg,
               COUNT(*) as gp
        FROM game_box_scores bs
        JOIN games g ON g.id = bs.game_id
        JOIN players p ON p.id = bs.player_id
        WHERE g.league_id = 1 AND p.first_name = 'Nikola'
        GROUP BY p.id, p.first_name, p.last_name
        HAVING COUNT(*) >= 2
    """)
    for r in rows:
        name = (r['first_name'] + ' ' + r['last_name']).encode('ascii','replace').decode()
        print(f"  {name}: OVR {r['overall']} | {r['ppg']} PPG / {r['rpg']} RPG / {r['apg']} APG in {r['gp']} GP")

    # APG leaders (assists check)
    print("\n=== APG Leaders ===")
    rows = await c.fetch("""
        SELECT p.first_name || ' ' || p.last_name as name, t.nba_team_code,
               ROUND(AVG(bs.assists)::numeric,1) as apg, COUNT(*) as gp
        FROM game_box_scores bs
        JOIN games g ON g.id = bs.game_id
        JOIN players p ON p.id = bs.player_id
        JOIN teams t ON t.id = bs.team_id
        WHERE g.league_id = 1
        GROUP BY p.id, p.first_name, p.last_name, t.nba_team_code
        HAVING COUNT(*) >= 3
        ORDER BY apg DESC
        LIMIT 8
    """)
    for r in rows:
        name = r['name'].encode('ascii','replace').decode()
        print(f"  {name} ({r['nba_team_code']}): {r['apg']} APG in {r['gp']} GP")

    # RPG leaders
    print("\n=== RPG Leaders ===")
    rows = await c.fetch("""
        SELECT p.first_name || ' ' || p.last_name as name, p.position, t.nba_team_code,
               ROUND(AVG(bs.rebounds_off + bs.rebounds_def)::numeric,1) as rpg, COUNT(*) as gp
        FROM game_box_scores bs
        JOIN games g ON g.id = bs.game_id
        JOIN players p ON p.id = bs.player_id
        JOIN teams t ON t.id = bs.team_id
        WHERE g.league_id = 1
        GROUP BY p.id, p.first_name, p.last_name, p.position, t.nba_team_code
        HAVING COUNT(*) >= 3
        ORDER BY rpg DESC
        LIMIT 8
    """)
    for r in rows:
        name = r['name'].encode('ascii','replace').decode()
        print(f"  {name} ({r['nba_team_code']}, {r['position']}): {r['rpg']} RPG in {r['gp']} GP")

    await c.close()

asyncio.run(check())
