# Changelog — DBA Refactor

## 2026-05-13

- **Added `/playoffs sim-series [series_id]` command** — Lets commissioners simulate an entire playoff series to completion in one call instead of manually executing up to 7 individual `/playoffs sim-game` commands. Displays per-game score updates as the series progresses, then posts a series summary embed with automatic bracket advancement and round completion checks. Reduces worst-case R1 workload from 56 manual calls to 8. Includes proper `sim_round` phase gating (first playoff sim command to enforce this guard).

  Files: bot/cogs/playoff_cog.py
