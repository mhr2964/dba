import asyncio
import asyncpg

async def check():
    c = await asyncpg.connect("postgresql://dba:dba@localhost:5434/dba")

    r = await c.fetchrow("""
        SELECT playmaking, ast_tendency, tendency_pass, overall
        FROM players WHERE league_id=1 AND first_name='Nikola'
        ORDER BY overall DESC LIMIT 1
    """)
    print(f"Jokic: playmaking={r['playmaking']} ast_tendency={r['ast_tendency']} tendency_pass={r['tendency_pass']}")

    r2 = await c.fetchrow("""
        SELECT playmaking, ast_tendency, tendency_pass, overall
        FROM players WHERE league_id=1 AND last_name='Ball' AND first_name='LaMelo'
    """)
    if r2:
        print(f"LaMelo: playmaking={r2['playmaking']} ast_tendency={r2['ast_tendency']} tendency_pass={r2['tendency_pass']}")

    # Trae Young
    r3 = await c.fetchrow("""
        SELECT playmaking, ast_tendency, tendency_pass, overall
        FROM players WHERE league_id=1 AND first_name='Trae'
    """)
    if r3:
        print(f"Trae: playmaking={r3['playmaking']} ast_tendency={r3['ast_tendency']} tendency_pass={r3['tendency_pass']}")

    await c.close()

asyncio.run(check())
