# Architecture — DBA

Layered-monolith Python/discord.py/PostgreSQL Discord bot simulating an NBA fantasy league.

## Layers

| Package | Role |
| --- | --- |
| `bot/` | Discord-facing layer: `cogs/` (16 slash-command groups), `embeds/` (18 embed builders), `ui/` (5 interactive views/modals), `client.py` (bot bootstrap) |
| `services/` | Business logic: 45 top-level modules + `personas/` (20, columnist voice/prompt definitions), `philosophies/` (9, coaching-bias role-assignment modifiers), `trade_signals/` (10, trade-context signal providers) |
| `data/` | Persistence: `db.py` (asyncpg pool), `repositories/` (25 modules, one per aggregate — the only place raw SQL should live), `seeds/`, cached rating/stat snapshots (`2k_ratings/`, `bdl_cache/`, `stats_ratings/`, `tendency_cache/`) |
| `core/` | Cross-cutting: `config.py` (fail-fast required env vars), `errors.py`, `logging.py` |
| `phase/` | Season-phase state machine: `states.py`, `transitions.py`, `guards.py`, `helpers.py` |
| `alembic/` | 45 migrations, linear `down_revision` chain from `001` through `045` |
| `tests/` | pytest + pytest-asyncio, 253 tests (242 passing, 10 `xfail`, 1 integration-marked skip as of 2026-07-22) |
| `scripts/` | Durable CLI utilities (seed builders, rating fetchers, `purge_dba.py`, `query_db.py`) — one-off diagnostic/backfill scripts are not kept here; they're disposable once their fix has shipped |

## Invariants

- **`services/` never imports `discord`.** As of 2026-07-22, 11 of the ~84 service files still do (`batch_sim_runner.py`, `cpu_trade_proposals.py`, `cpu_trade_service.py`, `draft_service.py`, `fa_service.py`, `feedback_log.py`, `import_service.py`, `league_service.py`, `news_router.py`, `notifier_service.py`, `playoff_service.py`). Business logic should return plain payload dataclasses; posting to Discord is a presentation concern. `grep -rn "^import discord\|^from discord" services/` is the check — not yet clean, enforced incrementally as Phase 2 extractions land.
- **`data/repositories/` owns all SQL.** Raw `pool.fetch*/execute` calls scattered through `services/` should move into a repository module as each is touched. `grep -rn "pool\.\(fetch\|fetchval\|fetchrow\|execute\)" services/` is the check — same incremental enforcement as above.
- **`Announcer` protocol** (`services/announcer_protocol.py`) is the seam between business logic and Discord posting: `post_embed(channel_key, EmbedData)`, `post_text(channel_key, content)`. `channel_key` is the same string already passed to `league_repo.get_channel(pool, league.id, channel_key)` (e.g. `"trade-block"`) — this formalizes an existing convention rather than inventing a new one. `EmbedData` is a plain dataclass (title/description/color/fields/footer/thumbnail_url/image_url) covering the field usage seen across current `discord.Embed(...)` call sites. Every extraction in Phase 2 that currently posts directly to a channel implements or consumes this instead of importing `discord` itself; concrete adapters (`cpu_trade_announcer.py`, `sim_channel_announcer.py`) are added as part of that split, not before.

These two invariants are the organizing principle behind the two-largest-files split (`services/cpu_trade_proposals.py`, 4,274 LOC; `services/batch_sim_runner.py`, 3,653 LOC) — see the re-architecture plan referenced in `HANDOFF.md` for the current split status, and `trade-logic-rules.md` for the trade-evaluator behavior those files implement.

## Known non-architecture debt

- `README.md` previously claimed a `jobs/` and `notifier/` package split that never existed as anything but empty stub packages (removed 2026-07-22) — notifier logic lives in `services/notifier_service.py`.
- Cog and service files well over ~800 LOC beyond the two named above (`trade_evaluator.py` 1,819, `columnist_service.py` 1,531, `franchise_plan_service.py` 1,439, `ra_reasoning.py` 1,384, `trade_service.py` 1,352, `role_service.py` 1,163, plus oversized cogs) are lower-priority opportunistic splits — same Category 11 split + "cogs dispatch only" rule applies whenever normal feature work touches them.
