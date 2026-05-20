import asyncio
import os
import aiohttp
from dotenv import load_dotenv

load_dotenv()

async def find():
    token = os.environ.get("DISCORD_TOKEN")
    guild_id = "1503802254346551318"
    headers = {"Authorization": f"Bot {token}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"https://discord.com/api/v10/guilds/{guild_id}/members?limit=200",
            headers=headers
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                print(f"Error {resp.status}: {text}")
                return
            members = await resp.json()
            for m in members:
                u = m["user"]
                name = u['username'].encode('ascii', 'replace').decode()
                print(f"{u['id']} | {name}")

asyncio.run(find())
