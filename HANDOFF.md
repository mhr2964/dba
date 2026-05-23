# HANDOFF — dba

B8 gate parity + B5 contender sub-rules (two commits, run 4 regressions).

```yaml
session: 2026-05-22
agent: backend-dev (claude-sonnet-4-6)
commits: 762a950 (B8), 7f96c2b (B5 retune)
branch: master
```

## did

### B8 — Gate parity on outgoing-first path

Extracted `_apply_final_trade_gates()` from the inline pre-propose checks in
`_run_incoming_first_for_team`. The helper runs six gates:
1. Sanity floor (mode-specific ratio threshold)
2. OVR sanity (no 10+ OVR giveaway)
3. Lopsided check (ratio outside [0.50, 2.00])
4. B1 posture gate (each incoming player must match team A's mode)
5. B5 asymmetric rejection (`cpu_should_accept` from proposer's POV)
6. B6 archetype redundancy on receiving side (2+ existing → reject)

`_run_incoming_first_for_team` now calls the helper instead of inline checks.
`_attempt_outgoing_first_offer` calls the helper after `_score_outgoing_pair`
picks the winner, before `trade_service.propose`.

### B5 retune — Contender pick-parity and 2-for-1 upgrade rules

Added `_CONTENDER_TIER_MODES` constant and two sub-rules to `cpu_should_accept`
after the existing 15% differential block:

**Sub-rule 1 (pick parity):** contender-tier team sending any pick with
net OVR change <= 0 → reject. Catches DEN/HOU (Gordon + 2nd → Brooks)
and LAC/TOR (depth + pick → Poeltl downgrade) from run 4.

**Sub-rule 2 (2-for-1 upgrade):** contender-tier team shipping 2+ starters
(OVR >= 75) must receive 1 player with OVR strictly greater than each
outgoing starter. Only fires when receiving <= 1 player (2-for-2 unaffected).
Catches NYK/GSW (Bridges + Anunoby → Kuminga) from run 4.

## found

- `_team_a_wants_player` for "contending" mode requires OVR >= 79 (comfortable urgency).
  B8 tests that use OVR 76 for incoming players will be blocked by B1 before B5.
  This is correct behavior — tests updated to use OVR 80+ to reach B5.
- The fleecing floor in `cpu_should_accept` fires before the new sub-rules if score
  ratios are below 0.85. B5 test fixtures calibrated to pass the floor.
- `test_progression.py::test_high_potential_grows_more` is a pre-existing flaky test
  (fails occasionally in full suite, passes in isolation). Not a regression.

## files-touched

- `services/cpu_trade_proposals.py` — `_apply_final_trade_gates` (new), refactored
  `_run_incoming_first_for_team` pre-propose block, wired into `_attempt_outgoing_first_offer`
- `services/trade_evaluator.py` — `_CONTENDER_TIER_MODES`, `_STARTING_QUALITY_OVR` constants;
  B5 sub-rules 1 and 2 in `cpu_should_accept`
- `tests/test_apply_final_trade_gates.py` — 4 new tests (B8)
- `tests/test_cpu_should_accept_contender_rules.py` — 9 new tests (B5 sub-rules)

## contract changes callers need to know

- `_apply_final_trade_gates` is a new module-level async function in
  `cpu_trade_proposals`. Signature: see docstring. Takes `postures` dict
  as final kwarg so both callers thread the live posture map.
- `cpu_should_accept` now rejects more aggressively for contender-tier modes.
  Callers that simulate `cpu_should_accept` in tests should expect False when
  a contender sends picks on laterals or does non-upgrade 2-for-1 consolidations.
- `_CONTENDER_TIER_MODES` is exported at module level in `trade_evaluator` for
  any future callers that need the same posture set.

## test counts

Pre-existing failures: 10 (test_setup_cog × 8, test_trade_evaluator × 2)
New tests: 13 (4 B8, 9 B5)
All pass: yes. No new failures introduced.
