# Draft Logic Rules — CPU Draft Selection & Class Generation

Durable rule specs for the CPU draft pick logic (`services/draft_service.py`) and the synthetic draft class generator (`services/draft_class_generator.py`), following the same rule-id + status-line convention as `docs/design/trade-logic-rules.md`. This is part of the 2026-07 realism sweep's Domain B (Draft) — see the sibling `docs/design/trade-logic-rules.md` (trades/CPU scheme) and the Free Agency/Progression domains done in the same pass.

**Current implementation status (as of 2026-07-25):** D1 and D2 shipped. D3 (rookie-to-roster minutes integration) is explicitly deferred. D4 (test suite) shipped alongside D1/D2 — see `tests/test_draft_service.py`, `tests/test_draft_class_generator.py`, `tests/test_draft_repo.py`.

---

## D1. CPU draft selection should weight positional need and GM philosophy, not just best-player-available

**Status:** SHIPPED (2026-07-25)

**Evidence:** `_cpu_select`'s own docstring admitted the gap directly: "Roster-need weighting is not implemented here; add if team roster data is available at call site." The function was pure best-OVR + `±2` noise — a CPU team already 4-deep at center would draft another center over a thin position of real need, and every CPU GM philosophy (`star_maxer`, `chaos`, `defense_first`, etc. — see `services/team_intel.py::PHILOSOPHY_DESCRIPTIONS`) picked identically at the draft table despite having very different offer/role behavior everywhere else in the sim.

**Rule:**
- `_cpu_select` scores each prospect as `overall * position_need_multiplier * philosophy_bias_multiplier + noise`.
- Positional need (`_position_need_multiplier`): mirrors the shape of `trade_proposal_scoring.py::_roster_hole_penalty`, but as a straight scoring multiplier and with a wider surplus floor (draft rosters carry more bench depth than a trade-active lineup):
  - `< 2` active players at the prospect's position: `1.25x` (need).
  - `>= 4` active players at the prospect's position: `0.85x` (surplus).
  - Otherwise: `1.0x`.
- GM philosophy bias (`_PHILOSOPHY_DRAFT_BIAS`, module-level in `draft_service.py`):
  - `star_maxer`: dampens the noise band from the default `(-2.0, 2.0)` to `(-0.5, 0.5)` — closer to pure best-player-available, matching its real-roster behavior of always chasing top-2 OVR regardless of fit.
  - `chaos`: widens the noise band to `(-6.0, 6.0)` — matches its "wildly varied, reproducible but illogical" role-assignment behavior elsewhere.
  - `vet_overrater` / `defense_first`: apply a scoring bonus (`1.10x` / `1.15x`) to prospects whose `defense` attribute clears a floor (`60` / `65`) — both philosophies lean toward "prove it on D first" in their existing role-scoring behavior.
  - `youth_developer`, `tendency_respecter`, `egalitarian`: **intentionally no entry** — rookies are already young, so `youth_developer`'s real-roster bias (push young players to feature roles) doesn't need a draft-specific rule; `tendency_respecter`/`egalitarian` have no tendency/archetype signal to read off a not-yet-rostered prospect.
- `_cpu_select` stays a **pure function** — `position_counts: dict[str, int] | None` and `philosophy: str | None` are optional params the caller (`advance_pick`) fetches and passes in as plain data; `_cpu_select` never touches the DB. Both default to `None`, so the signature change is additive and any caller that doesn't pass them gets the old best-player-available behavior back (modulo the default `±2` noise, unchanged).
- `advance_pick` fetches the on-the-clock team's active roster position counts (`SELECT position, COUNT(*) ... WHERE team_id = $1 AND roster_status = 'active' GROUP BY position`) and philosophy (`team_intel.get_team_philosophy`) once per CPU pick, before calling `_cpu_select`.

**Tests:** `tests/test_draft_service.py` — `test_cpu_select_prefers_position_of_need`, `test_cpu_select_downweights_surplus_position`, `test_cpu_select_with_no_position_counts_is_pure_bpa`, `test_cpu_select_philosophy_bias_defense_first_favors_defense_floor`, `test_cpu_select_philosophy_bias_vet_overrater_favors_defense_floor`, `test_cpu_select_star_maxer_dampens_noise_toward_pure_bpa`, `test_cpu_select_chaos_widens_noise_enough_to_flip_outcomes`, `test_cpu_select_unknown_philosophy_falls_back_to_default`.

## D2. Draft prospect potential must be able to fall below overall (true busts and independent booms)

**Status:** SHIPPED (2026-07-25)

**Evidence:** `generate_draft_class` computed `potential = _clamp(overall + random.randint(0, 15), 50, 99)` — the delta was strictly non-negative, so **every** generated prospect's potential was guaranteed `>= overall`. No prospect could ever be a true bust (a player whose realized ceiling ends up lower than their draft-day overall suggested), which is a real and common outcome in actual scouting.

**Rule:**
- `potential_delta = round(random.gauss(mu=_slot_tier_bust_boom_mu(slot), sigma=10))`, then `potential = _clamp(overall + potential_delta, 50, 99)`.
- `_slot_tier_bust_boom_mu(slot)` keeps the *average* early pick trending toward a higher ceiling (scouting isn't pure noise — a real signal exists), while allowing genuine busts/booms at any slot via `sigma=10`:
  - picks 1-5: `mu = +6`
  - picks 6-14: `mu = +3`
  - picks 15-30: `mu = 0`
  - picks 31-60 (second round): `mu = -2`
- `_TIER_RANGES` (the OVR band per draft slot tier) widened by ~3-4 points on each end (e.g. picks 1-5 was `(72, 82)`, now `(68, 86)`) — a tuning change so draft slot doesn't so tightly predetermine realized `overall`, not a rearchitecture. Band ordering (elite > lottery > first-round-2nd-half > second-round) is unchanged.

**Tests:** `tests/test_draft_class_generator.py` — `test_potential_can_be_below_overall`, `test_potential_variance_by_slot` (seeded — see D4 note below), `test_slot_tier_bust_boom_mu_shape`, `test_position_distribution_weights`, `test_all_prospects_have_valid_attribute_ranges`.

## D3. Rookie-to-roster minutes integration

**Status:** DEFERRED — no files touched.

**Evidence:** Lottery picks (especially top-5) often don't get rotation minutes matching their draft grade once they land on a roster — the minutes/rotation assignment in `strategy_service.py` has no rookie-specific signal at all (a top-3 pick can sit behind a replacement-level vet indefinitely with no mechanism nudging playing time toward the investment).

**Rule (not implemented):** would need `strategy_service.py`'s rotation/minutes logic to read draft pedigree (pick number, draft class year) and apply a floor or bias toward rotation minutes for high picks in their first 1-2 seasons — likely tapering as veteran competition proves itself. **Deliberately out of scope for this pass**: `strategy_service.py` (and its sibling `role_service.py`) is a large, separately-tuned system; touching it needs its own scoping/exploration pass rather than folding it into a Draft-domain sweep. Left untouched per explicit instruction.

## D4. Draft test suite (built from scratch)

**Status:** SHIPPED (2026-07-25)

**Evidence:** Zero test files existed for draft anywhere in `tests/` before this pass (confirmed via `ls tests/ | grep -i draft` returning nothing) — the single biggest gap in this domain, given draft touches contracts, roster assignment, and CPU decision logic all at once.

**Rule / what shipped:**
- `tests/test_draft_class_generator.py` — pure, no DB. D2 regressions plus position-distribution and attribute-range sanity checks.
- `tests/test_draft_repo.py` — CRUD/query coverage for every public function in `data/repositories/draft_repo.py`: draft create/get/status update, available-prospects filtering (excludes already-drafted players via the `draft_selections` anti-join), pick recording/retrieval, on-the-clock team resolution, and class seeding.
- `tests/test_draft_service.py` — `_cpu_select` D1 regressions (pure), seeded statistical lottery tests (`test_run_lottery_weighted_odds`, `test_run_lottery_playoff_teams_get_picks_after_lottery_group`), and the pick/contract flow (`advance_pick` resolving CPU picks + assigning rookie-scale contracts, human-on-clock pausing for manual pick, `make_pick`'s wrong-team/already-drafted rejections).
- Statistical tests (`test_run_lottery_weighted_odds`, D2's `test_potential_variance_by_slot`) seed the RNG explicitly (`random.seed(...)`) from the start rather than relying on iteration count alone to survive sampling noise — applying the lesson `tests/test_progression.py`'s `test_high_potential_grows_more` learned the hard way (see that test's own docstring) from the very first draft test written, instead of hitting the same flake and fixing it later.
- `tests/conftest.py`'s `patch_get_pool` fixture was missing `services.draft_service.get_pool` entirely (every other DB-backed service was already listed) — added so `db_pool`-based draft tests can actually route through the test pool instead of erroring on a real connection attempt.
