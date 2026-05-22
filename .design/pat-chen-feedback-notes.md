# Pat Chen — Feedback Notes

Running notes capturing themes from columnist ride-along feedback runs on
`pat_chen` (Dr. Pat Chen). Surface in scope: persona prompt only
(`services/personas/pat_chen.py`). Build phase deferred until more feedback has
accumulated.

Sister doc: `.design/marcus-cole-and-trade-logic-feedback-notes.md`.

## How to use this doc

Same conventions as the Marcus Cole notes:
- After a run, append new themes to the relevant section below, citing source
  trade/article (headline + JSONL article_id) so the build phase can re-read
  the source.
- Themes mark `OBSERVED` (1 case) → `READY` (2+ independent supporting cases).
- Some themes here are **cross-cutting** — they apply to every columnist, not
  just Pat Chen. Flagged explicitly so the build phase doesn't scope them too
  narrowly.

---

## Source runs

| Run start (UTC)         | Persona      | JSONL log                                                                                    | Pauses |
| ----------------------- | ------------ | -------------------------------------------------------------------------------------------- | ------ |
| 2026-05-22T14:39:13Z    | pat_chen     | `headless_logs/columnist_ride_along_pat_chen_20260522_103913.jsonl`                          | 7      |

---

# Themes

## P1. Trim verbosity to ~1/3 of current length
**Status:** READY (multi-pause complaint about overall length / structure)

**Evidence:**
- Pause #1 (SGA vs SAS): "this entire post is a bit too wordy. we need to trim this columnist down to about a third of its size."
- Pause #5 (SGA vs WAS): "this is solid analysis it just needs the same feedback from the last two" — endorses tightening.

**Current behavior:** Pat Chen produces ~250-400 word embeds with an "Observation" paragraph, a multi-line "Evidence" section rendered as prose-paragraphs-disguised-as-bullets, and frequently a sidebar. Each bullet runs 1-3 sentences with em-dashes and follow-on clauses.

**Proposed prompt rules:**
- Hard length cap: total body ≤ ~120 words (down from current ~350+).
- Observation paragraph: ≤ 2 sentences. No "X didn't just Y — he Z'd" framings.
- Evidence section: TRUE bullet points, one stat or fact per line, ≤ ~12 words per bullet. Max 3 bullets.
- No sidebar section at all (Pat Chen currently emits one; user wants it gone — see P1a).

## P1a. Strip the sidebar entirely
**Status:** READY (called out explicitly in P1)

**Evidence:** Pause #1: "strip out the sidebar we dont need it."

**Current behavior:** Pat Chen voice_notes / shape includes a sidebar field that the renderer surfaces.

**Proposed change:**
- Remove the sidebar field from the persona's body shape.
- If the renderer hard-references it, gate to "if sidebar absent, skip the section."
- Plumbing check: search `services/personas/pat_chen.py` and `services/columnist_service.py::_assemble_*` for sidebar references.

## P2. One topic per article — no double-headlines
**Status:** READY (multi-pause)

**Evidence:**
- Pause #1: "i dont like that he has two topics in the headline/post. keep it focused" (article tried to cover BOTH SGA's efficiency AND OKC's "perfect record club").
- Pause #4 ("MIL runs past SAC—and sets sights on PHI"): article framed the SAC win AND a hypothetical future PHI matchup — same dual-topic issue.

**Current behavior:** Pat Chen frequently writes headlines that try to bridge two angles, then the body splits attention between them.

**Proposed prompt rules:**
- "Headline names ONE story. ONE game, ONE player, ONE statistical point. Compound headlines with 'and' or em-dash bridging two topics are BANNED."
- "Body stays on the headline's topic. No 'meanwhile, around the league' tangents. No 'sets sights on next opponent' coda."

## P3. NO AI-style writing tells (CROSS-CUTTING — applies to every columnist)
**Status:** READY (called out once but explicitly generalized by user)

**Evidence:** Pause #2: "lets try and strip out the ai speak (this honestly goes for all columnists) no more 'thats not ____ it's ___' or 'he didn't ___ he ___' or other ai tells like em dash spamming and stuff."

**Specific patterns to ban:**
- The "X isn't Y, it's Z" rhetorical reframe.
- The "He didn't just Y — he Z'd" upgrade pattern. ("Didn't just win — he dismantled."  "Didn't just score — he orchestrated.")
- Em-dash spamming. (Pat Chen averages 2-3 em-dashes per paragraph; should be ≤ 1.)
- Other LLM tells worth banning (carried over from generally-known patterns):
  - "It's worth noting that..."
  - "What's interesting here..."
  - "More than just a..."
  - Compound sentences with multiple semicolons.
  - "Surgical" as a metaphor for efficiency (every Pat Chen article uses it).

**Proposed prompt rule** (add to EVERY persona, not just Pat Chen):
- "HARD RULE: Avoid LLM writing tells. Specifically banned: 'X isn't Y, it's Z' rhetorical reframes; 'didn't just A — he B'd' upgrade patterns; em-dash chains (≤ 1 em-dash per paragraph); the words 'surgical', 'masterclass', 'dismantled', 'orchestrated' as descriptors of basketball action. Write like a human columnist who wouldn't notice they were avoiding these."
- Apply to: `services/personas/pat_chen.py`, `services/personas/marcus_cole.py`, `services/personas/darius_cole.py`, `services/personas/carla_knox.py`, `services/personas/big_picture.py`, `services/personas/rookie_watch.py`, `services/personas/coach_beat.py`, `services/personas/the_ledger.py`, and any other passthrough-style persona.

## P4. Player-level details, not abstract analysis
**Status:** READY (multi-pause)

**Evidence:**
- Pause #3 (Irving vs DET): "i like how this is referencing the gameplan and position/role choices by each team, but i want names. i want to know what players failed on the perimeter against kyrie? why did they fail? do they have low def are they not suited for that role? did they just get cooked by a great scorer? did dallas' gameplan work really well in getting him open in some way that works with the personnel matchups? i want details."
- Pause #4 (Giannis vs SAC): "i want more details. why couldnt they stop him down hill with their personell? what was their plan to stop him? who got hot on milwaukee off those 12 assists? did dame give them buckets? was the two man game humming?"

**Current behavior:** Pat Chen describes "Dallas exploited spacing gaps that plague Detroit's perimeter defense" — abstract. He doesn't name the defender(s), reference their defensive OVR, or describe specific play-types or schemes.

**Proposed prompt rules:**
- "When discussing a defensive failure, NAME the defender(s) who got cooked. Reference their defensive role assignment if it's in context (e.g., 'two_way_wing', 'rim_protector', 'on_ball_pest'). If the defender's defensive OVR is low, say so. If they were miscast in role, say so."
- "When discussing playmaking, NAME at least one teammate who benefited and what they did with the touches ('Lillard hit 4 of 6 from three off Giannis-collapse kickouts')."
- "When discussing a winning gameplan, describe ONE specific scheme or action (e.g., 'Dallas ran high pick-and-roll with Doncic-Lively, daring Detroit's drop coverage to defend Kyrie on switches')."
- Plumbing: check what player-level and gameplan data is available in context. Defensive role assignments are in `team_intel.recent_role_changes`. Possessions / play-types may live in `services/sim_engine.py` — worth seeing what's exposed to the columnist context.

## P5. No "league-wide arms race" overreach
**Status:** OBSERVED (1 case; watch for corroboration — directionally consistent with P3 though)

**Evidence:**
- Pause #6 (Cunningham 33/14): "i dont think we need him to push these league wide narratives over regular things like getting 14 assists. other players have done that before just in years before the sim league's start. this doesnt need to be 'redefining the arms race of playmaking' or whatever. just report and find impactful stats."

**Current behavior:** Pat Chen frequently elevates an individual stat line to a league-wide trend ("redefining the arms race", "joining the perfect-record club", "DBA's premier franchise star"). The simulated league is too young to have meaningful historical baselines for this kind of framing.

**Proposed prompt rule:**
- "Do NOT frame an individual game or stat line as a league-wide trend, arms race, or 'club.' The DBA is in its first season — there is no historical baseline for 'redefining' anything. Stick to what happened in THIS game and what it says about THIS player or THIS team."

## P6. POSITIVE: the coaching grade concept is good
**Status:** OBSERVED (user explicit positive)

**Evidence:** Pause #3: "the coaching grade thing is a nice concept."

**Current behavior:** Pat Chen articles include a coaching-grade element (referenced in pause #3 but I don't have the rendered grade in front of me). User likes it.

**Proposed action:** PRESERVE. Don't lose this in the verbosity-trim pass. Whatever structural slot the coaching grade lives in (likely a closer field or markup line), keep it. If anything, surface it more prominently after the body trim.

---

# Build sequencing (when ready)

Pat Chen build is smaller-scope than Marcus Cole — single file (`services/personas/pat_chen.py`), no cross-system plumbing. Cross-cutting P3 rule should land separately or in the same commit that updates other persona files.

1. **Pat Chen prompt update (P1, P1a, P2, P4, P5, P6 preserve)** — single file. Hard length cap, sidebar removal, one-topic rule, player-detail rules, no-arms-race rule, keep coaching grade.
2. **Cross-cutting P3 AI-tells rule** — add to every passthrough-style persona file. Sister doc's Marcus Cole build is a natural co-commit.
3. **Sidebar removal plumbing** — if `_assemble_*` hard-references a sidebar field, gate gracefully.
4. **Verify length cap by running another ride-along** — verbosity is the headline issue; need a feedback run to confirm the trim took.

---

# Open questions for future runs

- Does Pat Chen have other structural elements beyond Observation / Evidence / Sidebar / Coaching Grade? If yes, are any of them load-bearing?
- Is the coaching grade currently rendered prominently or buried in a footer? (Determines whether P6 needs surfacing work or just preservation.)
- What player-level data is actually available in Pat Chen's context block today? P4 may be partly a plumbing problem (data not in context) vs purely a prompt problem (data in context but ignored).
- Does P3's AI-tell list match what shows up in OTHER persona articles? Worth sampling marcus_cole / darius_cole / carla_knox output for the same patterns to confirm the cross-cutting application is needed.
