import json
import os
import time

import requests
from dotenv import load_dotenv
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = "1503802254346551318"
hdrs = {"Authorization": f"Bot {TOKEN}"}

teams = json.load(open("data/seeds/nba_teams.json"))
dba_names = {f"{t['city']} {t['name']}" for t in teams} | {"DBA Commissioner"}

roles = requests.get(f"https://discord.com/api/v10/guilds/{GUILD_ID}/roles", headers=hdrs).json()
dba_roles = [r for r in roles if isinstance(r, dict) and r.get("name") in dba_names]
print(f"Deleting {len(dba_roles)} remaining DBA roles...")

deleted = failed = 0
for role in dba_roles:
    while True:
        r = requests.delete(f"https://discord.com/api/v10/guilds/{GUILD_ID}/roles/{role['id']}", headers=hdrs)
        if r.status_code == 429:
            retry = r.json().get("retry_after", 1.0)
            time.sleep(retry + 0.2)
            continue
        if r.status_code in (204, 404):
            deleted += 1
            print(f"  OK  {role['name']}")
        else:
            failed += 1
            print(f"  FAIL {role['name']}: {r.status_code} {r.text[:60]}")
        time.sleep(0.6)
        break

print(f"\nDeleted: {deleted}  Failed: {failed}")
