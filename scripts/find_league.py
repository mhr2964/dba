"""Print all leagues in the database."""
import asyncio, asyncpg, os
from dotenv import load_dotenv
load_dotenv()

async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    rows = await c.fetch("SELECT id, name, discord_guild_id, current_phase FROM leagues ORDER BY id")
    for r in rows:
        print(dict(r))
    await c.close()

asyncio.run(main())
