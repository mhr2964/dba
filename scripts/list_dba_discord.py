"""Inspect raw Discord state for the DBA test guild — finds every category and
its channels regardless of name, and prints which are DBA-managed (registered
in league_channels) vs orphaned (Discord-only with no DB row).
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
load_dotenv(PROJECT_ROOT / ".env")
GUILD_ID = 1503802254346551318


class Inspector(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        super().__init__(intents=intents)

    async def on_ready(self) -> None:
        try:
            guild = self.get_guild(GUILD_ID) or await self.fetch_guild(GUILD_ID)
            channels = await guild.fetch_channels()
            roles = await guild.fetch_roles()

            print(f"GUILD: {guild.name} (id={guild.id})")
            print(f"  total channels: {len(channels)}")
            print(f"  total roles: {len(roles)}")

            registered_channel_ids = await _fetch_registered_channel_ids()

            categories = [c for c in channels if isinstance(c, discord.CategoryChannel)]
            print(f"\nCATEGORIES ({len(categories)}):")
            for cat in categories:
                children = [c for c in channels if getattr(c, "category_id", None) == cat.id]
                managed = " [DBA-managed]" if any(c.id in registered_channel_ids for c in children) else ""
                orphan = " [ORPHAN — no DB row]" if (cat.name.startswith("\U0001F3C0") or "dba" in cat.name.lower()) and not managed else ""
                print(f"  {cat.name!r} (id={cat.id}){managed}{orphan}")
                for c in children:
                    tag = " [tracked]" if c.id in registered_channel_ids else ""
                    print(f"    - #{c.name} (id={c.id}){tag}")

            print("\nROOT CHANNELS (no category):")
            for c in [c for c in channels if not isinstance(c, discord.CategoryChannel) and getattr(c, "category_id", None) is None]:
                print(f"  - #{c.name} (id={c.id})")

            print("\nALL ROLES (filtered to DBA-relevant):")
            for r in roles:
                if r.name == "DBA Commissioner" or "DBA" in r.name or any(team_marker in r.name for team_marker in ("Lakers", "Celtics", "Warriors", "Bulls", "Heat", "Nets", "Knicks", "Bucks", "Suns", "Mavericks", "Nuggets", "76ers", "Cavaliers", "Pistons", "Hawks", "Raptors", "Spurs", "Rockets", "Thunder", "Timberwolves", "Trail Blazers", "Jazz", "Kings", "Clippers", "Grizzlies", "Pelicans", "Wizards", "Magic", "Hornets", "Pacers")):
                    print(f"  - {r.name!r} (id={r.id})")
        finally:
            await self.close()


async def _fetch_registered_channel_ids() -> set:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        rows = await conn.fetch("SELECT discord_channel_id FROM league_channels")
        return {r["discord_channel_id"] for r in rows}
    finally:
        await conn.close()


async def main() -> None:
    bot = Inspector()
    await bot.start(os.environ["DISCORD_TOKEN"])


if __name__ == "__main__":
    asyncio.run(main())
