# HANDOFF — dba

```yaml
last-model: claude-sonnet-5
last-session: 2026-07-24
state: yellow
```

## Next action

**RESOLVED 2026-07-24:** the Message Content Intent toggle was already on (or fixed silently at some point without this file being updated) — confirmed by running `python main.py` directly: connected clean, synced 168 commands to guild `1503802254346551318`, no intent error. This was a stale blocker in this file, not a real one. Next: verify the 3 pending trade-restructure fixes against the B5/B7/B8 rules in `docs/design/trade-logic-rules.md`: LAC shouldn't ship 3 players + 2nd for Poeltl; NYK shouldn't ship Bridges + Anunoby for Kuminga; DEN shouldn't ship Gordon + 2nd for Brooks. This needs an actual trade scenario run against real team state — either live in Discord or via a scripted repro — not yet scoped.

## Traps

- **The IPC sidecar (`scripts/columnist_feedback.ps1`) and the Discord-reply feedback flow coexist by design** — don't rip either out for the other.
- **`current_game_date`/`sim_date` isn't threaded to most columnist register calls** — only `game_index` lands on most rows; resolve calendar date via a games-table lookup if needed.
- **10 pre-existing test failures are quarantined** via `@pytest.mark.xfail(strict=False)` (`test_setup_cog.py` ×8, `test_cpu_trade_acceptance.py` ×2). Full suite baseline: 456 passed, 10 xfailed, 1 skipped. A regression shows up as a NEW failure, not hidden inside these.
- **`dba-site` (sibling repo) has an unpushed commit** — command reference resync (`17feab1`) is committed but not pushed to the `heroku` remote, since pushing deploys the live site immediately. Needs explicit go-ahead.

## Do not touch

- None currently.

## Recent context

- 2026-07-24: Fixed the flaky `test_progression.py::test_high_potential_grows_more` (`2328d22`) — under-powered statistical assertion, not shared state. Resynced `Projects/dba-site`'s command reference (separate repo, `17feab1`). Closed the last opportunistic item from the sweep: split `sim_batch_hooks.py` out of `batch_sim_runner.py` (renamed `sim_orchestrator.py`, `c79adbb`) — see `docs/design/architecture.md`'s Split status for why the discord-import invariant still isn't fully closed for these two files (DM sends need a separate abstraction, out of scope).
- 2026-07-22: Completed the full re-architecture sweep — Phase 0 (hygiene), Phase 1 (Announcer protocol seam), Phase 2 (both god-file splits: `cpu_trade_proposals.py`, `batch_sim_runner.py`), Phase 3 (6 oversized service files split), and a cog-splitting extension (6 oversized cogs split). See `docs/design/architecture.md`'s "Split status" / "Phase 3 splits" / "Cog splits" sections for the durable breakdown; git log has full per-split detail.
- 2026-05-23: Bidirectional CPU trade proposals, B7 posture fix, B8 gate-parity helper, B5 retune. See session note `Brain/General Session Notes/2026-05-23 - DBA Trade Restructure - Bidirectional Proposals, B7 Fix, Marcus Prompt.md`.

---

Re-architecture sweep (Phases 0-3, cog splits, and the opportunistic sim_orchestrator split) is fully done. Only 3 loose ends remain: the Discord intent toggle, the 3 unverified trades, and the unpushed dba-site commit. When those close, prune this back further or delete it — don't leave a stale "complete" handoff lying around.
