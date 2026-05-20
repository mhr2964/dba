"""One-off cleanup: wipe all DBA leagues from DB and all DBA artifacts from the test guild.

Use when the bot is offline (no /admin purge-server available) or after a race-induced
inconsistency between Discord state and DB state.

Run from the project root:
    .venv/Scripts/python.exe scripts/purge_dba.py
"""

import asyncio
import io
import json
import os
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import asyncpg
import discord
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

GUILD_ID = 1503802254346551318
DATABASE_URL = os.environ["DATABASE_URL"]
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]


async def wipe_db() -> dict:
    """Delete all leagues (CASCADE clears teams, players, games, channels, roles tables)."""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        before = await conn.fetch("SELECT id, name, discord_guild_id FROM leagues ORDER BY id")
        result = await conn.execute("DELETE FROM leagues")
        # result like 'DELETE 3'
        deleted = int(result.split()[-1]) if result.startswith("DELETE") else 0
        return {"before": [dict(r) for r in before], "deleted": deleted}
    finally:
        await conn.close()


class PurgeBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        super().__init__(intents=intents)
        self.report: dict = {}

    async def on_ready(self) -> None:
        try:
            guild = self.get_guild(GUILD_ID) or await self.fetch_guild(GUILD_ID)
            # Fetch fresh channel/role state via REST.
            channels = await guild.fetch_channels()
            roles = await guild.fetch_roles()

            team_full_names: set[str] = set()
            seeds_path = PROJECT_ROOT / "data" / "seeds" / "nba_teams.json"
            if seeds_path.exists():
                team_full_names = {
                    f"{t['city']} {t['name']}"
                    for t in json.loads(seeds_path.read_text())
                }

            deleted_categories: list[str] = []
            deleted_channels: int = 0
            deleted_roles: list[str] = []
            channel_errors: list[str] = []
            role_errors: list[str] = []

            for category in [c for c in channels if isinstance(c, discord.CategoryChannel)]:
                name_lower = category.name.lower()
                if category.name.startswith("\U0001F3C0") or "dba" in name_lower:
                    for ch in [c for c in channels if getattr(c, "category_id", None) == category.id]:
                        try:
                            await ch.delete(reason="DBA cleanup script")
                            deleted_channels += 1
                        except discord.HTTPException as exc:
                            channel_errors.append(f"{ch.name}: {exc}")
                    try:
                        await category.delete(reason="DBA cleanup script")
                        deleted_categories.append(category.name)
                    except discord.HTTPException as exc:
                        channel_errors.append(f"category {category.name}: {exc}")

            for role in roles:
                if role.name == "DBA Commissioner" or role.name in team_full_names:
                    try:
                        await role.delete(reason="DBA cleanup script")
                        deleted_roles.append(role.name)
                    except discord.HTTPException as exc:
                        role_errors.append(f"{role.name}: {exc}")

            self.report = {
                "guild": guild.name,
                "categories_deleted": deleted_categories,
                "channels_deleted": deleted_channels,
                "roles_deleted": deleted_roles,
                "channel_errors": channel_errors,
                "role_errors": role_errors,
            }
        finally:
            await self.close()


async def main() -> None:
    print("=== DB cleanup ===")
    db_result = await wipe_db()
    print(f"leagues before: {len(db_result['before'])}")
    for row in db_result["before"]:
        print(f"  id={row['id']}  name={row['name']!r}  guild={row['discord_guild_id']}")
    print(f"leagues deleted: {db_result['deleted']}")

    print("\n=== Discord cleanup ===")
    bot = PurgeBot()
    await bot.start(DISCORD_TOKEN)
    r = bot.report
    print(f"guild: {r.get('guild', '?')}")
    print(f"categories deleted ({len(r.get('categories_deleted', []))}): {r.get('categories_deleted')}")
    print(f"channels deleted: {r.get('channels_deleted')}")
    print(f"roles deleted ({len(r.get('roles_deleted', []))}): {r.get('roles_deleted')}")
    if r.get("channel_errors"):
        print(f"channel errors: {r['channel_errors']}")
    if r.get("role_errors"):
        print(f"role errors: {r['role_errors']}")


if __name__ == "__main__":
    asyncio.run(main())
