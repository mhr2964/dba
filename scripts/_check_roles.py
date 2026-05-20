import os

import requests
from dotenv import load_dotenv
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = "1503802254346551318"
hdrs = {"Authorization": f"Bot {TOKEN}"}

roles = requests.get(f"https://discord.com/api/v10/guilds/{GUILD_ID}/roles", headers=hdrs).json()
sorted_roles = sorted([r for r in roles if isinstance(r, dict)], key=lambda r: r.get("position", 0), reverse=True)

print("Top 20 roles by position:")
for r in sorted_roles[:20]:
    tag = " (bot/managed)" if r.get("managed") else ""
    name = r["name"].encode("ascii", "replace").decode()
    print(f"  pos={r['position']:3d}{tag:15} {r['id']}  {name[:40]}")

# highlight remaining DBA roles
import json
teams = json.load(open("data/seeds/nba_teams.json"))
dba_names = {f"{t['city']} {t['name']}" for t in teams} | {"DBA Commissioner"}
dba_remaining = [r for r in roles if isinstance(r, dict) and r.get("name") in dba_names]
print(f"\nRemaining DBA roles: {len(dba_remaining)}")
for r in dba_remaining:
    print(f"  pos={r['position']:3d}  {r['id']}  {r['name']}")
