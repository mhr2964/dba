# In-Game Strategy / Coaching AI Logic Rules

Durable rule specs for the coaching-AI domain: CPU gameplan decisions
(`services/cpu_coach_service.py`), the strategy modifier layer
(`services/strategy_service.py`, `data/repositories/strategy_repo.py`), and
the parts of the sim engine (`services/sim_engine.py`) where a coach's
decisions actually land on the box score. Same convention as
`docs/design/rollover-logic-rules.md` and this project's other realism
sweeps: each rule (CA1-CA10) records the observed problem, the evidence that
justified it, and the actual fix — so a future change to this domain can be
checked against *why* the rule exists, not just *that* it exists. This is the
fifth realism sweep in this project (`docs/design/{fa,draft,progression,
playoffs-awards-hof,rollover}-logic-rules.md` covered the prior four) and the
last of the four-domain initiative begun with sweep 1 (trades/scheme). It is
also the only one of the five domains that had a PRIOR, undocumented realism
pass (visible only as inline "Finding #1"-"Finding #5" code comments) — see
the retroactive section at the bottom for that history.

**Current implementation status (as of 2026-07-25):** CA1, CA2, CA3, CA4,
CA5, CA6, CA7 shipped. CA8, CA9, CA10 deferred (documented decisions — see
their entries below).

**Architectural constraint that shapes every rule below:** `sim_engine.sim_game`
(`services/sim_engine.py`, `sim_game` function) is a PURE, SINGLE-SHOT
function: final scores are computed from pace+PPP first, then box-score stats
are allocated against the already-known totals. There is no possession loop,
no quarter-by-quarter engine, no game clock. Every SHIP rule below reacts to
values already computed earlier in the same pass (final score margin, planned
minutes, roster attributes) — none of them, and none of the DEFER rules
either, could be fixed by adding causal mid-game logic, because the engine
has no mid-game state to be causal about.

---

## CA1. Blowout garbage-time minutes never existed — starters played full minutes regardless of final margin

**Status:** SHIPPED (disclosed placeholder constants)

**Evidence:** `_build_box_for_team`'s minutes computation (both the
`minutes_override` branch and the auto-allocate branch) had no adjustment for
the game's outcome at all — a 40-point blowout gave starters the same
~33-38 minutes as a 2-point nailbiter. Real coaches pull starters once a game
is decided; the sim had no equivalent, for either coach-set or auto-allocated
rotations.

**Fix:** Added a post-processing pass at the end of the minutes computation
(after both branches, before the role-based touch-share section) that scales
down starters' minutes above a floor once `abs(score_diff) >=
_BLOWOUT_THRESHOLD` (20.0), with severity scaling up to `_BLOWOUT_MAX_THRESHOLD`
(30.0). `_GARBAGE_TIME_STARTER_FLOOR` (22.0) is the minimum a starter is ever
pulled to. Freed minutes are redistributed to bench players proportionally,
capped at the existing bench-minutes ceiling (38.0, matching the cap already
used by both branches above it). Applies unconditionally as a final
adjustment pass — not conditional on how minutes were originally decided, so
a coach-set rotation still gets pulled in a real blowout. All three
thresholds are disclosed placeholders, not derived from a formula.

**Verification:** Mandatory live smoke test (`tests/test_sim_engine.py`) —
calls the real `_build_box_for_team` directly (not mocked) with a forced
30+ point margin vs. a close-game margin, asserts starters play measurably
fewer combined minutes and bench players measurably more in the blowout case;
a separate test confirms no starter drops below the 22-minute floor even at
maximum severity; a third confirms a mild (20-point) blowout reduces less
than a severe (35-point) one. Confirmed to fail against pre-fix code (2 of 3
new tests failed with identical mild/severe minutes, `193.3 == 193.3`) via
`git stash` of the source file, and to pass post-fix.

**Files:** `services/sim_engine.py`.

---

## CA2 + CA3. Usage-mode directives were dead-wired into the sim, and a redundant block stomped tanking teams' usage decisions — SHIPPED TOGETHER

**Status:** SHIPPED (CA2's exponent/nudge magnitudes are disclosed placeholders)

**Evidence:** Two bugs that only break in combination:

- **CA2:** `usage_weight` (set by `sim_persistence._apply_directives` from a
  player's `usage_mode` directive — `feature` ×1.4, `conserve` ×0.6) and
  `star_usage_mult` (computed by `strategy_service.get_sim_modifiers` from a
  team's `star_usage` setting) were both computed upstream but never read by
  `sim_engine._build_box_for_team`'s role-based touch-share branch (the live
  path for every game since the Phase 2 `player_roles` rollout). A "feature"
  directive had literally zero effect on scoring share once role data was
  stamped onto a player.
- **CA3:** `cpu_coach_service._decide_player_directives` had a redundant
  second `if posture == "tanking":` block that unconditionally overwrote the
  first if/elif chain's decision for every tanking team, on every single
  game — even when the first chain's earlier branches (e.g. `rank == 0 and
  scheme == "isolation" -> feature`) had already assigned a real "feature"
  directive to the team's own star. The redundant block always won.

Shipping either fix alone does nothing: without CA3, "feature" never
survives to reach the sim for a tanking team; without CA2, "feature" would
survive but still move zero box-score points.

**Fix:** CA2 — before per-game noise/renormalization, each player's touch
share is multiplied by `(usage_weight / 50.0) ** 0.6` (softer than the legacy
fallback formula's 1.55 exponent, since role-based `touch_share` already does
most of the concentration work). `star_usage_mult` now nudges ONLY the
single highest-OVR player's weight (`scoring_weights[star_idx] *= 1 +
(star_usage_mult - 1) * 0.6`), applied before renormalization — a uniform
team-wide scalar would cancel out entirely once `_distribute_proportional`
allocates the fixed team score by each player's *relative* weight share; only
a targeted, non-uniform nudge actually shifts the distribution. CA3 — deleted
the redundant tanking-override block entirely; the first chain's
tanking-aware `elif` branch is now the sole source of truth for tanking
teams' `usage_mode`.

**Verification:** Mandatory live smoke test
(`tests/test_coaching_ai_ca2_ca3_live_smoke.py`) — drives the real production
pipeline (`sim_orchestrator._sim_single_game`, real DB, `team_intel.compute_posture`
monkeypatched only to force a deterministic tanking posture, everything
downstream unmocked) across 30 real games for a tanking team with a
roster-true isolation star. Two dispositive assertions: (1) `"feature"`
appears in the star's persisted directive in at least one of 30 games
(pre-fix, via `git stash` of both source files: 0 of 30 — every single game
showed `"conserve"`); (2) games where the persisted directive was `"feature"`
average measurably more points for that player than games where it wasn't
(pre-fix: no `"feature"` games existed to even compare).

**Files:** `services/sim_engine.py`, `services/cpu_coach_service.py`,
`tests/test_coaching_ai_ca2_ca3_live_smoke.py` (new).

---

## CA4. `bench_leash`/`transition_aggression` columns existed but were silently dropped on every read, and no CPU coach ever set a real value

**Status:** SHIPPED (disclosed placeholder magnitudes)

**Evidence:** `strategy_repo.get_strategy`'s SELECT was missing
`bench_leash` and `transition_aggression` — both columns exist on
`team_strategies` with real DB defaults (`alembic/versions/035_cpu_gameplans.py`),
`bot/cogs/strategy_cog.py` already lets a human coach set them via slash
commands, and `set_strategy` already writes them — but every read silently
discarded whatever was written and fell back to a hardcoded default dict
missing those keys entirely. `cpu_coach_service._load_human_gameplan` had the
identical missing-column bug in its own separate inline query. Beyond the
read bug, no CPU coach ever computed a real value for either field in the
first place — `_decide_strategy` never set them, so every CPU team ran the
neutral default forever regardless of posture. Even `transition_aggression`
being read correctly wouldn't have mattered: `strategy_service.get_sim_modifiers`
passed the label through in its return dict but never converted it into an
actual pace/turnover modifier.

**Fix:** Added `bench_leash`/`transition_aggression` to both SELECTs and gave
`_DEFAULT_STRATEGY`/the inline fallback dict real defaults matching the DB
(`"normal"`/`"balanced"`). `cpu_coach_service._decide_strategy` now sets a
real value from the same `posture` signal the rest of the function already
uses: tanking -> short leash / retreat transition, contender -> long leash /
crash transition, else -> normal/balanced. `strategy_service.get_sim_modifiers`
converts `transition_aggression` into a real pace/turnover modifier (`crash`:
+2.0 pace / +0.3 turnover, `retreat`: -2.0 pace / -0.3 turnover) — disclosed
placeholders, deliberately smaller magnitude than `offensive_pace`'s primary
lever (+-5/+-6, up to +10 for `run_and_gun`) since this is a secondary,
transition-specific nudge on top of the team's main pace choice.
`sim_engine._build_box_for_team` now takes a `bench_leash` parameter and
scales CA1's blowout-severity reduction fraction by
`_BENCH_LEASH_SEVERITY_MULT = {"short": 1.3, "normal": 1.0, "long": 0.7}` —
applied to the reduction fraction, not to the pre-clamped severity value,
specifically so "short" and "normal" don't collapse to an identical result
at maximum blowout margin (where the effect matters most).

**Verification:** Two mandatory live smoke tests. (1)
`tests/test_strategy.py::test_bench_leash_and_transition_aggression_round_trip`
— writes `bench_leash="short"`/`transition_aggression="crash"` via the real
`set_strategy` against a real DB, reads it back via `get_strategy`, asserts
it round-trips; pre-fix (`git stash` of the 4 source files) this raised
`KeyError: 'bench_leash'`. (2)
`tests/test_sim_engine.py::test_short_bench_leash_pulls_starters_harder_than_long_leash_in_same_blowout`
— calls the real `_build_box_for_team` with an identical forced blowout for
a `"short"` vs `"long"` bench_leash team, asserts the short-leash team's
starters play measurably fewer minutes; pre-fix this raised `TypeError` for
the then-nonexistent `bench_leash` keyword argument.

**Files:** `data/repositories/strategy_repo.py`, `services/cpu_coach_service.py`,
`services/strategy_service.py`, `services/sim_engine.py`.

---

## CA5. CPU coaches had zero scheme memory — a fresh scheme was picked every single game

**Status:** SHIPPED (disclosed placeholder bonus)

**Evidence:** `gameplan_repo.get_scheme_history` already existed (built for
Coach Beat's columnist copy — "has this team run this scheme before, did it
work") but nothing in the actual gameplan-decision path
(`cpu_coach_service._decide_strategy`) ever read it. A CPU team's offensive
and defensive scheme was re-rolled from the same weighted-choice pool every
game with zero memory of what it had actually been running.

**Fix:** `_compute_cpu_gameplan` now fetches `get_scheme_history` once per
game and threads it into `_decide_strategy` via a new optional
`scheme_history` parameter (default `None`, backward compatible with
existing direct callers). A new `_apply_scheme_history_bonus` helper adds a
flat `+2` weight bonus (disclosed placeholder) to a team's historically
most-used offensive/defensive scheme in the weighted-choice options — but
only when that scheme is still on offer this game; `defense_options` is
already pruned by the CA1-era personnel gate (Finding #1, see below) before
the bonus is applied, so a team that lost the personnel to run its old
favorite scheme doesn't get it artificially propped back up.

**Verification:** Standard test suite regression (this is a new signal being
added, not a previously-dead path — not a mandatory smoke test).
`tests/test_cpu_coach_service.py` — 3 pure-function tests for
`_apply_scheme_history_bonus` (bumps a matching option; no-ops when the
historical scheme isn't currently offered; no-ops with no history), 1
pure-function frequency test (`_decide_strategy` picks the historical scheme
measurably more often across 200 seeds), and 1 real-DB integration test that
seeds 6 real simmed games with real `game_cpu_gameplans` rows (written via
the actual `gameplan_repo.record_gameplan` production path), then calls the
real `_compute_cpu_gameplan` against the real DB across 60 trials and
confirms the empirical scheme frequency shifts toward the real, persisted
history compared to a team with zero recorded games.

**Files:** `services/cpu_coach_service.py`.

---

## CA6. Stale comment described the clutch adjustment as a late-game-only mechanic

**Status:** SHIPPED (cosmetic, no behavior change)

**Evidence:** The comment above `_build_box_for_team`'s clutch adjustment
read "in close games, high-clutch players get more late-game usage" — but
`sim_game` has no quarters or clock to target "late" usage against at all
(the same single-shot-engine constraint every other rule in this doc
respects). The mechanic is actually a whole-game scoring-weight bump applied
when the FINAL score margin comes back close, not a live in-game event.

**Fix:** Corrected the comment to describe reality. No behavior change.

**Verification:** Behavior-preserving — full `tests/test_sim_engine.py`
unchanged pass/fail set before and after (19 passed).

**Files:** `services/sim_engine.py`.

---

## CA7. Dead auto-strategy archetype-inference code

**Status:** SHIPPED (cleanup)

**Evidence:** `services/auto_strategy.py`'s `infer_archetype` was only
reachable through `strategy_service.get_sim_modifiers`'s `_is_default and
players` branch. The only real caller that ever passes `players=`
(`sim_orchestrator._build_pre_sim_inputs`) always ALSO passes
`override_strategy`, which short-circuits before that branch could run —
confirmed dead in production, not just untested. Verified via a repo-wide
grep (including tests) for `auto_strategy`/`infer_archetype`/`_archetype_cache`/
`clear_archetype_cache` before deleting anything, per this sweep's own
verification requirement.

**Fix:** Deleted `services/auto_strategy.py` entirely. Removed its import,
the `_archetype_cache` dict, and `clear_archetype_cache()` from
`strategy_service.py`, along with the unreachable inference branch inside
`get_sim_modifiers`. `get_team_archetype_label` (still called live by
`sim_content_pipeline.py`'s Pat Chen enrichment) is kept as an always-`None`
stub since that caller already treats a `None` result as "no archetype label
available" and filters it out — no contract change needed for that one real
caller. Removed the two now-meaningless
`strategy_service.clear_archetype_cache()` calls from `sim_orchestrator.py`.

**Verification:** Standard test suite regression — `tests/test_strategy.py`,
`tests/test_cpu_coach_service.py`, `tests/test_sim_engine.py`,
`tests/test_maybe_post_columnist.py` (covers the Pat Chen `get_team_archetype_label`
call site) — 60 passed.

**Files:** `services/auto_strategy.py` (deleted), `services/strategy_service.py`,
`services/sim_orchestrator.py`, `tests/test_cpu_coach_service.py` (docstring
update only).

---

## CA8. No foul-out mechanic

**Status:** DEFERRED — documented decision, not a silent scope gap

**Reasoning:** Fouls are already tracked per player
(`_assign_team_stats`'s `fouls` field, capped at 6 per line), but nothing
reads that total to affect anything downstream — there is no foul-out, no
forced substitution, no minutes reduction tied to foul trouble. A real fix
needs a two-pass minutes<->fouls resolution loop: fouls are currently
allocated AFTER minutes are finalized (in the same `_build_box_for_team`
pass), so making foul trouble affect minutes would require either
allocating fouls first and feeding them back into the minutes computation,
or an iterative reconciliation between the two — a genuinely new
architectural piece, not a scoped fix that slots into an existing pass the
way CA1-CA7 did. Left as a deliberate, disclosed scope boundary rather than
a rushed one-way door.

**Files:** *(deferred — no files touched)*

---

## CA9. No same-game injury timing

**Status:** DEFERRED — direct byproduct of the single-shot sim architecture

**Reasoning:** `_roll_injuries` rolls an injury outcome for every player who
played more than 10 minutes, AFTER the full box score (including that
player's final stat line) is already computed. A real injury should truncate
the player's remaining minutes/usage for the rest of that same game and
redistribute them to teammates — but `sim_game` has no game clock to
"truncate" against; minutes and stats are single-shot totals, not an
evolving simulation. Fixing this properly would require the same kind of
possession-loop/clock rework the top-level architectural constraint already
rules out of scope for this entire sweep, not a targeted change to
`_roll_injuries` itself.

**Files:** *(deferred — no files touched)*

---

## CA10. Flat defensive matchups (no lineup-slot defensive assignment)

**Status:** DEFERRED — already a self-documented accepted simplification

**Reasoning:** The only defensive-assignment-flavored mechanic that exists is
`_find_star_debuff_targets` (man-to-man vs. an opposing star with OVR >= 88
gets an -8% scoring debuff) — a team-vs-team-star signal, not a genuine
per-matchup (e.g. "this defender is guarding that scorer") assignment. Real
lineup-slot defensive assignment would need the engine to track which
defender is matched against which offensive player and let that matchup
independently affect both players' lines — a new tracking dimension the
engine doesn't have anywhere else (minutes, touch share, and defensive
stats are all computed per-player against team-level pools, never
player-vs-player). `_find_star_debuff_targets`'s own docstring already
frames the -8% debuff as a stand-in for "defensive assignment quality," i.e.
this is an already-accepted simplification in the code, not a newly
discovered gap.

**Files:** *(deferred — no files touched)*

---

## Historical context: the prior, undocumented realism pass ("Finding #1"-"Finding #5")

Before this sweep, this domain had already been through one realism pass —
but it was never given a design doc, unlike every other domain this
four-sweep initiative covered. Its fixes are visible only as inline "Finding
#N" code comments in `services/cpu_coach_service.py`, `services/sim_engine.py`,
`services/strategy_service.py`, `services/sim_persistence.py`, and
`services/role_scoring.py`. Recorded here retroactively so this doc captures
the domain's full history, not just this sweep's additions.

- **Finding #1 — Personnel gating for press/switch_all.** `cpu_coach_service._decide_strategy`
  used to select the `press`/`switch_all` defensive schemes purely off
  opponent OVR/archetype, with zero read of the team's own speed or
  defensive personnel (a slow team could "press" full-court with no
  consequence). Fixed by pruning both options from the weighted-choice pool
  entirely unless the roster clears a personnel bar (`avg_speed >= 74` for
  press; `avg_defense >= 74` and `avg_defensive_effort >= 60` for
  switch_all) — see `_PRESS_SPEED_THRESHOLD`/`_SWITCH_ALL_DEFENSE_THRESHOLD`/
  `_SWITCH_ALL_EFFORT_THRESHOLD` in `cpu_coach_service.py`. `strategy_service.get_sim_modifiers`
  also conditions press's/switch_all's actual in-sim magnitudes on the same
  roster averages (a marginal roster keeps the old flat magnitude as its
  floor; a genuinely elite one gets a much smaller concession).

- **Finding #2 — Skill-conditioned scheme magnitudes.** Scheme-driven
  tendency/attempt-rate bumps (e.g. `three_heavy`'s +12 `tendency_3pt`,
  `three_rate_adj`'s tpa scaling) used to apply flat to every player on the
  floor regardless of whether they could actually convert the extra volume —
  a 30-rated shooting center got pushed to jack up 3s exactly like the
  team's best shooter. Fixed via `_scheme_fit_factor` (`sim_engine.py`), a
  reusable 0..1 linear fit ramp keyed off a player's own relevant skill
  rating, applied everywhere a scheme-level bump touches an individual
  player's tendencies or shot volume.

- **Finding #3a — Scheme-synergy mismatch penalty.** `sim_persistence._stamp_role_data`'s
  `scheme_synergy` bonus used to be a one-way +15% for a role that matched
  the team's active offensive scheme, with no penalty for an active mismatch
  (e.g. a `movement_shooter` role running under an `isolation` scheme).
  Fixed by adding a -8% touch-share penalty for roles with a non-empty
  `scheme_synergy` list that does NOT include the active scheme (roles with
  an empty list — `glue_guy`, `veteran_mentor`, etc. — aren't scheme-committed
  at all, so they get neither bonus nor penalty).

- **Finding #3b — All-big-top-3 role routing.** `role_scoring._derive_tendency_respecter`'s
  primary-scoring-role assignment used to always pull from the guard/wing
  role pool for a team's top-3-OVR players, even when all three were bigs —
  forcing a ball-handling identity (e.g. `primary_initiator`) onto a center
  with no real guard/wing on the roster. Fixed by routing to a
  big-appropriate role pool (`_BIG_PRIMARY_ROLES`: `post_anchor`,
  `pick_and_pop`, `rim_runner`, `screen_roller`) when no guard/wing exists in
  the top-3.

- **Finding #4 — Role-diversity nudge.** Step 4 of role derivation (general
  role assignment for players not already locked into a primary/anchor/depth
  slot) used to have no mechanism discouraging duplicate archetypes — a
  roster could end up with 3 near-identical `spot_up_shooter`s. Fixed with a
  soft per-pass diversity penalty (`_ROLE_DIVERSITY_PENALTY = 6.0`) applied
  to a role each time it's already been claimed once this pass — a nudge,
  not a hard cap, so a genuinely lopsided roster can still stack a role if
  the fit gap is large enough to survive the penalty.

- **Finding #5 — Posture-source unification.** `cpu_coach_service._classify_posture`
  used to compute its own local, record-only heuristic (`win_pct`/`wins`/
  `pct_complete` from `standings_cache`) that never read `franchise_plan.goal`
  — the same defect the trade-posture pipeline (`team_intel.compute_posture`)
  had already fixed. A team with a stated `win_now` plan on a losing skid
  could get classified `"tanking"` here and receive forced-slow-pace/
  bench-conserving gameplans that contradicted the front office's actual
  plan. Fixed by having `_classify_posture` call the same, already-fixed
  `team_intel.compute_posture` directly instead of maintaining a second,
  independently-drifting heuristic — gameplan posture and trade posture can
  no longer diverge.
