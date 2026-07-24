# HANDOFF — dba

```yaml
last-model: claude-sonnet-5
last-session: 2026-07-24
state: yellow
```

## Next action

Only one loose end remains: push the `dba-site` commit (`17feab1`, sibling repo) to the `heroku` remote when ready — holding since that deploys the live site immediately.

## Traps

- **The IPC sidecar (`scripts/columnist_feedback.ps1`) and the Discord-reply feedback flow coexist by design** — don't rip either out for the other.
- **`current_game_date`/`sim_date` isn't threaded to most columnist register calls** — only `game_index` lands on most rows; resolve calendar date via a games-table lookup if needed.
- **10 pre-existing test failures are quarantined** via `@pytest.mark.xfail(strict=False)` (`test_setup_cog.py` ×8, `test_cpu_trade_acceptance.py` ×2). Full suite baseline: 456 passed, 10 xfailed, 1 skipped. A regression shows up as a NEW failure, not hidden inside these.
- **`dba-site` (sibling repo) has an unpushed commit** — command reference resync (`17feab1`) is committed but not pushed to the `heroku` remote, since pushing deploys the live site immediately. Needs explicit go-ahead.

## Do not touch

- None currently.

## Recent context

- 2026-07-24: Closed out both remaining sweep items that turned out to be stale, not real. (1) Discord intent toggle: ran `python main.py` directly — connected clean, 168 commands synced, no intent error; the "won't connect" trap had gone stale across sessions. (2) The 3 "unverified" trade-restructure fixes (LAC/Poeltl, NYK/Kuminga, DEN/Brooks) already had passing unit tests directly against the B5/B8 gate logic the whole time (`test_cpu_should_accept_contender_rules.py::test_contender_rejects_downgrade_with_lost_pick`, `test_apply_final_trade_gates.py::test_contender_lateral_swap_with_pick_rejected` and `::test_contender_2for1_without_upgrade_rejected`) — ran all 3 explicitly, confirmed passing. Also fixed the flaky `test_progression.py::test_high_potential_grows_more` (`2328d22`), resynced `Projects/dba-site`'s command reference (`17feab1`, unpushed), and split `sim_batch_hooks.py` out of `batch_sim_runner.py` (renamed `sim_orchestrator.py`, `c79adbb`) — the last opportunistic item from the sweep.
- 2026-07-22: Completed the full re-architecture sweep — Phase 0 (hygiene), Phase 1 (Announcer protocol seam), Phase 2 (both god-file splits), Phase 3 (6 oversized service files split), cog-splitting extension (6 oversized cogs split). See `docs/design/architecture.md`'s Split status sections; git log has full per-split detail.
- 2026-05-23: Bidirectional CPU trade proposals, B7 posture fix, B8 gate-parity helper, B5 retune. See session note `Brain/General Session Notes/2026-05-23 - DBA Trade Restructure - Bidirectional Proposals, B7 Fix, Marcus Prompt.md`.

---

Re-architecture sweep is fully done and verified. Only the dba-site push is outstanding. When that closes, delete this file — don't leave a stale "complete" handoff lying around.
