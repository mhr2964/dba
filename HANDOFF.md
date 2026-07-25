# HANDOFF — dba

```yaml
last-model: claude-sonnet-5
last-session: 2026-07-25
state: green
```

## Next action

**Plan C (Franchise Plan realism sweep) is shipped this session. Plans D (Role Assignment), E (Phase/State Transitions), and F (CPU Trade-Block Listing) are still queued** in the current 4-plan initiative — full spec for all four: `C:\Users\Owner\.claude\plans\swirling-stargazing-rabin.md`. Recommended order per that file: D → E → F (largest/most novel first, smallest/most mechanical last). No hard dependency between plans; each touches fully disjoint files.

**Plan C (Franchise Plan) shipped this session** — single builder agent, no worktree split (small enough scope: 3 files). FP1 (the main finding): `franchise_plan_service.derive_and_persist_all`'s reassessment/pivot checkpoint only fired once per top-level `sim_until_rival`/`sim_range` call, against standings snapshotted *before* any games in that call were simmed — even though a single call can span 50+ games across many internal ~10-game sub-batches. Fixed by adding `sim_orchestrator._maybe_refresh_franchise_plans` and calling it at the same per-sub-batch cadence `_maybe_run_cpu_trades` already uses (mid-batch flush + final flush, in both `sim_until_rival` and `sim_range`), alongside the existing pre-call snapshot derive. FP2: derive-failure logging upgraded from a bare `log.warning` to `log.error` with `exc_info=True`, team code, and game index — confirmed scoped-to-observability was the correct call (a failed derive never advances `last_derived_game_index`, so the next call naturally retries; no real retry mechanism needed). FP3: new `tests/test_franchise_plan_service.py` (9 tests, real DB, zero mocks on the service layer) — first-ever coverage of `derive_plan`/`persist_plan`/`get_or_derive`/`derive_and_persist_all` themselves. FP4 (no `franchise_plans` rows before the first in-season sim batch) confirmed DEFERRED per the plan — not touched. New `docs/design/franchise-plan-logic-rules.md` (FP1-FP4).

FP1's mandatory live smoke test drives the real `sim_orchestrator.sim_range` across 45 games in one call (engineered roster: avg_age≈32.75 + one OVR-90 star vs. a ~24-OVR-point-stronger opponent, so the pre-call snapshot at game 0 always derives `goal='win_now'`). A real 0-30 collapse pivots the persisted plan away from `win_now` (lands on `rebuild` via a full `derive_plan` re-run — see Traps for why that's *stronger* proof than landing on `_should_pivot`'s own "transition" label would have been). **Proven to fail pre-fix via `git stash` on `services/sim_orchestrator.py`**: `last_derived_game_index` stuck at 0, `goal` stuck at `win_now` for the full 45-game call; restored and reran to confirm post-fix pass.

Commits this session: `3f00f4a` (FP1), `2baeee3` (FP2), `29a0444` (FP3 tests), `d1ed4af` (docs). All tagged `[claude-sonnet-5]` with EXPECTED/VERIFIED-BY bodies.

Full suite: **802 passed, 1 skipped, 10 xfailed, 0 failed** (up from 793 passed pre-sweep; net +9 from FP3's new test file, zero regressions). Verified via the same two-batch foreground split as prior sweeps (see Traps).

## Traps

- **Full-suite pytest runs can silently die if backgrounded or run with too short a timeout in this environment** — root cause never identified across multiple sweeps, DB-level causes ruled out. Workaround (unchanged from prior sweeps): split `tests/test_*.py` into ~2 batches, run each in the **foreground** with a long explicit `Bash` timeout (500s+), redirecting output straight to a file rather than piping through `tail`.
- **`ruff` is not on PATH / not in `.venv`** — use the absolute path `C:\Users\Owner\AppData\Local\Programs\Python\Python312\Scripts\ruff.exe` directly.
- **Driving the real `sim_orchestrator.sim_range`/`sim_until_rival` entry points in a test requires patching `services.sim_orchestrator.get_pool` locally** (`patch("services.sim_orchestrator.get_pool", AsyncMock(return_value=db_pool))`) — it is NOT in `tests/conftest.py`'s `_GET_POOL_PATCH_TARGETS` list (that list only covers modules previously exercised this way). A bare `MagicMock()` guild with `.get_channel = MagicMock(return_value=None)` and no `league_channels` rows seeded is sufficient to no-op every `_maybe_post_*`/announcer call cleanly (each independently fetches its own channel and returns early when absent) — see `tests/test_franchise_plan_service.py::test_fp1_pivot_fires_mid_call_against_real_sim_range` for the full working pattern, and `tests/test_playoff_sim_live_smoke.py` for the same pattern applied to `playoff_service.sim_series_game`.
- **`_should_pivot`'s returned `new_goal` value is a log-line label only, not the actual next goal.** `derive_and_persist_all` calls it purely to decide *whether* to pivot and to compose the `FRANCHISE-PIVOT` log message; the persisted `goal` always comes from a full, independent `derive_plan` re-run against the live record. For a win_now collapse this can land on `rebuild` rather than `_should_pivot`'s own `"transition"` label if the live record is bad enough (e.g. 0-30) — this is correct behavior (proves the re-derive is genuinely live-driven), not a bug. Don't "fix" the two to match without checking which one a test/doc actually needs.
- **10 pre-existing test failures are quarantined** via `@pytest.mark.xfail(strict=False)` (`test_setup_cog.py` ×8, `test_cpu_trade_acceptance.py` ×2), unchanged this sweep. A regression shows up as a NEW failure, not hidden inside these.
- See prior HANDOFF history (git log on this file, or session notes) for traps specific to the now-closed Plans A/B (rollover, coaching AI) sweeps — segfault-prone `with (...)` blocks in `conftest.py`, alembic drift under concurrent worktrees, Docker Postgres flakiness under heavy load. Still generally true of this environment; not re-hit this session.

## Do not touch

- None currently.

## Recent context

- 2026-07-25 (latest): Shipped **realism sweep, Plan C (Franchise Plan)** — see "Next action" above for full detail. New `docs/design/franchise-plan-logic-rules.md`. Full suite 802 passed/1 skipped/10 xfailed (up from 793, zero regressions).
- 2026-07-25 (earlier): Shipped **realism sweep, Plan B (In-Game Strategy/Coaching AI)** — CA1-CA7 shipped (headline fix CA2+CA3 shipped together, verified with a mandatory live smoke test), CA8-CA10 deferred. `docs/design/coaching-ai-logic-rules.md`.
- 2026-07-25 (earlier): Shipped **realism sweep, Plan A (Season Rollover)** — RO1-RO6/RO8/RO10 shipped, RO7/RO9 deferred. `docs/design/rollover-logic-rules.md`.
- This is Plan C of a 4-plan initiative (D/E/F still queued) that itself followed three earlier, separately-tracked realism sweeps (trades/scheme, FA/draft/progression, playoffs/awards/HOF) and a multi-phase columnist-content overhaul — all tracked in `C:\Users\Owner\.claude\plans\swirling-stargazing-rabin.md`. Read that file's Execution Tracker before starting the next plan.
