# Franchise Plan Logic Rules

Durable rule specs for CPU franchise strategic-direction derivation
(`services/franchise_plan_service.py`, `services/franchise_plan_math.py`,
`services/franchise_plan_production.py`), plus the two sim-orchestrator call
sites (`services/sim_orchestrator.py::sim_until_rival`/`sim_range`) that
invoke `derive_and_persist_all` once per top-level sim call. Same convention
as `docs/design/rollover-logic-rules.md` and the other realism-sweep design
docs: each rule (FP1-FP4) records the observed problem, the evidence that
justified it, and the actual fix — so a future change to this domain can be
checked against *why* the rule exists, not just *that* it exists.

**Current implementation status (as of 2026-07-25):** FP1, FP2, FP3 shipped.
FP4 deferred (documented decision — see its entry below). This is Plan C of a
4-plan realism-sweep initiative covering `services/` domains not yet
audited by any of the five prior sweeps (trades/scheme, FA/draft/progression,
playoffs/awards/HOF, season rollover, in-game coaching AI); Plans D
(role assignment), E (phase/state transitions), and F (CPU trade-block
listing) are still queued — see `~/.claude/plans/swirling-stargazing-rabin.md`
for the full initiative tracker.

---

## FP1. Reassessment/pivot checkpoint only fired once per top-level sim call, against pre-call standings

**Status:** SHIPPED

**Evidence:** `franchise_plan_service.derive_and_persist_all` is only ever
called from two sites: `sim_orchestrator.sim_until_rival` and `sim_range`,
each exactly once, at the very top of the call, using standings fetched
*before* any games in that call were simmed. But a single `/sim season` or
`/sim games count:80` call can simulate 50+ games across many internal
~10-game sub-batches — the same sub-batch loop `_maybe_run_cpu_trades`
(`services/cpu_trade_round_trigger.py`) already re-fires at every batch flush
(`_BOX_SCORE_BATCH_SIZE = 10`). `franchise_plan_math._is_reassessment_checkpoint`
and `_should_pivot` implement a real per-checkpoint gating mechanism (initial /
offseason_start / trade_deadline / mid_season_checkpoint / sticky), but
because `derive_and_persist_all` was never invoked again *within* the call,
that gate could only ever evaluate the standings snapshot taken at game 0 of
the batch — a team that collapsed or surged over the following 40+ games
simulated in that same call got zero pivot opportunity until the *next*
top-level sim command. Same sequencing-bug class as the rollover sweep's
RO1/RO3: a Phase-4 gating mechanism running at the wrong granularity relative
to the loop it's supposed to govern.

**Fix:** Added `sim_orchestrator._maybe_refresh_franchise_plans` (mirrors
`_maybe_run_cpu_trades`'s own swallow-and-log exception wrapper) and call it
at the exact same per-sub-batch cadence `_maybe_run_cpu_trades` already uses
— once at the mid-batch flush point (`len(batch_results) >= _BOX_SCORE_BATCH_SIZE`)
and once at the final/tail-batch flush — in both `sim_until_rival` and
`sim_range`, immediately before the `_maybe_run_cpu_trades` call at each of
those points. The original pre-call snapshot derive (at the very top of each
function) is kept as-is: it's still required to cover the zero-games-simmed
/ offseason-admin-call case where the sub-batch loop never runs at all.
`derive_and_persist_all`'s own internal stickiness gate
(`_is_reassessment_checkpoint`) means these extra calls are cheap no-ops
("sticky") for every team outside an active checkpoint window — this fix
changes *how often the gate is asked*, not the gate's own logic.

**Verification:** Mandatory live smoke test
(`tests/test_franchise_plan_service.py::test_fp1_pivot_fires_mid_call_against_real_sim_range`)
— seeds a real two-team league where one team's roster (avg_age ≈ 32.75, one
OVR-90 "star", rest OVR ≈ 58) satisfies `_derive_goal_and_horizon`'s
early-season `avg_age>=30 and has_any_star` branch, so the pre-call snapshot
(0 games played) always derives `goal='win_now'` — a real, reproducible CPU
decision, not a hand-seeded plan. The opponent roster is ~24 OVR points
stronger. 82 regular-season games are scheduled, but only 45 are simmed in
ONE real `sim_orchestrator.sim_range` call — spanning three internal
~10-game sub-batches past `_PIVOT_MIN_GAMES=30`. The weak team goes 0-30 in
real, unmocked simulated games; the persisted plan's `last_derived_game_index`
advances to 30 (not stuck at the pre-call 0), `derived_from_record` reflects
the live 0-30 record, and `goal` pivots away from `win_now` (a full
`derive_plan` re-run against the live record lands on `rebuild` — the
`_should_pivot`-returned "transition" label is only used for the
`FRANCHISE-PIVOT` log line, not applied literally; landing on the
independently-recomputed `rebuild` is *stronger* proof this reflects live
standings, not a hardcoded pivot target). Proven to fail against the
pre-fix cadence via `git stash` on `services/sim_orchestrator.py`:
`last_derived_game_index` stayed `0` and `goal` stayed `'win_now'` for the
full 45-game call; restored and reran to confirm it passes post-fix.

**Files:** `services/sim_orchestrator.py`.

---

## FP2. Derive failures were swallowed by a bare warning with no context or traceback

**Status:** SHIPPED (scoped to observability)

**Evidence:** `derive_and_persist_all`'s per-team `try/except` caught any
`derive_plan`/`persist_plan` failure and logged only
`log.warning("franchise_plan derive failed: league=%d team=%d season=%d — %s", ...)`
— no traceback, no game-index correlation, no retry/staleness tracking. Before
implementing a real retry mechanism, confirmed whether the *next* top-level
sim call already naturally retries derivation for that team: it does. A
failed attempt never reaches `persist_plan`, so `last_derived_game_index`
never advances past its prior (possibly `None`) value in the DB. On the next
call, `_is_reassessment_checkpoint` re-evaluates against that same stale
`last_derived_game_index` and fires the same checkpoint reason again
(`'initial'` if the team never had a plan at all, or the same
`trade_deadline`/`mid_season_checkpoint` window otherwise) — there is no
permanent-staleness path. FP1 also makes this retry happen *sooner*
(next sub-batch, not just next top-level command).

**Fix:** Scoped to observability only, per the plan's own guidance. Upgraded
the log call to `log.error` (a stale plan silently feeds trade decisions —
`cpu_trade_evaluation.py`'s `get_or_derive` calls and
`trade_block_builder._get_franchise_plan`'s read-only lookups — worth
surfacing above warning level) with `exc_info=True` for a real stack trace,
`team.nba_team_code` for human-readable correlation alongside the numeric id,
and the triggering `current_game_index` so a failure can be tied to the sim
batch that caused it.

**Verification:** New test
(`tests/test_franchise_plan_service.py::test_derive_failure_logs_with_league_team_game_context_and_naturally_retries`)
forces `derive_plan` to raise exactly once for a real seeded team via
`patch.object`, asserts an ERROR-level log record whose message contains the
league id, team code, and game index, asserts no partial `franchise_plans`
row was written, then calls `derive_and_persist_all` again (no code change,
no manual retry logic) and asserts the same team is picked up and persisted
successfully — the natural-retry claim, proven directly rather than assumed.

**Files:** `services/franchise_plan_service.py`.

---

## FP3. Zero test coverage for franchise_plan_service.py itself

**Status:** SHIPPED

**Evidence:** `tests/test_franchise_plan_math.py` and
`tests/test_franchise_plan_early_season.py` only covered the pure
classification/gating functions in `franchise_plan_math.py`.
`franchise_plan_service.py`'s orchestration layer — `derive_plan`,
`persist_plan`, `get_plan`, `get_or_derive`, and `derive_and_persist_all`'s
bulk-loop behavior (human-team skip, checkpoint-gated re-derive,
`last_derived_game_index` persistence, exception handling) — had never been
exercised against a real database. Same "the sequencing bug is structurally
untestable by the existing suite" gap this project's rollover sweep (RO3)
and other sweeps documented repeatedly; required regardless of which of
FP1/FP2 shipped, since FP1's fix needs a real DB-backed test to prove it.

**Fix:** New `tests/test_franchise_plan_service.py` (9 tests), all against a
real seeded Postgres test DB (no mocks on the service layer): basic
`derive_plan` shape, `persist_plan`/`get_plan` roundtrip, `get_plan` returning
`None` when absent, `get_or_derive`'s create-when-absent and
never-re-derive-when-present contract, `derive_and_persist_all`'s
human-managed-team skip and `last_derived_game_index` persistence, plus the
FP1 and FP2 mandatory live smoke tests described above.

**Verification:** `pytest tests/test_franchise_plan_service.py` — 9 passed.

**Files:** `tests/test_franchise_plan_service.py` (new).

---

## FP4. No franchise_plans rows exist before the first in-season sim batch

**Status:** DEFERRED — already disclosed, not a hidden bug

**Evidence:** `derive_and_persist_all` is only invoked from
`sim_until_rival`/`sim_range` — there is no seed-on-league-creation,
seed-on-draft-completion, or seed-on-offseason-transition call site. Before
the first in-season sim batch runs (preseason, immediately post-draft,
offseason trade windows), no `franchise_plans` row exists for any team.
`trade_block_builder._get_franchise_plan` already discloses this in its own
docstring/log line ("franchise_plan missing for team ... — derive_and_persist_all
may not have run this batch") and falls back to legacy `cpu_mode` heuristics
until a plan is derived.

**Reasoning for deferring:** Fixing this requires a design decision about
*when* a plan should first be seeded — on league creation? on draft
completion? on the first offseason trade window open? — each a different
design tradeoff (a plan seeded before the roster is finalized by the draft
would need to be re-derived anyway) with no ground truth in the existing
code the way FP1-FP3's fixes had. Same reasoning class as the rollover
sweep's RO7 deferral: inventing a rule here risks the exact "silently wrong
forever" failure mode this sweep exists to fix, when the current behavior is
already a disclosed, graceful fallback rather than a crash or silent
misclassification.

**Files:** *(deferred — no files touched)*
