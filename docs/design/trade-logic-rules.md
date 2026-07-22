# Trade Logic Rules — CPU Trade Evaluator & Proposal Generator

Durable rule specs for the CPU trade evaluator (split 2026-07-22 into `services/trade_value_math.py`, `services/trade_context_builder.py`, `services/trade_grading.py`, `services/cpu_trade_acceptance.py`, and `services/trade_ai_reasoning.py` — see `docs/design/architecture.md`'s Split status for the breakdown) and `services/cpu_trade_proposals.py`. Each rule (B1-B8) records the observed problem, the evidence that justified it, and the proposed/actual fix — so a future change to trade logic can be checked against *why* the rule exists, not just *that* it exists.

**Current implementation status (as of 2026-07-22):** B1, B3, B6, B7 partially shipped via the trade-proposal restructure (`762a950`, `087a249`, `4be6242`, `803e24d`). B8 (gate parity on the outgoing-first path) is the highest-leverage unshipped item — ride-along run 4 showed all three surfaced problem trades (LAC, NYK, DEN) share the same root cause: `_attempt_outgoing_first_offer` doesn't call the same final-pass gates `_run_incoming_first_for_team` does. B2, B4 remain single-case / deferred. See `Brain/Note Pad/dba/marcus-cole-feedback-eval.md` for the columnist-side (Marcus Cole prompt) companion themes and ongoing ride-along evidence tracking.

---

## B1. Posture-mode should gate trade types

**Status:** READY (10+ cases across 3 runs) — depends on B7 being correct first, since gating on a wrong posture is worse than no gate.

**Evidence:** Many observed "doesn't make sense" trades violate the team's stated `posture.mode` — NYK accepting youth-for-win-now while in (presumed) `contender`/`play_in_fringe` mode; DEN trading core+pick for raw youth while in (presumed) `contender` mode; CHA going vet-heavy while still mid-rebuild around a recent draft pick.

**Rule:**
- A team in `contender` or `play_in_fringe` posture should heavily downweight (or reject) trades where they receive a player whose OVR < threshold AND age < ~25, unless the trade nets a clear consolidation (giving up surplus depth, not core).
- A team in `tanking` or `soft_rebuild` posture should downweight trades where they receive a vet over age ~30, unless that vet comes with picks or is on an expiring contract.
- Posture should be a hard check before the proposal is even scored, not just a tiebreaker.

## B2. Don't strip support around recently drafted top-5 picks

**Status:** OBSERVED (1 case — defer until corroborated)

**Evidence:** A team trading away vets that would mentor/pair with a recent top-5 pick (e.g. CHA around Brandon Miller) implies an implicit development-window commitment.

**Rule:**
- If a team has a player drafted in the top 5 within the last 2 seasons: downweight trades giving away players in the 24-29 age range with OVR ≥ 75 ("veteran support" archetype), and heavily downweight giving up future R1 picks.
- Edge case: a team can still trade away the top-5 pick itself if development is failing — that's a separate "cutting bait" signal, not the default.

## B3. Asset upside should factor into trade valuation, not just OVR

**Status:** READY (5+ cases across 4 runs; Kel'el Ware undervalued in every run — canonical regression case)

**Evidence:** The evaluator sees only OVR, not "ROY top-5 + young + high ceiling." Age premium has an observed shape: strong ≤22, fading 23-25, gone ~26+.

**Rule:**
- Valuation should add a positive modifier for: age ≤ 22, drafted in top 10 within the last 2 seasons, or currently in ROY/MVP/DPOY top 5.
- Modifier should scale — a 22yo OVR-78 player in the ROY top-3 should price closer to OVR-83.
- Applies to both proposal generation (don't offer them as filler) and acceptance (don't accept them as the headline return on a vet swap).

## B4. Multi-step strategy: don't leave a team mid-pivot with no plan

**Status:** READY (4+ cases across 3 runs) — hardest of the trade-logic changes; may ship after B1-B3.

**Evidence:** Trades that leave an obvious roster gap (e.g. a big-man hole) without a plausible followup read as incomplete/unrealistic.

**Rule:**
- After scoring a trade as acceptable, check whether it leaves the receiving team with a position-group hole. If yes: either queue a followup proposal in the same trade-window batch, or downweight the original trade — unless the team is in deep rebuild (holes don't matter when tanking).

## B5. Don't accept trades where one side is materially worse for no reason

**Status:** READY (10+ cases across 4 runs)

**Evidence:** GSW/NYK and DEN/TOR both went through despite one side getting a worse-than-fair return with no compensating strategic reason (cap relief, future picks). Run 4 added three more contender-overpays-or-downgrades cases (LAC/TOR Poeltl, GSW/NYK Kuminga, DEN/HOU Brooks) that the existing 15% raw-value differential threshold (now `services/cpu_trade_acceptance.py`, `_b5_threshold`) doesn't catch, because it's calibrated for value parity, not for "gave away a pick on a lateral swap" or "gave up two starters for one same-OVR youth piece."

**Rule:**
- Reject a trade where the accepting team's incoming value (after B3 modifiers) is materially below their outgoing value, with no compensating strategic gain (cap relief above a threshold, future R1s, posture-aligned reset).
- **Pick parity (contenders):** if you send a pick, you must receive either an equal-or-better-tier pick or an OVR upgrade ≥2 at the position of need.
- **2-for-1 consolidation (contenders):** if a contender ships 2 starters (OVR ≥ 75) for 1 player, that player's OVR must strictly exceed EACH outgoing player's OVR, or reject.

## B6. Trade logic should respect archetype/role distribution

**Status:** READY

**Evidence:** DEN/TOR Barrett — DEN gave up an elite role player for a ball-dominant primary initiator despite already running Jokić/Murray as ball-dominant cores; TOR gave up a player on a primary-creator trajectory for a same-age role-player archetype. Role data already exists (`recent_role_changes`: `iso_scorer`, `primary_initiator`, `post_anchor`, `two_way_wing`, etc. — see `services/role_service.py`) but wasn't weighed in valuation.

**Rule:**
- If the receiving team already has 2+ players in the same archetype the incoming player would slot into, downweight the trade unless it swaps out one of the archetype-overlapping players in the same deal.
- Treat archetype trajectory as a value modifier separate from OVR — a 22-25yo primary-creator archetype is worth more than a same-age/same-OVR role-player.

## B7. Posture-mode assignment can itself be wrong — upstream of every other gate

**Status:** READY — structurally different from B1-B6: those gate *on top of* the posture signal; this says the signal itself can be mislabeled, which breaks every downstream gate regardless of how well-tuned it is.

**Evidence:** NYK repeatedly makes trades that don't match a roster with Brunson + KAT (both All-Star caliber), across every ride-along run. `services/team_intel.py::build_team_intel` derives `posture.mode` from record + projected wins + age, and appears to under-weight roster star quality — a team can read `transition`/`soft_rebuild` off a rough record stretch even with 2+ stars on the roster.

**Rule:**
- Posture should be a function of roster quality + record + `franchise_plan.goal`, not record alone. A team with 2+ players at OVR ≥ 85 should not classify as `transition` or `soft_rebuild` off a mid-season record dip.
- If `franchise_plan.goal` is explicitly `contend`, posture should respect it unless evidence strongly contradicts (e.g. a genuine lottery-bound stretch).
- Spot-verify method: read a team's live posture mode + star count directly; if star_count is 0 despite qualifying players existing on roster, the count source is buggy (roster vs. lineup mismatch); if star_count is correct but mode still doesn't reflect it, the floor isn't being read on that code path.

## B8. Safety gates (B1, B5, B6, B7) must fire on the outgoing-first path too

**Status:** READY — highest-leverage unshipped item as of this writing.

**Evidence:** Run 4's three problem trades (LAC/TOR Poeltl, GSW/NYK Kuminga, DEN/HOU Brooks) all plausibly went through the outgoing-first path, which does not call the same final-pass gates the incoming-first path does. `_attempt_outgoing_first_offer` was shipped as "intentional minimal-viable" without B1/B5/B6/sanity-floor checks; ride-along evidence shows the cost.

**Rule:**
- After `_score_outgoing_pair` picks the winning `(counterparty, return)` pair in `_attempt_outgoing_first_offer`, run the same final-pass gates incoming-first applies before `trade_service.propose`: B1 posture gate, B5 asymmetric rejection, B6 archetype redundancy on the receiving side, and the sanity-floor/lopsided ratio check.
- These gates should be extracted into a single shared helper (`_apply_final_trade_gates`, already extracted in `cpu_trade_proposals.py` — target home is `services/trade_gates.py` once the Phase 2 file split lands, see `architecture.md`) callable from both code paths — never duplicated logic.
- Land B8 before retuning B5 thresholds — tightening a threshold that only runs on one of two code paths doesn't fix the outgoing-first regressions.
