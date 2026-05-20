import asyncio
import asyncpg

async def fix():
    c = await asyncpg.connect("postgresql://dba:dba@localhost:5434/dba")

    # Find Jokic by OVR and position (avoid unicode issue)
    jokic = await c.fetchrow("""
        SELECT id, first_name, last_name, ast_tendency, overall
        FROM players WHERE league_id=1 AND position='C' AND overall=92
        LIMIT 1
    """)
    if jokic:
        name = (jokic['first_name'] + ' ' + jokic['last_name']).encode('ascii','replace').decode()
        print(f"Found: {name} OVR {jokic['overall']} ast_tendency={jokic['ast_tendency']}")
        await c.execute("UPDATE players SET ast_tendency=97 WHERE id=$1", jokic['id'])
        print("Updated ast_tendency to 97")

    # Fix PG playmaking=99 being unrealistic - it should be <= 95
    result = await c.execute("""
        UPDATE players SET playmaking = LEAST(playmaking, 95)
        WHERE league_id=1 AND position='PG' AND playmaking > 95
    """)
    print(f"Capped PG playmaking at 95: {result}")

    # Verify
    rows = await c.fetch("""
        SELECT first_name || ' ' || last_name as name, position, overall, ast_tendency, playmaking
        FROM players WHERE league_id=1 AND position='C' AND overall >= 85
        ORDER BY overall DESC
    """)
    print("\nTop C tendencies after fix:")
    for r in rows:
        name = r['name'].encode('ascii','replace').decode()
        print(f"  {name}: OVR {r['overall']} | ast_t={r['ast_tendency']} | pm={r['playmaking']}")

    await c.close()

asyncio.run(fix())
