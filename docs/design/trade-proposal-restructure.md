# Trade Proposal Restructure — Swap-Aware Bidirectional Initiation

**Date:** 2026-05-22
**Author:** architect (design only — no code)
**Scope target:** `services/cpu_trade_proposals.py`, light touch on `services/cpu_trade_service.py`
**Trigger:** User directive (verbatim above) + B6 over-correction at `cpu_trade_proposals.py:871-874`.

---

## 1. Summary

- Today the proposal generator only initiates one way: pick team A, pick a player on team B that A wants, then build a return package. The "what's going OUT of team A" is decided LAST, which is why the B6 archetype check at lines 871-874 had to use the entire `block_by_team[a.id]` as a stand-in — the actual outgoing player is unknown at scoring time.
- We restructure the per-team proposal loop around an explicit **mode dispatcher** that picks one or both of `incoming_first` (today's behavior, preserved) and `outgoing_first` (new). Mode is data-driven from posture + plan goal + surplus list + cap pressure.
- In **outgoing-first**, the outgoing player is the seed of the search. We rank counterparties for that specific player, derive the return they'd plausibly send, and score the (counterparty, return) pair. The archetype check becomes EXACT (post-trade roster is known) rather than heuristic.
- In **incoming-first**, we keep today's scan + score flow but split it into two passes: (a) score candidates *without* the B6 archetype penalty, take a top-K shortlist, (b) build the speculative return for each shortlisted candidate, then re-score with the EXACT archetype check applied to "current roster minus the players we're actually shipping out." The current heuristic at lines 871-874 is deleted; the band-aid stops existing.
- Scope estimate: ~600 net new lines, ~120 deletions/restructures inside `_attempt_one_offer`, two new top-level helpers (`pick_proposal_modes`, `_attempt_outgoing_first_offer`), one new field on the plan dict (`shop_intent`), backward-compatible. **~10-14 hours backend-dev** for one well-tested pass, or stage as B7-style two-PR rollout (mode dispatcher + outgoing-first as one PR, incoming-first re-score restructure as a follow-up).
- B4 (multi-step strategy) gets a partial unlock for free — see §7.

---

## 2. Current flow (one paragraph)

`cpu_trade_service._propose_round` calls `_build_cpu_trade_block` once per round (produces `block_by_team: dict[team_id, list[player_id]]` — who each team is willing to ship), then in a loop calls `_attempt_one_offer` N times. `_attempt_one_offer` (proposals.py:753-2213) does this:

1. Pre-load every CPU team's plan + context + R1 count once (lines 775-818).
2. Loop over teams A in shuffled order.
3. For each team A, if `_plan_a.surplus_player_ids` has anything, run `_league_scan_counterparties` to rank top-3 counterparties for A's best surplus player and use that ranking to ORDER the b-loop (lines 893-978). **This is NOT outgoing-first — it just reorders the inner loop. A still picks an incoming player from B's block.**
4. Compute team A's archetype distribution by subtracting A's entire trade block from A's roster, then counting (lines 871-874). **This is the bug** — block != outgoing player.
5. For each candidate player on B's block, score with `value × need × plan_bias × ctx × upside × archetype_penalty` (lines 1027-1229). Pick the highest.
6. Build the return package via `_build_return_package` (lines 1335-1345, body at 2540-2757) — surplus-first, then flex, with picks gap-filling. **This is the first time A's outgoing players are concretized.**
7. Maybe sweetener, ride-along, propose.

**Limitation:** the entire scoring pipeline runs before A's outgoing players are known. Any signal that depends on the post-trade roster (archetype count, positional distribution, cap-after, "did we just create a hole") is either a band-aid or impossible. The user's directive ("return package needs to be part of step 2's consideration") is asking us to fix this structurally.

---

## 3. Proposed flow

### 3.1 Two modes, one dispatcher

```
for team_a in shuffled(cpu_teams):
    modes = pick_proposal_modes(team_a, posture_a, plan_a, cap_state_a, roster_a)
    for mode in modes:
        if mode == "incoming_first":
            await _attempt_incoming_first_offer(team_a, ...)  # restructured from today's _attempt_one_offer
        elif mode == "outgoing_first":
            await _attempt_outgoing_first_offer(team_a, ...)  # new
        if proposed_count >= n_offers_for_round:
            return
```

The outer `_attempt_one_offer` becomes a thin dispatcher; the two mode functions own their respective scoring pipelines. Shared infrastructure (cp_plans, cp_contexts, cp_r1_counts, league scan helpers) stays in the dispatcher and gets passed down.

### 3.2 `pick_proposal_modes(team, posture, plan, cap_state, roster) -> list[Literal["incoming_first", "outgoing_first"]]`

**Inputs (all already available — no new DB plumbing needed):**
- `posture`: `_compute_team_posture` result (mode + urgency).
- `plan`: franchise plan dict (has `goal`, `surplus_player_ids`, `asset_targets`).
- `cap_state`: derived from `cp_contexts[team.id].current_payroll` vs `league.salary_cap` and luxury threshold.
- `roster`: from `player_repo.get_roster`.

**Decision table (data-driven, narrow rules — fall back to `["incoming_first"]`):**

| Condition | Returns |
|---|---|
| Tank posture AND surplus list non-empty | `["outgoing_first"]` |
| Rebuild plan goal AND surplus_player_ids non-empty AND asset_targets non-empty | `["outgoing_first", "incoming_first"]` |
| Contender (win_now) AND no surplus | `["incoming_first"]` |
| Contender AND surplus_player_ids non-empty | `["incoming_first", "outgoing_first"]` (consolidation) |
| Cap-strapped (payroll ≥ luxury threshold) regardless of posture | `["outgoing_first"]` first, then `["incoming_first"]` if not over hard cap |
| Soft_rebuild with any surplus | `["outgoing_first"]` |
| play_in_fringe / developing default | `["incoming_first"]` |

Ordering matters in the list — when both modes are returned, the first one attempts first; the second runs only if the first didn't fire (i.e., dispatch breaks early once a proposal is produced for this `n_offers` slot). This keeps `n_offers` semantics unchanged.

### 3.3 Outgoing-first scoring: how to score `(team_b, outgoing_player)`

**Loop shape:**
```
for outgoing_pid in ordered_surplus_then_flex(team_a):
    receiving_candidates = await _league_scan_counterparties(outgoing_player, team_a, ...)
    # already exists at proposals.py:693-749 — reuse as-is
    for (team_b, cp_score, reason) in receiving_candidates[:5]:
        # derive what B would plausibly send back:
        speculative_return = await _derive_return_from_b(team_b, outgoing_player, team_a.asset_targets, ...)
        if not speculative_return:
            continue
        pair_score = _score_outgoing_pair(team_a, outgoing_pid, team_b, speculative_return, plan_a, posture_a)
        candidates.append((pair_score, team_b, speculative_return))
candidates.sort(reverse=True); pick top; propose.
```

**`_derive_return_from_b(team_b, outgoing_player, asset_targets_a, ...)`:**

This is the new logical inverse of `_build_return_package`. Given that B is the receiver, what would B send? Same plumbing in mirror:

- B's outgoing pool = B's surplus + flex players (NOT B's core). Already in plan dict.
- B's pick pool = `trade_repo.get_team_picks(pool, league.id, team_b.id)` with the same `mode`/`goal` driven `max_picks` logic that `_build_return_package` uses for team A.
- Target value to match = `target_value` = team-specific value of `outgoing_player` to B (use existing `trade_evaluator.player_team_specific_value`, same call signature already used in `_score_counterparty_for_target`).
- Bias the selection of what B sends back toward `asset_targets_a` — if A wants picks_r1, prefer picks from B's pool; if A wants young_u23, prefer B's flex players under 23; if A wants veterans, prefer B's surplus vets.
- Tolerance: same 25% as `_build_return_package`.

**`_score_outgoing_pair(team_a, outgoing_pid, team_b, speculative_return, plan_a, posture_a)`:**

The "do we want to make this trade" function from team A's perspective, applied to the FULL package, not just the incoming candidate:

- Base value: sum of team-specific values of `speculative_return` items to team A.
- Need-multiplier: count A's positional needs after subtracting the outgoing player AND adding the incoming players (now an EXACT post-trade calculation).
- `_plan_bias` from `asset_targets_a` — same logic that exists at lines 1122-1154, applied to incoming items.
- `_ctx_modifier` from `trade_context.compute_context_modifier` — apply per incoming player.
- `_upside_mod` per incoming player.
- **B6 archetype penalty: EXACT.** Compute `_team_archetype_counts(roster_a - {outgoing_pid} + speculative_incoming_players)`. Now the count is true. If the incoming archetype's count in that post-trade roster is ≥ 2, apply the penalty. No more guessing.

Return the aggregate score. The (team_b, speculative_return) with the highest score wins.

### 3.4 Incoming-first re-score restructure

Today's flow scores 1 candidate at a time using `_a_archetype_counts` computed from `roster_a - block_set_a`. Two issues: the block is a superset of the true outgoing, and we never re-evaluate after the actual return is built.

**Proposed two-pass:**

**Pass 1 — shortlist without archetype penalty:**
- Loop over B's block, score with `value × need × plan_bias × ctx × upside` (drop `_arch_penalty` here).
- Sort, take top-K (K = 3 is enough; the existing code already accepts the top-1).

**Pass 2 — speculative return + exact archetype re-score per shortlisted candidate:**
- For each of the top-K, call `_build_return_package` (already exists) to get the actual outgoing players for THIS specific candidate.
- Compute `_a_archetype_counts_post = _team_archetype_counts(roster_a - set(actual_outgoing_pids) + {candidate})`.
- Re-score with the EXACT `_arch_penalty` derived from `_a_archetype_counts_post`.
- Sort by re-score; pick best.

**Cost:** K extra `_build_return_package` calls per team A per round. Each is ~1 DB roster fetch + 1 picks fetch + a few contracts. Already cached at the request level for picks, but roster is not. K=3 means ~3x return-package cost per team A in the incoming-first path. Mitigation: memoize `player_repo.get_roster(team_a.id)` for the duration of `_attempt_incoming_first_offer` (cheap dict reuse).

### 3.5 B6 archetype check, per mode

| Mode | When does B6 fire? | Exactness |
|---|---|---|
| outgoing-first | At `_score_outgoing_pair` time, after `_derive_return_from_b` resolves the speculative incoming bundle | EXACT — outgoing is the seed, speculative incoming is known |
| incoming-first | In pass 2 after `_build_return_package` resolves outgoing for the shortlisted candidate | EXACT — outgoing is now known per candidate |

**Recommendation for the incoming-first restructure:** option (a) — build the speculative return BEFORE the final archetype check on a top-K shortlist. Option (b) (defer + re-score) is what we're doing, just phrased differently; option (c) (heuristic) is what we have today and what we're getting rid of. (a)/(b) cost is bounded (K=3), simpler than trying to invent a smarter heuristic, and produces the same answer the user would compute by hand. Pick (a).

---

## 4. Function-level changes

### Added

- **`pick_proposal_modes(team, posture, plan, cap_state, roster) -> list[str]`** — new pure function. Place near the top of `cpu_trade_proposals.py`, alongside `_urgency_allows_flex` (~line 54). No DB calls; reads dicts/objects already in scope at the dispatcher.
- **`_attempt_outgoing_first_offer(pool, league, season, team_a, cpu_teams, block_by_team, used_pairs, taken_player_ids, deadline_game_index, recently_signed_ids, guild, postures, cp_plans, cp_contexts, cp_r1_counts) -> int`** — new function. Signature mirrors `_attempt_one_offer` but takes the precomputed plan/context/r1 maps as inputs (no recomputation per-team). New file section, placed after `_attempt_one_offer`.
- **`_derive_return_from_b(pool, league, team_b, outgoing_player, asset_targets_a, taken_player_ids, recently_signed_ids, plan_b, posture_b) -> tuple[list[int], list[int], float]`** — new helper. Logical inverse of `_build_return_package`; can share the inner pick-ranking + value-accumulation logic if we extract a shared `_fill_to_value(...)` helper.
- **`_score_outgoing_pair(team_a, outgoing_pid, team_b, speculative_return, plan_a, posture_a, cp_contexts, roster_a_cache, archetype_counts_base) -> float`** — new pure scoring function. No DB. Roster + archetype counts passed in from the caller's cache.

### Modified

- **`_attempt_one_offer` (lines 753-2213)** — the body becomes a dispatcher:
  - Keep the precomputation block (775-818, cp_plans/cp_contexts/cp_r1_counts).
  - Replace the team-A loop body with `modes = pick_proposal_modes(...)` then dispatch.
  - The current b-loop scoring + return-building + ride-along + propose code (~lines 980-2213) moves WHOLESALE into a new `_attempt_incoming_first_offer` function. The bug-site lines 871-874 are removed; archetype count moves into pass 2 of that function.
  - Function rename optional but recommended: `_attempt_one_offer` → `_run_proposal_dispatch` to make it obvious the function no longer makes one offer; it dispatches modes. Update the single import in `cpu_trade_service.py` (line 15).

- **`_attempt_incoming_first_offer` (newly extracted from `_attempt_one_offer` lines 836-2213)** — the surgery inside:
  - Delete lines 871-874 (the `_a_archetype_counts` precompute against `block_set`).
  - Pass 1 scoring loop (lines 980-1229): drop `_arch_penalty` from the `_score = ...` line at 1228. Keep everything else.
  - After pass 1 sorts `_scored_candidates` (line 1235), take top 3 instead of best 1.
  - Pass 2 (NEW, ~40-60 lines inserted between line 1235 and 1248):
    - For each of top-3 (score, b, p, posture_b), call the existing `_build_return_package` with target_value computed for that specific p.
    - Compute exact post-trade roster archetype counts.
    - Recompute `_arch_penalty_exact` for the candidate's archetype against those exact counts.
    - Multiply score by `_arch_penalty_exact / 1.0` (since pass 1 didn't apply it).
  - After pass 2, pick highest re-scored candidate; bind `team_a`/`target_team`/`target_player` from that.
  - The rest of the function (lines 1248-2213: sweetener, ride-along, propose) is unchanged — `offer_player_ids` from the chosen candidate's pass-2 return-package is what gets used.

- **`_build_return_package` (lines 2540-2757)** — unchanged externally, but extract the inner "fill list to value within tolerance" logic into a private `_fill_to_value(scored_items, picks, target_value, max_picks, tolerance) -> tuple[list, list, float]` helper that BOTH `_build_return_package` and the new `_derive_return_from_b` can use. ~30 lines of de-duplication; not strictly required for correctness but reduces drift risk.

- **`_team_archetype_counts` (lines 2877-2904)** — unchanged signature. Callers change: today it's called once per team A with `roster - block_set`. After the restructure it's called per (outgoing_pid, candidate) combo with the exact post-trade roster. Cost goes up; acceptable because the call is pure-Python over ~15-player rosters.

- **`pick_proposal_modes` should consider `cap_state`** — to derive `cap_state` cheaply, read `cp_contexts[team_a.id].current_payroll` (already memoized) and compare to `league.salary_cap` plus a hard-coded luxury multiplier (~1.18). No new DB calls.

### Deleted

- **Lines 871-874** (the `_a_block_set` archetype precompute) — replaced by pass-2 exact computation.
- Optional: **`_league_scan_counterparties` call site at lines 893-978** can move into `_attempt_outgoing_first_offer` and out of the dispatcher entirely. The league-scan-as-ordering-hint pattern is incoming-first-flavored ("which counterparty should I call first about my surplus player?") but the outgoing-first flow uses the same primitive more naturally ("for THIS surplus player, who would buy?"). Keep the function; relocate the call.

### Surplus-list field (`shop_intent`) — recommendation

`plan.surplus_player_ids` ALREADY EXISTS in the franchise plan schema (`franchise_plan_service.py:1129` writes it; `cpu_trade_proposals.py:898` reads it). No new field needed for the basic outgoing-first signal.

**OPTIONAL enhancement (not blocking):** add a `shop_intent: dict[int, str]` field that maps each surplus player_id to a reason — `"age_misfit"`, `"cap_dump"`, `"positional_logjam"`, `"flip_asset"`. Today the surplus categorization in `_categorise_players` already knows these reasons (age window violations etc., see `franchise_plan_service.py:702-787`), it just throws the reason away. Surfacing it would let `pick_proposal_modes` make better choices ("cap_dump surplus → outgoing-first regardless of posture") and would feed B4 plumbing (flip_asset is the multi-step-strategy marker). Flag this as **NEW FIELD with migration** — `derived_from_record` JSONB column already exists so this can land WITHOUT a schema migration (embed in `derived_from_record.shop_intent`). Treat as a follow-up after the core restructure ships.

---

## 5. Open questions / decisions needed from the user

1. **Backward-compat strictness.** The incoming-first re-score pass 2 will change scoring for teams currently using only incoming-first. Reason: the archetype check becomes EXACT instead of heuristic — the new behavior matches user intent but the specific proposal a team makes on a given seed may differ. Acceptable, or do you want a feature flag that keeps today's heuristic until you've eyeballed a few cycles?

2. **`shop_intent` enrichment — now or later?** It's a small write-side change in `_categorise_players` plus a read-side consumer in `pick_proposal_modes`. Doing it now makes `pick_proposal_modes` smarter (cap-dump teams stop trying incoming-first); doing it later means `pick_proposal_modes` uses cruder rules but the core restructure ships sooner. Recommendation: ship the restructure first with crude rules, follow with `shop_intent` in a second PR.

3. **Mode selection rule for cap-strapped contenders.** A win_now team over the luxury line wants to dump salary AND wants to acquire. The decision table says outgoing-first first, then incoming-first. Is that the priority you want, or should they go incoming-first first (to identify the upgrade target) and then outgoing-first (to fund it)? This isn't a code question — it's a how-do-NBA-front-offices-actually-think question.

4. **`_attempt_three_team_deal` interaction.** The 3-team path (cpu_trade_proposals.py:162-565) is its own incoming-first flow with a value gap. Out of scope for this restructure? Or should the dispatcher route to it as a third mode? Recommendation: out of scope. The 3-team path is a niche escape valve; the restructure is about 2-team flows.

---

## 6. Scope and risk

### Scope estimate

- **New code:** `pick_proposal_modes` (~40 lines), `_attempt_outgoing_first_offer` (~250 lines, structurally a sibling of the incoming-first path), `_derive_return_from_b` (~120 lines), `_score_outgoing_pair` (~80 lines), `_fill_to_value` refactor (~40 lines extraction). **~530 new lines.**
- **Modified:** `_attempt_one_offer` reorganized into dispatcher (~30 lines after extraction) + `_attempt_incoming_first_offer` body (~600 lines, mostly relocated, with the pass-2 insertion of ~50 lines). **Net ~600 lines moved + 50 added + 30 deleted.**
- **Deleted:** lines 871-874 (B6 heuristic), maybe ~60 lines of duplicate fill-to-value logic if `_fill_to_value` extracted.
- **Touched files:** `cpu_trade_proposals.py` (heavy), `cpu_trade_service.py` (one import rename if `_attempt_one_offer` is renamed), optionally `franchise_plan_service.py` (only if `shop_intent` ships in the same PR).
- **Backend-dev hours:** **10-14 hours** if done as one pass with tests. Closer to 18-20 if `shop_intent` rides along.

### Rollout strategy

**Recommended: feature-flag gated, two-step.**

- Step 1 (one PR): add `pick_proposal_modes` and `_attempt_outgoing_first_offer`. Add an env var `DBA_PROPOSAL_DISPATCHER_V2=1`. When unset, `_attempt_one_offer` calls the existing flow unchanged. When set, it routes through the dispatcher. The flag means today's behavior is preserved bit-for-bit for any league not running the flag. Ship this; run a few headless cycles with the flag on; compare ride-along outputs against the unflagged baseline.
- Step 2 (second PR): when satisfied, remove the flag, delete the lines 871-874 heuristic, restructure incoming-first into the two-pass form. This is the "breaking change" PR — even with the flag removed, incoming-first behavior shifts because pass 2 changes scoring on a small minority of trades (the ones the heuristic was getting wrong).

### Rollback

- Step 1 rollback: unset the env var. Zero risk; behavior reverts.
- Step 2 rollback: git revert. Some plans/blocks accumulated under v2 will be fine — none of this writes new schema (except optionally `shop_intent` in `derived_from_record` JSONB, which old code ignores).

### Verification before merging step 2

- Run the ride-along headless harness for 1 season with v2 flag on, 1 season with flag off, same seed.
- Diff the `headless_logs/columnist_ride_along_*.jsonl` outputs. Spot-check 10 trades from each. Acceptance criteria: outgoing-first produces trades that look like sellers selling (the marcus-cole feedback themes A2/B1 are about this); incoming-first produces the same trades as today EXCEPT where the deleted lines 871-874 heuristic was firing wrongly (B6's intent was right; the implementation was the band-aid).

### Risks

- **[Correction — PR 2]** The PR 1 implementation used a synthetic `{salary: 0, years: 1}` shim for contracts in `_score_outgoing_pair` when the contract map was absent. This caused UNDER-valuing of incoming players (zero salary inflates perceived value less than a real contract), not over-valuing as originally described. PR 2 replaces the shim with an assert so the bug fails loud if the caller is wrong.

- **Risk:** `_derive_return_from_b` may diverge from `_build_return_package` over time, producing asymmetric value estimates that the actual cpu_should_accept then disagrees with. **Mitigation:** extract `_fill_to_value` as a shared helper; cover with one explicit test that calls both with mirrored inputs and asserts symmetry.
- **Risk:** the outgoing-first loop ranks K counterparties × O surplus players, each requiring contract fetches and pick lookups. Per round per team that's O(K·O) extra DB hits. **Mitigation:** the cp_plans/cp_contexts/cp_r1_counts memoization (lines 775-818) already exists; reuse it. Cap surplus loop at top-3 by value to bound work.
- **Risk:** dispatcher fires both modes for a team and produces two proposals when `n_offers` budget should only allow one. **Mitigation:** dispatcher returns early as soon as any mode produces a proposal (or returns 1).
- **Risk:** `pick_proposal_modes` mis-classifies a team (e.g., returns outgoing-first for a contender that should be buying). **Mitigation:** the decision table is narrow and falls back to `["incoming_first"]`; the failure mode is "same as today," not "weirder than today."

---

## 7. Interaction with existing themes

### B1 (posture-mode gating) — `_team_a_wants_player`, proposals.py:2761
Unchanged by this restructure. B1 fires in incoming-first only (it gates which incoming players A considers). The outgoing-first path doesn't need it because the "do I want this trade" check is the full `_score_outgoing_pair` aggregate, which already factors plan and posture. **No work.**

### B4 (multi-step strategy) — currently DEFERRED
Outgoing-first **structurally enables half of B4 for free**. The B4 case "team acquires player X intending to flip them later" works as: (1) cycle N, team uses incoming-first to acquire X, (2) cycle N+M, X is on team's surplus list, outgoing-first naturally fires and ships X. The "intending to flip" intent doesn't need to be explicitly modeled — surplus categorization already captures it (X gets categorized as surplus on the next plan derivation because they're age-misfit/positional-logjam for the team that just acquired them).

What's missing for full B4 is the "two trades scheduled together at acquisition time" pattern (CHA acquires vet bigman, simultaneously dangles LaMelo). The user's directive doesn't ask for that. The restructure plus a future `shop_intent: "flip_asset"` marker on freshly-acquired players gives 70% of B4 with no extra plumbing. Recommend: **call B4 partially unlocked; promote to fully READY after the restructure lands; final 30% (synchronous double-trade) stays deferred**.

### B6 (archetype redundancy) — current heuristic at lines 871-874
**Deleted by the restructure.** Replaced by exact computation in both modes. This is the most direct payoff of doing the structural change — B6's intent was always "don't double up on archetypes," and the heuristic only existed because the architecture made the exact check impossible at scoring time. Recommend: delete the heuristic in step 2 of the rollout (when v2 becomes default). Do NOT delete in step 1; the dispatcher-off path should remain untouched.

### B7 (posture floor) — already shipped, drives `pick_proposal_modes`
No change. `pick_proposal_modes` consumes posture mode as an input. The B7 fix to `compute_team_mode` (already in `trade_evaluator.py:303`) ensures the input is correct. The restructure rides on top of B7's already-good posture signal.

### Marcus Cole themes A2 / A4
A2 ("reference team trajectory") and A4 ("speculate about followup moves when a trade looks incomplete") are downstream — they live in the columnist persona prompt. The restructure doesn't touch them. BUT: outgoing-first proposals will produce trades that look like "ATL selling Trae" / "MIL flipping Lopez after acquiring Bitadze" — the exact patterns A2/A4 want Marcus to recognize. The restructure may **reduce the volume of "doesn't match where this team is" trades** that A1 currently has to call out, by producing trades that better match team trajectory at the source.

---

## 8. Handoff to backend-dev

Implementation order:

1. Extract `_fill_to_value` from `_build_return_package`. Verify tests still pass.
2. Add `pick_proposal_modes` pure function + a small set of unit tests (one per row of the decision table).
3. Add `_derive_return_from_b` using `_fill_to_value`. Symmetry test: same value target, same inputs → same package shape as `_build_return_package`.
4. Add `_score_outgoing_pair` pure function.
5. Add `_attempt_outgoing_first_offer`. Wire behind `DBA_PROPOSAL_DISPATCHER_V2` env var inside `_attempt_one_offer`. Ship as PR 1.
6. After PR 1 burns in: restructure incoming-first into two-pass (Pass 1 drop archetype penalty; Pass 2 build return + exact archetype recompute). Delete lines 871-874. Remove env var. Ship as PR 2.
7. (Optional, PR 3): add `shop_intent` to `derived_from_record` in `_categorise_players`; consume in `pick_proposal_modes` for cap-dump signals.

The user explicitly does NOT want a band-aid on lines 871-874. Step 2 of the rollout is the "real fix" — don't merge a heuristic improvement to those lines in the meantime.
