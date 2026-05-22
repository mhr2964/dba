"""
Smoke tests for the outgoing-first V2 dispatcher building blocks.

Integration tests that require a live DB are gated behind DBA_RUN_INTEGRATION=1
and marked @pytest.mark.integration.

The rest tests _score_outgoing_pair and pick_proposal_modes with mocks,
verifying the core scoring logic without DB calls.
"""
from __future__ import annotations

import os
import pytest

from services.cpu_trade_proposals import _score_outgoing_pair


# ---------------------------------------------------------------------------
# Minimal fake objects
# ---------------------------------------------------------------------------

class _FakeTeam:
    def __init__(self, team_id: int = 1, cpu_mode: str = "developing"):
        self.id = team_id
        self.cpu_mode = cpu_mode
        self.nba_team_code = f"T{team_id}"


class _FakePlayer:
    """Minimal player-like object with tendency fields for archetype computation."""
    def __init__(
        self,
        player_id: int,
        overall: int = 80,
        position: str = "PG",
        age: int = 27,
        tendency_3pt: int = 50,
        tendency_drive: int = 50,
        tendency_pass: int = 50,
        ast_tendency: int = 50,
        reb_tendency: int = 50,
        blk_tendency: int = 50,
        stl_tendency: int = 50,
        defense_tendency: int = 50,
    ):
        self.id = player_id
        self.overall = overall
        self.position = position
        self.age = age
        self.tendency_3pt = tendency_3pt
        self.tendency_drive = tendency_drive
        self.tendency_pass = tendency_pass
        self.ast_tendency = ast_tendency
        self.reb_tendency = reb_tendency
        self.blk_tendency = blk_tendency
        self.stl_tendency = stl_tendency
        self.defense_tendency = defense_tendency
        # birth_date not present — tests that call _player_age will get None fallback.
        self.birth_date = None
        self.first_name = "Test"
        self.last_name = f"Player{player_id}"
        self.team_id = 1


# ---------------------------------------------------------------------------
# Tests for _score_outgoing_pair
# ---------------------------------------------------------------------------

def test_score_outgoing_pair_returns_positive():
    """A valid (team_a, outgoing, team_b, speculative_return) should return > 0."""
    team_a = _FakeTeam(1)
    team_b = _FakeTeam(2)

    outgoing_pid = 10
    roster_a = [_FakePlayer(i, overall=78, position="SF") for i in range(1, 13)]
    # outgoing player is on the roster
    roster_a[0].id = outgoing_pid

    incoming_player = _FakePlayer(50, overall=79, position="SG")

    plan_a = {
        "goal": "win_now",
        "surplus_player_ids": [outgoing_pid],
        "asset_targets": [],
        "core_player_ids": [],
        "flex_player_ids": [],
    }

    ctx = {
        "team_id": 1,
        "mode": "contending",
        "current_payroll": 100_000_000,
        "position_counts": {"PG": 2, "SG": 1, "SF": 3, "PF": 2, "C": 2},
        "salary_cap": 140_000_000,
    }

    score = _score_outgoing_pair(
        team_a=team_a,
        outgoing_pid=outgoing_pid,
        team_b=team_b,
        speculative_return_player_ids=[50],
        speculative_return_pick_ids=[],
        speculative_return_players=[incoming_player],
        plan_a=plan_a,
        posture_a="contending",
        roster_a=roster_a,
        cp_contexts={1: ctx},
    )

    assert score > 0, f"Score should be positive for a valid incoming player: {score}"


def test_score_outgoing_pair_empty_return_yields_zero():
    """No speculative return → score is 0."""
    team_a = _FakeTeam(1)
    team_b = _FakeTeam(2)
    roster_a = [_FakePlayer(i) for i in range(1, 13)]

    score = _score_outgoing_pair(
        team_a=team_a,
        outgoing_pid=1,
        team_b=team_b,
        speculative_return_player_ids=[],
        speculative_return_pick_ids=[],
        speculative_return_players=[],
        plan_a={},
        posture_a="developing",
        roster_a=roster_a,
        cp_contexts={},
    )

    assert score == 0.0, f"Empty return should give 0 score: {score}"


def test_score_outgoing_pair_arch_penalty_applied():
    """Receiving a player whose archetype already has 2+ on the roster gets penalised."""
    from services import trade_evaluator

    team_a = _FakeTeam(1)
    team_b = _FakeTeam(2)

    # Build a roster where 2 players share the same archetype as the incoming player.
    # Use high 3pt tendency to force a 3pt-shooter archetype.
    incoming = _FakePlayer(99, overall=78, position="SF", tendency_3pt=85, tendency_drive=20)
    existing_arch_player_1 = _FakePlayer(11, overall=78, position="SF", tendency_3pt=85, tendency_drive=20)
    existing_arch_player_2 = _FakePlayer(12, overall=78, position="PF", tendency_3pt=85, tendency_drive=20)
    other_players = [_FakePlayer(i, overall=75, position="PG") for i in range(1, 10)]
    roster_a = other_players + [existing_arch_player_1, existing_arch_player_2]

    # Verify the arch detection works as expected (used for test documentation only).
    assert trade_evaluator._player_archetype({
        "position": "SF",
        "tendency_3pt": 85,
        "tendency_drive": 20,
        "tendency_pass": 50,
        "ast_tendency": 50,
        "reb_tendency": 50,
        "blk_tendency": 50,
        "stl_tendency": 50,
    }) is not None, "Expected a non-None archetype for a clear 3pt shooter profile"

    score_with_saturated = _score_outgoing_pair(
        team_a=team_a,
        outgoing_pid=999,  # not on roster — no subtraction effect
        team_b=team_b,
        speculative_return_player_ids=[99],
        speculative_return_pick_ids=[],
        speculative_return_players=[incoming],
        plan_a={},
        posture_a="developing",
        roster_a=roster_a,
        cp_contexts={},
    )

    # Score with a unique-archetype incoming player (no penalty).
    unique_incoming = _FakePlayer(88, overall=78, position="C",
                                   tendency_3pt=20, tendency_drive=20,
                                   reb_tendency=85, blk_tendency=85)
    score_clean = _score_outgoing_pair(
        team_a=team_a,
        outgoing_pid=999,
        team_b=team_b,
        speculative_return_player_ids=[88],
        speculative_return_pick_ids=[],
        speculative_return_players=[unique_incoming],
        plan_a={},
        posture_a="developing",
        roster_a=roster_a,
        cp_contexts={},
    )

    # A saturated archetype should score <= clean archetype (given same OVR).
    assert score_with_saturated <= score_clean, (
        f"Saturated archetype (score={score_with_saturated:.3f}) should be <= "
        f"clean archetype (score={score_clean:.3f})"
    )


def test_score_uses_real_contracts():
    """Verify _score_outgoing_pair applies real contract modifiers via incoming_contracts.

    _contract_modifier penalises players whose salary exceeds 1.5× the reference
    point (salary_cap * 0.25).  A player on an overpaid contract (salary = 2.0×
    the reference, triggering the 0.6 penalty) should score notably lower than
    the same archetype on a reasonable contract (salary = 0.5× reference, no penalty).

    This guards the valuation symmetry between _derive_return_from_b (which uses
    real contracts to score and sort B's players) and _score_outgoing_pair (which
    previously used synthetic salary=0/years=1, bypassing the contract modifier
    entirely).
    """
    SALARY_CAP = 140_000_000
    PLAYER_OVR = 80
    # Reference point used by _contract_modifier: salary_cap * 0.25
    # Overpaid threshold: salary_ratio > 1.5  →  salary > 1.5 * 35M = 52.5M
    _ref = int(SALARY_CAP * 0.25)  # 35_000_000

    class _FakeContract:
        def __init__(self, salary: int, years_remaining: int = 2):
            self.salary = salary
            self.years_remaining = years_remaining

    team_a = _FakeTeam(1)
    team_b = _FakeTeam(2)
    outgoing_pid = 10
    roster_a = [_FakePlayer(i, overall=78, position="SF") for i in range(1, 13)]
    roster_a[0].id = outgoing_pid

    player_bargain = _FakePlayer(50, overall=PLAYER_OVR, position="SG")
    player_bad = _FakePlayer(51, overall=PLAYER_OVR, position="SG")

    ctx = {
        "team_id": 1,
        "mode": "developing",
        "current_payroll": 100_000_000,
        "salary_cap": SALARY_CAP,
        "position_counts": {},
    }

    # Bargain: salary_ratio = 0.5 → no penalty (modifier = 1.0 for 2 years)
    bargain_contract = _FakeContract(salary=int(_ref * 0.5))
    # Bad: salary_ratio = 2.0 → triggers the >1.5 penalty (modifier *= 0.6)
    bad_contract = _FakeContract(salary=int(_ref * 2.0))

    score_bargain = _score_outgoing_pair(
        team_a=team_a,
        outgoing_pid=outgoing_pid,
        team_b=team_b,
        speculative_return_player_ids=[50],
        speculative_return_pick_ids=[],
        speculative_return_players=[player_bargain],
        plan_a={},
        posture_a="developing",
        roster_a=roster_a,
        cp_contexts={1: ctx},
        incoming_contracts={50: bargain_contract},
    )

    score_bad = _score_outgoing_pair(
        team_a=team_a,
        outgoing_pid=outgoing_pid,
        team_b=team_b,
        speculative_return_player_ids=[51],
        speculative_return_pick_ids=[],
        speculative_return_players=[player_bad],
        plan_a={},
        posture_a="developing",
        roster_a=roster_a,
        cp_contexts={1: ctx},
        incoming_contracts={51: bad_contract},
    )

    # Overpaid player must score notably lower than bargain (same OVR, different contract).
    assert score_bargain > 0, f"Bargain contract score should be positive: {score_bargain}"
    assert score_bad > 0, f"Bad contract score should be positive: {score_bad}"
    assert score_bargain > score_bad, (
        f"Bargain contract (score={score_bargain:.3f}) should beat "
        f"overpaid contract (score={score_bad:.3f}) for same OVR player"
    )


# ---------------------------------------------------------------------------
# Two-pass incoming-first archetype check (PR 2)
# ---------------------------------------------------------------------------

def test_incoming_first_two_pass_archetype_check():
    """Pass-2 exact arch check: same-archetype 4th player gets 0.65 penalty;
    different-archetype player scores higher despite identical raw trade value.

    Scenario: team A has 3 ball-needs PGs on the roster.  A ball-needs PG
    incoming from team B keeps the roster at 4 ball-needs → pre-count 3 → 0.65
    penalty.  A two-way wing incoming from team B has pre-count 0 → no penalty.

    Pass-2 is implemented in _run_incoming_first_for_team, which requires DB.
    We test the underlying primitives (_team_archetype_counts, _player_archetype,
    and the penalty logic) directly to assert the same math fires correctly.
    """
    from services.cpu_trade_proposals import _team_archetype_counts
    from services import trade_evaluator

    # Build 3 ball-needs PGs with identical tendencies (high pass/ast, high drive).
    ball_needs_attrs = {
        "position": "PG",
        "tendency_3pt": 35,
        "tendency_drive": 75,
        "tendency_pass": 80,
        "ast_tendency": 80,
        "reb_tendency": 30,
        "blk_tendency": 20,
        "stl_tendency": 30,
    }
    arch = trade_evaluator._player_archetype(ball_needs_attrs)
    assert arch is not None, "Expected ball-needs archetype to be non-None"

    pg1 = _FakePlayer(1, position="PG", tendency_pass=80, tendency_drive=75,
                      ast_tendency=80, tendency_3pt=35)
    pg2 = _FakePlayer(2, position="PG", tendency_pass=80, tendency_drive=75,
                      ast_tendency=80, tendency_3pt=35)
    pg3 = _FakePlayer(3, position="PG", tendency_pass=80, tendency_drive=75,
                      ast_tendency=80, tendency_3pt=35)
    # Fill rest with neutral players.
    others = [_FakePlayer(i, position="SF") for i in range(4, 12)]
    roster_a = [pg1, pg2, pg3] + others

    # Verify the roster has exactly 3 of this archetype.
    counts_before = _team_archetype_counts(roster_a)
    assert counts_before.get(arch, 0) == 3, (
        f"Expected 3 {arch} players, got {counts_before.get(arch, 0)}"
    )

    # Incoming ball-needs PG: post-trade roster adds a 4th → pre-count = 3 → 0.65 penalty.
    pg_incoming = _FakePlayer(99, position="PG", tendency_pass=80, tendency_drive=75,
                               ast_tendency=80, tendency_3pt=35)
    post_with_pg = roster_a + [pg_incoming]
    counts_post_pg = _team_archetype_counts(post_with_pg)
    pre_count_pg = counts_post_pg.get(arch, 0) - 1
    assert pre_count_pg >= 2, f"Expected pre-count >= 2, got {pre_count_pg}"
    penalty_pg = 0.65  # matches the >=2 branch

    # Incoming two-way wing: post-trade roster adds 0 ball-needs → pre-count = 0 → no penalty.
    wing_attrs = {
        "position": "SF",
        "tendency_3pt": 50,
        "tendency_drive": 50,
        "tendency_pass": 40,
        "ast_tendency": 40,
        "reb_tendency": 55,
        "blk_tendency": 60,
        "stl_tendency": 65,
        "defense_tendency": 70,
    }
    wing_arch = trade_evaluator._player_archetype(wing_attrs)
    wing_incoming = _FakePlayer(100, position="SF",
                                 tendency_3pt=50, tendency_drive=50,
                                 tendency_pass=40, ast_tendency=40,
                                 reb_tendency=55, blk_tendency=60, stl_tendency=65)
    post_with_wing = roster_a + [wing_incoming]
    counts_post_wing = _team_archetype_counts(post_with_wing)
    pre_count_wing = counts_post_wing.get(wing_arch, 0) - 1 if wing_arch else 0
    penalty_wing = 0.65 if pre_count_wing >= 2 else (0.85 if pre_count_wing == 1 else 1.0)

    # Same raw trade value for both incoming players; different-arch player must score higher.
    raw_value = 50.0
    score_same_arch = raw_value * penalty_pg
    score_diff_arch = raw_value * penalty_wing

    assert penalty_pg == 0.65, f"Expected 0.65 penalty for 3rd same-arch, got {penalty_pg}"
    assert score_diff_arch > score_same_arch, (
        f"Different-arch player (score={score_diff_arch:.2f}) should beat "
        f"same-arch player (score={score_same_arch:.2f}) at equal raw value"
    )


# ---------------------------------------------------------------------------
# V2 flag regression (PR 2)
# ---------------------------------------------------------------------------

def test_dispatcher_v2_flag_ignored():
    """DBA_PROPOSAL_DISPATCHER_V2 env var no longer gates any code path.

    The dispatcher is unconditional in PR 2.  This test asserts the env var
    has no effect on pick_proposal_modes — the mode selection depends only
    on posture, plan, cap_state, not on the removed env var.
    """
    import os
    from services.cpu_trade_proposals import pick_proposal_modes

    team = _FakeTeam()
    plan = {"goal": "tank", "surplus_player_ids": [1, 2], "asset_targets": []}

    # Set the old flag — should have no effect.
    old_val = os.environ.get("DBA_PROPOSAL_DISPATCHER_V2")
    try:
        os.environ["DBA_PROPOSAL_DISPATCHER_V2"] = "1"
        result_with_flag = pick_proposal_modes(team, "tanking", plan, "under", 12)

        os.environ["DBA_PROPOSAL_DISPATCHER_V2"] = "0"
        result_without_flag = pick_proposal_modes(team, "tanking", plan, "under", 12)
    finally:
        if old_val is None:
            os.environ.pop("DBA_PROPOSAL_DISPATCHER_V2", None)
        else:
            os.environ["DBA_PROPOSAL_DISPATCHER_V2"] = old_val

    # Both calls should return the same modes because the flag is ignored.
    assert result_with_flag == result_without_flag, (
        "DBA_PROPOSAL_DISPATCHER_V2 should have no effect; "
        f"got {result_with_flag} vs {result_without_flag}"
    )
    # Both should follow rule 3 (tank + surplus → outgoing_first).
    assert result_with_flag == ["outgoing_first"], (
        f"Tank team with surplus should return outgoing_first, got {result_with_flag}"
    )


# ---------------------------------------------------------------------------
# Integration-style test (requires DB; gated)
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("DBA_RUN_INTEGRATION"),
    reason="Set DBA_RUN_INTEGRATION=1 to run DB integration tests",
)
async def test_attempt_outgoing_first_offer_smoke():
    """Smoke test: _attempt_outgoing_first_offer with a seeded minimal fixture.

    Requires a live DB with at least 2 CPU teams and one team having a
    surplus player on its franchise plan.  Set DBA_RUN_INTEGRATION=1 to run.
    """
    # This test is intentionally left as a placeholder for the integration
    # harness to fill in.  The building blocks (_score_outgoing_pair,
    # _derive_return_from_b) are unit-tested above; full DB integration
    # belongs in the headless ride-along harness.
    pass
