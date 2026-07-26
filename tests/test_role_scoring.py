"""
Characterization tests for the pure role-scoring/derivation functions now
in role_scoring.py (originally written against role_service.py before the
Phase 3 split; re-run unchanged afterward -- see HANDOFF.md).

Pure functions -- no DB, no async -- so no fakes/mocks needed. Zero
coverage existed for _score_role_fit or _derive_tendency_respecter before
this file, despite them being the core of CPU role assignment.
"""
from __future__ import annotations

import datetime

from services import role_scoring as rs


# ---------------------------------------------------------------------------
# _age_from_birth
# ---------------------------------------------------------------------------

def test_age_from_birth_none_returns_none():
    assert rs._age_from_birth(None, 2025) is None


def test_age_from_birth_computes_relative_to_season_start():
    birth_date = datetime.date(2000, 10, 1)
    age = rs._age_from_birth(birth_date, 2025)
    assert abs(age - 25.0) < 0.01


# ---------------------------------------------------------------------------
# _score_role_fit
# ---------------------------------------------------------------------------

def _player(**overrides):
    base = {
        "player_id": 1, "overall": 80, "position": "PG",
        "tendency_3pt": 50, "tendency_drive": 50, "tendency_pass": 50,
        "ast_tendency": 50, "reb_tendency": 50, "blk_tendency": 50,
        "stl_tendency": 50, "defense_tendency": 50, "usage_weight": 50,
        "defensive_archetype": None,
    }
    base.update(overrides)
    return base


def test_score_role_fit_iso_scorer_favors_guards_and_top_ovr():
    guard = _player(position="PG", overall=90, tendency_drive=80)
    center = _player(position="C", overall=90, tendency_drive=80)
    guard_score = rs._score_role_fit(guard, "iso_scorer", ovr_rank=0, n_players=12)
    center_score = rs._score_role_fit(center, "iso_scorer", ovr_rank=0, n_players=12)
    assert guard_score > center_score


def test_score_role_fit_post_anchor_penalizes_3pt_tendency():
    low_3pt_center = _player(position="C", overall=85, tendency_3pt=10, reb_tendency=70)
    high_3pt_center = _player(position="C", overall=85, tendency_3pt=90, reb_tendency=70)
    low_score = rs._score_role_fit(low_3pt_center, "post_anchor", ovr_rank=0, n_players=12)
    high_score = rs._score_role_fit(high_3pt_center, "post_anchor", ovr_rank=0, n_players=12)
    assert low_score > high_score


def test_score_role_fit_rim_protector_archetype_bonus():
    with_arch = _player(position="C", overall=80, blk_tendency=60, defensive_archetype="rim_protector")
    without_arch = _player(position="C", overall=80, blk_tendency=60, defensive_archetype=None)
    with_score = rs._score_role_fit(with_arch, "rim_protector", ovr_rank=3, n_players=12)
    without_score = rs._score_role_fit(without_arch, "rim_protector", ovr_rank=3, n_players=12)
    assert with_score > without_score


def test_score_role_fit_rim_protector_null_archetype_center_still_scores_well():
    # A center with strong block tendency and low 3pt gets a bonus even with
    # no defensive_archetype backfilled (Gobert-style NULL-archetype case).
    gobert_like = _player(position="C", overall=82, blk_tendency=50, tendency_3pt=0, defensive_archetype=None)
    score = rs._score_role_fit(gobert_like, "rim_protector", ovr_rank=1, n_players=12)
    assert score > 40  # base tendency contribution + the +30 NULL-archetype bonus


def test_score_role_fit_veteran_mentor_favors_old_low_ovr_players():
    old_player = _player(overall=65)
    young_player = _player(overall=65)
    old_score = rs._score_role_fit(
        {**old_player, "_age": 35}, "veteran_mentor", ovr_rank=13, n_players=15,
    )
    young_score = rs._score_role_fit(
        {**young_player, "_age": 22}, "veteran_mentor", ovr_rank=13, n_players=15,
    )
    assert old_score > young_score


def test_score_role_fit_developmental_favors_young_low_ovr_players():
    young_player = _player(overall=65)
    old_player = _player(overall=65)
    young_score = rs._score_role_fit(
        {**young_player, "_age": 21}, "developmental", ovr_rank=13, n_players=15,
    )
    old_score = rs._score_role_fit(
        {**old_player, "_age": 35}, "developmental", ovr_rank=13, n_players=15,
    )
    assert young_score > old_score


def test_score_role_fit_end_of_bench_favors_lowest_ovr():
    low_ovr = _player(overall=60)
    high_ovr = _player(overall=78)
    low_score = rs._score_role_fit(low_ovr, "end_of_bench", ovr_rank=13, n_players=15)
    high_score = rs._score_role_fit(high_ovr, "end_of_bench", ovr_rank=13, n_players=15)
    assert low_score > high_score


def test_score_role_fit_applies_philosophy_bias_when_team_context_given():
    player = _player(overall=80, tendency_drive=80)
    team_context = {
        "league_id": 1, "team_id": 1, "season": 2025,
        "philosophy": "tendency_respecter",
    }
    with_ctx = rs._score_role_fit(player, "iso_scorer", ovr_rank=0, n_players=12, team_context=team_context)
    without_ctx = rs._score_role_fit(player, "iso_scorer", ovr_rank=0, n_players=12)
    # tendency_respecter is the identity bias (base philosophy) -- scores should match.
    assert abs(with_ctx - without_ctx) < 0.01


# ---------------------------------------------------------------------------
# _derive_tendency_respecter
# ---------------------------------------------------------------------------

def _roster_player(pid, overall, position="SF", age=27, **overrides):
    p = {
        "player_id": pid, "overall": overall, "position": position, "_age": age,
        "tendency_3pt": 50, "tendency_drive": 50, "tendency_pass": 50,
        "ast_tendency": 50, "reb_tendency": 50, "blk_tendency": 50,
        "stl_tendency": 50, "defense_tendency": 50, "usage_weight": 50,
        "defensive_archetype": None, "slot": pid,
    }
    p.update(overrides)
    return p


def test_derive_tendency_respecter_empty_roster_returns_empty():
    assert rs._derive_tendency_respecter([]) == []


def test_derive_tendency_respecter_exactly_one_guard_wing_primary_scorer():
    # post_anchor is excluded from the guard/wing primary pool by design (it's
    # filled via the separate defensive-anchor step instead, even though it's
    # also nominally a "primary scoring" role) -- so a team can independently
    # get a post_anchor via the anchor path AND a guard/wing primary scorer.
    # The real invariant is: exactly one of the 4 guard/wing primary roles fires.
    roster = [
        _roster_player(1, 92, position="PG", tendency_drive=80),
        _roster_player(2, 88, position="SG"),
        _roster_player(3, 85, position="C", reb_tendency=70),
        *[_roster_player(i, 75, position="SF") for i in range(4, 13)],
    ]
    assignments = rs._derive_tendency_respecter(roster)
    guard_wing_primary_roles = {"iso_scorer", "primary_initiator", "movement_shooter", "slashing_lead"}
    primary_count = sum(1 for a in assignments if a["role"] in guard_wing_primary_roles)
    assert primary_count == 1


def test_derive_tendency_respecter_exactly_one_defensive_anchor():
    roster = [
        _roster_player(1, 92, position="PG"),
        _roster_player(2, 85, position="C", blk_tendency=70, reb_tendency=70),
        _roster_player(3, 83, position="C", blk_tendency=65, reb_tendency=65),
        *[_roster_player(i, 75, position="SF") for i in range(4, 13)],
    ]
    assignments = rs._derive_tendency_respecter(roster)
    anchor_roles = {"rim_protector", "two_way_big", "switching_big", "post_anchor"}
    anchor_count = sum(1 for a in assignments if a["role"] in anchor_roles)
    assert anchor_count == 1


def test_derive_tendency_respecter_bottom_3_get_depth_roles():
    roster = [_roster_player(i, 90 - i, position="SF", age=27) for i in range(12)]
    assignments = rs._derive_tendency_respecter(roster)
    depth_roles = {"end_of_bench", "developmental", "veteran_mentor"}
    bottom_3_ids = {a["player_id"] for a in sorted(roster, key=lambda p: p["overall"])[:3]}
    for a in assignments:
        if a["player_id"] in bottom_3_ids:
            assert a["role"] in depth_roles


def test_derive_tendency_respecter_all_big_top3_does_not_force_guard_wing_role():
    """Finding #3b fix: when the top-3 OVR players are ALL bigs (no real
    guard/wing present), Step 2 must NOT fall back to the guard/wing pool
    (`primary_pool_ids = top_3_guard_wing_ids or top_3_ids`) -- that used to
    force a ball-handling primary role (e.g. primary_initiator) onto a
    center. It now routes to a big-appropriate pool instead
    (`_BIG_PRIMARY_ROLES`: post_anchor / pick_and_pop / rim_runner /
    screen_roller).

    Pre-fix, this exact roster produced `top_player_role in
    guard_wing_primary_roles` (confirmed manually before the fix landed).
    """
    roster = [
        _roster_player(1, 95, position="C", reb_tendency=70, blk_tendency=60),
        _roster_player(2, 90, position="PF", reb_tendency=65),
        _roster_player(3, 88, position="C", reb_tendency=60),
        *[_roster_player(i, 75, position="SF") for i in range(4, 13)],
    ]
    assignments = rs._derive_tendency_respecter(roster)
    guard_wing_primary_roles = {"iso_scorer", "primary_initiator", "movement_shooter", "slashing_lead"}
    big_primary_roles = {"post_anchor", "pick_and_pop", "rim_runner", "screen_roller"}
    top_player_role = next(a["role"] for a in assignments if a["player_id"] == 1)
    assert top_player_role not in guard_wing_primary_roles
    assert top_player_role in big_primary_roles


def test_derive_tendency_respecter_step4_diversifies_identical_players_post_fix():
    """Finding #4 fix: Step 4 now applies a mild per-pass diversity nudge
    (role_usage_counts * _ROLE_DIVERSITY_PENALTY) so identical bench players
    no longer all independently converge on the exact same best-fitting role.

    Pre-fix, this exact roster produced len(set(bench_roles)) == 1 (confirmed
    manually before the nudge landed -- Step 4 had zero uniqueness constraint
    beyond the primary-scorer/anchor exclusions from Steps 2-3). The nudge is
    soft by design (not a hard cap), so this only asserts SOME diversification
    appears, not full 1-per-role uniqueness.
    """
    roster = [
        _roster_player(1, 95, position="PG", tendency_drive=80),
        _roster_player(2, 90, position="C", reb_tendency=70, blk_tendency=60),
        *[_roster_player(i, 70, position="SF") for i in range(3, 13)],
    ]
    assignments = rs._derive_tendency_respecter(roster)
    # Bottom-3 OVR (ids 10, 11, 12) get depth roles -- the remaining identical
    # bench players (ids 3-9) go through Step 4's argmax.
    bench_roles = [a["role"] for a in assignments if a["player_id"] in range(3, 10)]
    assert len(set(bench_roles)) > 1, (
        f"Diversity nudge should spread identical bench players across >1 role, got {set(bench_roles)}"
    )


def test_derive_tendency_respecter_returns_touch_share_and_rationale():
    roster = [_roster_player(i, 80 - i) for i in range(5)]
    assignments = rs._derive_tendency_respecter(roster)
    assert len(assignments) == 5
    for a in assignments:
        assert "player_id" in a
        assert "role" in a
        assert "touch_share" in a
        assert "rationale" in a


# ---------------------------------------------------------------------------
# _rookie_pedigree_bonus / rookie-pedigree bias in _score_role_fit — D3
# Option B (docs/design/draft-logic-rules.md).
#
# Per the plan's explicit instruction, these assert the DIRECTION of the
# effect ("a top-5 rookie's rotation-role score is measurably higher than an
# equivalent non-rookie's"), not a specific tuned number — the exact
# magnitude/taper is a placeholder pending real evidence (see rule doc).
# ---------------------------------------------------------------------------


def test_rookie_pedigree_bonus_zero_for_non_pedigreed_player():
    # No pick_number/years_pro at all — undrafted/legacy data, must no-op.
    player = _player()
    assert rs._rookie_pedigree_bonus(player, "iso_scorer") == 0.0


def test_rookie_pedigree_bonus_zero_for_low_pick():
    # A late-second-round pick shouldn't get the bonus even in year 0.
    player = _player(pick_number=45, years_pro=0)
    assert rs._rookie_pedigree_bonus(player, "iso_scorer") == 0.0


def test_rookie_pedigree_bonus_zero_after_year_1():
    # A top-5 pick with 2+ years of pro experience is no longer "recent."
    player = _player(pick_number=3, years_pro=2)
    assert rs._rookie_pedigree_bonus(player, "iso_scorer") == 0.0


def test_rookie_pedigree_bonus_zero_for_bench_depth_roles():
    # The bonus only nudges toward starter/rotation tiers, not bench/depth --
    # a bonus toward "sit correctly on the bench" would contradict the rule's
    # own intent.
    player = _player(pick_number=1, years_pro=0)
    assert rs._rookie_pedigree_bonus(player, "end_of_bench") == 0.0
    assert rs._rookie_pedigree_bonus(player, "veteran_mentor") == 0.0


def test_rookie_pedigree_bonus_positive_for_top_pick_rookie_season_rotation_role():
    player = _player(pick_number=3, years_pro=0)
    assert rs._rookie_pedigree_bonus(player, "catch_and_shoot") > 0.0


def test_rookie_pedigree_bonus_tapers_between_year_0_and_year_1():
    rookie_season = _player(pick_number=3, years_pro=0)
    year_1 = _player(pick_number=3, years_pro=1)
    bonus_year_0 = rs._rookie_pedigree_bonus(rookie_season, "catch_and_shoot")
    bonus_year_1 = rs._rookie_pedigree_bonus(year_1, "catch_and_shoot")
    assert bonus_year_0 > bonus_year_1 > 0.0


def test_score_role_fit_top5_rookie_scores_higher_than_equivalent_non_rookie():
    """Direction-only assertion (per plan): an otherwise-identical top-5
    rookie's rotation-role fit score must be measurably higher than a
    non-pedigreed player's, all else equal."""
    rookie = _player(overall=72, position="SG", pick_number=4, years_pro=0)
    non_rookie = _player(overall=72, position="SG")
    rookie_score = rs._score_role_fit(rookie, "catch_and_shoot", ovr_rank=8, n_players=14)
    non_rookie_score = rs._score_role_fit(non_rookie, "catch_and_shoot", ovr_rank=8, n_players=14)
    assert rookie_score > non_rookie_score


# ---------------------------------------------------------------------------
# RA2 -- minimum-roster guard (realism sweep Plan D). Pre-fix, Step 1's
# unconditional bottom-3-OVR depth slice could consume the ENTIRE roster (n<=3)
# or leave nothing for the anchor step (n==4), leaving the team with ZERO
# primary scorer and ZERO defensive anchor -- a much stronger violation than
# the "exactly one" duplicate-role edge case those roles' overlap (post_anchor
# is reachable from both the Step 2 big-primary pool and the Step 3 anchor
# pool -- see test_derive_tendency_respecter_exactly_one_guard_wing_primary_scorer
# above) can produce. These assertions are deliberately >= 1 (occurrence), not
# == 1 (exclusivity) -- RA2 fixes the "assigned at all" guarantee; the
# separate "exactly one" exclusivity invariant is covered elsewhere and isn't
# what regressed here. These sizes (1-6) were completely unexercised before --
# the smallest existing fixture above is 12 players.
# ---------------------------------------------------------------------------

_GUARD_WING_PRIMARY_ROLES = {"iso_scorer", "primary_initiator", "movement_shooter", "slashing_lead"}
_BIG_PRIMARY_ROLES = {"post_anchor", "pick_and_pop", "rim_runner", "screen_roller"}
_ANCHOR_ROLES = {"rim_protector", "two_way_big", "switching_big", "post_anchor"}
_ALL_PRIMARY_ROLES = _GUARD_WING_PRIMARY_ROLES | _BIG_PRIMARY_ROLES


def test_derive_tendency_respecter_one_player_roster_still_gets_primary_scorer():
    # n=1: pre-fix, Step 1's bottom_3 slice consumed the single player into a
    # depth role, leaving Step 2's primary-scorer pool empty AND the Step 2
    # fallback loop (which only checks unassigned players) with nothing left.
    roster = [_roster_player(1, 80, position="PG")]
    assignments = rs._derive_tendency_respecter(roster)
    assert len(assignments) == 1
    assert assignments[0]["role"] in _ALL_PRIMARY_ROLES


def test_derive_tendency_respecter_two_player_roster_gets_primary_and_anchor():
    roster = [
        _roster_player(1, 85, position="PG"),
        _roster_player(2, 80, position="C", reb_tendency=70, blk_tendency=60),
    ]
    assignments = rs._derive_tendency_respecter(roster)
    assert len(assignments) == 2
    roles = {a["player_id"]: a["role"] for a in assignments}
    primary_count = sum(1 for r in roles.values() if r in _ALL_PRIMARY_ROLES)
    anchor_count = sum(1 for r in roles.values() if r in _ANCHOR_ROLES)
    assert primary_count >= 1
    assert anchor_count >= 1


def test_derive_tendency_respecter_three_player_roster_gets_primary_and_anchor():
    roster = [
        _roster_player(1, 88, position="PG"),
        _roster_player(2, 82, position="C", reb_tendency=70, blk_tendency=60),
        _roster_player(3, 70, position="SF", age=34),
    ]
    assignments = rs._derive_tendency_respecter(roster)
    assert len(assignments) == 3
    roles = [a["role"] for a in assignments]
    primary_count = sum(1 for r in roles if r in _ALL_PRIMARY_ROLES)
    anchor_count = sum(1 for r in roles if r in _ANCHOR_ROLES)
    assert primary_count >= 1
    assert anchor_count >= 1


def test_derive_tendency_respecter_four_player_roster_gets_primary_and_anchor():
    # n=4 is the case the plan called out specifically: pre-fix, Step 1 took
    # 3 for depth roles and Step 2 took the last remaining player for primary,
    # leaving literally zero candidates for Step 3's anchor pool.
    roster = [
        _roster_player(1, 90, position="PG"),
        _roster_player(2, 85, position="C", reb_tendency=70, blk_tendency=60),
        _roster_player(3, 75, position="SF"),
        _roster_player(4, 65, position="SF", age=34),
    ]
    assignments = rs._derive_tendency_respecter(roster)
    assert len(assignments) == 4
    roles = [a["role"] for a in assignments]
    primary_count = sum(1 for r in roles if r in _ALL_PRIMARY_ROLES)
    anchor_count = sum(1 for r in roles if r in _ANCHOR_ROLES)
    assert primary_count >= 1
    assert anchor_count >= 1


def test_derive_tendency_respecter_five_player_roster_gets_primary_and_anchor():
    roster = [
        _roster_player(1, 90, position="PG"),
        _roster_player(2, 85, position="C", reb_tendency=70, blk_tendency=60),
        _roster_player(3, 80, position="SF"),
        _roster_player(4, 72, position="SG"),
        _roster_player(5, 65, position="SF", age=34),
    ]
    assignments = rs._derive_tendency_respecter(roster)
    assert len(assignments) == 5
    roles = [a["role"] for a in assignments]
    primary_count = sum(1 for r in roles if r in _ALL_PRIMARY_ROLES)
    anchor_count = sum(1 for r in roles if r in _ANCHOR_ROLES)
    assert primary_count >= 1
    assert anchor_count >= 1


def test_derive_tendency_respecter_n_ge_5_depth_slice_unchanged_by_guard():
    # RA2's guard must reproduce the ORIGINAL "always take bottom 3" behaviour
    # once n >= 5 (reserve=2 leaves n-2 >= 3, so depth_count is still capped at
    # 3) -- this asserts the existing 12+-player characterization in
    # test_derive_tendency_respecter_bottom_3_get_depth_roles still holds at
    # the n=5 boundary specifically.
    roster = [_roster_player(i, 90 - i, position="SF", age=27) for i in range(5)]
    assignments = rs._derive_tendency_respecter(roster)
    depth_roles = {"end_of_bench", "developmental", "veteran_mentor"}
    bottom_3_ids = {a["player_id"] for a in sorted(roster, key=lambda p: p["overall"])[:3]}
    depth_assigned = {a["player_id"] for a in assignments if a["role"] in depth_roles}
    assert depth_assigned == bottom_3_ids


def test_score_role_fit_rookie_bonus_applies_alongside_philosophy_bias():
    """The rookie bonus is additive on top of whatever the philosophy bias
    already contributed -- both signals should stack, not override."""
    rookie = _player(overall=72, position="SG", pick_number=4, years_pro=0)
    team_context = {
        "league_id": 1, "team_id": 1, "season": 2025,
        "philosophy": "tendency_respecter",
    }
    with_ctx = rs._score_role_fit(
        rookie, "catch_and_shoot", ovr_rank=8, n_players=14, team_context=team_context,
    )
    without_ctx = rs._score_role_fit(rookie, "catch_and_shoot", ovr_rank=8, n_players=14)
    # tendency_respecter is the identity bias, so both should include the
    # same rookie bonus and land on (approximately) the same score.
    assert abs(with_ctx - without_ctx) < 0.01
