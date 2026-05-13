# scripts/

Standalone CLI utilities for DBA league operations. These scripts connect directly to the database via `DATABASE_URL` and do not require the bot to be running.

---

## import_players.py

Populates players for a league from the 2024-25 NBA rosters via `nba_api`. This is the CLI alternative to the `/season import-players` bot command — useful for testing, resets, or headless environments.

**Usage:**

```bash
python scripts/import_players.py --league-id 1 --season 2024
python scripts/import_players.py --league-id 1 --season 2024 --dry-run
```

**Arguments:**

| Argument | Required | Description |
|---|---|---|
| `--league-id` | Yes | Integer ID of the target league (must exist in DB) |
| `--season` | Yes | Season start year, e.g. `2024` for the 2024-25 season |
| `--dry-run` | No | Print all actions without writing to the database |

**What it does:**

- Fetches rosters for all 30 NBA teams from `nba_api`
- Generates overall ratings, per-position attribute spreads, and hidden attributes (potential, loyalty, market preference)
- Assigns contracts based on experience and overall rating (rookie scale for first-year players)
- Auto-generates starting lineups ranked by overall
- Respects the league's salary cap when setting contract values

**Notes:**

- Requires `DATABASE_URL` in environment (`.env` is loaded automatically)
- Rate-limited by `nba_api` — a full 30-team import takes approximately 2 minutes
- Run `--dry-run` first to verify output before committing data
- If the league does not exist or a team code is missing, the script logs a warning and skips rather than failing hard
