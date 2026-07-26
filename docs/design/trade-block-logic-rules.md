# Trade-Block Logic Rules — CPU Trade-Block Listing Heuristic

Durable rule specs for `services/cpu_block_service.py`, following
`docs/design/trade-logic-rules.md`'s convention: each finding gets a short
rule-id, a scoped fix, a status line (SHIPPED/DEFERRED), and its own test.
Written 2026-07-25 as Plan F of the realism-sweep initiative (Plans C, D, E —
phase-transition, role-assignment, free-agency — shipped separately; see
`Brain/General Session Notes` for the sweep's full scope).

**Current implementation status (as of 2026-07-26):** TB1-TB3 addressed (TB1
shipped as a real-bug fix; TB2 verified and also fixed after confirming a
genuine units mismatch; TB3 is the display-layer follow-up TB2 flagged as
deferred).

---

## TB1. [Real bug] Candidate-selection branch read a stale, creation-time `cpu_mode` column instead of live posture

**Status:** SHIPPED

**Evidence:** `refresh_team_block` read `team.cpu_mode` — a column set once
when the team row is created and never updated afterward — to decide which of
the three candidate-selection branches (rebuilding / contending / developing)
applies. A team's actual in-season situation (record, conference rank, star
talent, franchise-plan goal) had zero influence on which players it listed on
the trade block: a team that started "developing" but is now a clear tanking
team (or a rebuilding team that turned into a surprise contender) kept
generating its trade-block list off its creation-time label forever. This is
the exact same anti-pattern FA1 (`docs/design/fa-logic-rules.md`) found and
fixed in `services/fa_service.py`'s `submit_cpu_offers`.

**Rule:**
- Fetch the team's live posture via
  `team_intel.build_team_intel(pool, league, league.current_season, [team_id], include=("posture",))`
  (single-team call — `refresh_team_block` operates on one team at a time,
  unlike FA1's batched all-CPU-teams call in `submit_cpu_offers`).
- `trade_context_builder.compute_team_mode` (the single source of truth
  `build_team_intel` delegates to) returns exactly 5 mode values in practice:
  `contending`, `play_in_fringe`, `soft_rebuild`, `rebuilding`, `developing`.
  (`transition` appears only in internal floor-check `in (...)` tuples inside
  `compute_team_mode` — it is never actually assigned to `raw_mode`/`base_mode`
  and so is never returned. Confirmed by reading the full function body rather
  than assuming from the floor-check tuples.)
- `_POSTURE_BLOCK_BRANCH` maps the live mode to the pre-existing
  three-branch candidate selector:
  - `contending` / `play_in_fringe` → contending-mode candidates
    (`_candidates_contending`)
  - `soft_rebuild` / `rebuilding` → rebuilding-mode candidates
    (`_candidates_rebuilding`)
  - `developing` (and `transition`, mapped defensively even though it's
    currently dead) → developing-mode candidates (`_candidates_developing`)
- If `build_team_intel` returns no posture data for the team (e.g. no
  `standings_cache` row yet, or the league itself isn't found), falls back to
  the old static `team.cpu_mode` 2-way-plus-default branch and logs a warning
  — defensive, mirrors FA1's fallback exactly (same log-and-fallback shape,
  same "developing is the default for any unrecognized value" behavior).

**Test:** `tests/test_cpu_block_service.py::test_refresh_team_block_uses_live_posture_not_stale_cpu_mode`
— live smoke test against a real seeded `dba_test` league. Team row created
with `cpu_mode='developing'`, but seeded with a 5-55 record so live posture
resolves to `rebuilding` (`projected_wins` ≈ 7, well under
`compute_team_mode`'s `<=25` hard-rebuild cutoff). Roster is a single age-31
OVR-75 veteran on a non-expiring 2yr deal — a `_candidates_rebuilding` hit
(age ≥ 30, OVR ≥ 72) but *not* a `_candidates_developing` hit (age 31 is under
the 32 veteran floor there, and there's no redundant-position depth with only
one player). Confirmed by running the test against pre-fix code
(`git stash` the fix, re-run): pre-fix produces `listed_count == 0` (reads the
stale `cpu_mode='developing'` column); post-fix produces `listed_count == 1`
with the expected `"Rebuilding — veteran asset available"` note.

---

## TB2. [Real bug, found during verification] `asking_price` was the only place in the codebase that scaled `player_trade_value`'s raw output into dollars

**Status:** SHIPPED (bug confirmed, not "no bug" — see verification below)

**Evidence — verification steps taken before touching any code:**
1. Read `services/trade_value_math.py::player_trade_value` — its docstring and
   every internal modifier (`_age_multiplier`, `_upside_modifier`,
   `_contract_modifier`, `defensive_impact_modifier`, `_star_leverage_modifier`,
   `experience_premium`) describe an abstract, unitless comparison score (the
   OVR curve is normalized so OVR 80 anchors near 40; typical outputs range
   roughly 20-100), never dollars.
2. Read every other call site of `player_trade_value` in the codebase (not
   just the two named as a minimum): `trade_grading.py` (`evaluate_trade`'s
   `score_a`/`score_b`/`market_score_a`/`market_score_b`, and `grade_trade`'s
   percentage-of-max-side thresholds), `cpu_trade_evaluation.py`
   (`cpu_score = evaluation["score_a"] - evaluation["score_b"]`, passed straight
   to `trade_repo.set_cpu_evaluation`), `cpu_trade_acceptance.py` (fleecing-floor
   ratio math), `trade_gates.py` (B5 differential/max-side math), `trade_magnitude.py`
   (sum of raw values as a trade's "magnitude"), `trade_proposal_scoring.py`, and
   `trade_return_builder.py`. **Zero of these eight call sites apply any dollar
   scaling** — every one treats the raw return value as a direct, unscaled
   comparison score.
3. Computed representative examples with the actual formula
   (`player_trade_value(...) * 1_000_000`, the pre-fix code):
   - OVR 68 bench player, $2M/yr contract → raw value ≈ 40.1 → fabricated ask
     **$40,120,000/yr** for a replacement-level bench piece.
   - OVR 95 age-23 star on a $50M max-tier deal → raw value ≈ 98.5 → fabricated
     ask **$98,540,000/yr** — more than the entire $136M team salary cap, for
     one player.
   - Compared against `services/player_decision.py::_salary_floor`, the
     codebase's actual established OVR→dollar conversion (tiers a fraction of
     `salary_cap` by OVR band, e.g. OVR ≥ 90 → 28% of cap ≈ $38M for a $136M
     cap) — the trade-block numbers above are wildly out of line with what the
     game's own salary structure would ever produce for players of that
     quality/contract.
4. Confirmed `asking_price` is never read back into any evaluation/matching
   logic (`data/repositories/trade_block_repo.py` — display-only column,
   consumed solely by `bot/embeds/trade_embeds.py`'s `${asking_price:,}`
   formatting), so this wasn't merely a cosmetic quirk contained to file
   internals it was designed for.

**Conclusion:** genuine mismatch — confirmed bug, not "verified, no bug."
`cpu_block_service.py`'s `* 1_000_000` was the single, unexplained departure
from `player_trade_value`'s universal "unscaled comparison score" convention
used at every other call site.

**Rule:**
- Removed the `* 1_000_000` multiplier. `asking_price` is now
  `int(player_trade_value({"overall": p.overall, "age": age_map[p.id]}, c, salary_cap))`
  — numerically identical to what a live trade proposal computes for the same
  player via the same function (matching `trade_grading.evaluate_trade`'s
  `market_score_a`/`market_score_b` exactly for players where
  `asset_upside_modifier` is 1.0, i.e. age ≥ 26 with no award-race ranks
  supplied).
- **Known side effect, explicitly out of scope this pass:** `asking_price` no
  longer renders as a plausible dollar figure in
  `bot/embeds/trade_embeds.py` (e.g. a solid starter now shows `$42` instead
  of `$42,000,000`), and it's no longer on the same visual scale as a human
  manager's own hand-entered `asking_price` (which really is annual dollars,
  per `bot/cogs/trade_block_cog.py`'s own parameter description) when both
  appear side-by-side in `trade_block_league_embed`. Fixing that display-scale
  question would require either reformatting the embed for CPU-listed entries
  specifically or inventing a new points-to-dollar conversion anchored to the
  salary cap (there is no such conversion established anywhere in the
  codebase to reuse) — both are a separate design decision, out of scope for
  a "make the units consistent with every other call site" bug fix. Flagged
  here rather than silently left for a future pass to rediscover.

**Test:** `tests/test_cpu_block_service.py::test_refresh_team_block_asking_price_matches_real_trade_valuation`
— seeds a single expiring-contract candidate (OVR 70, age 28, $8M/yr,
1yr remaining) and asserts the stored `asking_price` equals
`int(trade_grading.evaluate_trade(...)["market_score_a"])` computed for the
identical player/contract/cap inputs. Confirmed by running against pre-fix
code: pre-fix stored `27,890,000` where post-fix (and a live trade proposal's
own valuation of the same player) is `27`.

---

## TB3. [Display bug, follow-up to TB2] Trade-block embeds rendered every `asking_price` as a dollar figure, even for CPU-generated entries now holding an unscaled score

**Status:** SHIPPED

**Evidence:** TB2 removed the `* 1_000_000` multiplier from
`cpu_block_service.refresh_team_block`, correctly making `asking_price`
numerically consistent with every other `player_trade_value` call site — but
`bot/embeds/trade_embeds.py`'s `trade_block_team_embed` and
`trade_block_league_embed` both unconditionally formatted every entry's
`asking_price` as `${entry['asking_price']:,}`, with no awareness that the
value's unit now depends on which path produced the entry. Post-TB2, a
CPU-generated entry shows a small unscaled comparison score (e.g. `27`) while
a human `/block add` entry shows a real annual salary (e.g. `18,000,000`) —
both rendered with the identical `$X,XXX` format, so `/block view` and
`/block league` now display visibly broken-looking numbers like `"— asking
$27"` next to `"— asking $18,000,000"` in the same embed.

**Rule:**
- A team is CPU-controlled iff `team.manager_user_id is None` (confirmed via
  `cpu_block_service.refresh_team_block`, which explicitly skips any team
  where `manager_user_id is not None`). This is already available at render
  time with zero schema change.
- **Approved fix (user decision, not reconsidered here):** hide the
  asking-price line entirely for CPU-generated entries. Human-submitted
  entries keep their existing `$X,XXX,XXX` format completely unchanged.
  CPU-generated entries show just the player and their existing qualitative
  `note` field, with no price line at all.
- Two alternatives were considered and explicitly rejected by the user in
  favor of the above: (1) a non-dollar labeled score (e.g. `"trade value:
  27"`) — rejected as adding a number Discord users would still misread as
  a price; (2) deriving a real dollar figure from the player's contract
  salary at render time — rejected because it would require inventing a new
  points-to-dollar (or salary-passthrough) conversion with no established
  precedent in the codebase, purely for cosmetic display, when the
  underlying `asking_price` column is intentionally not a salary for CPU
  listings.
- `trade_block_team_embed(team, block_entries, players_by_id)`: the
  asking-price branch now also requires `team.manager_user_id is not None`
  (the function already receives a single `team` for all entries).
- `trade_block_league_embed(entries_by_team, teams_by_id, players_by_id)`:
  same gate, but per-entry — looked up via `teams_by_id.get(team_id)` inside
  the existing per-team loop (mirrors the lookup already done there for the
  team name). If a `team_id` has no matching `Team` in `teams_by_id`, the
  price line is also hidden defensively (treated the same as an unknown/CPU
  team, not the same as a confirmed human team).
- `trade_block_added_embed` (the `/block add` confirmation embed) is
  untouched — it is only ever called from the human `/block add` path with a
  real dollar figure the user typed, never from CPU-generated listings.

**Test:** `tests/test_trade_embeds.py` (new file) — pure/synchronous unit
tests, no live-DB smoke test needed (display-only change, same reasoning as
TB1/TB2's own precedent for pairing DB-backed logic fixes with lighter tests
for pure display functions):
- `test_team_embed_cpu_team_hides_asking_price_but_keeps_note` — CPU team
  (`manager_user_id=None`) with a non-`None` `asking_price`: asserts no `$` /
  "asking" text appears, but the player's `note` and name still render.
- `test_team_embed_human_team_keeps_formatted_asking_price` — human team
  (`manager_user_id=555`): asserts the existing `— asking $18,000,000`
  format renders exactly as before (regression guard for the human path).
- `test_league_embed_mixed_cpu_and_human_teams` — both team types passed to
  `trade_block_league_embed` in the same call: asserts the CPU team's field
  has no `$` while the human team's field in the same embed still does.
- `test_asking_price_none_still_omits_price_line_for_both_team_types` —
  `asking_price=None` (e.g. a human `/block add` with no price given)
  renders no price line for either team type, confirming the pre-existing
  `None` behavior is unchanged by the new gate.

**Files:** `bot/embeds/trade_embeds.py` (display-only change),
`tests/test_trade_embeds.py` (new).

---

## Deferred (explicitly not built this pass)

None currently outstanding. TB1-TB3 cover every issue found across the two
verification passes (Plan F, Plan G) into `services/cpu_block_service.py` and
its downstream Discord rendering.
