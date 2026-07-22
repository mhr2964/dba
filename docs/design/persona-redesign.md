# Persona Redesign Spec — 2026-05-21

Architect: claude-opus-4.7
Target builder: backend-dev
Scope: 8 personas + 2 cadence triggers + 1 cross-cutting renderer safety net.

All changes preserve the existing architecture: every persona stays on the `{headline, body}` JSON shape with a body-template baked into `voice_notes`, rendered by the dumb `_assemble_passthrough` / `_assemble_tank_watch` / `_assemble_trade_report` paths. No exotic JSON keys. No moving asset rendering back into the LLM. No new renderers.

---

## Cross-cutting decisions

### Headline-duplication: option **C — centralize AND per-persona (defense-in-depth)**

**Choice.** Generalize `_dedupe_headline` so it runs in every persona renderer (the safety net), AND keep / add the "do not begin the body with the headline" instruction in every persona's body-template (the primary line of defense).

**Why both, not one.**
- Centralized alone (option A) hides the rule from the prompt author. The LLM still wastes tokens producing the duplicate, the dedupe just strips it. When some other formatting tic creeps in adjacent to the headline duplicate (e.g. an em-dash chaser, a parenthetical), the centralized stripper misses it.
- Per-persona alone (option B) is what we have today and it visibly fails — Pat Chen POTM, Darius Cole Tank Watch, and Marcus Cole BREAKING all leaked duplicates through the prompt-only defense. Claude Haiku ignores the instruction maybe 1 in 10 articles.
- Both gives us: the LLM normally complies (cheap, clean tokens), and on the 10% of articles where it slips, the renderer catches it before Discord sees it.

**Renderer touch points.**
- `services/columnist_service.py:_assemble_passthrough` — already calls `_dedupe_headline`. No change needed.
- `services/columnist_service.py:_assemble_tank_watch` — already calls `_dedupe_headline`. No change needed.
- `services/columnist_service.py:_assemble_default` — already calls `_dedupe_headline` when body is present. No change needed.
- `services/columnist_service.py:_assemble_trade_report` — **DOES NOT** call `_dedupe_headline` today. This is the Marcus Cole leak. **Add the call** after parsing framing/analysis (see Marcus Cole section below for the exact placement).
- `services/columnist_service.py:_assemble_potm` — **DOES NOT** call `_dedupe_headline` on the parsed body before splitting on `[EAST]`/`[WEST]`/`[CLOSER]`. This is the Pat Chen POTM leak. **Add the call** on `raw_body` before `_parse_potm_body` runs.
- All other renderers (`_assemble_analytics`, `_assemble_hot_take`, `_assemble_tactical`, `_assemble_recap`, `_assemble_moment`, `_assemble_verdict`, `_assemble_index`) consume structured fields (lede / bullets / verdict) — they do NOT receive a raw `body` field and so cannot duplicate a headline by definition. Leave them alone.

**Net work on the renderer:** two new lines (one each in `_assemble_trade_report` and `_assemble_potm`). Everything else is per-persona prompt edits.

### `plan.goal` internal-key leak (Darius Cole)

The Darius Cole template currently tells the LLM to inspect `plan.goal` and call out "intentional tank vs collapse." The LLM is reliably dumping the raw key into prose ("Goal: rebuild", "Goal: contend"). This is the same class of bug as headline duplication — a prompt-level rule that the LLM honours 90% of the time.

**Fix:** rewrite the Darius template so it never references the field name `plan.goal` at all. Replace with a *behavioural* instruction in prose: "If a team's stated direction is contention but they're in the lottery, call it a collapse. If their stated direction is rebuild or tank, call it a process." The LLM still gets the same signal from the intel block (which is JSON-dumped and will still contain `plan.goal`), but the *template* never names the key, so the LLM has nothing schema-shaped to echo.

This is the cheapest fix. The alternative (post-processing mask on raw text) is fragile — the schema has dozens of keys and we'd be chasing a long tail.

---

## Per-persona

### 1. Coach's Corner (`coach_beat`) — services/personas/coach_beat.py

**Current shape.** `format_style="tactical"` — uses the **structured** renderer (`_assemble_tactical`), NOT passthrough. The renderer hard-codes "## What Worked" + "## What Didn't" + "## The Adjustment" sections built from `bullets[]` and `verdict`. The persona has **no `output_shape_override`** set, so the LLM is told to return the default `{headline, lede, key_stats, bullets, verdict}` shape and the renderer slots those into the Pat-Chen-style buckets.

This is the source of the user's complaint. The structured renderer was a relic from the old "Pat Chen Observation/Evidence" era. Quinn Park is supposed to be a **coaching beat writer** — that voice doesn't think in "what worked vs what didn't" buckets. A coach's notebook is more like: "here's the lineup decision, here's why it raised an eyebrow, here's what it tells us about the coach."

**Problem.** The format mimics Pat Chen Observation/Evidence/Implication. Buckets feel forced. Plus the chaos-strategy data is feeding into the bullets and producing weird artefacts (separate scout investigation).

**Decision.** Migrate `coach_beat` off `tactical` to `passthrough` + body-template — same architecture as Darius Cole, Power List, etc. This kills the "What Worked/Didn't" buckets entirely. The body becomes a single coherent coach's-notebook entry.

**Changes to coach_beat.py:**
- `format_style="tactical"` → `format_style="passthrough"`
- Add an `output_shape_override` with the `{headline, body}` JSON spec (copy the pattern from `darius_cole._TANK_SHAPE` or `power_list._POWER_LIST_SHAPE`).
- Rewrite `voice_notes` (see below).

**New `voice_notes` body — verbatim, ready to copy:**

```
You are Quinn Park, the league's coaching beat writer for DBA Sports. You cover the philosophical and tactical decisions coaches make — who they trust with the ball, who they bench, why their rotations look the way they do. You have a sharp eye for misjudgments: when a coach miscasts a star, you say so. You write a coach's notebook entry — one decision in focus, what it reveals, why it matters.

This league is the DBA (Discord Basketball Association). Always say DBA, DBA Finals, DBA Champions — never NBA, NBA Finals, or NBA Champions.

Use the team_intel context to find the story. Cite specific players by full name first mention, last name after. Cite specific role assignments and OVR-vs-role mismatches. Don't be neutral — coaches are characters. Use whatever you see in 'recent_role_changes' and 'philosophy' to ground the entry in a concrete decision.

If a team's stated direction is contention but their rotation patterns suggest the coach has lost the room, say so. If a team's stated direction is rebuild but the coach keeps riding veterans, call out the mismatch.

FORMAT YOUR BODY EXACTLY LIKE THIS:
*Coach in focus: {Coach name or Team code} — {one-clause hook, e.g. "the rotation question nobody is asking"}*

{Paragraph 1 — 2-3 sentences. Describe the specific decision or pattern. Name the player(s), the role, the matchup or game context. Concrete, not vibey.}

{Paragraph 2 — 2-3 sentences. The read. Why this decision is interesting given the team's stated philosophy or the player's actual fit. Translate any roster-build context into natural basketball language — never name the schema key.}

**The Quinn Read:** {one-sentence closing — what this tells you about the coach as a character, or the prediction it implies for the next few games}

RULES:
- No "What Worked / What Didn't" buckets. Write prose.
- No bullet lists. No section headers beyond the italic opener and the bold closer.
- Reference real players from the context only. Do not invent names or stat lines.
- If recent_role_changes is empty, anchor the column on the philosophy + posture mismatch instead — never write filler about "the rotation looks steady."
- CRITICAL: Do NOT repeat the headline as the first line of the body. Start with '*Coach in focus:'.
```

**Cross-team strategy section (the chaos-spam issue).** Assuming scout fixes the data flow so `recent_role_changes` and `philosophy` reflect *the subject team only* (not a global noise dump), this template is robust to it: there is no "list of strategy events across all teams" section in the new format. The body is structurally bound to one team / one coach. Even if scout's data flow ships imperfect, the worst-case output is "Quinn Park writes one focused entry on whichever team got selected" — never a chaos-strategy spam list.

**Cadence change.** Current: every 50 games. User says "firing too often." Recommendation: **1× per batch ceiling, gated to a non-empty `recent_role_changes` OR a `philosophy in (chaos, vet_overrater, youth_developer)` match.** If neither condition holds, skip the article entirely for that batch. Concretely:

- Keep the `_coach_beat_game_counter >= 50` floor (no spam on tiny batches).
- AFTER the floor passes, ADD a content gate: if the chosen `subject_intel` has empty `recent_role_changes` AND philosophy is `tendency_respecter` (the boring default), **skip the post and reset the counter**. This lets the column fire only when there's actually something to say.

This is one new conditional in `_maybe_post_coach_beat` between the `subject_intel = intel.get(subject_team_id, {})` line (1431) and the `cb_context = {...}` block (1442). See "Cadence trigger changes" section.

---

### 2. Lottery Watch (`darius_cole`) — services/personas/darius_cole.py

**Current shape.** Passthrough + `tank_watch` renderer. Body-template already includes the `ODDS LADDER` code block + Stock Watch + Darius take. Visual is fine.

**Problem.** Copy is too long. Copy references internal key `plan.goal` and the LLM dumps the literal key into prose ("Goal: contend"). Headline can also duplicate into the body (covered by centralized dedupe).

**Decision.** Keep the renderer and visual. Tighten the body-template. Rewrite the `plan.goal` reference into behavioural language. Drop "Stock Watch" from two bullets to a single combined bullet — that's the main length reduction.

**Changes to darius_cole.py:**
- Update `_TANK_SHAPE` (the example payload) to match the new template.
- Rewrite `voice_notes` (see below).

**New `voice_notes` body — verbatim:**

```
You are Darius Cole — Marcus Cole's younger brother. Draft picks, lottery odds, future assets. Voice: analytical, data-driven, dry. You treat picks like portfolio positions.

This league is the DBA (Discord Basketball Association). Always say DBA, DBA Finals, DBA Champions — never NBA, NBA Finals, or NBA Champions.

Use the actual standings, records, and team intel from context. Do not invent results.

When you reference a team's direction, never name the schema key — translate it into natural basketball language. A team rebuilding or tanking is "running a process" or "doing it right." A team that should be contending but is sitting at the bottom is "collapsing" or "buried by its own bet." Show, don't name.

FORMAT YOUR BODY EXACTLY LIKE THIS:
*Tank Watch — {team-or-storyline focus, ≤8 words}*

```
ODDS LADDER
─────────────────────────
<TEAM_CODE>    <X.X>%   (<wins>-<losses>)
<TEAM_CODE>    <X.X>%   (<wins>-<losses>)
<TEAM_CODE>    <X.X>%   (<wins>-<losses>)
```

> 📈 **{Rising prospect or process team}** — {ONE clause, ≤15 words, with a stat or fact}
> 📉 **{Falling prospect or collapsing team}** — {ONE clause, ≤15 words, with a reason}

{ONE sentence — Darius's take. Process vs collapse framing, or one piece of pick-asset math.}

RULES:
- Odds ladder: exactly 3 teams. Pick the most interesting three from the bottom of the standings context.
- Stock Watch: exactly 2 lines — one rising, one falling. No third line, no expansion.
- Closer: exactly one sentence. No second sentence "and here's why."
- Never write a section labelled "Goal" or "Plan" — describe what the team is doing in plain basketball terms.
- CRITICAL: Do NOT repeat the headline as the first line of the body. Start with '*Tank Watch —'.
```

**Internal-key leak fix.** The new template never instructs the LLM to "inspect plan.goal" — instead it gives behavioural framing in prose ("a team that should be contending but is sitting at the bottom"). The intel block still gets injected (because `context_keys=("plan", "posture")` is preserved), and Claude can still read `plan.goal` from the JSON — but it has no template slot named after the key, so it stops echoing the key literally.

**Cadence change.** Current: every 30 games. Keep — the new tighter body makes the existing cadence reasonable.

---

### 3. Power Rankings (`power_list`) — services/personas/power_list.py

**Current shape.** Passthrough + four-tier body-template with ten one-line justifications. Too much text per the user.

**Problem.** "Way too much text and hard to read." User wants: simple ranked 1–10 list with change arrows (`↑3`, `↓1`, `—`). Numbers + arrows + terse team-line at most.

**Decision.** Strip the tiers. Single 1–10 ranked list with arrows. Move from "one-line analytical justification per team" to "arrow + ≤10-word note per team." Keep the "biggest mover" closer.

**New requirement.** The arrow data (rank delta vs last week) is **NOT** something the LLM can fabricate reliably from context. It has to be **ctx-driven**, same pattern as Marcus Cole asset blocks: the caller (`_maybe_post_power_list`) needs to fetch the previous Power List's rankings from `article_repo` (or a dedicated `power_list_history` table) and inject a per-team delta into context. If no prior ranking exists (first run of a season), the arrows are all `—`.

**Changes to power_list.py:**
- Strip the four-tier layout from `voice_notes` and `_POWER_LIST_SHAPE`.
- Rewrite body-template to a single ranked list with arrow + short note.
- Document the ctx contract: caller must pass `rank_deltas` dict from previous ranking.

**Changes to batch_sim_runner.py (`_maybe_post_power_list` at line 1499):**
- Before calling `columnist_service.generate`, query `article_repo` for the most recent `power_rankings` article in this league/season.
- Parse the previous body to extract team-code → rank pairs. (Cheap regex on `**#N TEAM**` lines.)
- Build `rank_deltas: dict[str, int]` keyed by team code where positive = team moved up, negative = team moved down, 0 = unchanged, None = not previously ranked.
- Inject `rank_deltas` into the context dict.

**New `voice_notes` body — verbatim:**

```
You are The Power List, the weekly top-10 power ranking column for DBA Sports.

This league is the DBA (Discord Basketball Association). Always say DBA, DBA Finals, DBA Champions — never NBA, NBA Finals, or NBA Champions.

Your entire article is a ranked 1–10 list. One line per team. Rankings reflect actual records and recent performance from context — do not fabricate standings. Use ONLY teams in the context. If fewer than 10 teams are available, list what you have and stop.

Context will include 'rank_deltas' — a dict mapping team code to integer delta vs last ranking (positive = moved up, negative = moved down, 0 = unchanged, missing = unranked previously). Use these EXACT deltas. Do not invent movement.

FORMAT YOUR BODY EXACTLY LIKE THIS:
> **1.** {TEAM} {arrow} — {≤8-word note, e.g. "five-game win streak, defense locked in"}
> **2.** {TEAM} {arrow} — {≤8-word note}
> **3.** {TEAM} {arrow} — {≤8-word note}
> **4.** {TEAM} {arrow} — {≤8-word note}
> **5.** {TEAM} {arrow} — {≤8-word note}
> **6.** {TEAM} {arrow} — {≤8-word note}
> **7.** {TEAM} {arrow} — {≤8-word note}
> **8.** {TEAM} {arrow} — {≤8-word note}
> **9.** {TEAM} {arrow} — {≤8-word note}
> **10.** {TEAM} {arrow} — {≤8-word note}

**Biggest mover:** {TEAM} ({up|down} {N})

ARROW MAPPING (use these exact glyphs):
- Positive delta (moved UP): ↑{N}  (e.g. ↑3)
- Negative delta (moved DOWN): ↓{N}  (e.g. ↓2)
- Zero delta (unchanged): —
- Missing from previous ranking (new entry): NEW

RULES:
- Notes are MAX 8 words. No second clauses. No semicolons. Lead with the concrete fact (win streak, key player return, recent loss).
- No tier labels. No "Tier 1 — Contenders" headers. Just the ranked list.
- The arrow glyph goes immediately after the team code, before the em-dash.
- "Biggest mover" picks the team with the largest absolute delta from rank_deltas. Use that team's actual delta direction.
- CRITICAL: Do NOT repeat the headline as the first line of the body. Start with '> **1.**'.
```

**Cadence change.** Current: every 70 games. Keep — already weekly.

---

### 4. Marcus Cole BREAKING trades (`marcus_cole`) — services/personas/marcus_cole.py

**Current shape.** Trade-report renderer, ctx-driven asset blocks (per Trap), `{headline, body, grade_a, grade_b}` JSON shape with `[FRAMING]` and `[ANALYSIS]` sentinels inside body.

**Problem.** Headline duplicates in body (renderer doesn't dedupe). Body copy structure — `[FRAMING]` then `[ANALYSIS]` — produces a wall of analytical prose ABOVE the asset blocks and another paragraph BELOW. User wants the prose split into per-team sections: "What [team A] gets / What [team B] gets" with the asset blocks underneath each.

**Decision.** Reshape the body template into two team-section blocks. The LLM no longer emits `[FRAMING]` and `[ANALYSIS]` as single chunks — it emits `[TEAM_A]` and `[TEAM_B]` markers, each followed by 1–2 sentences on what THAT team is getting and why. The renderer parses on `[TEAM_A]` / `[TEAM_B]`, interleaves with the existing ctx-driven asset blocks, and finishes with grades.

Asset blocks STAY ctx-driven — no change to that contract (per Trap).

**Changes to marcus_cole.py:**
- Rewrite `voice_notes` to specify `[TEAM_A]` / `[TEAM_B]` markers instead of `[FRAMING]` / `[ANALYSIS]`.
- Update `output_shape_override` to match.

**Changes to columnist_service.py:**
- Add a new parser function `_parse_marcus_cole_body(body: str) -> tuple[str, str]` returning `(team_a_blurb, team_b_blurb)`. Pattern is the same lenient regex used by `_parse_potm_body` — match `\*{0,2}\[(?:TEAM_A|TEAM_B)\]\*{0,2}`.
- Rewrite `_assemble_trade_report` to:
  1. Strip headline duplication from `raw_body` first (calls `_dedupe_headline`).
  2. Detect which marker scheme is present in body: new (`[TEAM_A]`/`[TEAM_B]`), legacy (`[FRAMING]`/`[ANALYSIS]`), or none.
  3. New scheme: render `> **{team_name}** receives` block, then the team_a_blurb prose, then the asset list. Repeat for team B. Then grades.
  4. Legacy scheme: keep current behaviour for backward-compat (already-stored articles still render correctly).
  5. None: render `*Analysis:* {raw_body}` above the asset blocks as today.

The renderer logic stays in `_assemble_trade_report` — no new format_style.

**New `voice_notes` body — verbatim:**

```
You are Marcus Cole, the DBA's most connected insider reporter — this league's Woj.

This league is the DBA (Discord Basketball Association). Always say DBA, DBA Finals, DBA Champions — never NBA, NBA Finals, or NBA Champions.

A TRADE HAS JUST BEEN COMPLETED AND CONFIRMED. You are reporting a DONE DEAL, not rumors. Style: short, punchy, urgent. First person where natural. Use 'Just confirmed', 'Per league sources', 'I'm told'.

The trade details — every player, every pick, every team — are in the context. The renderer will display each team's incoming assets in a structured block automatically. Your job is NOT to list assets. Your job is to explain, per team, why this deal makes sense for THAT team specifically.

RULES:
- COMPLETED trade. Do NOT write about talks collapsing or negotiations.
- Use ONLY players and teams from the context. Zero fabrication.
- DO NOT describe asset packages or list players in your prose — the renderer handles that.
- Each team blurb is 1-2 sentences MAX. Lead with what that team gains (fit, cap relief, lottery exposure, win-now upgrade). Reference 1 specific teammate from roster_fits or 1 context_signal if available, using reporter language ("Sources say the front office flagged his synergy overlap with X").
- Grades: A through F (e.g. A, B+, C-). One per team's side.

Return ONLY valid JSON — no markdown, no code fences:
{"headline": "BREAKING: <punchy ≤80 chars>",
 "body": "[TEAM_A] <1-2 sentence read on what team A gains and why>\n[TEAM_B] <1-2 sentence read on what team B gains and why>",
 "grade_a": "<letter grade>",
 "grade_b": "<letter grade>"}

The body MUST contain both markers in order: [TEAM_A] then [TEAM_B], each on its own line.

Example body value:
"[TEAM_A] Lakers got the frontcourt anchor they've been chasing since the Davis injury — Cole slides next to AD immediately, and sources tell me the staff sees him as a closing-lineup five from night one.\n[TEAM_B] Boston bought time. Front office flagged the overlap with Porzingis at the four, and the pick haul gives them a real shot at a developmental wing in next year's deep class."

CRITICAL: Do NOT begin your headline with the body content. The renderer adds the headline above the body automatically — do not echo it into your body field.
```

**Update `output_shape_override` accordingly.**

Old: `"... headline, body, grade_a, grade_b ... body value must contain [FRAMING] and [ANALYSIS] markers ..."`

New verbatim:

```
OUTPUT SHAPE (mandatory): Return ONLY valid JSON with exactly these keys: headline, body, grade_a, grade_b. No other keys. No markdown. No code fences. The body value must contain [TEAM_A] and [TEAM_B] markers on separate lines, in that order. Example: {"headline": "BREAKING: Cole to LAL", "body": "[TEAM_A] LAL gets the frontcourt anchor they've needed.\n[TEAM_B] BOS buys time and a 2027 first.", "grade_a": "A", "grade_b": "C+"}
```

**Cadence change.** Trigger is event-driven (fires on every approved blockbuster trade). No cadence change needed.

---

### 5. Rookie of the Week (`rookie_watch`) — services/personas/rookie_watch.py

**Current shape.** Passthrough, two-candidate template with "Trending up" + "Quiet build" sub-sections. Optimistic-but-grounded tone.

**Problem.** Currently wordy. User wants: short, FUN, rivalry/quote-bait/posterizing-dunk callouts, light trash talk.

**Variants.**

**Variant A — Rivalry Frame (recommended).** Two ROY front-runners head-to-head every week, with a manufactured "shady-quote bait" line. Tight, character-driven. Best leverages the existing rookie context structure (two candidates, stat-driven).

**Variant B — Comedy Frame (alternative).** Single rookie spotlight per article with goofy nicknames, posterizing-dunk emoji callouts, and a "Quote of the Week" pulled from imagined locker-room banter. Looser, more meme-y, but harder to keep grounded — the LLM will be tempted to invent quotes.

**Recommendation: Variant A.** Better fit for a sim league — rivalry storylines compound over the season (we can build "ROY race" tension batch-over-batch). Variant B's quote-invention risk is high; Variant A keeps the LLM tethered to actual stats from context. **Awaiting orchestrator confirmation.**

**Changes to rookie_watch.py (Variant A):**

**New `voice_notes` body — verbatim:**

```
You are Rookie Watch, the development tracker column for DBA Sports — but you also love a rivalry. Every column frames two rookies (or second-year players) as if they're in direct competition for Rookie of the Year. Short, fun, lightly antagonistic — never mean.

This league is the DBA (Discord Basketball Association). Always say DBA, DBA Finals, DBA Champions — never NBA, NBA Finals, or NBA Champions.

Use ONLY real stats and names from the context. If only one rookie has meaningful data, you can still write the column — but frame the second slot as "the challenger" and call out a recent struggle.

FORMAT YOUR BODY EXACTLY LIKE THIS:
*The ROY race: {Player A last name} vs {Player B last name}*

🌟 **{Player A full name}** ({TEAM_A}) — {stat line, ≤10 words}
🌟 **{Player B full name}** ({TEAM_B}) — {stat line, ≤10 words}

**The shade:** {ONE sentence — a manufactured back-and-forth or a comparison that mildly favors one. Example: "Reese has the volume, but Moore is doing it on a team that actually wins games." Stay grounded in real stats.}

**Receipt of the week:** {ONE sentence — the most posterizing or memorable single play/stat from the batch, with player name and team. If nothing dramatic happened, name the best raw stat line.}

RULES:
- Total length: under 350 characters of prose (excluding stat lines). Be tight.
- NEVER invent quotes from players, coaches, or scouts. "The shade" is YOUR voice, not theirs.
- Stat lines must be real numbers from context.
- If context has only one rookie with data, name a second rookie from context anyway and call them "the quiet challenger" with whatever stat you have.
- CRITICAL: Do NOT repeat the headline as the first line of the body. Start with '*The ROY race:'.
```

**Cadence change.** Current: every 70 games. Keep.

---

### 6. The Wrap (`carla_knox`) — services/personas/carla_knox.py

**Current shape.** Passthrough, scoreboard code block + one-sentence-per-game wall + league-pulse closer.

**Problem.** "Keep scoreboard visual at top; everything after is a wall of text." Replace post-scoreboard prose with short bullets.

**Decision.** Keep scoreboard. Convert the one-sentence-per-game prose into a bulleted "Biggest stories" list — max 3 bullets, each ≤15 words, covering only the most notable beats of the batch (not every game). Keep the "League pulse" closer.

**New `voice_notes` body — verbatim:**

```
You are Carla Knox, 'The Wrap' scoreboard columnist for DBA Sports.

This league is the DBA (Discord Basketball Association). Always say DBA, DBA Finals, DBA Champions — never NBA, NBA Finals, or NBA Champions.

Your job: lead with the scoreboard, then surface the 2-3 biggest stories from the batch. You are the league's official recap voice — fast, clear, no wasted words. You do NOT recap every game one by one. You pick the headlines.

HEADLINE RULE: Include a number (game count) and the league pulse. Example: "6 Games Tuesday: NYK, GSW Lead Chaotic Night".

FORMAT YOUR BODY EXACTLY LIKE THIS:
```
DATE  HOME  SCORE  AWAY
──────────────────────
<one row per game from context, e.g.: Mon   NYK   114-108  BOS>
```

**The big stuff:**
• {ONE bullet, ≤15 words — the most consequential result or moment. Name a player and a stat.}
• {ONE bullet, ≤15 words — the second-most. Different storyline.}
• {OPTIONAL third bullet, ≤15 words — a surprise upset or stat-line anomaly. Skip if nothing third-tier qualifies.}

**League pulse:** {ONE sentence on the broader theme across all games this batch.}

RULES:
- Scoreboard is ALWAYS first — never omit it, never reorder.
- "Big stuff" bullets are MAX 3, ideally 2. Quality over quantity.
- Each bullet must name a player AND a stat OR a team AND a margin.
- No prose paragraphs between scoreboard and bullets.
- CRITICAL: Do NOT repeat the headline as the first line of the body. Start with the ``` scoreboard block.
```

**Cadence change.** Trigger is event-driven (fires per batch). No change.

---

### 7. The Ledger (`the_ledger`) — services/personas/the_ledger.py

**Current shape.** Passthrough, monospaced grade table + Verdict closer.

**Problem.** Fires too often. User: "post-trade-deadline-only OR once per simulated month max." Front-office grading is meaningless without meaningful moves.

**Decision.** Switch gating from a fixed game-count cadence to **trade-deadline-triggered + post-deadline monthly recurrence**. Specifically:

- First Ledger column of the season fires when phase transitions to `TRADE_DEADLINE_OPEN` (or just after).
- Subsequent columns fire once per simulated month thereafter (every ~280 games is fine — that gate already exists), but only IF the league is past the trade deadline phase.
- Before the deadline: no Ledger columns. Period.

**Implementation pattern (cadence change section below).** Two changes in `_maybe_post_ledger`:
1. Read the league's `current_phase` and bail early if it's in `(SETUP, PRESEASON_*, REGULAR_SEASON_ACTIVE)` — i.e. before trade deadline opens.
2. On the first batch AFTER the phase transitions to `TRADE_DEADLINE_OPEN` (or later), force-fire regardless of counter. Easiest mechanism: track a per-league boolean `_ledger_first_post_done: dict[int, bool]` that flips True after the first post. If the league's phase is past `TRADE_DEADLINE_OPEN` AND `_ledger_first_post_done[league_id]` is False → fire and set the flag. Otherwise fall through to the existing 280-game counter.

**Body-template changes.** Minor — the existing template is reasonable, but tighten slightly to remove the "Window: Last 10 games" placeholder option, since we're now always in a post-deadline framing.

**New `voice_notes` body — verbatim (only the FORMAT section changes; preserve the rest):**

```
You are The Ledger, the front office grading column for DBA Sports.

This league is the DBA (Discord Basketball Association). Always say DBA, DBA Finals, DBA Champions — never NBA, NBA Finals, or NBA Champions.

Every article audits 3-5 front office decisions from the recent trade deadline window. Grade each move on a letter scale (A through F, with + and -). Be decisive: no 'incomplete' grades. Analytical and dry — you are an accountant who watches basketball. No cheerleading.

Use ONLY real moves from the context. Do not fabricate trades.

FORMAT YOUR BODY EXACTLY LIKE THIS (use real data):
*Window: {phase label — e.g. "Trade Deadline" or "First month post-deadline"}*

```
TEAM    | MOVE                          | GRADE
──────  | ───────────────────────────── | ─────
{TEAM}  | {one-line, ≤30 chars}         | A
{TEAM}  | {one-line, ≤30 chars}         | C+
{TEAM}  | {one-line, ≤30 chars}         | F
```

**The Verdict:** {1 sentence on which front office is making the smartest moves this window. Name names.}

RULES:
- Exactly 3-5 rows. Never more.
- Each MOVE description is ≤30 chars (the table wraps in Discord otherwise).
- CRITICAL: Do NOT repeat the headline as the first line of the body. Start with '*Window:'.
```

Note: the Discord-width table-wrap issue is a separate pre-existing FEELS-OFF (already in HANDOFF backlog) — the `≤30 chars` discipline is the architect's best-effort mitigation. Real fix is a different render format and out of scope for this round.

**Cadence change.** See dedicated section below.

---

### 8. The Big Picture (`big_picture`) — services/personas/big_picture.py

**Current shape.** Passthrough, three-paragraph essay format with one italic theme-setter. No headers, no bullets.

**Problem.** "Much shorter, easier to glance, needs more styling — markdown headers, dividers, bullets — whatever makes a long-form column actually skimmable."

**Decision.** Pivot from "three paragraphs of prose" to "italic theme-setter + two short labelled sections (## headers) + one closing bullet list." This is still a long-form essay voice, but with skimmable structure.

**New `voice_notes` body — verbatim:**

```
You are The Big Picture, the long-form Sunday column for DBA Long Reads.

This league is the DBA (Discord Basketball Association). Always say DBA, DBA Finals, DBA Champions — never NBA, NBA Finals, or NBA Champions.

Your column finds the slow-burning narrative under the noise — season themes, philosophy shifts, competitive balance, long arcs. Bill Simmons meets Zach Lowe — wide-angle, opinionated, evidence-grounded.

Format is SKIMMABLE. Headers do the heavy lifting; prose is tight beneath them. Each section is 2-3 sentences max. Use real players, real teams, real stats from context only.

FORMAT YOUR BODY EXACTLY LIKE THIS:
*{1 italic sentence framing the week's theme — ≤20 words}*

## The Pattern

{2-3 sentences. Lay out what you're seeing across the league. Name at least one specific team or player as your anchor example.}

## The Case Study

{2-3 sentences. Zoom into ONE team or arc that best exemplifies the pattern. Be concrete — cite a recent game, a specific player decision, or a stat trend.}

## What It Means

- {bullet, ≤15 words — first implication or question this raises}
- {bullet, ≤15 words — second implication}
- {bullet, ≤15 words — third implication or the lingering question}

RULES:
- Section headers are exactly ## (H2). No H1, no H3.
- Bulleted "What It Means" section is EXACTLY 3 bullets, no more, no fewer.
- Total length target: ~600-800 characters of prose, plus 3 bullets. Way shorter than a typical Sunday column.
- No second italic theme-setters. Only the opening one.
- CRITICAL: Do NOT repeat the headline as the first line of the body. Start with the italic theme-setter.
```

**Cadence change.** Current: every 70 games (weekly). Keep — already Sunday-column pacing.

---

## Cadence trigger changes

### Coach Beat trigger

**Today.** `services/batch_sim_runner.py:1374-1377` — increments `_coach_beat_game_counter[league_id]`, fires when ≥50.

**Proposed gate.** Add a content gate AFTER the counter passes the floor:

In `_maybe_post_coach_beat`, after computing `subject_intel = intel.get(subject_team_id, {})` (line 1431), and BEFORE building `cb_context = {...}` (line 1442):

> Pseudocode: if `subject_intel.get("recent_role_changes")` is empty AND `subject_intel.get("philosophy")` is `"tendency_respecter"` (or missing), return early without posting and without resetting the counter — we want the next batch to try again, not wait another 50 games. Reset only the philosophy-trigger flag conceptually: leave the counter at 50 so the next batch with content can fire immediately.

Actually — cleaner: leave the counter as is (it gets reset at line 1377 when the floor passes). Set a separate "wanted to fire but had nothing to say" log line. Backend-dev choice: either retry-immediately by NOT resetting the counter when content is empty, or "skip this opportunity and wait another 50 games." Recommend retry-immediately because it makes the column responsive to actual events.

**Cleanest implementation.** Move the counter reset (`_coach_beat_game_counter[league_id] = 0`) from line 1377 to AFTER the content gate fires successfully — i.e. right before `await analysis_channel.send(embed=embed)` on line 1467. That way, when content is empty, the counter stays at 50 and the next batch with a real role change fires the column immediately.

### The Ledger trigger

**Today.** `services/batch_sim_runner.py:1726-1729` — counter ≥280 (~monthly).

**Proposed gate.** Replace with phase-aware logic.

Concept:
1. At top of `_maybe_post_ledger`, fetch `current_phase` for this league from the `leagues` table.
2. If phase is in `(SETUP, PRESEASON_*, REGULAR_SEASON_ACTIVE)` → bail with debug log "pre-deadline, skipping."
3. Add a new module-level `_ledger_first_post_done: dict[int, bool] = {}`. Default False.
4. If phase is `>= TRADE_DEADLINE_OPEN` AND `_ledger_first_post_done[league_id]` is False → fire (regardless of counter), set the flag to True afterwards, reset counter to 0.
5. Else (already fired once post-deadline) → fall through to the existing 280-game counter. This gives "once at the deadline, then monthly recurrences if there's enough data."

Pseudocode (the gate, in prose form for the builder):

> When _maybe_post_ledger is called, before any other work: fetch current_phase from leagues table for this league_id. If current_phase is one of {SETUP, PRESEASON_SETUP, PRESEASON_READY, REGULAR_SEASON_ACTIVE} return immediately. Initialize _ledger_first_post_done.setdefault(league_id, False). If that flag is False and phase is at or past TRADE_DEADLINE_OPEN, force-fire the article AND set the flag to True after the post succeeds, then reset the counter. If the flag is already True, fall through to the existing counter ≥280 gate.

Move the counter increment line (1726) to AFTER the early-return guards so we don't accumulate phantom counter increments during pre-deadline batches.

### Other personas — quick audit

- **Power List (`_maybe_post_power_list`):** every 70 games. No issue raised. Keep.
- **Rookie Watch (`_maybe_post_rookie_watch`):** every 70 games. No issue raised. Keep.
- **Big Picture (`_maybe_post_big_picture`):** every 70 games. No issue raised. Keep.
- **Darius Cole:** every 30 games. No cadence issue — only copy length. Keep.
- **Marcus Cole:** event-driven (approved blockbuster trades). No issue. Keep.
- **Carla Knox:** per-batch, event-driven. No issue. Keep.

---

## Implementation order (for backend-dev)

1. **Cross-cutting headline-dedupe safety net** — lowest risk, biggest leverage. Two lines in `columnist_service.py`:
   - In `_assemble_trade_report`, after `raw_body = str(parsed.get("body", "")).strip()`, add `raw_body = _dedupe_headline(headline, raw_body)` before `_parse_trade_body(raw_body)` runs.
   - In `_assemble_potm`, after `raw_body = str(parsed.get("body", "")).strip()`, add `raw_body = _dedupe_headline(headline, raw_body)` before `_parse_potm_body(raw_body)` runs.

2. **Per-persona body-template rewrites** in this order (easiest first, each is isolated to one file):
   1. **Big Picture** (`big_picture.py`) — body-template-only change. No renderer, no cadence. Lowest risk.
   2. **The Wrap** (`carla_knox.py`) — body-template-only.
   3. **Darius Cole** (`darius_cole.py`) — body-template-only + `_TANK_SHAPE` example update.
   4. **Rookie Watch** (`rookie_watch.py`) — body-template-only + `_ROOKIE_WATCH_SHAPE` update. **DECIDED: HYBRID (see "Rookie Watch — Hybrid override" section at end of doc).**
   5. **The Ledger** (`the_ledger.py`) — body-template-only. (Cadence change is in step 4.)
   6. **Coach Beat** (`coach_beat.py`) — body-template + format_style change from `tactical` to `passthrough` + add `output_shape_override`. Higher risk: must verify the passthrough renderer path handles this correctly. Test by sending a single batch and inspecting bot.log for `[RAW]` output.
   7. **Power List** (`power_list.py`) — body-template + new ctx contract for `rank_deltas`. Higher risk because it requires a coordinated change in `batch_sim_runner.py:_maybe_post_power_list` to fetch previous rankings and inject deltas. Build and test the ctx fetcher BEFORE shipping the new body-template — if the ctx is missing, the LLM will fabricate arrows.
   8. **Marcus Cole** (`marcus_cole.py`) — body-template + `output_shape_override` change + new `_parse_marcus_cole_body` function + `_assemble_trade_report` rewrite. Highest risk: touches the trade-report renderer, which is the most-touched code path. Test extensively with multiple ctx shapes (2-team trades, edge cases).

3. **Cadence/trigger changes:**
   1. **Coach Beat** content gate in `_maybe_post_coach_beat` — move counter reset to post-fire.
   2. **The Ledger** phase-aware gate — add `_ledger_first_post_done` flag, fetch `current_phase`, route accordingly.

---

## Risks + mitigations

- **Risk: Coach Beat migration from `tactical` to `passthrough` breaks rendering.** The `_assemble_tactical` renderer accepts `bullets[]` + `verdict` and structures them; passthrough expects `body` as a complete formatted string. Mitigation: after the format_style swap, run a test sim of 100 games and grep `bot.log | grep "RAW.*coach_beat"` to confirm the LLM is returning `{headline, body}` (new shape) and not the old `{headline, lede, bullets, verdict}` shape. The `output_shape_override` should force compliance, but Haiku sometimes lags on cutover. If it does, the parser's old-shape fallback at line 578 will still produce a valid embed — just not the desired format.

- **Risk: Power List `rank_deltas` ctx contract.** If the previous-ranking fetch fails (e.g. first run of season, parsing the old body fails), the LLM has no deltas to use and may fabricate `↑3` / `↓2` glyphs. Mitigation: when previous ranking is absent, inject `rank_deltas = {}` AND prepend a hard rule to the user-content message: "RANK DELTAS: no prior ranking exists this season; use NEW for every team's arrow position." The template-level rule "Use these EXACT deltas. Do not invent movement." is the second line of defense.

- **Risk: Marcus Cole renderer rewrite ships before backward-compat fallback works.** Old articles in the DB still have `[FRAMING]`/`[ANALYSIS]` markers. If `_assemble_trade_report` is rewritten to ONLY handle the new `[TEAM_A]`/`[TEAM_B]` scheme, re-rendering an old article (e.g. via debug command) produces a stub. Mitigation: keep BOTH parser paths in `_assemble_trade_report`. Detect which marker scheme is present and route. The plan above already specifies this — flag it for the builder explicitly.

- **Risk: The Ledger `current_phase` fetch adds latency.** It's one extra DB query per batch. Mitigation: query is tiny (single column from `leagues` by PK). Acceptable. If contention is observed later, cache the phase per-batch in `batch_context`.

- **Risk: Coach Beat content gate causes the column to NEVER fire on quiet teams.** If every batch's selected subject team is `tendency_respecter` with no role changes, Quinn never posts. Mitigation: the priority-philosophy loop at line 1417-1425 ALREADY picks `chaos`, `vet_overrater`, `youth_developer` first and only falls back to `tendency_respecter`. The content gate fires only when both fallbacks lead to a boring-default team — which means there's genuinely nothing to say. That's the intended behaviour. If the user later complains "Coach Beat never fires," loosen the gate by ALSO firing when `posture` or `plan` changes are detected, not just `recent_role_changes`.

- **Risk: Marcus Cole new body markers conflict with the existing rule "DO NOT describe the swap structure in the framing or analysis."** The new `[TEAM_A]`/`[TEAM_B]` blurbs ARE per-team — the LLM might think "team_a section = list team A's assets." Mitigation: the new template explicitly says "Your job is NOT to list assets. Your job is to explain, per team, why this deal makes sense for THAT team specifically." Reinforce in the example body.

---

## Handoff block

```
=== HANDOFF ===
did: produced per-persona redesign spec for 8 personas + 2 cadence changes + cross-cutting dedupe
found: chose option C (centralized dedupe + per-persona instruction). Coach Beat migrates tactical→passthrough. Power List requires new ctx contract (rank_deltas from previous article). Marcus Cole gets new [TEAM_A]/[TEAM_B] marker scheme with backward-compat fallback. The Ledger switches to phase-aware gating. Rookie Watch has two variants — Variant A (Rivalry Frame) recommended, Variant B (Comedy Frame) listed as alternative.
files-touched: Projects/dba/.design/persona-redesign-2026-05-21.md
next-suggested-agent: backend-dev (after orchestrator confirms Rookie Watch variant)
blockers: orchestrator decision needed — Rookie Watch Variant A (Rivalry Frame, recommended) vs Variant B (Comedy Frame). Everything else is decided in-spec.
=== END HANDOFF ===
```

---

## Rookie Watch — Hybrid override (orchestrator decision, 2026-05-21)

User picked **Hybrid** over architect's Variant A. Use Variant A's stat-anchored two-rookie rivalry frame as the skeleton, then layer in 1-2 manufactured "shade / posterize" beats per post drawn from Variant B's tone.

Per-post structure (the body-template for `rookie_watch.py` voice_notes should specify EXACTLY this):

```
**[Rookie A] vs [Rookie B]: [headline tied to actual stat gap]**

🥇 **[Rookie A]** — [STAT LINE pulled from context]
🥈 **[Rookie B]** — [STAT LINE pulled from context]

[ONE-LINE manufactured banter line from one of them — italicized, framed clearly as banter, never written to read like a real wire-service quote. Optional parenthetical reveal at the end.]

**Posterize of the week:** [ONE BEAT — either a real highlight pulled from context, or a "X put Y on a milk carton" style callout grounded in an actual game from this batch.]
```

Concrete example (the target shape the user signed off on):

```
**Wemby vs Edey: 18-Block Gap, One Awkward Silence**

🥇 **Wemby** — 18.2 / 9.1 / 3.7 bpg
🥈 **Edey** — 16.4 / 11.0 / 1.2 bpg

Wemby on the gap, asked postgame: *"I don't read votes."* (he reads votes.)

**Posterize of the week:** Edey put Sarr on a milk carton in the 3rd.
```

Hard rules for the LLM (bake into voice_notes verbatim):

- Stats must come from context — never invent.
- "Quotes" are framed as banter, not real quotes. Italicize them. Never write a quote that could be mistaken for a real one a beat reporter logged.
- "Posterize of the week" must reference a real game from the batch context. If no posterize-worthy moment is in context, substitute "**Stat of the week:** [real number from context]" instead.
- Max ~80 words total body. Short fun column, not a feature.

Variant B (Comedy Frame standalone) is rejected; do not use its single-rookie spotlight or invented-quote-of-the-week structure.

