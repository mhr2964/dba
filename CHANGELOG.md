# Changelog — DBA

## 2026-05-21
- **All 17 columnists persona-redesigned per architect spec** — Voice rewrites, tier-ranking arrows (Power List), marker schemes (Pat Chen, Marcus Cole), passthrough renderers (Coach Beat), emoji rules, schema-key translation rules, and headline-dedupe enforcement across `_assemble_trade_report`, `_assemble_potm`, `_assemble_tank_watch`. Verified 4/4 PASS on final dual-account testing (structure, schema cleanliness, content gates, phase guards all green).
  Files: services/personas/*.py, services/columnist_service.py, services/batch_sim_runner.py

- **Coach Beat columnist now posts** — Was silently failing every batch on a JSON serialization crash in the context payload; added `default=str` to `json.dumps()` to handle datetime and Decimal objects.
  Files: services/columnist_service.py

- **Carla Knox, Keisha Williams, and heavy-prompt personas no longer time out** — Increased `asyncio.wait_for` timeouts from 8–10s to 15–20s across all columnist generators, recovering ~39 timeouts per full season.
  Files: services/batch_sim_runner.py

- **Marcus Cole articles now gated on executed trades only** — Restructured Marcus Cole generation gate in the sim loop; pending commissioner trades (human-involved) no longer leak BREAKING articles before approval. Added observability logging for verification.
  Files: services/batch_sim_runner.py

- **Marcus Cole trade-skip events surfaced in logs** — Elevated Marcus Cole skip notifications from DEBUG to INFO level so the trade-execution gate is visible during normal operation.
  Files: services/batch_sim_runner.py

## 2026-05-15
- **`#standings` channel receives dedicated posts** — After each batch sim, a standings-only embed (East/West sorted by win%) now posts to the `#standings` channel instead of lumping all standings into `#box-scores`.
  Files: services/batch_sim_runner.py

- **Trade block shows league-wide snapshot** — On every `/block add` or `/block remove`, the `#trade-block` channel receives a full league snapshot embed showing every team's trade-block players grouped by team, replacing individual per-player cards.
  Files: commands/admin/trade_block.py

- **`/admin rebuild-lineups` command added** — New commissioner command rebuilds starter/minute assignments for all teams based on current OVR ratings, useful after roster reseeds that corrected OVR values.
  Files: commands/admin/admin.py

- **`/season import-players` embed improved** — Now shows "X teams imported, Y already up to date" instead of confusing "0 teams / 0 players" when all teams were already seeded.
  Files: commands/commissioner/season.py

- **Sim loop protected against Discord errors** — All `channel.send()` calls in `sim_until_rival` and `sim_range` are now individually wrapped in try/except blocks. Discord HTTP errors no longer abort mid-batch sims; errors log as warnings and the sim continues.
  Files: services/batch_sim_runner.py

## 2026-05-14
- **Records floors prevent early-season noise** — Scoring records now require 30+ pts (player) or 120+ (team), blowouts need 20+ margin. Triple-doubles unchanged. Prevents announcements from season-opening games.
  Files: services/records_service.py

- **Awards Races replaces Around the League** — New `_maybe_post_awards_races` fires after each batch (gated: 25+ games played) and calls Claude Haiku to generate live MVP/DPOY/ROY/6MOY odds using real cumulative stats. Posts to #league-news.
  Files: services/batch_sim_runner.py

- **CPU trades wired into sim pipeline** — `cpu_trade_service.maybe_initiate_round` now fires after each 10-game batch in `sim_until_rival` and `sim_range`. Trade announcements post to #transactions. Phase-gated (no-op outside REGULAR_SEASON_ACTIVE / TRADE_DEADLINE_OPEN).
  Files: services/batch_sim_runner.py, services/cpu_trade_service.py

- **Marcus Cole persona (trade insider)** — New Woj-bomb style columnist fires exclusively on CPU trades. Posts breaking-news style trade columns to #analysis. Not in regular rotation.
  Files: services/personas/marcus_cole.py

- **Dr. Pat Chen persona (film analyst)** — New tactical analyst examines shot selection, defensive schemes, and strategy using real team data from DB. Added to columnists rotation (5th slot).
  Files: services/personas/pat_chen.py

## 2026-05-13

- **Added `/playoffs sim-series [series_id]` command** — Lets commissioners simulate an entire playoff series to completion in one call instead of manually executing up to 7 individual `/playoffs sim-game` commands. Displays per-game score updates as the series progresses, then posts a series summary embed with automatic bracket advancement and round completion checks. Reduces worst-case R1 workload from 56 manual calls to 8. Includes proper `sim_round` phase gating (first playoff sim command to enforce this guard).

  Files: bot/cogs/playoff_cog.py
