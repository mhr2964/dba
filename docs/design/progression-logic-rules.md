# Progression Logic Rules — End-of-Season Player Development & Aging

Durable rule specs for `services/progression_service.py` (per-player attribute progression, breakout/injury modifiers, retirement) and its handoff from `services/rollover_service.py`. Each rule (P1-P7) records the observed problem, the fix, and its status — mirrors `docs/design/trade-logic-rules.md`'s convention so a future change to progression logic can be checked against *why* the rule exists.

**Current implementation status (as of 2026-07-24):** P1-P6 shipped as part of a realism audit sweep covering free agency, draft, and progression/aging (companion to `trade-logic-rules.md`'s original trade/scheme audit). The larger version of P6 (role-fit-quality affecting development magnitude) is deferred — see P6 below.

---

## P1. Season off-by-one in the rollover -> progression handoff

**Status:** SHIPPED — verified real, live bug; highest-priority fix in this pass.

**Evidence:** `rollover_service.run_rollover` incremented `leagues.current_season` and only then set `current_phase = PROGRESSION_PENDING`. `bot/cogs/offseason_cog.py`'s progression command called `progression_service.run_progression(league.id, league.current_season)` — the already-incremented value. `_avg_minutes`/`_has_season_ending_injury` filter games/injuries by that season number, but the just-finished season's games/injuries were recorded under the OLD (pre-increment) season — so low-minutes penalties and season-ending-injury setbacks silently never fired in real play.

**Fix:**
- New column `leagues.pending_progression_season` (migration `047_pending_progression_season.py`).
- `run_rollover` sets it to the pre-increment `season` in the SAME statement that sets `current_phase = PROGRESSION_PENDING` (no extra round-trip).
- `offseason_cog.py`'s progression command reads `league.pending_progression_season` instead of `league.current_season`, with a fallback to `current_season - 1` + a logged warning for in-flight leagues that rolled over before this fix (column is NULL).
- The column is cleared back to NULL after progression completes successfully, so it can't be reused stale on a future run.
- Regression test: `tests/test_rollover.py::test_rollover_then_progression_uses_correct_season` — seeds a low-minutes game log and a season-ending injury under the pre-rollover season, runs the real `run_rollover` -> `run_progression` handoff via `league.pending_progression_season`, and asserts both penalties actually appear in `progression_log`. This test fails against the pre-fix code path.

**Related discovery (folded into this fix, same function):** `_avg_minutes`'s query filtered `g.status = 'completed'`, a status value no code path in the codebase ever writes (`data/repositories/game_repo.py` only ever sets `'scheduled'` or `'simmed'`). Even with the season number fixed, the low-minutes penalty would have kept silently never firing in real play. Changed the filter to `g.status = 'simmed'` to match what's actually written. **Not fixed here** (out of file scope for this domain, flagged for the FA/draft domains): `services/draft_service.py:55` and `services/fa_service.py:655` (`_get_team_wins`) both filter on `status = 'final'`, which is equally never written — those read paths are likely dead in production too and should be checked by whichever domain owns those files.

## P2. Breakout bonus should scale with potential

**Status:** SHIPPED

**Evidence:** The breakout bonus (`_process_player`, growth stage, `years_pro <= 3`) applied a flat 8% chance regardless of potential — a 60-potential player had identical breakout odds to a 95-potential one, despite `_potential_growth_weight(player.potential)` already being computed and available.

**Fix:** `breakout_chance = 0.02 + 0.12 * potential_growth_weight`. At `potential_weight ≈ 0.5` (potential ≈ 50) this reduces to 0.08, matching the old flat constant; it ranges from ~0.068 (potential 40) to 0.14 (potential 99). Test: `tests/test_progression.py::test_breakout_scales_with_potential` (seeded, 60 trials, high-potential breakout count must exceed low-potential's).

## P3. `test_high_potential_grows_more` flakiness

**Status:** SHIPPED

**Evidence:** The test's own docstring diagnosed genuine sampling noise on an unseeded RNG mean comparison — previously band-aided by bumping iterations 10->50 rather than fixing the root cause (no seed).

**Fix:** `random.seed(20260724)` at the start of the test, re-verified after P2 changed breakout odds. Iteration count reduced back down to 15 now that determinism removes the need for a large statistical cushion.

## P4. `potential` was never enforced as a ceiling on `overall`

**Status:** SHIPPED

**Evidence:** Pre-peak growth deltas are always `>= +1` and `peak_age_start` is independently randomized, so a low-potential player could in principle grind past their own "ceiling" given enough seasons — `potential` was descriptive, not load-bearing.

**Fix:** One-line clamp in `_process_player`, after the breakout block finishes adjusting `attr_values["overall"]` but BEFORE the injury-setback block runs: `attr_values["overall"] = min(attr_values["overall"], player.potential)` (with a `potential_ceiling` progression_log entry when it actually clamps something). Injury setbacks can still push a player below their potential — only the upper bound is clamped. Test: `tests/test_progression.py::test_potential_ceiling_not_exceeded` (seeded, 20 repeated progression cycles on a player one point below their own potential).

## P5. Active-roster age-driven retirement

**Status:** SHIPPED — the one player/manager-facing fix in this pass (can remove a manager's rostered player), confirmed wanted by the user before implementing.

**Evidence:** Retirement previously only fired for UNSIGNED free agents with `years_pro > 8` (`services/fa_service.py::close_fa`, ~line 513). A rostered 40-year-old veteran never retired regardless of how far they'd declined.

**Fix:** `_maybe_retire_player`, called from `run_progression` as a post-`_process_player` step per player (retirement is a roster-status change, not an attribute delta, so it's deliberately not folded into `_process_player` itself). Exact thresholds (tune here if too aggressive/lax):
- Gate: `stage == "decline"` AND `age >= 36` (`_RETIREMENT_MIN_AGE`) AND post-progression `overall < 68` (`_RETIREMENT_MAX_OVERALL` — calibrated against `draft_class_generator.py`'s own tier bands, where 55-70 covers second-round/late-first-round rookies; 68 sits just above that, i.e. "no longer even a fringe rotation piece").
- Chance: `retire_chance = min(0.40, 0.05 * (age - 35))` (`_RETIREMENT_CHANCE_PER_YEAR` / `_RETIREMENT_CHANCE_CAP`) — 5% at age 36, 25% at age 40, capped at 40% from age 43+.
- On hit: `roster_status = 'retired'`, `team_id = NULL`, active contract deactivated (`is_active = FALSE, terminated_reason = 'retired'` — mirrors the same soft-delete pattern used by every other contract termination path in the codebase), and a `progression_log` row with `reason = 'retirement'`.
- Test: `tests/test_progression.py::test_retirement_at_advanced_age` (seeded, 200 trials each at age 42 vs age 36, same low overall — old age must retire measurably more often).

## P6. Low-minutes penalty ignored by stable/decline stages; perverse `<=10`-game exemption

**Status:** SHIPPED (extension + threshold fix). Larger version DEFERRED (see below).

**Evidence:** The low-minutes penalty (`low_minutes: bool` param, halves deltas) only existed in `_build_deltas_growth`. `_build_deltas_stable` and `_build_deltas_decline` ignored it entirely, so a bench/poor-fit veteran progressed or declined identically to a starter getting real minutes. Separately, the threshold itself (`games_played > 10 and avg_min < 15.0`) EXCLUDED deep-bench players with `<= 10` games from any penalty at all — a player who barely played got a full, unpenalized roll.

**Fix:**
- `_build_deltas_stable(player, low_minutes)`: when `low_minutes`, shifts the `overall`/sub-attribute delta distributions' weights toward negative/neutral outcomes instead of halving (a stable-stage player not getting run should be more likely to slip, not stay flat).
- `_build_deltas_decline(player, age, low_minutes)`: when `low_minutes`, adds 1 extra point to the `overall` drop specifically — NOT a halving. Halving a negative delta via floor division (`-3 // 2 == -2`) would perversely make low-minutes veterans decline LESS than starters; the fix moves the same direction "not playing is bad for you" that growth-stage halving already leans (leniently) the wrong way for decline.
- `barely_played = games_played <= 10` now folds into the SAME penalty branch as `low_minutes` (`low_minutes = low_minutes or barely_played`) — a deep-bench player (including one with zero recorded games) is never accidentally exempted.
- Tests: `tests/test_progression.py::test_low_minutes_penalty_applied_in_stable_stage`, `test_low_minutes_penalty_applied_in_decline_stage` (pure, seeded, confirm decline gets worse not better), `test_barely_played_player_gets_penalty_not_exemption` (integration, <=10 games at a healthy per-game average must still get tagged `low_minutes`).

**Deferred:** the larger version of P6 — `role_scoring`/`role_service` fit-quality affecting development magnitude (e.g. a poor-fit starter developing worse than a good-fit one at the same minutes) — requires new cross-service coupling and its own calibration pass with real evidence, same reasoning the original trade audit used to defer evidence-gated items (see `trade-logic-rules.md` B2).

## P7. Test coverage

**Status:** SHIPPED — see individual P1-P6 entries above for the specific test names; this entry just tracks the sweep as a whole.

New tests: `test_low_minutes_penalty_applied_in_stable_stage`, `test_low_minutes_penalty_applied_in_decline_stage`, `test_barely_played_player_gets_penalty_not_exemption`, `test_breakout_scales_with_potential`, `test_potential_ceiling_not_exceeded`, `test_retirement_at_advanced_age` (all in `tests/test_progression.py`); `test_injury_setback_branch` (`tests/test_progression_integration.py` — not previously covered); `test_rollover_sets_pending_progression_season` and `test_rollover_then_progression_uses_correct_season` (`tests/test_rollover.py` — the last one is the actual P1 regression test, proven to fail against the pre-fix handoff).
