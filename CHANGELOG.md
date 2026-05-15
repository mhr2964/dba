# Changelog — DBA

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
