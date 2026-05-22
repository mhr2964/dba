"""
Unit tests for pick_proposal_modes — one test per decision-table row.

Pure Python, no DB, no async.  Each test constructs minimal fake inputs
and asserts the returned mode list.
"""
from __future__ import annotations

import pytest

from services.cpu_trade_proposals import pick_proposal_modes


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
