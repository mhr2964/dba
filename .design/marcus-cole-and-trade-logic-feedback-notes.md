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
| 2026-05-22T02:00:00Z*   | marcus_cole  | `headless_logs/columnist_ride_along_marcus_cole_<latest>.jsonl`                              | 4      |

\* approx; resolve from filename timestamp.

**Cross-run patterns worth noting separately:**
- The same trade *archetypes* keep firing across runs (DEN landing Barrett, MIA <-> someone for a young big). Suggests the bot has consistent biases the trade-logic fix should target, not one-off bad rolls.
- Kel'el Ware appeared in both runs as the undervalued young-big going for an older win-now piece. He's the canonical asset-upside test case.

---

# A. Marcus Cole analysis themes

## A1. Call out asymmetric trade logic — name the loser side
**Status:** READY (5+ cases across 2 runs)

**Evidence:**
- Run 1: GSW/NYK (Anunoby + Bridges to GSW for Podziemski + Kuminga): user "doesn't understand why NYK makes this trade since they have a win-now core with Brunson and KAT."
- Run 1: DEN/TOR (Barrett to DEN for MPJ + Jordan + pick): user "doesn't get why DEN went young for the pickup instead of looking for a star to pair Jokić with."
- Run 2: NYK/MIA (Ware to NYK, Anunoby to MIA): "makes no sense for either team really" — both sides flagged.
- Run 2: DEN/TOR (Barrett to DEN for Braun): "trade makes no sense for toronto or den."
- Run 2: HOU/ATL (Daniels to HOU, Brooks to ATL): "bad trade for atlanta" — explicit loser-side naming.
- Run 2 positive contrast: LAL/POR (Williams III to LAL, Hachimura + pick to POR): "good trade for the lakers" — confirms asymmetric language is the issue, not all critique.

**Current behavior:** Marcus Cole describes each team's gain positively, even when one side clearly looks worse. He doesn't currently call out asymmetry or question motive.

**Proposed prompt rules:**
- Add: "If one team's return looks materially weaker than what they gave up given their roster context, name it. 'Why TEAM made this' or 'Sources are puzzled by TEAM's logic here' beats neutral both-sides framing."
- Add: "Both-sides cheerleading is BANNED when the deal is asymmetric. Pick one side to question if context supports it."

## A2. Reference team trajectory (mode), not just position fit
**Status:** READY (5+ cases across 2 runs)

**Evidence:**
- Run 1: ATL/POR (Okongwu + pick to POR for vets): user reads it as "Atlanta starting to sell" and "expects a Trae Young trade for youth and picks after this."
- Run 1: MIA/WAS (Brogdon + Middleton to MIA for Ware): user frames it as "MIA timeline mismatch under Bam, limited Ware role" — i.e., posture-driven.
- Run 2: NYK/MIA: "NYC loses a win now piece as a team with a win now core" / "Miami ... arent anywhere near good enough to contend yet" — both sides framed by posture.
- Run 2: LAL/POR Hachimura: "Portland should be planning to trade Rui ... since he will just rot on portland and doesnt match the rebuild window" — explicit window/trajectory framing.
- Run 2: HOU/ATL Daniels: implicit — the user's "bad trade for atlanta" rests on ATL's trajectory not justifying the swap.

**Current behavior:** Marcus Cole describes positional fits and synergy but rarely says "this fits the team's current trajectory" or "this signals a pivot."

**Proposed prompt rules:**
- Add: "When `team_intel.posture.mode` is in context (e.g., `contender`, `play_in_fringe`, `soft_rebuild`, `tanking`), weave the team's directional arc into the read. 'This is a tanker buying picks' / 'This is a contender consolidating' — frame the deal in terms of the arc, not just the player."
- Already in voice_notes: `roster_fits` and `context_signals_per_player`. Add: posture mode as a first-class field to reference by name.

## A3. Asset upside beyond OVR — draft pedigree, ROY race, age
**Status:** READY (3+ cases across 2 runs)

**Evidence:**
- Run 1: MIA/WAS Ware return: "they should have gotten more for Ware seeing as he's second in the ROY race and has high upside."
- Run 2: NYK/MIA Ware again: "high upside talent thats 22 for an older player." Same player, second run — undervaluation is systemic.
- Run 2: HOU/ATL Daniels/Brooks: "23 year old great defender for a 30 year old" — age premium ignored on Daniels' side.

**Current behavior:** Marcus Cole references OVR explicitly (e.g., "Barrett's 79 OVR scoring punch"). He doesn't reference age trajectory, draft pedigree, or in-season race positioning.

**Proposed prompt rules:**
- Add: "OVR is not the only valuation signal. If a player is young (≤23), recently drafted high, or in an award race (ROY/MVP/DPOY top-5), reference that as a value modifier. A 76-OVR rookie ROY contender is worth more than a 76-OVR vet on a contract year."
- Plumbing: need `roy_rank`, `mvp_rank`, `draft_year`, `draft_pick` available in the trade context. Check `services/team_intel.py` and `services/awards_service.py` for existing hooks.

## A4. Speculate about followup moves when a trade looks incomplete
**Status:** READY (3+ cases across 2 runs)

**Evidence:**
- Run 1: CHA trade for vets around LaMelo: "only makes sense if they have a followup deal in the works for a good big man to replace Williams."
- Run 1: ATL/POR Okongwu trade: "expects a Trae Young trade for youth and picks after this."
- Run 2: LAL/POR Hachimura: "they should probably be planning to trade rui soon after or at the next offseason ... they can get another pick out of him."

**Current behavior:** Marcus Cole writes each trade as a self-contained story. No "what comes next" framing.

**Proposed prompt rules:**
- Add: "If the deal leaves an obvious roster gap or signals a directional pivot, close with one sentence speculating about the next move. 'Watch for a followup big-man add to round this out' or 'Don't be surprised if STAR is the next domino.' Optional — only when the gap or pivot is unambiguous."

## A6. Player ARCHETYPE / role fit, not just position fit
**Status:** READY (2 strong cases in run 2)

**Evidence:**
- Run 2: DEN/TOR Barrett: "den gives up an elite role player skill set guy for someone that needs the ball in their hands even though jokic and murray are on the team." — Barrett's archetype (ball-needs primary) collides with two existing ball-dominant cores. Position (SG/wing) was fine; the *role* was the mismatch.
- Run 2: DEN/TOR Braun (TOR side): "tor gives away a guy that should be a primary option or a secondary guy for a while for a guy the same age but with more of a role player skillset." — TOR traded a player whose archetype trajectory mattered (future primary) for a role-player archetype. Same-age comparison made archetype the load-bearing variable.
- Run 2: HOU/ATL "for 'defense'" scare-quoting — ATL nominally got a defender but archetype downgrade (older + lower OVR for same defensive role) was the user's whole complaint.

**Current behavior:** Marcus Cole references positional fit ("perimeter wing", "rim-running center") but doesn't reference player ROLE archetype against the team's existing role distribution. The role data IS in context (`recent_role_changes` shows assignments like `iso_scorer`, `primary_initiator`, `post_anchor`, `two_way_wing`) — it's just not being woven into the analysis.

**Proposed prompt rules:**
- Add: "When a team's existing core already has the archetype the incoming player provides (e.g., two ball-dominant scorers, two rim-protecting bigs), call out the redundancy. 'Adding another primary initiator to a Jokić-Murray backcourt is curious' beats neutral fit framing."
- Add: "When a team gives up a player whose ARCHETYPE was on a primary/secondary creation trajectory for a similar-age player with a role-player ceiling, flag the trajectory downgrade — even if OVR is comparable."

**Proposed trade-logic rules (call these B6):**
- The proposal generator should consider the receiving team's role distribution. If they already have 2+ players in the same archetype the incoming player would slot into, downweight unless the trade *swaps out* one of those archetype-overlapping players in the same deal.
- The valuation function should treat player archetype trajectory as a value modifier separately from OVR — a 22-25yo with primary-creator archetype is worth more than a same-age role-player even at identical OVR.
- Plumbing check: role assignments live in `services/role_service.py` (per memory) and appear in `team_intel.recent_role_changes`. Trade evaluator needs to compare incoming archetype vs existing roster archetype distribution.

## B6. Trade logic should respect archetype/role distribution
**Status:** READY (paired with A6; see A6 evidence)

**Proposed evaluator rules:** captured under A6's "Proposed trade-logic rules" section above. Promote into standalone B6 here for build sequencing reference.

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
**Status:** READY (8+ cases across 2 runs; see A1, A2 evidence)

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
**Status:** READY (3+ cases — see A3 evidence; Ware shows up across both runs as the canonical undervalued asset)

**Evidence:** Ware undervalued by trade evaluator because it sees only OVR, not "ROY top-5 + young + high ceiling."

**Proposed evaluator rules:**
- Player valuation function should add a positive modifier for:
  - Age ≤ 22 (high ceiling)
  - Drafted in top 10 within last 2 seasons (still pedigree premium)
  - Currently in ROY/MVP/DPOY top 5 (in-season production premium)
- Modifier should scale: a 22yo OVR-78 player in the ROY top-3 is closer to OVR-83 trade value than the 78 face value.
- This should affect both proposal generation (don't offer them as filler) and trade acceptance (don't accept them as the headline return on a vet swap).

## B4. Multi-step strategy: don't leave a team mid-pivot with no plan
**Status:** READY (3+ cases — see A4 evidence; LAL/POR Hachimura is the cleanest example: Portland received him as a flip asset and the bot needs to know that)

**Evidence:** CHA "only makes sense with a followup big-man deal." ATL "expects a Trae Young trade after this." Trades that create obvious unaddressed gaps shouldn't fire in isolation.

**Proposed evaluator rules:**
- After scoring a trade as acceptable, run a post-check: does this trade leave the receiving team with a position-group hole (e.g., 0 centers above replacement-level)? If yes:
  - Either: queue a follow-up trade proposal in the same trade-window batch (hard).
  - Or: downweight the original trade unless the team's already in deep rebuild (don't care about holes if you're tanking).
- This is the hardest of the trade-logic changes. Build phase may want to ship B1-B3 first and revisit B4 separately.

## B5. Don't accept trades where one side is materially worse for no reason
**Status:** READY (5+ cases across 2 runs — see A1 evidence)

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

1. **Marcus Cole prompt update** — A1-A6 rules added to voice_notes. Single file. Quick win; reversible. **A1, A2, A3, A4, A6 are READY**; A5 still single-case.
2. **Trade evaluator B1 (posture gating)** — highest-impact trade-logic change. READY. Hooks already exist via `team_intel.posture.mode`.
3. **Trade evaluator B3 (asset upside modifiers)** — READY. Touches valuation function. May need new plumbing for ROY-race / draft-year fields.
4. **Trade evaluator B6 (archetype/role-distribution check)** — READY. Touches proposal generator + valuation. Role data already in `team_intel.recent_role_changes`; needs new aggregation pass to count archetype distribution per team.
5. **Trade evaluator B5 (asymmetric rejection threshold)** — READY. Tune after B1+B3+B6 are live; otherwise tuning is against a moving target.
6. **Trade evaluator B2 (draft pick continuity)** — still single-case. Defer to gather more signal.
7. **Trade evaluator B4 (multi-step)** — READY but complex. Ship as a separate workstream after B1+B3+B5+B6 are stable.

Marcus Cole work and trade-logic work can run in parallel; they don't share files.
