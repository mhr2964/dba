# Phase/State Transition Logic Rules

Durable rule specs for league phase-state-machine correctness
(`services/league_service.py::advance_phase`, the new `phase/graph.py`
adjacency table, `services/sim_batch_hooks.py`, `services/rollover_service.py`,
`bot/cogs/setup_cog.py`). Same convention as the other realism-sweep design
docs: each rule (PT1-PT5) records the observed problem, the evidence that
justified it, and the actual fix — so a future change to this domain can be
checked against *why* the rule exists, not just *that* it exists.

**Current implementation status (as of 2026-07-25):** PT1-PT5 all shipped.
This domain was explicitly identified and deferred during the season-rollover
sweep (RO9 in `rollover-logic-rules.md`) — this doc is that deferred sweep,
now completed. This is Plan E of the same 4-plan realism-sweep initiative that
shipped `franchise-plan-logic-rules.md` (Plan C) and `role-assignment-logic-rules.md`
(Plan D); Plan F (CPU trade-block listing) is still queued.

---

## PT1. `advance_phase` performed zero validation that a transition was a legal move between phases

**Status:** SHIPPED

**Evidence:** `services/league_service.py::advance_phase(league_id, new_phase)`
validated only that `new_phase` parsed as *some* member of the `Phase` enum
(`phase/states.py`), then ran a blind `UPDATE leagues SET current_phase = $1`
— no check whatsoever that the move was reachable from the league's actual
current phase. Confirmed 12 real call sites: 11 pass a hardcoded
`Phase.X.value`/literal string (not exploitable for an arbitrary jump, but
still unvalidated), and exactly one — `/league advance <phase_name>`
(`bot/cogs/setup_cog.py`) — takes commissioner-typed free text with only a
narrow "unsimmed games" guard scoped to the five playoff phases. Nothing
stopped, for example, `REGULAR_SEASON_ACTIVE` -> `PROGRESSION_PENDING` or
`NBA_FINALS` -> `SETUP` via that command.

**The naive fix doesn't work — the enum's declared order isn't the real
season cycle.** `phase/states.py`'s `Phase` enum lists its 20 members in one
linear declaration order, and the obvious fix is "legal next phase = next
enum member." Checked against every real `advance_phase` call site in the
codebase, that model is wrong in several places:

- `rollover_service.run_rollover` (routed through `advance_phase` by PT3,
  below) fires from either `OFFSEASON_AWARDS_CLOSED` or `DRAFT_LOTTERY_DONE`
  (`/offseason rollover`'s own precondition — `bot/cogs/offseason_cog.py`)
  and always lands on `PROGRESSION_PENDING` — skipping `DRAFT_IN_PROGRESS`
  and `POST_DRAFT_TRADES_OPEN` entirely when the commissioner rolls over
  before manually walking the league through the draft phases. Confirmed
  `awards_cog.py` and `draft_cog.py` never call `advance_phase` at all — the
  *only* way a league ever enters `OFFSEASON_AWARDS_OPEN`, `DRAFT_LOTTERY_DONE`,
  `DRAFT_IN_PROGRESS`, or `POST_DRAFT_TRADES_OPEN` in the first place is the
  commissioner manually running `/league advance` through each one in turn.
- `playoff_cog.py`'s champion-decided auto-advance jumps `NBA_FINALS` ->
  `OFFSEASON_AWARDS_CLOSED` directly, skipping the `OFFSEASON_AWARDS_OPEN`
  voting phase (`awards_cog.py`'s `/awards` commands never touch
  `current_phase` — voting can run while the league is still nominally in
  `NBA_FINALS`).
- `bot/cogs/offseason_cog.py`'s `/offseason progression` handler calls
  `advance_phase(league.id, Phase.FA_OPEN.value)` unconditionally from
  `PROGRESSION_PENDING` — even though `PROGRESSION_PENDING` is declared
  *last* in the enum (after `WAIVERS_OPEN`), this is the real, only way a
  league ever reaches `FA_OPEN`.
- `phase/helpers.py`'s own `_NEXT_COMMAND_HINT`/`PHASE_SUGGESTIONS` text
  confirms `WAIVERS_OPEN` -> `PRESEASON_READY` (via manual `/league advance`)
  closes the season loop into the next season — again out of the enum's
  declared order.
- `season_cog.py`'s `/season start` accepts either `SETUP` or
  `PRESEASON_READY` and always lands on `REGULAR_SEASON_ACTIVE` — a league's
  very first season can legitimately skip `PRESEASON_READY` entirely.
- `sim_batch_hooks._maybe_advance_season_complete` (PT4, below) must be able
  to close the season directly from `REGULAR_SEASON_ACTIVE` when a league's
  schedule never produced a computable trade-deadline game index
  (`game_repo.get_deadline_game_index` can return `None`), not only from
  `REGULAR_SEASON_POSTDEADLINE`.

**Fix:** New `phase/graph.py` — an explicit `NEXT_PHASES: dict[Phase,
frozenset[Phase]]` adjacency table (distinct from `phase/transitions.py`'s
`ALLOWED` map, which answers a different question: "which phases may this
*command* run in," used by `phase/helpers.require_phase` for per-command
eligibility gating — RO9's original "is this dead code" concern, already
corrected in `rollover-logic-rules.md`). `phase/graph.py` answers "which
phase may a league legally move *to* from its current phase," built from the
real evidence above rather than enum-index-plus-one, with every branch point
and out-of-declared-order edge documented inline in its module docstring.
`league_service.advance_phase` now reads the league's real current phase,
checks `phase.graph.is_legal_transition(current, new_phase)`, and raises a
`PhaseError` naming the current phase, the rejected target, and the actual
legal next phase(s) if the move isn't in the graph — instead of writing
blindly.

**Verification:** Mandatory live smoke test, case 2
(`tests/test_league_service.py::test_advance_phase_rejects_illegal_forward_skip`)
— seeds a real league at `REGULAR_SEASON_ACTIVE`, calls the real
`league_service.advance_phase(league_id, "FA_OPEN")` (a 13-phase skip),
asserts it raises `PhaseError` and that the DB row was never written. Proven
to silently succeed pre-fix via `git stash` on `services/league_service.py`
alone — every one of 5 new phase-validation tests (`DID NOT RAISE`) passed
with the pre-fix code, confirming zero validation existed:

```
FAILED tests/test_league_service.py::test_advance_phase_rejects_illegal_forward_skip
FAILED tests/test_league_service.py::test_advance_phase_rejects_backward_jump
FAILED tests/test_league_service.py::test_advance_phase_rejects_same_phase_noop
FAILED tests/test_league_service.py::test_advance_phase_error_message_lists_legal_next_phases
FAILED tests/test_league_service.py::test_advance_phase_nonexistent_league_raises_dba_error
5 failed, 8 passed, 10 deselected
```

`git stash pop` restored the fix; all 13 `advance_phase`-related tests in
`tests/test_league_service.py` pass post-fix. Additional tests lock in each
deliberate graph deviation described above (`OFFSEASON_AWARDS_CLOSED`/
`DRAFT_LOTTERY_DONE` -> `PROGRESSION_PENDING`, `NBA_FINALS` ->
`OFFSEASON_AWARDS_CLOSED`, `PROGRESSION_PENDING` -> `FA_OPEN`, `WAIVERS_OPEN`
-> `PRESEASON_READY`, `SETUP` -> `REGULAR_SEASON_ACTIVE`) as their own named
test, so a future accidental tightening of the graph fails loudly instead of
silently breaking a real command.

**Files:** `services/league_service.py`, `phase/graph.py` (new),
`tests/test_league_service.py`.

---

## PT2. `/league advance <phase_name>` had no clear rejection message for an invalid jump

**Status:** SHIPPED

**Evidence:** `/league advance` (`bot/cogs/setup_cog.py`) is the one
commissioner-typed, free-text call site identified in PT1 — the real
production attack surface for the missing validation. Before PT1's gate
existed there was nothing to reject in the first place; once it exists, a
`PhaseError` propagating uncaught would still reach the user via the global
`app_commands` error handler (`core.errors.handle_app_command_error`), but
that path is generic and this command already has its own explicit
"Unknown phase" `try/except` for the analogous bad-input case just above.

**Fix:** Wrapped the `league_service.advance_phase` call in a
`try/except PhaseError`, matching the command's existing local-catch style,
and responds with `exc.message` — the exact "Cannot advance from `X` to `Y`
— ... Legal next phase(s): ..." text PT1's gate produces — ephemerally,
instead of letting it fall through to the generic global handler.

**Verification:** New live-DB test
(`tests/test_setup_cog.py::test_advance_command_rejects_invalid_jump_with_clear_message`)
drives the REAL `LeagueGroup.advance` command callback (not mocked) against a
real seeded league at `REGULAR_SEASON_ACTIVE`, requests a jump to `FA_OPEN`,
and asserts the response text names both the current and rejected phases and
does not contain the generic "Something went wrong" fallback, plus asserts
the DB was never written. A companion regression test
(`test_advance_command_allows_legal_jump`) confirms a legal jump still
succeeds through the same command path.

**Files:** `bot/cogs/setup_cog.py`, `tests/test_setup_cog.py`.

---

## PT3. `rollover_service.py` wrote `current_phase` directly, bypassing `advance_phase` (and PT1's new gate) entirely

**Status:** SHIPPED

**Evidence:** `rollover_service.run_rollover` ran its own
`UPDATE leagues SET current_phase = $1, pending_progression_season = $2 WHERE id = $3`
— a second, fully independent write path to `current_phase` that PT1's new
phase-graph validation would never see or cover, no matter how complete the
gate in `advance_phase` became.

**Fix:** Split the statement: `pending_progression_season` is still set via
a direct `UPDATE` (unrelated to phase), and the `current_phase` write now
goes through `league_service.advance_phase(league_id, Phase.PROGRESSION_PENDING.value)`.
Confirmed against PT1's graph before switching the call site over, per the
plan's own ordering requirement: `/offseason rollover`'s precondition
(`bot/cogs/offseason_cog.py`) only ever calls `run_rollover` when
`current_phase` is `OFFSEASON_AWARDS_CLOSED` or `DRAFT_LOTTERY_DONE` — both
are legal predecessors of `PROGRESSION_PENDING` in `phase/graph.py` (see
PT1's branch-point evidence above), so no further graph changes were needed
to make this swap safe. This was the expected outcome, not a case requiring
the fallback "handle it correctly" investigation the plan flagged as a
possibility.

**Test-data fix required:** Three pre-existing tests in
`tests/test_rollover.py`, plus one in `tests/test_offseason_cog_progression.py`,
seeded their test league directly at `'PROGRESSION_PENDING'` (or, in the
offseason_cog test, `'REGULAR_SEASON_ACTIVE'`) and then called
`rollover_service.run_rollover` directly — neither is a phase
`run_rollover` can actually be reached from in production, and neither was a
legal predecessor of `PROGRESSION_PENDING` in the new graph. Updated all four
seeds to `'OFFSEASON_AWARDS_CLOSED'`, the real precondition
`/offseason rollover` enforces. This was a pre-existing test-data smell (the
seeded phase was never checked against reality before this sweep), not a sign
PT3's fix was wrong.

**Verification:** Re-ran the existing RO1/RO3 rollover live smoke tests
(`tests/test_rollover.py::test_rollover_extension_activates_with_full_new_years_term`
and `::test_hof_induction_runs_after_progression_not_rollover`) plus the full
`tests/test_rollover.py` and `tests/test_offseason_cog_progression.py` suites
after the seed-phase fix — all pass, zero regressions. This is a call-site
swap, not new behavior, so no new smoke test was added beyond confirming the
existing ones still pass, per the plan's verification requirement.

**Files:** `services/rollover_service.py`, `tests/test_rollover.py`,
`tests/test_offseason_cog_progression.py`.

---

## PT4. `_maybe_advance_season_complete` didn't re-check `current_phase`, silently skipping `REGULAR_SEASON_POSTDEADLINE`

**Status:** SHIPPED — landed in the same change as PT1, per the plan's
non-negotiable ordering requirement (see reasoning below)

**Evidence:** `services/sim_batch_hooks.py`'s three phase-transition hooks
fire at different points in `sim_orchestrator.sim_until_rival`/`sim_range`'s
batch loop: `_maybe_advance_trade_deadline` fires at every sub-batch flush
(`REGULAR_SEASON_ACTIVE` -> `TRADE_DEADLINE_OPEN`, re-checking phase before
firing), `_maybe_close_trade_window` fires once at the *start* of a sim
entry-point (`TRADE_DEADLINE_OPEN` -> `REGULAR_SEASON_POSTDEADLINE`, also
re-checking phase), and `_maybe_advance_season_complete` fires once at the
very *end* of the whole call — but, unlike its two siblings, it called
`advance_phase(league_id, Phase.REGULAR_SEASON_COMPLETE.value)`
unconditionally once every regular-season game was simmed, without checking
what phase the league was actually in. A single long sim call (e.g.
`/sim season`) that crosses both the trade-deadline game index AND finishes
the season in the same call opens the deadline mid-call (via
`_maybe_advance_trade_deadline`) but never gets a chance to close it — that
only happens at the *start* of a subsequent call — so by the time
`_maybe_advance_season_complete` runs, `current_phase` is still
`TRADE_DEADLINE_OPEN`, and the unconditional call landed directly on
`REGULAR_SEASON_COMPLETE`, silently skipping `REGULAR_SEASON_POSTDEADLINE`
entirely.

**Why this had to ship in the same change as PT1, not after:** if PT1's
graph gate landed first without this fix, the very next real `/sim season`
call to cross both boundaries would flip from a silent, undetected
phase-skip into a hard `PhaseError` — since `TRADE_DEADLINE_OPEN`'s only
legal next phase in `phase/graph.py` is `REGULAR_SEASON_POSTDEADLINE`, not
`REGULAR_SEASON_COMPLETE`. Verified directly: with PT1's gate active and only
`services/sim_batch_hooks.py`'s fix reverted via `git stash`, the mandatory
smoke test below fails with a real, hard exception instead of a silent skip:

```
core.errors.PhaseError: Cannot advance from `TRADE_DEADLINE_OPEN` to
`REGULAR_SEASON_COMPLETE` — not a legal phase transition. Legal next
phase(s) from `TRADE_DEADLINE_OPEN`: REGULAR_SEASON_POSTDEADLINE.
```

**Fix:** `_maybe_advance_season_complete` now re-reads `current_phase`
before advancing, mirroring its siblings' pattern. If `current_phase` is
`TRADE_DEADLINE_OPEN`, it first steps through
`advance_phase(league_id, Phase.REGULAR_SEASON_POSTDEADLINE.value)` (closing
the trade window inline, since the sibling hook never got the chance to run
mid-call), then proceeds. If `current_phase` is neither
`REGULAR_SEASON_ACTIVE` nor `REGULAR_SEASON_POSTDEADLINE` at that point
(e.g. a re-entrant call after the season is already complete, or the
just-closed `TRADE_DEADLINE_OPEN` window), it returns `False` without
touching the DB rather than blindly stomping `current_phase`. The
`REGULAR_SEASON_ACTIVE` -> `REGULAR_SEASON_COMPLETE` direct edge (bypassing
`TRADE_DEADLINE_OPEN`/`REGULAR_SEASON_POSTDEADLINE` entirely) is legal in
`phase/graph.py` for the deadline-less-schedule case
(`game_repo.get_deadline_game_index` returning `None`).

**Verification:** Mandatory live smoke test
(`tests/test_sim_batch_hooks.py::test_pt4_season_complete_steps_through_postdeadline_when_boundaries_cross_mid_call`)
schedules a full real 82-game regular season between two real CPU teams and
drives the REAL `sim_orchestrator.sim_range` entry point across the ENTIRE
season in one call (`to_game_index=82`), spying on (not replacing)
`league_service.advance_phase` to record the real phase-write sequence.
Asserts the sequence contains `TRADE_DEADLINE_OPEN`, then
`REGULAR_SEASON_POSTDEADLINE`, then `REGULAR_SEASON_COMPLETE`, strictly in
that order, and that the league's final `current_phase` is
`REGULAR_SEASON_COMPLETE`. Proven to fail against the pre-fix hook via
`git stash` on `services/sim_batch_hooks.py` alone (PT1's gate in
`services/league_service.py` kept active, per this rule's own ordering
requirement) — the pre-fix hook hard-errors with the `PhaseError` quoted
above rather than reaching `REGULAR_SEASON_COMPLETE` through
`REGULAR_SEASON_POSTDEADLINE`; restored and reran to confirm all 10 tests in
`tests/test_sim_batch_hooks.py` (including this one) pass post-fix.

**Files:** `services/sim_batch_hooks.py`, `tests/test_sim_batch_hooks.py`
(new).

---

## PT5. Zero test coverage existed on `advance_phase`, the phase-graph gate, or hook ordering

**Status:** SHIPPED

**Evidence:** `tests/test_phase.py` covered only `phase/helpers.require_phase`
and `phase/transitions.is_allowed` (command-eligibility, not phase-graph
validation). `tests/test_league_service.py` had zero `advance_phase`
references. No test file exercised `services/sim_batch_hooks.py`'s three
hooks against a real DB or a real sim call at all.

**Fix:** Expanded `tests/test_league_service.py` with 13 new tests covering:
the ordinary legal-transition case, PT1's mandatory illegal-forward-skip and
backward-jump smoke tests, a same-phase no-op rejection, the error message's
content (feeds PT2), an unknown-phase-string `ValueError`, a
nonexistent-league `DBAError`, and one dedicated regression test per
deliberate graph branch point documented in `phase/graph.py`. New
`tests/test_sim_batch_hooks.py` (10 tests) covers `_maybe_advance_trade_deadline`
and `_maybe_close_trade_window` directly (fire/no-op cases),
`_maybe_advance_season_complete`'s direct-call contract (games-remain no-op,
normal postdeadline path, deadline-less direct path, re-entrant no-op), and
PT4's mandatory live smoke test.

**Verification:** These test files are the deliverable. Full suite run after
all of PT1-PT5 landed: 849 passed, 1 skipped, 10 xfailed — zero regressions
against the pre-sweep baseline of 824 passed, 1 skipped, 10 xfailed (25 new
tests: 13 in `test_league_service.py`, 10 in `test_sim_batch_hooks.py`, 2 in
`test_setup_cog.py`).

**Files:** `tests/test_league_service.py`, `tests/test_sim_batch_hooks.py`
(new), `tests/test_setup_cog.py`.
