import asyncio
import asyncpg

async def check():
    c = await asyncpg.connect("postgresql://dba:dba@localhost:5434/dba")

    # Jokic tendencies
    rows = await c.fetch("""
        SELECT p.first_name, p.last_name, p.position, p.overall,
               p.ast_tendency, p.reb_tendency,
               p.tendency_3pt, p.tendency_drive, p.tendency_mid
        FROM players p
        WHERE p.league_id = 1 AND p.first_name = 'Nikola'
        ORDER BY p.overall DESC
        LIMIT 3
    """)
    for r in rows:
        name = (r['first_name'] + ' ' + r['last_name']).encode('ascii','replace').decode()
        print(f"{name} (OVR {r['overall']}, {r['position']}):")
        print(f"  ast_tendency={r['ast_tendency']} reb_tendency={r['reb_tendency']}")
        print(f"  3pt={r['tendency_3pt']} drive={r['tendency_drive']} mid={r['tendency_mid']}")

    # Check CPU trade block debug - why no trades?
    print("\n=== Trade block samples ===")
    rows = await c.fetch("""
        SELECT t.nba_team_code, p.first_name || ' ' || p.last_name as player,
               p.overall, tb.note
        FROM trade_block tb
        JOIN teams t ON t.id = tb.team_id
        JOIN players p ON p.id = tb.player_id
        WHERE tb.league_id = 1
        ORDER BY p.overall DESC
        LIMIT 10
    """)
    for r in rows:
        name = r['player'].encode('ascii','replace').decode()
        print(f"  {r['nba_team_code']}: {name} (OVR {r['overall']}) - {r['note'] or 'no note'}")

    # Check team modes for CPU teams
    print("\n=== CPU team modes (dynamic posture) ===")
    rows = await c.fetch("""
        SELECT t.nba_team_code, t.cpu_mode, sc.wins, sc.losses
        FROM teams t
        LEFT JOIN standings_cache sc ON sc.team_id = t.id AND sc.league_id = t.league_id AND sc.season = 2024
        WHERE t.league_id = 1 AND t.manager_user_id IS NULL
        ORDER BY sc.wins DESC NULLS LAST
        LIMIT 8
    """)
    for r in rows:
        print(f"  {r['nba_team_code']}: cpu_mode={r['cpu_mode']} | {r['wins'] or 0}-{r['losses'] or 0}")

    await c.close()

asyncio.run(check())
