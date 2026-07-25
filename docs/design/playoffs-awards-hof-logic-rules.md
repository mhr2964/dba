# Playoffs / Awards / HOF Logic Rules

Durable rule specs for the playoff sim path (`services/playoff_service.py`, plus the
`services/sim_orchestrator.py` pre-sim extraction it now shares with the regular
season), the CPU awards voting pipeline (`services/awards_service.py`,
`services/cpu_voter.py`), Player of the Month (`services/potm_service.py`), and
Hall of Fame induction (`services/hof_service.py`). Same convention as
`docs/design/trade-logic-rules.md`: each rule (PA1-PA14) records the observed
problem, the evidence that justified it, and the actual fix — so a future change
to this domain can be checked against *why* the rule exists, not just *that* it
exists.

**Current implementation status (as of 2026-07-25):** PA1-PA8, PA10-PA14 shipped.
PA9 deferred (real schema gap — see its entry below). This is the third realism
sweep in this project (`docs/design/{fa,draft,progression}-logic-rules.md` covered
free agency/draft/progression); Playoffs/Awards/HOF was the last major
CPU-decision-driven domain from the original audit's scope, and turned out to
contain the single most severe bug found across all three sweeps (PA1).

---

## PA1. Playoff games silently ran sim_engine's legacy fallback — no CPU gameplans, no directives, no scheme, no minutes plan, no fatigue

**Status:** SHIPPED

**Evidence:** `services/playoff_service.py::_run_one_game` called
`sim_engine.sim_game(home_dict, away_dict, home_players, away_players, rng_seed)`
with only 5 positional args. `services/sim_orchestrator.py::_sim_single_game` (the
regular-season path) calls the same function additionally passing `fatigue=`,
`home_strategy=`, `away_strategy=`, `home_minutes=`, `away_minutes=`.
`services/sim_engine.py` branches on
`_has_role_data = any(p.get("_role_touch_share") is not None ...)`, and
`playoff_service.py` never stamped that field — every playoff/play-in game really
did run the "should not be reached in normal operation post-Phase-1" legacy
fallback (`sim_engine.py`'s OVR × usage-weight formula), meaning zero CPU
gameplans, zero player directives, zero scheme modifiers, zero coach-set minutes
plan, and zero fatigue modeling for the entire playoffs.

**Fix:** Extracted the pre-sim input-building step of
`sim_orchestrator._sim_single_game` — CPU/human gameplan decision
(`cpu_coach_service.decide_gameplans` + `gameplan_repo.record_gameplan`),
directive application (`_apply_cpu_directives`/`_apply_directives`), strategy
modifiers (`strategy_service.get_sim_modifiers`), role-stamping
(`sim_persistence._stamp_role_data`), coach minutes plan
(`strategy_repo.get_team_minutes_plan`), and back-to-back fatigue
(`game_repo.is_back_to_back`) — into a new shared function,
`sim_orchestrator._build_pre_sim_inputs`. Both `_sim_single_game` (regular
season) and `playoff_service._run_one_game` now call this one function before
their respective `sim_engine.sim_game` calls, so a real, ROLE-stamped sim runs
for every playoff and play-in game, not just the regular season.

**Persistence deliberately stays separate.** `playoff_service._run_one_game`
does NOT route through `sim_persistence._persist_game_result` — that call
chain calls `game_repo.update_standings`, which must never see playoff/play-in
results (playoff outcomes must not perturb regular-season `standings_cache`).
`playoff_service.py` keeps its own series-aware persistence path, now fed by
the same rich sim inputs the regular season uses. PA4 and PA5 (below) wire the
two pieces of `_persist_game_result`'s behavior that ARE safe to reuse directly
into that same path.

**No circular import:** `playoff_service.py` imports `sim_orchestrator` at
module level (`from services import records_service, sim_engine,
sim_orchestrator`). Confirmed no module `sim_orchestrator` imports (directly or
transitively) imports `playoff_service` — the only other reference to
`playoff_service` anywhere in `services/` is a docstring comment in
`sim_content_pipeline.py`, not an import.

**Known remaining gap (not closed by this pass):** `playoff_service._load_lineup`
does not LEFT JOIN `player_directives` the way
`sim_persistence._load_lineup_for_team` does, so a human-managed playoff team's
manually-set per-player directives (`/directive` command) still won't apply in
the playoffs — only CPU-computed directives do (via the newly-wired
`_apply_cpu_directives` path, when `gameplan["source"] == "cpu"`). This is a
narrower, pre-existing gap than the one PA1 closes (before this fix, NEITHER
CPU nor human directives applied in the playoffs at all) and was out of this
pass's explicit scope; flagged here for a future pass rather than silently
left undocumented.

**Files:** `services/sim_orchestrator.py` (new `_build_pre_sim_inputs`),
`services/playoff_service.py` (`_run_one_game` calls it).

---

## PA2. NBA Finals home court unconditionally went to the East winner

**Status:** SHIPPED

**Evidence:** `playoff_service.advance_playoff_round`'s Finals-creation branch
set `high_seed_id=east_winner, low_seed_id=west_winner` unconditionally,
regardless of either finalist's actual regular-season record — a Western
Conference #1 seed with a much better record than a mediocre Eastern
Conference winner would still play the Finals on the road.

**Fix:** New `playoff_service._finals_home_seed(pool, league_id, season,
east_winner_id, west_winner_id)` fetches both finalists' `standings_cache`
records via the same `game_repo.get_standings` call `seed_playoffs` already
uses, and picks whichever finalist has the better `(-wins, losses)` key — the
same tie-break convention `_standings_to_seeds` already applies within a
conference. Falls back to the pre-PA2 East-as-high-seed default only if
standings data is missing for either team (shouldn't happen in normal flow,
both teams just finished a full regular season). The Finals prelude-preview
post (`sim_content_pipeline._maybe_post_prelude`) was updated to label
`high_seed_team`/`low_seed_team` consistently with the corrected seed, instead
of always describing the East winner as "high seed."

**Files:** `services/playoff_service.py` (`_finals_home_seed`, Finals-creation
call site).

---

## PA3. Series MVP / stats queries leaked regular-season head-to-head games into "series" stats

**Status:** SHIPPED

**Evidence:** `_compute_series_mvp` and `_get_series_stats_for_player` gated
their box-score aggregation via an `EXISTS` clause that only checked that a
game's two teams matched the series' two teams
(`g.home_team_id IN (high, low) AND g.away_team_id IN (high, low)`) — it never
checked `g.series_id` or `g.season_type`. Any regular-season game between the
same two teams (which happens routinely — teams play each other multiple times
per season) counted toward Conference-Finals-MVP/Finals-MVP stat lines and
scoring.

**Fix:** Both queries now filter directly on `g.series_id = $N AND
g.season_type IN ('playoff', 'play_in')` instead of the team-pair `EXISTS`
subquery. `games.series_id` is already populated correctly at game-insert time
(`playoff_service._run_one_game` always passes it), so this is a pure
narrowing of an existing, already-available column — no new data needed.

**Files:** `services/playoff_service.py` (`_compute_series_mvp`,
`_get_series_stats_for_player`).

---

## PA4. Playoff injuries were never persisted or announced

**Status:** SHIPPED — bundled with PA1/PA5 (same function, same root cause: the playoff path skipped the regular-season plumbing entirely)

**Evidence:** `sim_engine.sim_game`'s result dict always includes an
`"injuries"` key, but `playoff_service._run_one_game` never read it —
playoff/play-in injuries vanished with zero record, and playoff rosters never
reflected an injury incurred during the postseason.

**Fix:** `_run_one_game` now calls `sim_persistence._persist_injuries` directly
(imported, not through `_persist_game_result`) after persisting the box score.
When a `guild` is available (threaded through from `sim_series_game`/
`sim_play_in`'s existing `guild` parameter), the same "injuries" channel
lookup the regular season uses (`sim_channel_announcer._get_injury_channel`,
which falls back to the news channel) is resolved so playoff injuries are
announced, not just silently written to the `injuries` table.

**Files:** `services/playoff_service.py` (`_run_one_game`, plus threading
`guild` through `sim_series_game`/`sim_play_in`'s existing `_run_one_game`
call sites).

---

## PA5. Playoff performances never checked against all-time records

**Status:** SHIPPED — bundled with PA1/PA4

**Evidence:** `records_service.check_and_update_records` is already
standalone (no `standings_cache` coupling — confirmed safe to call directly,
unlike `_persist_game_result`), but `playoff_service._run_one_game` never
called it. A 60-point playoff explosion or a record-setting playoff blowout
would never register as an all-time record.

**Fix:** `_run_one_game` injects `home_team_id`/`away_team_id` onto the sim
`result` dict (matching what `sim_persistence._persist_game_result` does for
the regular season, since `check_and_update_records` needs those fields to
resolve team names) and calls `records_service.check_and_update_records`
directly after persisting the box score. Announcements are logged (no
Discord channel is threaded into this call specifically — future work could
wire a `#records` post here the way the regular season does, but persistence
of the record itself, the part PA5 actually asked for, is unconditional).

**Files:** `services/playoff_service.py` (`_run_one_game`).

---

## PA6. Three inconsistent "canonical MVP formula" copies — cpu_voter's team-component now calls awards_service's

**Status:** SHIPPED — bundled with PA7 (same root cause)

**Evidence:** `awards_service._mvp_score` (in-season race ranking, no team
term by design), `cpu_voter.score_player_for_award`'s actual MVP formula (the
real full formula, including `win_pct*32` and a tank-team cap), and a stale
comment in `cpu_voter.py` claiming the two were "kept in sync" with
`awards_service._mvp_team_adjustments(win_pct, wins)` — except that function
was **dead code** (zero call sites) with an unused `wins` parameter, despite
its own docstring promising a "tank-team hard cap at 35."

**Fix:** `cpu_voter.score_player_for_award`'s MVP branch now actually calls
`awards_service._mvp_team_adjustments(win_pct, wins)` for its team-component
(deferred import inside the function body to avoid a circular import —
`awards_service` imports `cpu_voter` at module level). `_mvp_team_adjustments`
itself now implements the tank-team floor its docstring already promised: it
only sees `win_pct`/`wins`, not a player's box-score terms, so it can't
reproduce the old cpu_voter behavior of clamping the WHOLE mvp score at 35 —
instead, a team with fewer than 25 wins gets its team-component floored at
**-15** (rather than the small single-digit value the raw formula would
otherwise produce), which — combined with typical MVP-caliber stat
contributions (~40-50 points under the ppg/apg/rpg/ts_pct weights) — still
lands the total score comfortably under the ~35 threshold real MVP candidates
clear. This is a deliberate, narrower substitute for the old whole-score
clamp, disclosed here rather than silently changing behavior: the separate
`conf_rank >= 12` gate a few lines later in `cpu_voter.py` still independently
hard-ceilings bottom-of-conference teams at 25, so the two protections
overlap for most tank-team cases in practice.

**Files:** `services/awards_service.py` (`_mvp_team_adjustments`),
`services/cpu_voter.py` (`score_player_for_award`'s MVP branch).

---

## PA7. AI awards-odds prompt claimed a formula that doesn't match reality

**Status:** SHIPPED, bundled with PA6 (same root cause — drifted "canonical formula" copies)

**Evidence:** `awards_service.generate_awards_race_odds`'s Claude prompt
claimed: *"the canonical ranking formula is ppg\*1.0 + apg\*0.6 + rpg\*0.4 +
team_win_pct\*20 + ts_pct\*10. The candidates are already sorted by this
formula."* Neither half was true: `_mvp_score` (the actual sort key used to
rank/slice race leaders) has **no** `team_win_pct` term at all — its own
docstring explains why (standings aren't always available in the race-leader
context) — so the candidates were sorted by a *different*, simpler formula
than the one described to the AI.

**Fix:** Corrected the prompt text to state the real formula
(`ppg*1.0 + apg*0.6 + rpg*0.4 + ts_pct*10`, with a parenthetical noting team
record isn't a factor at this stage) — which also makes the "already sorted
by this formula" claim true again, since the corrected text now matches
`_mvp_score` exactly.

**Files:** `services/awards_service.py` (`generate_awards_race_odds` prompt string).

---

## PA8. close_voting capped All-NBA to top-5 but not single-winner awards

**Status:** SHIPPED

**Evidence:** `close_voting`'s All-NBA branch already sliced `ranked[:5]`
before persisting `award_results` rows, but the single-winner branch
(mvp/dpoy/roy/6moy) persisted **every** player who received even one stray
vote, forever. This also silently broke HOF's MVP-vote threshold (PA10/PA11
below assume `award_results` rows for `mvp` reflect real vote leaders, not
every player who got a single CPU vote).

**Fix:** The single-winner branch now slices `ranked[:5]` too, gated on
`award_type in _SINGLE_WINNER_AWARDS` (mvp/dpoy/roy/6moy). All-Star
(`all_star_east`/`all_star_west`) is explicitly NOT capped — a conference
roster legitimately has more than 5 All-Stars, so the uncapped behavior there
is correct and unchanged.

**Files:** `services/awards_service.py` (`close_voting`).

---

## PA9. Per-season historical OVR snapshot for HOF's elite-seasons count

**Status:** DEFERRED — real schema gap, same bar as trade-audit's B2 and progression's larger P6 deferral

**Evidence:** `hof_service._count_elite_seasons` is gated on the player's
*current* `overall` >= 85 as a lower bound, then counts distinct
`contracts.signed_in_season` values as a proxy for "seasons played" — there is
no per-season OVR snapshot table, so a player who peaked at 88 OVR at age 26
and declined to 79 by retirement gets ZERO elite seasons counted, even though
several of their actual seasons genuinely cleared the 85 threshold.

**Reasoning for deferring:** Fixing this properly needs a new table (a
per-season OVR snapshot, written at rollover time) plus a backfill story for
leagues that already have history — a new schema + write-path design, not a
scoped fix. Do not build a "minimal version" via a new table this pass. PA10
(All-NBA/All-Star selection counts) ships the practical mitigation instead:
data that's already correctly historical (season-scoped at vote time), no new
schema needed, covers much of the same "legitimately great career, hard to
prove from current-OVR alone" gap PA9 was reaching for.

**Files:** *(deferred — no files touched)*

---

## PA10. New HOF induction path: All-NBA / All-Star selection counts

**Status:** SHIPPED

**Evidence:** A long-tenured 2nd/3rd-team All-NBA player, or a perennial
All-Star who never won an MVP vote or a championship, had no induction path
at all short of the 15-year veteran-longevity fallback — a real gap for
players whose career case rests on sustained recognition rather than a single
peak or team success.

**Fix:** New `hof_service._count_all_nba_selections` /
`_count_all_star_selections` count `award_results` rows for
`award_type IN ('all_nba_1','all_nba_2','all_nba_3')` /
`award_type IN ('all_star_east','all_star_west')` respectively — same query
shape `_count_mvp_votes` already establishes for the `mvp` award type. `
_evaluate` gains a new induction path: **5+ All-NBA selections OR 8+ All-Star
selections**. Thresholds picked to sit clearly above "very good starter"
territory and in "unmistakable career" territory — a judgment call, same as
every other threshold in this file, not derived from a formula.

**Files:** `services/hof_service.py` (`_count_all_nba_selections`,
`_count_all_star_selections`, `_evaluate`).

---

## PA11. Retirement/age gate added to every non-longevity induction path

**Status:** SHIPPED

**Evidence:** `check_and_induct`'s 3 pre-existing non-longevity paths
(championships / elite-seasons / MVP-votes) had no age or career-length gate
at all — only the veteran-longevity path implicitly gated on career length
(`years_pro >= 15`). A 24-year-old still-active player who happened to win 3
championships early (e.g. drafted onto an already-stacked roster) or rack up
8 stray MVP votes could be inducted mid-career, years before their actual body
of work is complete.

**Fix:** `check_and_induct` computes `retirement_eligible = roster_status ==
'retired' OR years_pro >= 8` per candidate, and `_evaluate` gates the
championships / elite-seasons / MVP-votes paths AND the new PA10 All-NBA/
All-Star path behind it. The veteran-longevity path is intentionally
excluded from this gate — its own `years_pro >= 15` requirement already sits
well above the 8-year floor, so gating it again would be redundant.
`_RETIREMENT_GATE_YEARS_PRO = 8` is a judgment call (not derived from a
formula): long enough that a player has a real, evaluable body of work, short
enough not to functionally require retirement for every non-longevity path.

**Files:** `services/hof_service.py` (`check_and_induct`, `_evaluate`).

---

## PA12. DPOY's flat -3 penalty for centers was backwards

**Status:** SHIPPED

**Evidence:** `cpu_voter.score_player_for_award`'s DPOY branch applied a flat
`-3` penalty for `position == 'C'` — directly contradicting real NBA DPOY
history, which is dominated by rim-protecting centers (Gobert, Mutombo, Ben
Wallace, etc.), not biased against them.

**Fix:** Removed the penalty outright rather than replacing it with a
position-based bonus (which would just be a different arbitrary thumb on the
scale in the other direction). The existing `elite_blocker_bonus` (+3 for
2.0+ bpg) already rewards the specific skill — shot-blocking — that makes
centers legitimate DPOY candidates, without re-introducing any position-based
adjustment.

**Files:** `services/cpu_voter.py` (`score_player_for_award`'s DPOY branch).

---

## PA13. Player of the Month used a bare ppg/apg max(), no efficiency or team success

**Status:** SHIPPED

**Evidence:** `potm_service.check_and_get_potm_awards` picked each
conference's monthly winner via
`max(conf_players, key=lambda r: (float(r["ppg"]), float(r["apg"])))` — pure
volume scoring, no shooting efficiency, no team-success weighting. A
high-volume, inefficient scorer on a losing team would always beat a more
efficient, winning-team player with slightly lower ppg.

**Fix:** The monthly query now also aggregates `ts_pct`
(`SUM(points) / (2*(SUM(fga) + 0.44*SUM(fta)))`, same formula
`awards_service._get_eligible_players` already uses) and a new query computes
each team's month-scoped win_pct (regular-season, simmed games in the award
window only). A new `_potm_score` composite reuses `cpu_voter`'s
efficiency/win_pct weighting *philosophy* —
`ppg*1.0 + apg*0.6 + rpg*0.4 + ts_pct*10 + win_pct*10` — but deliberately does
NOT reuse cpu_voter's exact season-MVP weights: a POTM window is 10-20 games,
not a full 82-game season, so month-scoped win_pct is a much noisier signal
and gets a smaller weight (`_POTM_WIN_PCT_WEIGHT = 10` vs cpu_voter's `32`)
rather than pretending a 10-game win_pct deserves the same trust as a
season-long record. `_POTM_TS_PCT_WEIGHT`/`_POTM_WIN_PCT_WEIGHT` are named
module constants so the magnitudes are visible and adjustable in one place.

**Files:** `services/potm_service.py` (monthly query, new team-win_pct query,
`_potm_score`, winner-selection loop).

---

## PA14. CPU voter profile was `team_id % 4` — replaced with real team signals

**Status:** SHIPPED, lowest-confidence item in this domain — the team-signal→voter-lean mapping itself is a judgment call with no ground truth; treat post-ship tuning as a follow-up, not evidence it shouldn't have shipped

**Evidence:** `cpu_voter.get_cpu_profile(team_id)` picked a `VoterProfile` via
`team_id % len(VoterProfile)` — completely arbitrary, unrelated to anything
about the team.

**Fix:** `get_cpu_profile` is now a pure function of three real signals the
caller (`awards_service.generate_cpu_votes`) fetches upstream (same pattern
already used for `records_by_team`/`conf_rank` elsewhere in that function):
`offense_rating`, `defense_rating`, and `win_pct`.
- `win_pct >= 0.60` (a real contender) → `WINNING` (a winning team's front
  office values team success in its own award ballot).
- Otherwise, a meaningful offense/defense rating gap (`>= 3` either
  direction) → `SCORER` (offense-heavy identity) or `DEFENSE`
  (defense-heavy identity).
- A balanced, non-winning team → `EFFICIENCY` (the "no strong signal" default).

`team_id` is kept as the function's first positional argument so the call
shape doesn't change for the one existing call site, but it plays no role in
the branch logic anymore — the real signals do. `generate_cpu_votes` fetches
`teams.team_offense_rating`/`team_defense_rating` plus a month-agnostic
`standings_cache` win_pct for each CPU voting team (not the eligible
candidates' teams — a distinct, separately-fetched set, since a voting team
and a candidate's team aren't guaranteed to overlap).

**Files:** `services/cpu_voter.py` (`get_cpu_profile`),
`services/awards_service.py` (`generate_cpu_votes` — new `voter_signals` fetch
and call-site update).

---

## Confirmed clean, no action needed

Play-in/bracket seeding logic (`seed_playoffs`, `sim_play_in`) is correct
standard NBA format — 7v8/9v10 play-in, standard bracket reseeding. Zero live
instances of the `games.status='final'`/`'completed'` dead-filter bug class
(already fully stamped out by the prior realism sweep) exist anywhere in this
domain.
