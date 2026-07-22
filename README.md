# DBA — Discord Basketball Association

A fully simulated NBA league game for Discord. GMs draft, trade, and manage rosters; the sim engine handles 82-game seasons, a full postseason bracket, and year-over-year player progression — all inside a Discord server.

---

## Features

- 🏀 **League Management**: 30 NBA teams, commissioner control, phase-based season flow
- 📅 **Season Simulation**: possession-based game engine, 82-game schedule, back-to-back fatigue, injury system
- 🤝 **Transactions**: trades with CPU evaluator + commissioner veto, free agency with player decision model, draft with real prospect classes
- 🏆 **Postseason**: play-in tournament, 16-team bracket, series tracking, championship history
- 📊 **Stats**: live standings, leaderboards, career stats, all-time records, power rankings
- 🧠 **Strategy**: offensive/defensive schemes, pace settings, player minutes management — all affect simulation output
- 🤖 **GM Tools**: contract extensions, trade block management, trade grades, Hall of Fame voting
- 📰 **Columnists**: LLM-voiced persona articles (trade reports, power rankings, awards, tank watch) reacting to league events

---

## Prerequisites

- Python 3.12+
- Docker (for local Postgres)
- Discord bot token — create one at [discord.com/developers](https://discord.com/developers/applications)

---

## Quick Start (local dev)

```bash
git clone <repo>
cd dba
cp .env.example .env
# Edit .env — set DISCORD_TOKEN and DATABASE_URL

docker compose up -d           # Start Postgres on port 5434
pip install -r requirements.txt
python -m alembic upgrade head  # Apply all migrations
python main.py                  # Start the bot
```

The local `DATABASE_URL` for the Docker Postgres instance is:

```
postgresql://dba:dba@localhost:5434/dba
```

---

## First League Setup

Run these slash commands in Discord after the bot is online:

1. `/league create name:DBA season:2025` — creates the league, channels, and roles
2. `/season import-players` — imports 2024-25 NBA rosters (~2 min, calls nba_api)
3. `/team assign @user TEAMCODE` — assign teams to GMs; unclaimed teams are run by CPU
4. `/season start` — generates the 82-game schedule
5. All GMs `/ready` → `/sim rivalry` — simulates to the first human matchup

---

## Season Flow

```
League setup → Import players → Assign teams → Generate schedule
  → Regular season (sim games, /trade, /fa) → Trade deadline
  → Playoff bracket (play-in + 16-team) → Championship
  → Awards → Draft → Free agency → Player progression → Rollover → Next season
```

The `phase` system enforces which commands are available at each stage. Attempting an out-of-phase action returns a clear error message.

---

## Configuration

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | Yes | Bot token from discord.com/developers |
| `DATABASE_URL` | Yes | PostgreSQL connection string |
| `LOG_LEVEL` | No | Log verbosity — `DEBUG`, `INFO` (default), `WARNING` |
| `ANTHROPIC_API_KEY` | No | Enables columnist/awards/storyline article generation; those features skip gracefully if unset |

---

## Architecture

Layered monolith: discord.py 2.x (slash commands) + asyncpg (async DB access) + PostgreSQL.

```
bot/cogs/       — discord.py Cog classes, 16 command groups
bot/embeds/     — embed builders
bot/ui/         — interactive views/modals
services/       — business logic (personas/, philosophies/, trade_signals/ subpackages)
data/           — DB pool + repositories (one per aggregate) + seed data
core/           — config, logging, error handling
phase/          — season-phase state machine
scripts/        — durable CLI utilities (seed builders, rating fetchers, DB tools)
alembic/        — 45 migrations
tests/          — pytest suite, 253 tests
```

Full architecture doc, invariants, and current file-split status: [docs/design/architecture.md](docs/design/architecture.md). Trade-evaluator rule specs: [docs/design/trade-logic-rules.md](docs/design/trade-logic-rules.md).

---

## Running Tests

```bash
pip install -r requirements-dev.txt
pytest -v
```

253 collected: 242 passing, 10 `xfail` (pre-existing, tracked issues — see markers in `tests/test_setup_cog.py` and `tests/test_trade_evaluator.py`), 1 integration-marked skip. Requires Postgres running (`docker compose up -d`) and migrations applied.

Manual dual-account Discord testing protocol: [docs/testing.md](docs/testing.md).

---

## Deployment

See [docs/deployment.md](docs/deployment.md) for Railway (recommended) and Fly.io step-by-step guides.
