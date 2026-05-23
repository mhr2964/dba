# HANDOFF — dba

B7 root-cause + B8/B5 BLOCKER fixes (3 commits, run 5 prep).

```yaml
session: 2026-05-22
agent: backend-dev (claude-sonnet-4-6)
commits: 087a249 (gates/B6/B3/NIT6+7), 4be6242 (B5-sub1/NIT5), 803e24d (B7)
branch: master
```

## did

### BLOCKER 1 — Outgoing-first iterates all candidates on gate failure

`_attempt_outgoing_first_offer` previously took only `all_candidates[0]`, the
highest-scored pair. If `_apply_final_trade_gates` rejected it, the function
returned 0 — even though 14+ (surplus × counterparty) combinations remained.

Fix: iterate `all_candidates` in score order; on gate rejection, log at
`log.info` + `_HEADLESS` print (NIT 7) and continue to the next candidate.
`used_pairs.add(pair_key)` deferred until a proposal actually fires (previously
poisoned the pair before gate even ran).

### BLOCKER 2 — B5 sub-rule 1 now exempts equal-or-better incoming pick tier

Spec: "if you SEND a pick you must RECEIVE either a pick of equal-or-better
tier OR an OVR upgrade ≥2."  Prior code only checked net_OVR_change <= 0 and
ignored incoming picks entirely.

Fix: compute `outgoing_best_tier` and `incoming_best_tier` (R1=1, R2=2).
Only reject when `incoming_best > outgoing_best` (i.e., worse or absent).
Receiving R1 when sending R2 is equal-or-better → allow.

### BLOCKER 3 — Remove catch-and-ignore around cpu_should_accept

`_apply_final_trade_gates` wrapped the `cpu_should_accept` call in
`except Exception: log.debug(...)` — a real bug in B5 logic would silently
pass every gate. Removed the catch-all; exceptions now propagate.

### BLOCKER 4 — B6 is soft-penalty in incoming-first; hard-reject in outgoing-first

Pre-B8, incoming-first applied B6 as a scoring penalty in pass-2 (×0.65/×0.85).
After B8 introduced `_apply_final_trade_gates`, B6 was hard-rejected from BOTH
paths via the helper — overriding the soft penalty intent.

Fix (Option B): removed B6 from `_apply_final_trade_gates` entirely.
- Incoming-first: B6 soft penalty remains in pass-2 (unchanged).
- Outgoing-first: B6 hard-reject applied inline before calling the helper.

`_apply_final_trade_gates` signature also cleaned up: removed unused params
`plan_a`, `plan_b`, `posture_b` (NIT 6). Both call sites updated.

### NIT 5 — Removed dead "win_now" from _CONTENDER_TIER_MODES

`cpu_team_mode` is always a posture string (`contending`, `play_in_fringe`, etc.),
never a plan goal. `"win_now"` was dead in that set.

### B7 part 1 — Floor preseason play_in_fringe + star at win_now

`_derive_goal_and_horizon` early-season block (games_played < 10) routed
`soft_rebuild/rebuilding → rebuild` and `contending → win_now` but had no
branch for `play_in_fringe + has_any_star`. NYK shape (avg_age ≈ 28-29, KAT
OVR 89) fell through to `("transition", 2)`.

`last_derived_game_index` stays at 0 preseason, so the plan never re-derived.
Downstream: `cpu_trade_proposals` reads the plan goal as "transition" →
Bridges + Anunoby classified as flex/tradeable.

Fix: added `if mode == "play_in_fringe" and has_any_star: return "win_now", 2`
before the fall-through. Mirrors the identical post-record check.

### B7 part 2 — Fix swapped SQL args in cpu_trade_posture plan_goal lookup

`franchise_plans` WHERE clause: `$1=league_id, $2=team_id, $3=season`.
Args were passed as `(league.id, league.current_season, team_id)` — season and
team_id transposed. Query always returned None; `plan_goal` was always None.

Fix: reorder to `(league.id, team_id, league.current_season)`, matching
`trade_service.py:246` and `team_intel.py:167`.

## found

- BLOCKER 4 resolution chosen as Option B (remove B6 from helper, inline it
  in outgoing-first) because Option A would require returning a score from the
  helper — changing its contract for all callers.
- The soft-penalty for B6 already fires in pass-2 for incoming-first; the gate
  helper no longer touches B6 at all. Existing pass-2 code is unchanged.
- NIT 7 (outgoing-first gate rejection visibility): logs now fire at `log.info`
  plus a `_HEADLESS` print per rejected candidate, matching incoming-first.

## files-touched

- `services/cpu_trade_proposals.py` — outgoing-first iteration loop, B6 inline
  hard-reject, B3 catch removal, `_apply_final_trade_gates` signature (NIT 6),
  both call sites updated, NIT 7 log level
- `services/trade_evaluator.py` — B5-sub1 pick-tier exemption, NIT 5 dead entry
- `services/franchise_plan_service.py` — early-season `play_in_fringe + star` floor
- `services/cpu_trade_posture.py` — SQL arg order fix
- `tests/test_apply_final_trade_gates.py` — updated 3 existing calls (removed
  plan_a/plan_b/posture_b), added test 5 (next-candidate iteration), test 6 (B6 soft penalty)
- `tests/test_cpu_should_accept_contender_rules.py` — 3 new BLOCKER 2 tests
- `tests/test_franchise_plan_early_season.py` — NEW (5 tests, B7-part1)
- `tests/test_cpu_trade_posture_sql_args.py` — NEW (1 test, B7-part2)

## contract changes callers need to know

- `_apply_final_trade_gates` signature changed: `plan_a`, `plan_b`, `posture_b`
  removed. Any caller outside the two existing call sites must be updated.
- `_apply_final_trade_gates` no longer hard-rejects B6. If a new caller needs
  B6 enforcement it must apply it inline (as outgoing-first now does).
- `_CONTENDER_TIER_MODES` no longer contains `"win_now"` — check plan_goal
  separately if you need plan-goal gating.

## test counts

Pre-existing failures: 10 (test_setup_cog × 8, test_trade_evaluator × 2)
New tests added: 11 (2 gates, 3 B5-sub1, 5 franchise_plan, 1 posture SQL)
Total passing: 231. No new failures introduced.
