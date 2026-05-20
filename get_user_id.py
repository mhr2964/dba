import asyncio
import os
import discord
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    guild = client.get_guild(1503802254346551318)
    if guild:
        for member in guild.members:
            if "fox" in member.name.lower() or "mhr" in member.name.lower() or member.name == "foxplayer123":
                print(f"FOUND: {member.id} - {member.name} ({member.display_name})")
        print("All members:")
        for member in guild.members:
            print(f"  {member.id} - {member.name}")
    await client.close()

token = os.environ.get("DISCORD_TOKEN")
asyncio.run(client.start(token))
