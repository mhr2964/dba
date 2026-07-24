"""
Unit tests for pick_proposal_modes — one test per decision-table row.

Pure Python, no DB, no async.  Each test constructs minimal fake inputs
and asserts the returned mode list.
"""
from __future__ import annotations

import pytest

from services.trade_proposal_scoring import pick_proposal_modes


# ---------------------------------------------------------------------------
# Minimal fake team object
# ---------------------------------------------------------------------------

class _FakeTeam:
    def __init__(self, team_id: int = 1):
        self.id = team_id
        self.cpu_mode = "developing"


def _plan(
    goal: str = "transition",
    surplus: list | None = None,
    asset_targets: list | None = None,
) -> dict:
    return {
        "goal": goal,
        "surplus_player_ids": surplus or [],
        "asset_targets": asset_targets or [],
    }


# ---------------------------------------------------------------------------
# Rule 1: over_luxury + win_now → outgoing_first then incoming_first
# ---------------------------------------------------------------------------

def test_cap_strapped_contender_outgoing_first_then_incoming():
    """Rule 1: over-luxury + win_now plan → ['outgoing_first', 'incoming_first'].

    User decision: real GMs know their budget before shopping.
    """
    result = pick_proposal_modes(
        team=_FakeTeam(),
        posture="contending",
        plan=_plan(goal="win_now", surplus=[1, 2]),
        cap_state="over_luxury",
        roster_size=13,
    )
    assert result == ["outgoing_first", "incoming_first"], (
        f"Cap-strapped contender should shop surplus first: {result}"
    )


# ---------------------------------------------------------------------------
# Rule 2: over_luxury + non-win_now → outgoing only
# ---------------------------------------------------------------------------

def test_over_luxury_non_win_now_outgoing_only():
    """Rule 2: over-luxury + any non-win_now goal → ['outgoing_first']."""
    result = pick_proposal_modes(
        team=_FakeTeam(),
        posture="soft_rebuild",
        plan=_plan(goal="rebuild", surplus=[1]),
        cap_state="over_luxury",
        roster_size=14,
    )
    assert result == ["outgoing_first"], (
        f"Over-luxury non-win_now should dump salary only: {result}"
    )


# ---------------------------------------------------------------------------
# Rule 3: tank posture + non-empty surplus → outgoing only
# ---------------------------------------------------------------------------

def test_tanking_with_surplus_outgoing_only():
    """Rule 3: tank posture + surplus → ['outgoing_first']."""
    result = pick_proposal_modes(
        team=_FakeTeam(),
        posture="tanking",
        plan=_plan(goal="tank", surplus=[10, 11]),
        cap_state="under",
        roster_size=12,
    )
    assert result == ["outgoing_first"], (
        f"Tanking team with surplus should dump assets: {result}"
    )


def test_tanking_without_surplus_falls_through():
    """Rule 3 guard: tank posture but NO surplus — falls through to default."""
    result = pick_proposal_modes(
        team=_FakeTeam(),
        posture="tanking",
        plan=_plan(goal="tank", surplus=[]),
        cap_state="under",
        roster_size=12,
    )
    # No surplus → rule 3 doesn't apply; default = incoming_first.
    assert result == ["incoming_first"], (
        f"Tank without surplus should fall to incoming_first: {result}"
    )


# ---------------------------------------------------------------------------
# Rule 4: rebuild goal + surplus + asset_targets → both modes
# ---------------------------------------------------------------------------

def test_rebuild_goal_with_surplus_and_targets_both_modes():
    """Rule 4: rebuild goal + surplus + asset_targets → ['outgoing_first', 'incoming_first']."""
    result = pick_proposal_modes(
        team=_FakeTeam(),
        posture="rebuilding",
        plan=_plan(goal="rebuild", surplus=[5], asset_targets=["picks_r1", "young_u23"]),
        cap_state="under",
        roster_size=13,
    )
    assert result == ["outgoing_first", "incoming_first"], (
        f"Rebuild with surplus + targets should do both: {result}"
    )


def test_rebuild_goal_with_surplus_but_no_targets_falls_through():
    """Rule 4 guard: rebuild goal + surplus but NO asset targets → falls through to default."""
    result = pick_proposal_modes(
        team=_FakeTeam(),
        posture="rebuilding",
        plan=_plan(goal="rebuild", surplus=[5], asset_targets=[]),
        cap_state="under",
        roster_size=13,
    )
    # No asset_targets → rule 4 guard fails; default = incoming_first.
    assert result == ["incoming_first"], (
        f"Rebuild without asset_targets should fall to incoming_first: {result}"
    )


# ---------------------------------------------------------------------------
# Rule 5: win_now + surplus → incoming_first then outgoing_first (consolidation)
# ---------------------------------------------------------------------------

def test_win_now_with_surplus_consolidation_buyer():
    """Rule 5: win_now + surplus → ['incoming_first', 'outgoing_first'].

    Contender identifies upgrade target first, then finds assets to move.
    """
    result = pick_proposal_modes(
        team=_FakeTeam(),
        posture="contending",
        plan=_plan(goal="win_now", surplus=[20]),
        cap_state="under",  # not over luxury — so rules 1/2 don't fire
        roster_size=15,
    )
    assert result == ["incoming_first", "outgoing_first"], (
        f"Win-now with surplus should be consolidation buyer: {result}"
    )


# ---------------------------------------------------------------------------
# Rule 6: soft_rebuild + surplus → outgoing only
# ---------------------------------------------------------------------------

def test_soft_rebuild_with_surplus_outgoing_only():
    """Rule 6: soft_rebuild posture + surplus → ['outgoing_first']."""
    result = pick_proposal_modes(
        team=_FakeTeam(),
        posture="soft_rebuild",
        plan=_plan(goal="transition", surplus=[7, 8]),
        cap_state="near_cap",
        roster_size=13,
    )
    assert result == ["outgoing_first"], (
        f"Soft-rebuild with surplus should shop veterans: {result}"
    )


# ---------------------------------------------------------------------------
# Rule 7: play_in_fringe → incoming_first
# ---------------------------------------------------------------------------

def test_play_in_fringe_incoming_first():
    """Rule 7: play_in_fringe posture → ['incoming_first']."""
    result = pick_proposal_modes(
        team=_FakeTeam(),
        posture="play_in_fringe",
        plan=_plan(goal="transition"),
        cap_state="under",
        roster_size=13,
    )
    assert result == ["incoming_first"], (
        f"Play-in-fringe should use incoming_first: {result}"
    )


def test_developing_incoming_first():
    """Rule 7: developing posture → ['incoming_first']."""
    result = pick_proposal_modes(
        team=_FakeTeam(),
        posture="developing",
        plan=_plan(goal="transition"),
        cap_state="under",
        roster_size=14,
    )
    assert result == ["incoming_first"], (
        f"Developing team should use incoming_first: {result}"
    )


# ---------------------------------------------------------------------------
# Rule 8: default fallback
# ---------------------------------------------------------------------------

def test_default_fallback_incoming_first():
    """Rule 8 (default): no matching rule → ['incoming_first']."""
    result = pick_proposal_modes(
        team=_FakeTeam(),
        posture="contending",     # win_now but no surplus — falls through rules 1-7
        plan=_plan(goal="win_now", surplus=[]),
        cap_state="under",
        roster_size=15,
    )
    # win_now + no surplus → rule 5 guard fails → default
    assert result == ["incoming_first"], (
        f"Default fallback should be incoming_first: {result}"
    )


# ---------------------------------------------------------------------------
# shop_intent priority rules (PR 3)
# ---------------------------------------------------------------------------

def _plan_with_intent(
    goal: str = "transition",
    surplus: list | None = None,
    asset_targets: list | None = None,
    shop_intent: dict | None = None,
) -> dict:
    """Build a plan dict that includes shop_intent in derived_from_record."""
    return {
        "goal": goal,
        "surplus_player_ids": surplus or [],
        "asset_targets": asset_targets or [],
        "derived_from_record": {
            "shop_intent": {str(pid): intent for pid, intent in (shop_intent or {}).items()},
        },
    }


def test_cap_dump_intent_outgoing_first_regardless_of_posture():
    """shop_intent cap_dump → ['outgoing_first'] regardless of posture/goal."""
    result = pick_proposal_modes(
        team=_FakeTeam(),
        posture="developing",
        plan=_plan_with_intent(
            goal="transition",
            surplus=[5],
            shop_intent={5: "cap_dump"},
        ),
        cap_state="under",
        roster_size=13,
    )
    assert result == ["outgoing_first"], (
        f"cap_dump surplus should force outgoing_first: {result}"
    )


def test_flip_asset_intent_outgoing_first():
    """shop_intent flip_asset → ['outgoing_first'] (team acquired to re-sell)."""
    result = pick_proposal_modes(
        team=_FakeTeam(),
        posture="developing",
        plan=_plan_with_intent(
            goal="transition",
            surplus=[7],
            shop_intent={7: "flip_asset"},
        ),
        cap_state="under",
        roster_size=13,
    )
    assert result == ["outgoing_first"], (
        f"flip_asset surplus should force outgoing_first: {result}"
    )


def test_no_priority_intent_falls_through_to_normal_rules():
    """shop_intent age_misfit or other → no priority override; normal rules apply."""
    result = pick_proposal_modes(
        team=_FakeTeam(),
        posture="developing",
        plan=_plan_with_intent(
            goal="transition",
            surplus=[],
            shop_intent={},  # no intent entries
        ),
        cap_state="under",
        roster_size=13,
    )
    # No surplus + no priority intent → default incoming_first.
    assert result == ["incoming_first"], (
        f"No priority intent should fall through to normal rules: {result}"
    )


# ---------------------------------------------------------------------------
# #7: round_seed variety injection (opt-in — omitted round_seed keeps every
# test above byte-for-byte identical to pre-#7 deterministic behavior).
# ---------------------------------------------------------------------------


def test_round_seed_omitted_stays_fully_deterministic():
    """Without round_seed, rules 3/6/7 always return their primary option —
    the exact pre-#7 behavior every test above already exercises."""
    tank_result = pick_proposal_modes(
        team=_FakeTeam(1), posture="tanking",
        plan=_plan(goal="tank", surplus=[1]), cap_state="under", roster_size=12,
    )
    assert tank_result == ["outgoing_first"]

    soft_rebuild_result = pick_proposal_modes(
        team=_FakeTeam(2), posture="soft_rebuild",
        plan=_plan(goal="transition", surplus=[1]), cap_state="under", roster_size=12,
    )
    assert soft_rebuild_result == ["outgoing_first"]

    fringe_result = pick_proposal_modes(
        team=_FakeTeam(3), posture="play_in_fringe",
        plan=_plan(goal="transition"), cap_state="under", roster_size=12,
    )
    assert fringe_result == ["incoming_first"]


def test_round_seed_produces_variety_across_seeds():
    """With round_seed supplied, rule 6 (soft_rebuild + surplus) occasionally
    picks the alternative two-way mode list instead of always outgoing_first —
    sweeping many seeds must surface both options at least once."""
    seen: set[tuple[str, ...]] = set()
    for seed in range(200):
        result = pick_proposal_modes(
            team=_FakeTeam(7), posture="soft_rebuild",
            plan=_plan(goal="transition", surplus=[1]), cap_state="under",
            roster_size=12, round_seed=seed,
        )
        seen.add(tuple(result))

    assert ("outgoing_first",) in seen, f"Primary option never appeared across 200 seeds: {seen}"
    assert ("outgoing_first", "incoming_first") in seen, (
        f"Alternative option never appeared across 200 seeds — no variety injected: {seen}"
    )


def test_round_seed_is_reproducible_for_the_same_seed_and_team():
    """Same (team_id, round_seed) pair must always produce the same result —
    the variety is seeded/reproducible, not genuinely random per call."""
    results = [
        pick_proposal_modes(
            team=_FakeTeam(9), posture="tanking",
            plan=_plan(goal="tank", surplus=[1]), cap_state="under",
            roster_size=12, round_seed=42,
        )
        for _ in range(10)
    ]
    assert all(r == results[0] for r in results), (
        f"Same seed/team should always reproduce the same mode choice; got {results}"
    )


def test_round_seed_hard_requirement_rules_are_never_varied():
    """Rules with a hard requirement (here: over_luxury + win_now) must ignore
    round_seed entirely — #7 only varies the coaching-philosophy judgment calls."""
    for seed in range(50):
        result = pick_proposal_modes(
            team=_FakeTeam(3), posture="contending",
            plan=_plan(goal="win_now", surplus=[1, 2]), cap_state="over_luxury",
            roster_size=13, round_seed=seed,
        )
        assert result == ["outgoing_first", "incoming_first"], (
            f"Hard-requirement rule must not vary with round_seed (seed={seed}): {result}"
        )
