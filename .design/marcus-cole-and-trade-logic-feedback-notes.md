# Marcus Cole & Trade Logic — Feedback Notes

Running notes capturing themes from columnist ride-along feedback runs. Two
surfaces are in scope: Marcus Cole's analysis prompt (`services/personas/marcus_cole.py`)
and the CPU trade logic (`services/trade_evaluator.py`, `services/cpu_trade_proposals.py`,
likely `services/team_intel.py` for new signals). Build phase deferred until more
feedback has accumulated.

## How to use this doc

- Each ride-along run produces a JSONL at `headless_logs/columnist_ride_along_marcus_cole_<ts>.jsonl`.
- After a run, append new themes to the relevant section below. Cite the source
  trade (headline + JSONL article_id) so the build phase can re-read the source.
- Themes are observations about what the SYSTEM should do differently — not
  edits to specific articles. Concrete prompt rules / trade-logic checks belong
  in the "proposed rules" subsection of each theme.
- When a theme has 2+ independent supporting cases, mark it `READY` for build.
  Single-instance themes stay `OBSERVED` until corroborated.

---

## Source runs

| Run start (UTC)         | Persona      | JSONL log                                                                                    | Pauses |
| ----------------------- | ------------ | -------------------------------------------------------------------------------------------- | ------ |
| 2026-05-22T00:11:12Z    | marcus_cole  | `headless_logs/columnist_ride_along_marcus_cole_20260521_201112.jsonl`                       | 5      |

---

# A. Marcus Cole analysis themes

## A1. Call out asymmetric trade logic — name the loser side
**Status:** OBSERVED (2 cases)

**Evidence:**
- GSW/NYK (Anunoby + Bridges to GSW for Podziemski + Kuminga): user "doesn't understand why NYK makes this trade since they have a win-now core with Brunson and KAT."
- DEN/TOR (Barrett to DEN for MPJ + Jordan + pick): user "doesn't get why DEN went young for the pickup instead of looking for a star to pair Jokić with."

**Current behavior:** Marcus Cole describes each team's gain positively, even when one side clearly looks worse. He doesn't currently call out asymmetry or question motive.

**Proposed prompt rules:**
- Add: "If one team's return looks materially weaker than what they gave up given their roster context, name it. 'Why TEAM made this' or 'Sources are puzzled by TEAM's logic here' beats neutral both-sides framing."
- Add: "Both-sides cheerleading is BANNED when the deal is asymmetric. Pick one side to question if context supports it."

## A2. Reference team trajectory (mode), not just position fit
**Status:** OBSERVED (2 cases)

**Evidence:**
- ATL/POR (Okongwu + pick to POR for vets): user reads it as "Atlanta starting to sell" and "expects a Trae Young trade for youth and picks after this."
- MIA/WAS (Brogdon + Middleton to MIA for Ware): user frames it as "MIA timeline mismatch under Bam, limited Ware role" — i.e., posture-driven.

**Current behavior:** Marcus Cole describes positional fits and synergy but rarely says "this fits the team's current trajectory" or "this signals a pivot."

**Proposed prompt rules:**
- Add: "When `team_intel.posture.mode` is in context (e.g., `contender`, `play_in_fringe`, `soft_rebuild`, `tanking`), weave the team's directional arc into the read. 'This is a tanker buying picks' / 'This is a contender consolidating' — frame the deal in terms of the arc, not just the player."
- Already in voice_notes: `roster_fits` and `context_signals_per_player`. Add: posture mode as a first-class field to reference by name.

## A3. Asset upside beyond OVR — draft pedigree, ROY race, age
**Status:** OBSERVED (1 case)

**Evidence:**
- MIA/WAS Ware return: user said "they should have gotten more for Ware seeing as he's second in the ROY race and has high upside."

**Current behavior:** Marcus Cole references OVR explicitly (e.g., "Barrett's 79 OVR scoring punch"). He doesn't reference age trajectory, draft pedigree, or in-season race positioning.

**Proposed prompt rules:**
- Add: "OVR is not the only valuation signal. If a player is young (≤23), recently drafted high, or in an award race (ROY/MVP/DPOY top-5), reference that as a value modifier. A 76-OVR rookie ROY contender is worth more than a 76-OVR vet on a contract year."
- Plumbing: need `roy_rank`, `mvp_rank`, `draft_year`, `draft_pick` available in the trade context. Check `services/team_intel.py` and `services/awards_service.py` for existing hooks.

## A4. Speculate about followup moves when a trade looks incomplete
**Status:** OBSERVED (2 cases)

**Evidence:**
- CHA trade for vets around LaMelo: user said "only makes sense if they have a followup deal in the works for a good big man to replace Williams."
- ATL/POR Okongwu trade: user expected "a Trae Young trade for youth and picks after this."

**Current behavior:** Marcus Cole writes each trade as a self-contained story. No "what comes next" framing.

**Proposed prompt rules:**
- Add: "If the deal leaves an obvious roster gap or signals a directional pivot, close with one sentence speculating about the next move. 'Watch for a followup big-man add to round this out' or 'Don't be surprised if STAR is the next domino.' Optional — only when the gap or pivot is unambiguous."

## A5. Respect existing core continuity (recent high draft picks)
**Status:** OBSERVED (1 case)

**Evidence:**
- CHA trade: user noted "Brandon Miller who they recently got at 3rd in the draft is probably core for their future plans." A trade that strips support around a recently-drafted high pick should be flagged.

**Current behavior:** Marcus Cole references `core_player_ids` from plan context but doesn't currently weight "this team just used a top draft pick on PLAYER" as a constraint on what trades make sense.

**Proposed prompt rules:**
- Add: "If a team trades away support around a player they recently drafted in the top 5 (within last 2 seasons), name it as a tension point. 'CHA giving up depth in a season they just spent the #3 on Miller raises eyebrows.'"
- Plumbing: need recent draft picks (year + pick number + player) accessible in trade context. Check `services/draft_service.py`.

---

# B. Trade logic themes (CPU trade evaluator + proposal generator)

## B1. Posture-mode should gate trade types
**Status:** OBSERVED (4 cases — A1, A2 evidence applies here too)

**Evidence:** Many of the user's "doesn't make sense" trades violate the team's stated `posture.mode`:
- NYK accepting youth-for-win-now while in (presumed) `contender` / `play_in_fringe` mode
- DEN trading core+pick for raw youth while in (presumed) `contender` mode
- CHA going vet-heavy while still mid-rebuild around recent draft pick

**Proposed evaluator rules:**
- A team in `contender` or `play_in_fringe` posture should heavily downweight (or reject) trades where they receive a player whose OVR < threshold AND age < ~25 unless the trade nets a clear consolidation (e.g., giving up surplus depth, not core).
- A team in `tanking` or `soft_rebuild` posture should downweight trades where they receive a vet over age ~30, unless that vet comes with picks or is on an expiring contract.
- A team's posture should be a hard CHECK before the proposal even gets scored, not just a tiebreaker.

## B2. Don't strip support around recently drafted top-5 picks
**Status:** OBSERVED (1 case — A5 evidence applies)

**Evidence:** CHA trading away the kind of vets that would mentor / pair with Brandon Miller (their recent #3 pick) is the example. A team that just invested a top-5 pick has implicitly committed to a development window.

**Proposed evaluator rules:**
- If a team has a player drafted in the top 5 within the last 2 seasons:
  - Downweight trades that give away PLAYERS in the 24-29 age range with OVR ≥ 75 (the "veteran support" archetype).
  - Heavily downweight trades that give up future R1 picks (they need them for development around the existing piece).
- Edge case: a team can still trade away top-5 picks themselves if they're failing to develop — but that's a separate "we're cutting bait" signal, not the default.

## B3. Asset upside should factor into trade valuation, not just OVR
**Status:** OBSERVED (1 case — A3 evidence applies)

**Evidence:** Ware undervalued by trade evaluator because it sees only OVR, not "ROY top-5 + young + high ceiling."

**Proposed evaluator rules:**
- Player valuation function should add a positive modifier for:
  - Age ≤ 22 (high ceiling)
  - Drafted in top 10 within last 2 seasons (still pedigree premium)
  - Currently in ROY/MVP/DPOY top 5 (in-season production premium)
- Modifier should scale: a 22yo OVR-78 player in the ROY top-3 is closer to OVR-83 trade value than the 78 face value.
- This should affect both proposal generation (don't offer them as filler) and trade acceptance (don't accept them as the headline return on a vet swap).

## B4. Multi-step strategy: don't leave a team mid-pivot with no plan
**Status:** OBSERVED (2 cases — A4 evidence applies)

**Evidence:** CHA "only makes sense with a followup big-man deal." ATL "expects a Trae Young trade after this." Trades that create obvious unaddressed gaps shouldn't fire in isolation.

**Proposed evaluator rules:**
- After scoring a trade as acceptable, run a post-check: does this trade leave the receiving team with a position-group hole (e.g., 0 centers above replacement-level)? If yes:
  - Either: queue a follow-up trade proposal in the same trade-window batch (hard).
  - Or: downweight the original trade unless the team's already in deep rebuild (don't care about holes if you're tanking).
- This is the hardest of the trade-logic changes. Build phase may want to ship B1-B3 first and revisit B4 separately.

## B5. Don't accept trades where one side is materially worse for no reason
**Status:** OBSERVED (2 cases — A1 evidence applies)

**Evidence:** GSW/NYK and DEN/TOR both look like cases where the trade went through despite one side getting a worse-than-fair return, with no compensating strategic reason (cap relief, future picks, etc.).

**Proposed evaluator rules:**
- The accepting team's evaluator should reject a trade where:
  - Their incoming asset value (after B3 upside modifiers) is materially below their outgoing value, AND
  - There's no compensating strategic gain (cap relief above some threshold, future R1s, posture-aligned reset).
- Today the evaluator may be too willing to take the deal because it scores narrowly positive on raw production swap. The rejection threshold for asymmetric deals should be tighter.

---

# C. Open questions for future runs

- How often does Marcus Cole post articles about a team in `tanking` posture making a sensible move? (Establishes the prompt baseline — A2 / B1.)
- Do CPU trades involving recently-drafted players in their first 2 seasons currently happen? (If yes, frequency. If no, B2 may be moot.)
- Does the trade evaluator currently use `posture.mode` at all, or only OVR + cap? (Determines whether B1 is "add a hook" or "extend an existing weight.")
- What does the CHA-with-LaMelo-and-Miller archetype actually look like in the DB — does the bot recognize Miller as core, or is he flex/depth? (Determines whether B2 needs new plumbing or just better usage.)

---

# D. Build sequencing (when ready)

When themes are sufficiently corroborated to dispatch builders:

1. **Marcus Cole prompt update** — A1-A5 rules added to voice_notes. Single file. Quick win; reversible.
2. **Trade evaluator B1 (posture gating)** — likely the highest-impact trade-logic change. Hooks already exist via `team_intel.posture.mode`.
3. **Trade evaluator B3 (asset upside modifiers)** — touches valuation function. May need new plumbing.
4. **Trade evaluator B2 (draft pick continuity)** — needs plumbing from `draft_service.py`. Ship after B1+B3 are stable.
5. **Trade evaluator B5 (asymmetric rejection threshold)** — tune after B1-B3 are live; otherwise tuning is against a moving target.
6. **Trade evaluator B4 (multi-step) — DEFERRED** as a separate workstream. Most complex; not blocking.

Marcus Cole work and trade-logic work can run in parallel; they don't share files.
