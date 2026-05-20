import asyncio
import asyncpg

async def assign():
    conn = await asyncpg.connect("postgresql://dba:dba@localhost:5434/dba")

    lal = await conn.fetchrow("SELECT id FROM teams WHERE league_id=1 AND nba_team_code='LAL'")
    if lal:
        await conn.execute(
            "UPDATE teams SET manager_user_id=1069338716579037284 WHERE id=$1",
            lal["id"]
        )
        print(f"LAL (id={lal['id']}) assigned to mhr2964")

    managers = await conn.fetch(
        "SELECT nba_team_code, manager_user_id FROM teams WHERE league_id=1 AND manager_user_id IS NOT NULL"
    )
    print("Managed teams:", [(r["nba_team_code"], r["manager_user_id"]) for r in managers])

    await conn.close()

asyncio.run(assign())
