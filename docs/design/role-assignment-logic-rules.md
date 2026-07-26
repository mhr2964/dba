# Role Assignment Logic Rules

Durable rule specs for per-player role derivation and persistence
(`services/role_service.py`, `services/role_scoring.py`) and the two team-change
paths that must wire a player into a role after a roster move
(`services/fa_service.py`'s FA/waiver signing, `services/draft_service.py`'s
draft pick — the latter already correct and used as the reference pattern).
Same convention as `docs/design/franchise-plan-logic-rules.md` and the other
realism-sweep design docs: each rule (RA1-RA4) records the observed problem,
the evidence that justified it, and the actual fix.

**Current implementation status (as of 2026-07-25):** RA1, RA2, RA3, RA4 all
shipped. This is Plan D of a 4-plan realism-sweep initiative covering
`services/` domains not yet audited by any of the five prior sweeps
(trades/scheme, FA/draft/progression, playoffs/awards/HOF, season rollover,
in-game coaching AI) plus Plan C (franchise plan, shipped earlier this
initiative); Plans E (phase/state transitions) and F (CPU trade-block
listing) are still queued — see `~/.claude/plans/swirling-stargazing-rabin.md`
for the full initiative tracker.

**Five prior, undocumented fixes in this exact domain** (personnel gating,
skill-conditioned scheme magnitudes, scheme-synergy penalty, role routing,
role-diversity nudge) were retroactively documented during the coaching-ai
sweep rather than here — see
`docs/design/coaching-ai-logic-rules.md`'s "Historical context: the prior,
undocumented realism pass" section for Findings #1-#5. They are not
re-documented in this file; RA1-RA4 below are new findings from this sweep.

---

## RA1. FA/waiver signings left a player with no role — a "roster ghost"

**Problem.** `fa_service.py`'s `_sign_player` (the single shared helper behind
both the FA offer-acceptance SIGN path and `claim_waiver`) created the
player's contract and marked them active, but never inserted a `lineups` row,
never called `invalidate_role_cache`, and never re-derived roles. Both
`sim_persistence._load_lineup_for_team` (which `INNER JOIN`s `lineups`) and
`role_service.derive_roles` require a `lineups` row to see a player at all —
without one, a signed/claimed player sat active and under contract but was
completely invisible to the sim engine (zero touch_share, zero minutes) until
some unrelated later event (a trade, an admin rebuild) happened to touch that
team and incidentally re-derive roles.

**Evidence.** `draft_service.py`'s `_add_drafted_player_to_lineup` already
solves this exact problem for the draft path — its own docstring documents
the "roster ghost" failure mode and states it mirrors `trade_service.py`'s
post-trade pattern (insert at `MAX(slot)+1`, then `invalidate_role_cache` +
`derive_and_persist_all_for_team`). That fix was simply never applied to the
FA/waiver path.

**Fix (SHIP).** Ported `_add_drafted_player_to_lineup`'s pattern verbatim into
`fa_service._sign_player`: insert a `lineups` row at `MAX(slot)+1` for the
receiving team, then `invalidate_role_cache` + `derive_and_persist_all_for_team
(silent_emit=True)`. No signature or contract change for `_sign_player`'s
callers — the fix is purely additive internal side effects. Both the FA
SIGN path and `claim_waiver` get the fix for free, since both route through
this one helper.

**Verification.** Mandatory live smoke test:
`tests/test_role_service.py::test_ra1_waiver_claim_wires_up_lineup_role_and_sim_touch_share`
drives the real `fa_service.claim_waiver` entry point against a seeded
test-DB league, then asserts (1) a `lineups` row exists, (2) `player_roles`
has a real role + positive touch_share, (3) the real (unmocked)
`sim_persistence._load_lineup_for_team` + `_stamp_role_data` path returns the
claimed player with a positive `_role_touch_share`. Proven to fail pre-fix via
`git stash` on `services/fa_service.py` (assertion 1 fails — no `lineups` row;
the player is entirely absent from `_load_lineup_for_team`'s result), then
shown to pass post-fix.

**Files.** `services/fa_service.py`, `tests/test_role_service.py`.

---

## RA2. Small-roster role assignment could leave zero primary scorer / zero defensive anchor

**Problem.** `role_scoring.py`'s `_derive_tendency_respecter` greedy algorithm
documents a guarantee: every team gets exactly one primary scorer and one
defensive anchor. Step 1 unconditionally sliced the bottom 3 OVR players into
depth roles (`bottom_3_ids = sorted_by_ovr[max(0, n - 3):]`) *before* Steps 2
(primary scorer) and 3 (defensive anchor) ran. For a roster of ≤3 players,
this consumed the entire roster into depth roles, leaving nothing for
Steps 2-3. For a 4-player roster, Step 1 took 3 and Step 2's single-player
primary assignment took the last one, leaving zero candidates for Step 3's
anchor pool. The existing test suite's smallest fixture was 12 players, so
this edge was completely unexercised — and RA1's bug above (a signed/claimed
player invisible to `lineups`) could leave a team's *effective* roster this
thin in practice, not just in a theoretical edge case.

**Fix (SHIP).** Added `_MIN_RESERVED_FOR_PRIMARY_AND_ANCHOR = 2` and capped
Step 1's depth slice: `reserved = min(n, 2); depth_count = max(0, min(3, n -
reserved))`, reserving at least `min(n, 2)` top-OVR players for Steps 2-3
before Step 1 claims anyone. For n≥5 this reproduces the original
unconditional bottom-3 slice exactly (`reserved=2` still yields
`depth_count=3`), so no behavior change for any previously-tested roster
size — verified by both a regression test pinning the n=5 boundary and by
hand-tracing the algebra.

**Verification.** Mandatory live smoke test:
`tests/test_role_service.py::test_ra2_tiny_roster_gets_primary_scorer_and_anchor_via_real_entry_point`
seeds a real 3-player roster and drives the real
`role_service.derive_and_persist_all_for_team` entry point, asserting at
least one persisted role is a primary-scorer role and at least one is a
defensive-anchor role. Proven to fail pre-fix via `git stash` on
`services/role_scoring.py` (all 3 players land in depth roles, 0 of either
guaranteed role persisted), then shown to pass post-fix. Additional pure-function
coverage in `tests/test_role_scoring.py` and real-DB coverage in
`tests/test_role_service.py` exercise roster sizes 1-5 explicitly.

**Files.** `services/role_scoring.py`, `tests/test_role_scoring.py`,
`tests/test_role_service.py`.

---

## RA3. `/coach role show` could display percentages that don't sum to 100%

**Problem.** `role_service.py` documents an invariant: "touch_share
normalised so the team total equals 1.0." `/coach role assign` (manual
override) inserts the raw `ROLE_REGISTRY[role]["touch_share"]` constant
directly with `locked = TRUE`. `persist_roles`' UPSERT guard
(`WHERE locked = FALSE`) means a locked row is never renormalized by the
auto-derive pass again. Net effect: the *displayed* percentage in
`/coach role show` (rendered by `bot/cogs/coach_cog.py`'s `_roles_embed`) for
a team with any manually-locked role could fail to sum to 100%, contradicting
the module's own documented invariant for what the user actually sees.

Confirmed **sim math is unaffected**: `sim_persistence._stamp_role_data`
renormalizes touch_share unconditionally regardless of `locked` state, so
this is a display-only bug, not a simulation-correctness one.

**Fix (SHIP, display-layer only).** Added `_renormalized_touch_shares(roles)`
in `bot/cogs/coach_cog.py`, which recomputes each row's share against the
live fetched-team total before rendering; `_roles_embed` now uses this
renormalized share instead of the raw stored fraction. No changes to
`sim_persistence.py`, `role_service.py`'s persistence/derivation logic, or how
locked rows are stored in the DB (they keep their raw registry constant as
the storage-layer source of truth — only the render path changed).

**Verification.** Standard test (no live smoke test needed, per the
sim-math-unaffected finding above):
`tests/test_coach_cog_role_display.py` seeds one locked manual override plus
several unlocked auto-derived roles (raw total ≠ 1.0), calls the real
`_roles_embed` function, and asserts the rendered percentages sum to exactly
100 — plus a `pytest.approx(1.0)` check on the underlying float shares.
Confirmed no regression: `tests/test_role_scoring.py` and
`tests/test_sim_persistence.py` re-run clean.

**Files.** `bot/cogs/coach_cog.py`, `tests/test_coach_cog_role_display.py`.

---

## RA4. Zero direct test coverage existed for role_service.py's DB-orchestration layer

**Problem.** Only the pure scoring/derivation functions in `role_scoring.py`
had test coverage (`tests/test_role_scoring.py`). `role_service.py`'s own
DB-orchestration layer — `derive_and_persist_all_for_team`, `persist_roles`,
the veto-loop/bulk-derive/constraint-sync logic — had none, which is exactly
the kind of gap that let RA1 and RA2 go unnoticed: both bugs live at the
boundary between the pure scoring logic and its real DB-backed callers.

**Fix (SHIP).** New `tests/test_role_service.py`: real-DB tests (no
service-layer mocks) covering `derive_and_persist_all_for_team` (full-roster
persistence, stale-row pruning for players no longer in `lineups`) and
`persist_roles` (invalid-role rejection, upsert-updates-in-place behavior,
locked-row preservation), plus RA1's and RA2's mandatory smoke tests. This
file was built independently by two sibling builder agents (one per RA1/RA2)
in separate git worktrees and merged by hand — both had defined a same-named
`_insert_league` seeding helper with different schemas; the second copy was
renamed to `_insert_league_ra2` during the merge to avoid one silently
shadowing the other, and the merged file was re-run end-to-end (12/12 passed)
to confirm the reconciliation was functionally correct, not just textually
non-conflicting.

**Files.** `tests/test_role_service.py` (new).
