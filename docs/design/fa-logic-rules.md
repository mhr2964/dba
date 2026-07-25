# FA Logic Rules — Free Agency Offer & Decision Engine

Durable rule specs for `services/fa_service.py`, `services/player_decision.py`,
`services/fa_needs.py`, and `data/repositories/fa_repo.py`, following
`docs/design/trade-logic-rules.md`'s convention: each finding gets a short
rule-id, a scoped fix, a status line (SHIPPED/DEFERRED), and its own test.
Written 2026-07-24/25 as part of the Domain A (Free Agency) slice of the
second realism sweep (see `Brain/General Session Notes` for the sweep's full
3-domain scope — Draft and Progression/Aging shipped separately in the same
pass, in isolated worktrees, merged sequentially).

**Current implementation status (as of 2026-07-25):** FA1-FA9 shipped. Two
items are explicitly deferred (see bottom of this doc).

---

## FA1. Posture-mode should drive CPU offer terms, not a static team column

**Status:** SHIPPED

**Evidence:** `submit_cpu_offers` read `team["cpu_mode"]`, a column set once at
team creation and never updated afterward — a team's actual season situation
(record, star talent, franchise plan) had zero influence on its free-agent
behavior after the season began. The static 2-way branch was also internally
contradictory in its `"developing"` case: `min_ovr` used the rebuild-level
floor (0) while `preferred_years` used the contender-level ceiling (3) —
signing scrubs to long-term deals.

**Rule:**
- Fetch `team_intel.build_team_intel(pool, league, season, team_ids, include=("posture", "philosophy"))`
  once per `submit_cpu_offers` call for all CPU teams (batched — one call for
  the whole league, not per-team-per-loop), following `team_intel.py`'s own
  documented "≤6 queries" budget convention.
- Map the live 5-way `posture.mode` to offer terms via `_POSTURE_FA_TERMS`:
  - `contending` / `play_in_fringe` → `min_ovr=_CPU_CONTENDER_MIN_OVR (70)`, `preferred_years=3`
  - `developing` → `min_ovr=0`, `preferred_years=2` (deliberately splits the
    difference — this is the fix for the old contradiction)
  - `soft_rebuild` / `rebuilding` → `min_ovr=0`, `preferred_years=1`
- If `build_team_intel` returns no posture data for a team (e.g. no
  `standings_cache` row yet), fall back to the old static `team["cpu_mode"]`
  2-way branch and log a warning — defensive, not a hard failure.

**Test:** `tests/test_fa_service.py::test_submit_cpu_offers_respects_posture_mode`.

---

## FA2. Positional need should weight which free agents a CPU team pursues

**Status:** SHIPPED

**Evidence:** Every CPU team walked the same global `ORDER BY overall DESC`
list from `fa_repo.get_unsigned_players` regardless of its own roster
composition — a team with 4 centers and no point guards chased the exact same
names as a team starving at center.

**Rule:**
- `services/fa_needs.py::_position_need_multiplier(position_counts, position)`
  — a pure function, structural cousin of
  `trade_proposal_scoring.py::_roster_hole_penalty` (same core-position
  vocabulary and hole/surplus floors, but scores toward signing instead of
  penalizing a trade). Returns `1.4` if the position would still have `<2`
  players post-signing, `0.6` if the team already has `>=3` there, else `1.0`.
- `submit_cpu_offers` fetches each CPU team's active-roster position counts
  once via `player_repo.get_active_roster_position_counts` (new helper,
  batched across all CPU team IDs in a single `GROUP BY` query — no per-team
  loop), then scores every eligible free agent as
  `overall * _position_need_multiplier(...) * philosophy_lean` and iterates
  in descending score order instead of the raw OVR-sorted list.

**Test:** `tests/test_fa_service.py::test_submit_cpu_offers_prioritizes_positional_need`.

---

## FA3. GM philosophy should nudge CPU free-agent targeting

**Status:** SHIPPED

**Evidence:** Coach philosophy (`services/philosophies/*`, wired into role
assignment and referenced by the trade audit) had zero influence on free
agency — a `star_maxer` GM and an `egalitarian` GM behaved identically at the
negotiating table.

**Rule:**
- `_PHILOSOPHY_FA_BIAS` (fa_service.py) is a small local dict keyed by the
  same philosophy names `services/philosophies/_registry.py` registers
  (`star_maxer`, `egalitarian`, `defense_first`, `tendency_respecter`,
  `vet_overrater`, `youth_developer`, `chaos`). Only philosophies with an
  actual lean have entries; everything else (including any future philosophy
  added to the registry without an FA entry) falls back to baseline/no-op via
  `.get(philosophy, {})`.
  - `star_maxer`: `min_ovr_bonus=+5`, `years_bonus=+1`, `salary_mult_bonus=+0.05`
    — chases quality harder and is willing to go longer/pay more for it.
  - `youth_developer`: `max_age=27` — hard-skips any free agent older than
    27 outright, plus a `_philosophy_score_bonus` lean toward ≤24yo targets.
  - `vet_overrater`: `salary_mult_bonus=+0.05` plus a `_philosophy_score_bonus`
    lean toward ≥29yo targets ("bias toward older players").
  - `chaos`: `salary_mult_bonus=+0.15` — the widest, least disciplined
    offer-sizing bump of the table.
  - `egalitarian` / `tendency_respecter` / `defense_first`: no bias, baseline
    posture-driven behavior only (explicitly listed in the plan as "no bias").
- This is deliberately a small, FA-specific bias table — not a reuse of the
  role-scoring philosophy function signatures, per the finding's own scoping.

**Test:** covered implicitly by FA1/FA2 tests exercising `philosophy` alongside
`posture`; no standalone FA3 regression test was required by the sweep plan.

---

## FA4. Offer sizing has no star premium, no CPU-vs-CPU bidding awareness, and ignores market_pref

**Status:** SHIPPED (star premium + bidding awareness + market_pref asking-price nudge)

**Evidence:** Every CPU offer used a flat `int(floor * random.uniform(1.0, 1.10))`
regardless of the target's quality — a 92-OVR free agent and a 71-OVR
replacement-level player drew the same relative premium. CPU teams also never
looked at each other's live offers, so two CPU teams could both lowball the
same star on the same day with no escalation. `market_pref`
(`big_market`/`neutral`/`indifferent`, generated per free agent at creation
time — see `draft_class_generator.py`) was read nowhere in the codebase.

**Rule:**
- `_star_premium_range(overall)` replaces the flat multiplier with an
  OVR-tiered range: `>=90 → uniform(1.10, 1.25)`, `>=85 → uniform(1.05, 1.20)`,
  else `uniform(1.0, 1.10)`. Philosophy `salary_mult_bonus` (FA3) shifts both
  ends of the chosen range up further.
- CPU-vs-CPU bidding awareness: `submit_cpu_offers` fetches all of today's
  `fa_offers` for the whole `fa_day` once (not per-candidate) into an
  in-memory `offers_by_player` map, updated as this same call submits new
  offers. Before finalizing an offer, if another team's offer to the same
  free agent already exists today (`status` in `submitted`/`waiting`), the new
  offer is bumped to `1.03-1.08x` the existing best — but only when the
  bumped amount still fits under the team's cap; otherwise the original
  (unbumped) offer is used, subject to the normal cap check.
- `market_pref` now feeds `player_decision._salary_floor` as a small
  asking-price modifier: `indifferent` → `floor * 0.97`, `big_market` →
  `floor * 1.03`, `neutral` → no change. This reflects only the *player's own*
  generated preference — **there is no team market-size attribute in the
  schema**, so this cannot yet model "a big-market team overpays for a
  big-market player specifically." See Deferred items below.

**Test:** exercised indirectly by `test_submit_cpu_offers_prioritizes_positional_need`
and `test_submit_cpu_offers_respects_posture_mode` (both assert on
`fa_offers.salary_per_year` shape); no dedicated FA4-only regression test
was required by the sweep plan since the tiering/bump logic is simple
arithmetic with no branch a unit test would catch a regression in that these
two don't already exercise.

---

## FA5. Counter-salary ceiling ignored the offering team's real cap space

**Status:** SHIPPED

**Evidence:** `player_decision.decide()`'s `COUNTER` branch computed
`counter_sal = min(int(asking * 1.05), int(league.salary_cap * 0.35))` — a
flat 35%-of-cap ceiling with no regard for how much room the *specific*
offering team actually had left. A team with $2M in cap space could still get
counter-demanded into a salary north of $40M.

**Rule:**
- Added `cap_space: int` to `OfferContext` (no default — every construction
  site must supply it explicitly; there was exactly one production call site,
  `fa_service.advance_to_responses`, updated to populate it via the same
  `player_repo.get_team_cap_usage` lookup `submit_offer` already uses).
- `decide()`'s counter bound becomes
  `min(int(asking * 1.05), int(league.salary_cap * 0.35), max(offer.cap_space, _MIN_SALARY))`.

**Test:** `tests/test_player_decision.py::test_counter_bounded_by_offering_teams_cap_space`.

---

## FA6. [Real bug] `respond_to_counter` accepted counters without re-validating cap space

**Status:** SHIPPED

**Evidence:** `submit_offer` always re-checks `cap_used + salary > cap` before
inserting an offer, but `respond_to_counter`'s accept branch skipped this
entirely — a team could accept a counter that pushed it over the salary cap,
something no other signing path in the codebase allows.

**Rule:**
- Added the identical cap check `submit_offer` uses (same query, same
  `DBAError` message shape) to the accept path, now living in the shared
  `_accept_counter` helper (see FA7) so both the human and CPU counter-accept
  paths get it for free.

**Test:** `tests/test_fa_service.py::test_respond_to_counter_rejects_when_over_cap`.

---

## FA7. [Real bug] CPU-team counters were never resolved — they sat 'countered' forever

**Status:** SHIPPED

**Evidence:** `/fa counter-accept` and `/fa counter-decline` are the only way
to call `respond_to_counter`, and CPU teams have no manager to invoke them.
`advance_day` only ever expired offers with `status='waiting'`, never
`'countered'` — so any counter a CPU team's own offer drew (from
`player_decision.decide()`'s COUNTER branch) was permanently stuck pending,
silently removing that team from ever actually landing that player.

**Rule:**
- Factored the shared "cap-check + sign + log" body out of
  `respond_to_counter`'s accept branch into `_accept_counter(pool, league_id,
  counter_row, offer_row, team_id, player)` — a single implementation used by
  both the human path and the new CPU path (no duplicated ~15-line block).
- Added `_resolve_cpu_counters(pool, league_id, season, fa_day)`: finds
  pending `'countered'` rows (via `fa_counters.team_response = 'pending'`)
  whose original offer's team has no manager (`teams.manager_user_id IS
  NULL`), scoped to today's `fa_day`. For each: accepts (via
  `_accept_counter`, so it gets FA6's cap check for free) if
  `counter_salary <= original_offer_salary * 1.15`; declines otherwise, and
  also declines (rather than erroring) if `_accept_counter` raises `DBAError`
  (e.g. the team's cap situation changed since the counter was created).
- Called from `advance_day`, alongside the existing `'waiting'`-offer expiry,
  so CPU counters get resolved before the FA day closes.

**Test:** `tests/test_fa_service.py::test_cpu_counters_get_resolved_by_advance_day`.

---

## FA8. Test coverage

**Status:** SHIPPED

- `tests/test_player_decision.py` (pre-existing pure-unit file; extended
  rather than created fresh — it already covered sign-above-asking,
  wait-in-middle-band, counter-with-competing-offer, and
  last-day-forces-decision from an earlier pass). Added:
  `test_decline_below_threshold` and
  `test_counter_bounded_by_offering_teams_cap_space` (FA5).
- `tests/test_fa_service.py` additions: `test_respond_to_counter_rejects_when_over_cap`
  (FA6), `test_cpu_counters_get_resolved_by_advance_day` (FA7),
  `test_submit_cpu_offers_respects_posture_mode` (FA1),
  `test_submit_cpu_offers_prioritizes_positional_need` (FA2).

---

## FA9. [Real bug, found during verification] `fa_repo.get_cpu_teams` join fan-out inflated cap_used and active_player_count

**Status:** SHIPPED

**Evidence:** Discovered while writing FA2's regression test — a CPU team with
3 active players *and* 3 active contracts (a completely normal roster shape)
came back from `get_cpu_teams` with `active_player_count=9` and
`cap_used=$360,000,000` against a $140M cap, both exactly 3x their real
values. The query LEFT JOINed both `contracts` and `players` in the same
statement, matched only on `team_id`/`league_id` — not on a shared key
between the two joined tables — so N active contracts x M active players
produced N*M rows per team before the `GROUP BY`, inflating both
`COALESCE(SUM(c.salary))` and `COUNT(p.id)` by the *other* table's row count.
Every one of FA1/FA2/FA4's fixes in this pass builds directly on `cap_used`
and `active_player_count` being correct, and in practice this bug meant any
CPU team with more than one active contract and more than one active player
(i.e. essentially every real, non-trivial roster) would see wildly overcounted
values — `active_player_count` alone would usually exceed `_ROSTER_MAX` (15)
well before an actual 15-man roster filled up, silently skipping the team
from free agency entirely. Pre-existing tests never caught it because they
only exercised single-contract or 1:1 player:contract shapes, which don't
trigger the fan-out.

**Rule:**
- Replaced the two `LEFT JOIN` + `GROUP BY` aggregates with two independent
  correlated subqueries (one for `cap_used` over `contracts`, one for
  `active_player_count` over `players`), each scoped to the team on its own —
  no join between the two tables, so no fan-out is possible.

**Test:** exercised directly by `test_submit_cpu_offers_prioritizes_positional_need`
(FA2), which needs a team with 3 active players + 3 active contracts and
would fail on the old inflated cap math; also covered by the pre-existing
`test_cpu_fa.py::test_cpu_skips_full_roster` (15 players/contracts 1:1 — the
old fan-out gave a *coincidentally* correct skip there since 225 also exceeds
15, but the new query is now precise: `active_player_count=15`, not 225).

---

## Deferred (explicitly not built this pass)

**Archetype/role-fit checks on free agents.** No role is assigned to a player
before signing (roles are assigned post-signing by the strategy engine), so
there's nothing to score against — same reasoning the original trade audit
used to scope B6 out of anything pre-acquisition. Would need role data to
exist ahead of a signing decision, which is a larger design change than this
pass's scope.

**Full market-size-based `market_pref` modeling.** FA4 makes `market_pref`
move the *player's* asking price a small amount, but there is no
market-size/prestige attribute on the `teams` table — so a `big_market`
player currently can't specifically demand more from a big-market team vs. a
small-market one; the nudge is flat regardless of which team is offering.
Building this properly needs a new team schema attribute (e.g.
`teams.market_size`), which is out of scope for this pass per the sweep plan.
