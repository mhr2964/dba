# Season Rollover Logic Rules

Durable rule specs for the season-to-season transition pipeline
(`services/rollover_service.py`), plus the parts of contract extensions
(`data/repositories/extension_repo.py`), free-agency-side retirement
(`services/fa_service.py::close_fa`), and season-start validation
(`services/schedule_service.py::generate_season`, `services/roster_service.py`)
that this sweep found were entangled with rollover correctness. Same
convention as `docs/design/trade-logic-rules.md` and
`docs/design/playoffs-awards-hof-logic-rules.md`: each rule (RO1-RO10) records
the observed problem, the evidence that justified it, and the actual fix — so
a future change to this domain can be checked against *why* the rule exists,
not just *that* it exists.

**Current implementation status (as of 2026-07-25):** RO1, RO2, RO3, RO4, RO5,
RO6, RO8, RO10 shipped. RO7 and RO9 deferred (documented decisions — see their
entries below). This is the fourth realism sweep in this project
(`docs/design/{fa,draft,progression,playoffs-awards-hof}-logic-rules.md`
covered the prior three); Season Rollover was the last piece of season-to-
season connective tissue without a dedicated audit, despite running once for
every league, every season.

---

## RO1. Contract extension activation ran before contract aging, losing 1 year of term and mis-anchoring `signed_in_season`

**Status:** SHIPPED

**Evidence:** `services/rollover_service.py::run_rollover` called
`extension_repo.process_extensions_for_season` (which inserts a new active
contract with the full `new_years` term) *before* `_age_contracts` (which
unconditionally decrements `years_remaining` for every active contract in the
league) — the opposite of what `extension_repo.py`'s own docstring required
("Called during rollover after contracts are aged"). A 4-year extension
activated with `years_remaining = 3` before a single season had actually
elapsed. Separately, the new contract's `signed_in_season` was set to the
season that just ended, not the season the deal actually starts playing
under — inconsistent with every other contract-creation path (rookie
contracts, FA signings), and capable of throwing `hof_service._count_championships`'s
season-window match off by one for extended players.

**Fix:** Reordered `run_rollover` to call `_age_contracts` before
`process_extensions_for_season`. Added a `signed_in_season` parameter to
`process_extensions_for_season`, distinct from the `season` value still used
to match `activates_after_season`, set to `next_season`. Because aging now
runs first, a contract naturally reaching 0 this cycle is auto-expired
(`roster_status='free_agent'`) before the extension activates — the fix also
restores `roster_status='active'`/`team_id` on the extended player
immediately after inserting the new contract row, closing what would
otherwise be a new regression introduced by the reorder alone.

**Verification:** Mandatory live smoke test (`tests/test_rollover.py`) — seeds
a contract at `years_remaining=1` with a pending 4-year extension, runs the
real `run_rollover`, asserts `years_remaining == 4` and correct
`signed_in_season`. Proven to fail against the pre-fix call order
(`years_remaining == 3`) and pass post-fix. The existing isolated unit test
(`tests/test_strategy.py::test_process_extensions_activates`) calls the repo
function directly and structurally could not have caught this ordering bug.

**Files:** `services/rollover_service.py`, `data/repositories/extension_repo.py`.

---

## RO2. Rollover wiped ALL historical seasons' `standings_cache`, not just the season being reset

**Status:** SHIPPED

**Evidence:** `rollover_service._reset_game_state` accepted a `new_season`
parameter that was never referenced in its body — `DELETE FROM
standings_cache WHERE league_id = $1` had no season filter at all, despite
`standings_cache`'s primary key being `(league_id, season, team_id)`,
explicitly designed for multi-season retention. `/offseason history` queries
season-scoped `standings_cache` rows to show a past champion's regular-season
record; after the first rollover a league ever ran, every prior season's
record was gone, permanently.

**Fix:** Scoped the delete to `AND season = $2`, binding `new_season` (the
season about to start, whose rows don't exist yet — this makes the delete
defensive-cleanup-only for a retried/partial rollover, and doesn't touch
anything `/offseason history` needs).

**Verification:** Extended the existing real-DB `test_rollover_clears_standings_cache`
to seed a prior-season row and assert it survives rollover.

**Files:** `services/rollover_service.py`.

---

## RO3. Hall of Fame induction ran inside rollover, before progression ever set that season's retirement/`years_pro` state

**Status:** SHIPPED

**Evidence:** `hof_service.check_and_induct` was called from inside
`run_rollover` — a fully separate, earlier command than `/offseason
progression`. But it's `progression_service._maybe_retire_player`/`_process_player`
that actually sets `roster_status='retired'` and increments `years_pro` for
the season that just ended, and that only happens when the commissioner later
runs `/offseason progression`. So HOF's retirement-eligibility gate and
veteran-longevity path (`years_pro >= 15`) evaluated state that was a full
cycle stale for any player newly crossing a threshold, or retiring, in the
season that just ended.

**Fix:** Removed `check_and_induct` from `run_rollover` entirely. Added the
call to the `/offseason progression` command handler in
`bot/cogs/offseason_cog.py`, immediately after `progression_service.run_progression`
completes and before advancing phase to `FA_OPEN` — so induction now
evaluates the same season's freshly-updated state. Surfaced the result via a
new optional `hof_inducted` parameter on `progression_embeds.progression_summary_embed`
(previously buried in `run_rollover`'s return dict and never actually
rendered anywhere).

**Verification:** Mandatory live smoke test — seeds a player one cycle from
crossing a HOF threshold, runs the real `run_rollover` and confirms NOT YET
inducted (proving induction no longer fires against stale state), runs the
real `progression_service.run_progression` for that season, then confirms
induction now fires with fresh state. `tests/test_hof.py`'s pre-existing tests
all hand-seed state and call `check_and_induct` directly in isolation — by
construction, none of them could have caught this cross-command sequencing
bug.

**Files:** `services/rollover_service.py`, `bot/cogs/offseason_cog.py`,
`bot/embeds/progression_embeds.py`.

---

## RO4. FA-side retirement had no age gate, inconsistent with progression's shipped age-gated retirement

**Status:** SHIPPED

**Evidence:** `services/fa_service.py::close_fa`'s retirement branch retired
any unsigned free agent with `overall < 65 and years_pro > 8` — no
age/`birth_date` check at all, unlike progression's active-roster retirement
logic (already age-gated, per `progression-logic-rules.md` P5). A young bust
who simply sat unsigned for 8+ seasons got force-retired regardless of actual
age; a genuinely washed veteran with short tenure could never be retired via
this path.

**Fix:** Replaced the `years_pro > 8` condition with an age gate reusing
`progression_service._RETIREMENT_MIN_AGE` (36) and its `_compute_age` helper
(which falls back to `20 + years_pro` when `birth_date` is missing).

**Verification:** New `tests/test_fa_service.py` (this function had zero test
coverage before this sweep) — a young long-tenured player is no longer
retired post-fix; an old short-tenured player now correctly is. Both cases
confirmed to fail against the pre-fix tenure-only condition.

**Files:** `services/fa_service.py`.

---

## RO5. No roster-size floor enforced anywhere in rollover → FA → season-start

**Status:** SHIPPED (guard-rail, not automatic roster backfilling)

**Evidence:** Rollover's contract-aging step can mass-convert many players
per team to free agents in one shot with zero floor check; nothing downstream
(`roster_service.py`, `/season start`) validated per-team roster size before
locking in an 82-game schedule.

**Fix:** New `roster_service.get_teams_below_roster_floor(pool, league_id,
floor=8)`. Called from `schedule_service.generate_season` before building
schedule pairs; raises `ValueError` naming any under-floor team, mirroring the
function's existing "Expected 30 teams" check. This blocks season start and
tells the commissioner to sign more free agents — it does not auto-backfill
rosters (no clear "who gets added" policy exists, and inventing one is out of
scope). The floor of 8 is a disclosed judgment call, not derived from a
formula.

**Verification:** `tests/test_season_service.py` — an under-floor team blocks
`generate_season` and results in zero games written; a healthy league is
unaffected (regression check). `tests/test_roster_service.py` — unit coverage
of the new query.

**Files:** `services/roster_service.py`, `services/schedule_service.py`.

---

## RO6. Salary cap never grew season-over-season

**Status:** SHIPPED (disclosed placeholder rate)

**Evidence:** `leagues.salary_cap` was hardcoded at league creation and never
updated by any code path afterward — frozen forever across a multi-decade
simulated league, while every contract/trade valuation formula in the
codebase treats it as a live baseline.

**Fix:** Added `_SALARY_CAP_GROWTH_RATE = 0.03` (disclosed placeholder — real
NBA cap growth runs ~5-10%/year, but a smaller flat rate avoids destabilizing
existing trade-value formulas that implicitly assume a roughly-stable cap) to
`rollover_service.py`. The cap now grows in the same statement that advances
`current_season`, rounded to the nearest $100,000.

**Verification:** `tests/test_rollover.py::test_rollover_grows_salary_cap`.

**Files:** `services/rollover_service.py`.

---

## RO7. Unsigned free agents/waived players never age, decline, or progress

**Status:** DEFERRED — documented decision, not a silent scope gap

**Reasoning:** `progression_service.run_progression`'s query is
`WHERE roster_status = 'active'` only — an unsigned player is frozen in time
indefinitely. There is no ground truth anywhere in this codebase for what
"correct" unsigned-player treatment should look like (full progression?
decay-only? retirement-checks-only?) — each is a different design decision
with different balance implications, and none is derivable from existing
code the way RO1-RO6's fixes were. Inventing a rule here risks the exact
"silently wrong forever" failure mode this sweep exists to fix. Left as a
deliberate, disclosed scope boundary.

**Files:** *(deferred — no files touched)*

---

## RO8. `trade_block` listings never expired at rollover

**Status:** SHIPPED

**Evidence:** A trade-block listing from the season that just ended persisted
silently into the new season with no code path ever clearing it.

**Fix:** `trade_block_repo`'s read paths never filter by season — it's a pure
"current state" table, the same shape as `ready_status` (already cleared at
rollover in the same function). Added `DELETE FROM trade_block WHERE
league_id = $1` alongside the existing `ready_status` clear. No migration
needed.

**Verification:** Extended the RO2 standings_cache test to also seed and
assert a `trade_block` row is cleared.

**Files:** `services/rollover_service.py`.

---

## RO9. Phase state-machine has no validated transition graph

**Status:** DEFERRED — corrected finding, real gap reframed out of scope

**Evidence (correction):** Initial research claimed `phase/transitions.py`'s
`is_allowed()` was dead code. This is false — it's called via
`phase/helpers.require_phase`, used by 16 cog command handlers, and correctly
gates per-command phase *eligibility* today. The real, narrower gap is that
`league_service.advance_phase` performs zero validation that a transition is
a legal *move* between phases — it's a blind `UPDATE`, and the real season
cycle does skip several phases the enum's declared order implies should occur
(`DRAFT_LOTTERY_DONE`, `DRAFT_IN_PROGRESS`, `POST_DRAFT_TRADES_OPEN` between
rollover and FA).

**Reasoning for deferring:** This spans 6 files and 11 `advance_phase` call
sites, none of which is `rollover_service.py` — a repo-wide state-machine
consistency question, not a rollover-focused fix. Flagged for a future
dedicated phase/state-machine sweep.

**Files:** *(deferred — no files touched)*

---

## RO10. `rollover_complete_embed` read a dead dict key and had a stale footer

**Status:** SHIPPED

**Evidence:** Found via spot-check during this sweep's design review, not in
the original research pass. `bot/embeds/history_embeds.py::rollover_complete_embed`
read `summary["players_progressed"]`, a key removed from `run_rollover`'s
return dict in a prior refactor — every real `/offseason rollover` invocation
threw an uncaught `KeyError` after the DB rollover had already committed,
leaving the commissioner with an unhandled-command error instead of a
completion summary. The footer text also claimed the league was now in
`PRESEASON_READY` phase; the real next phase is `PROGRESSION_PENDING`.

**Fix:** Updated the embed to read the fields the summary dict actually has
(`season_archived`, `next_season`, `contracts_expired`, `extensions_activated`,
`picks_seeded`, `new_salary_cap`) and corrected the footer.

**Verification:** New `tests/test_history_embeds.py` — a plain unit test
against the real current summary-dict shape (no DB needed; this is a pure
function, and a test like this would have caught the bug on day one).

**Files:** `bot/embeds/history_embeds.py`.
