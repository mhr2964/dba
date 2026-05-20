import asyncio
import asyncpg

async def check():
    c = await asyncpg.connect("postgresql://dba:dba@localhost:5434/dba")

    # DEN player tendencies - are they getting assists in proportion to their ast_tendency?
    print("=== DEN players: tendencies vs actual APG ===")
    rows = await c.fetch("""
        SELECT p.first_name || ' ' || p.last_name as name,
               p.position, p.playmaking, p.ast_tendency, p.tendency_pass,
               ROUND(AVG(bs.assists)::numeric,1) as apg,
               ROUND(AVG(bs.minutes)::numeric,1) as mpg,
               COUNT(*) as gp
        FROM game_box_scores bs
        JOIN games g ON g.id = bs.game_id
        JOIN players p ON p.id = bs.player_id
        JOIN teams t ON t.id = p.team_id
        WHERE g.league_id = 1 AND t.nba_team_code = 'DEN'
        GROUP BY p.id, p.first_name, p.last_name, p.position, p.playmaking, p.ast_tendency, p.tendency_pass
        HAVING COUNT(*) >= 3
        ORDER BY apg DESC
    """)
    for r in rows:
        name = r['name'].encode('ascii','replace').decode()
        print(f"  {name} ({r['position']}) pm={r['playmaking']} ast_t={r['ast_tendency']} pass_t={r['tendency_pass']} | {r['apg']} APG in {r['mpg']} MPG / {r['gp']} GP")

    # Also check CHA (LaMelo) for comparison
    print("\n=== CHA players: tendencies vs actual APG ===")
    rows = await c.fetch("""
        SELECT p.first_name || ' ' || p.last_name as name,
               p.position, p.playmaking, p.ast_tendency, p.tendency_pass,
               ROUND(AVG(bs.assists)::numeric,1) as apg,
               ROUND(AVG(bs.minutes)::numeric,1) as mpg,
               COUNT(*) as gp
        FROM game_box_scores bs
        JOIN games g ON g.id = bs.game_id
        JOIN players p ON p.id = bs.player_id
        JOIN teams t ON t.id = p.team_id
        WHERE g.league_id = 1 AND t.nba_team_code = 'CHA'
        GROUP BY p.id, p.first_name, p.last_name, p.position, p.playmaking, p.ast_tendency, p.tendency_pass
        HAVING COUNT(*) >= 3
        ORDER BY apg DESC
        LIMIT 5
    """)
    for r in rows:
        name = r['name'].encode('ascii','replace').decode()
        print(f"  {name} ({r['position']}) pm={r['playmaking']} ast_t={r['ast_tendency']} pass_t={r['tendency_pass']} | {r['apg']} APG in {r['mpg']} MPG / {r['gp']} GP")

    await c.close()

asyncio.run(check())
