"""Stress-test /league create concurrency. Fires N concurrent league_service.create
calls against the real Discord guild, then asserts exactly ONE league row in DB
and exactly ONE 🏀 category in Discord. Cleans up after itself.

Run from project root:
    .venv/Scripts/python.exe scripts/stress_test_create.py
"""

import asyncio
import io
import os
import sys
from pathlib import Path

import asyncpg
import discord
from dotenv import load_dotenv

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

GUILD_ID = 1503802254346551318
COMMISSIONER_ID = 238430857327542272  # Eyeleg's user id from bot.log

CONCURRENT_CALLS = 5


async def cleanup() -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        await conn.execute("DELETE FROM leagues")
    finally:
        await conn.close()


class Stressor(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        super().__init__(intents=intents)
        self.result: dict = {}

    async def on_ready(self) -> None:
        try:
            # Initialize the bot's pool (league_service uses get_pool)
            from data import db
            await db.get_pool()

            from services import league_service

            guild = self.get_guild(GUILD_ID) or await self.fetch_guild(GUILD_ID)
            try:
                commissioner = guild.get_member(COMMISSIONER_ID) or await guild.fetch_member(COMMISSIONER_ID)
            except discord.NotFound:
                print(f"FATAL: commissioner {COMMISSIONER_ID} not in guild — using bot's own member as a substitute")
                commissioner = guild.me

            # Make sure starting state is clean
            await cleanup()
            channels = await guild.fetch_channels()
            for cat in [c for c in channels if isinstance(c, discord.CategoryChannel) and c.name.startswith("\U0001F3C0")]:
                for ch in [x for x in channels if getattr(x, "category_id", None) == cat.id]:
                    try: await ch.delete()
                    except Exception: pass
                try: await cat.delete()
                except Exception: pass
            roles = await guild.fetch_roles()
            for r in roles:
                if r.name == "DBA Commissioner":
                    try: await r.delete()
                    except Exception: pass

            print(f"firing {CONCURRENT_CALLS} concurrent create() calls...")
            tasks = [
                asyncio.create_task(league_service.create(guild=guild, commissioner=commissioner, name="StressTest", season_year=2024))
                for _ in range(CONCURRENT_CALLS)
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            ok = [r for r in results if not isinstance(r, Exception)]
            errs = [r for r in results if isinstance(r, Exception)]
            print(f"  successes: {len(ok)}")
            print(f"  errors: {len(errs)}")
            for e in errs:
                print(f"    - {type(e).__name__}: {e}")

            # Now check DB + Discord state
            conn = await asyncpg.connect(os.environ["DATABASE_URL"])
            try:
                rows = await conn.fetch("SELECT id, name FROM leagues")
            finally:
                await conn.close()

            channels = await guild.fetch_channels()
            cats_after = [c for c in channels if isinstance(c, discord.CategoryChannel) and c.name.startswith("\U0001F3C0")]

            self.result = {
                "concurrent_calls": CONCURRENT_CALLS,
                "successes": len(ok),
                "errors": len(errs),
                "db_leagues": len(rows),
                "discord_dba_categories": len(cats_after),
                "pass": len(ok) == 1 and len(rows) == 1 and len(cats_after) == 1,
            }

            # Cleanup after test
            from scripts.purge_dba import wipe_db
            await wipe_db()
            for cat in cats_after:
                for ch in [x for x in channels if getattr(x, "category_id", None) == cat.id]:
                    try: await ch.delete()
                    except Exception: pass
                try: await cat.delete()
                except Exception: pass
            roles = await guild.fetch_roles()
            for r in roles:
                if r.name == "DBA Commissioner" or r.name == "Los Angeles Lakers" or r.name == "Boston Celtics":
                    try: await r.delete()
                    except Exception: pass

        finally:
            await self.close()


async def main() -> None:
    bot = Stressor()
    await bot.start(os.environ["DISCORD_TOKEN"])
    r = bot.result
    print("\n=== RESULT ===")
    for k, v in r.items():
        print(f"  {k}: {v}")
    print(f"\n{'PASS' if r.get('pass') else 'FAIL'}: {'race fix holds' if r.get('pass') else 'race STILL allows multiple categories/leagues'}")


if __name__ == "__main__":
    asyncio.run(main())
