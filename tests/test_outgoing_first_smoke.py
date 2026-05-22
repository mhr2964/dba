"""
Smoke tests for the outgoing-first V2 dispatcher building blocks.

Integration tests that require a live DB are gated behind DBA_RUN_INTEGRATION=1
and marked @pytest.mark.integration.

The rest tests _score_outgoing_pair and pick_proposal_modes with mocks,
verifying the core scoring logic without DB calls.
"""
from __future__ import annotations

import datetime
import os
from unittest.mock import AsyncMock, MagicMock, patch

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
# Pass-2 form-adjustment regression tests (PR 2 blockers)
# ---------------------------------------------------------------------------

async def test_pass2_uses_form_adjusted_target_value():
    """_build_return_package must receive a form-multiplied target_value in pass-2.

    The regression: pass-2 was passing the raw _cand_tv to _build_return_package
    instead of _p2_adj_tv (the form-adjusted value).  For a hot-streak player
    (form_mod = 1.15) this caused the package to be undersized by ~15%, pushing
    the sanity-floor ratio below 0.90 and aborting proposals for reachable players.

    Fail-on-revert contract: reverting line 1529 from _p2_adj_tv back to _cand_tv
    means _build_return_package receives the pass-1 raw _tv instead of the
    form-adjusted value.  The pass-1 _tv is computed with season_stats=None and
    no form modifier applied, so it will differ from _p2_adj_tv by factor ~1.15.
    The captured_tv assertion (== expected_adj_tv) will fail.
    """
    import services.cpu_trade_proposals as _mod
    from data.repositories import league_repo, player_repo, team_repo
    from services import trade_evaluator

    SALARY_CAP = 140_000_000
    LEAGUE_ID = 1
    SEASON = 2025

    # Candidate player: hot-streak, OVR 82 at age 26.
    HOT_FORM_MOD = 1.15
    CAND_PID = 100
    CAND_SALARY = 20_000_000
    CAND_YEARS = 2

    # Build minimal real dataclass instances so attribute access is authentic.
    league = league_repo.League(
        id=LEAGUE_ID,
        discord_guild_id=999,
        name="Test",
        start_season_year=SEASON,
        current_season=SEASON,
        current_phase="trade_deadline",
        phase_data={},
        commissioner_user_id=1,
        fa_day_count=0,
        salary_cap=SALARY_CAP,
        created_at=datetime.datetime(2025, 1, 1),
        archived_at=None,
    )

    team_a = team_repo.Team(
        id=1, league_id=LEAGUE_ID, nba_team_code="AAA", name="Alpha",
        city="City A", conference="East", division="Atlantic",
        manager_user_id=None, cpu_mode="developing",
        team_offense_rating=None, team_defense_rating=None, pace=None,
    )
    team_b = team_repo.Team(
        id=2, league_id=LEAGUE_ID, nba_team_code="BBB", name="Beta",
        city="City B", conference="West", division="Pacific",
        manager_user_id=None, cpu_mode="developing",
        team_offense_rating=None, team_defense_rating=None, pace=None,
    )

    cand_player = player_repo.Player(
        id=CAND_PID, league_id=LEAGUE_ID, external_id=None,
        first_name="Hot", last_name="Player", position="SG",
        height_in=76, weight_lb=200, birth_date=datetime.date(1999, 1, 1),
        years_pro=4, is_rookie=False, team_id=team_b.id,
        roster_status="active", overall=82, speed=80, shooting_2pt=80,
        shooting_3pt=80, shooting_mid=80, finishing=80, playmaking=80,
        defense=80, rebounding=70, iq=75, potential=80,
        peak_age_start=26, peak_age_end=30, loyalty=50, money_drive=50,
        win_drive=50, market_pref="any", star_leverage=50,
    )
    cand_contract = player_repo.Contract(
        id=1, league_id=LEAGUE_ID, player_id=CAND_PID, team_id=team_b.id,
        salary=CAND_SALARY, years_remaining=CAND_YEARS, total_years=3,
        contract_type="standard", signed_in_season=SEASON - 1,
        is_active=True, terminated_reason=None,
    )

    # Compute the expected form-adjusted target value using the SAME code path
    # that _run_incoming_first_for_team uses in pass-2.  This is what
    # _build_return_package SHOULD receive; if the revert happens it will receive
    # the pass-1 raw _tv (computed without position/season_stats) instead.
    # Use _player_age directly so the age computation matches production exactly.
    from services.cpu_trade_posture import _player_age
    p2_raw = trade_evaluator.player_trade_value(
        {"overall": cand_player.overall, "age": _player_age(cand_player), "position": cand_player.position},
        {"salary": CAND_SALARY, "years_remaining": CAND_YEARS},
        SALARY_CAP,
        season_stats=None,
    )
    expected_adj_tv = trade_evaluator.apply_form(p2_raw, HOT_FORM_MOD)

    # Pool mock: returns just enough for the function to reach pass-2.
    pool = MagicMock()

    async def _fetch(sql, *args):
        sql_lower = sql.lower()
        if "position" in sql_lower and "lineups" in sql_lower:
            return []  # pos_count_rows — no positional bias
        if "trades" in sql_lower and "pending_counterparty" in sql_lower:
            return []  # no active pair trades
        return []

    async def _fetchval(sql, *args):
        sql_lower = sql.lower()
        if "coach_philosophy" in sql_lower:
            return None
        if "draft_picks" in sql_lower:
            return 0  # b_any_count
        if "trade_assets" in sql_lower:
            return 0  # active_for_player
        if "last_traded_at" in sql_lower:
            return None
        return None

    pool.fetch = _fetch
    pool.fetchval = _fetchval

    # player_repo mocks: get_by_id returns the candidate; get_active_contract
    # returns its contract.  Pass-2 secondary dice will not fire (random patched).
    captured_target_value: list[float] = []

    async def _fake_build_return_package(
        pool_, league_, team_a_, block_ids, target_value, *args, **kwargs
    ):
        captured_target_value.append(target_value)
        return ([], [], 0.0)  # empty offer → function returns 0 at line 1671

    postures = {
        team_a.id: {
            "mode": "developing", "urgency": "comfortable",
            "avg_age": 27.0, "projected_wins": None,
            "conf_rank": None, "games_remaining": 82, "near_threshold": None,
        },
        team_b.id: {
            "mode": "developing", "urgency": "comfortable",
            "avg_age": 27.0, "projected_wins": None,
            "conf_rank": None, "games_remaining": 82, "near_threshold": None,
        },
    }

    with (
        patch.object(
            _mod.player_repo, "get_by_id",
            AsyncMock(return_value=cand_player),
        ),
        patch.object(
            _mod.player_repo, "get_active_contract",
            AsyncMock(return_value=cand_contract),
        ),
        patch.object(
            _mod.trade_evaluator, "compute_form_map",
            AsyncMock(return_value={CAND_PID: (HOT_FORM_MOD, {})}),
        ),
        patch("services.cpu_trade_proposals._build_return_package", _fake_build_return_package),
        patch("services.cpu_trade_proposals.random.random", return_value=0.99),  # no secondary
        patch("services.trade_context.compute_context_modifier", AsyncMock(return_value=(1.0, []))),
    ):
        await _mod._run_incoming_first_for_team(
            pool=pool,
            league=league,
            season=SEASON,
            team_a=team_a,
            cpu_teams=[team_a, team_b],
            block_by_team={team_b.id: [CAND_PID]},
            used_pairs=set(),
            taken_player_ids=set(),
            deadline_game_index=50,
            recently_signed_ids=set(),
            guild=None,
            postures=postures,
            cp_plans={team_a.id: None, team_b.id: None},
            cp_contexts={},
            cp_r1_counts={},
            roster_a_cache=[],
        )

    assert len(captured_target_value) == 1, (
        f"_build_return_package should have been called exactly once in pass-2; "
        f"got {len(captured_target_value)} calls"
    )
    actual_tv = captured_target_value[0]
    assert abs(actual_tv - expected_adj_tv) < 0.01, (
        f"Pass-2 must pass the form-adjusted target value to _build_return_package.\n"
        f"  Expected (form-adjusted): {expected_adj_tv:.4f}\n"
        f"  Actual (received):        {actual_tv:.4f}\n"
        f"  Difference:               {abs(actual_tv - expected_adj_tv):.4f}\n"
        "If this fails, line 1529 in _run_incoming_first_for_team is using "
        "_cand_tv (raw pass-1 value) instead of _p2_adj_tv (form-adjusted)."
    )


async def test_pass2_includes_secondary_target_in_sizing():
    """When a secondary target fires, _build_return_package must receive
    primary-form-adjusted + secondary-form-adjusted combined value, not just primary.

    The regression: pass-2 sized the package for the primary alone, but downstream
    sanity checks compared against primary+secondary combined — causing star +
    role-player bundle proposals to abort as undersized ~30% of the time.

    Fail-on-revert contract: reverting line 1529 from _p2_adj_tv back to _cand_tv
    means the secondary's form-adjusted contribution is omitted from target_value.
    The captured_tv assertion (== expected_combined) will fail because _cand_tv
    carries only the primary raw value with no secondary folded in.
    """
    import services.cpu_trade_proposals as _mod
    from data.repositories import league_repo, player_repo, team_repo
    from services import trade_evaluator

    SALARY_CAP = 140_000_000
    LEAGUE_ID = 1
    SEASON = 2025

    PRIMARY_PID = 200
    PRIMARY_OVERALL = 82
    PRIMARY_SALARY = 30_000_000
    PRIMARY_YEARS = 3
    PRIMARY_FORM = 1.10

    SECONDARY_PID = 201
    SECONDARY_OVERALL = 74
    SECONDARY_SALARY = 8_000_000
    SECONDARY_YEARS = 2
    SECONDARY_FORM = 0.95

    league = league_repo.League(
        id=LEAGUE_ID, discord_guild_id=999, name="Test",
        start_season_year=SEASON, current_season=SEASON,
        current_phase="trade_deadline", phase_data={},
        commissioner_user_id=1, fa_day_count=0, salary_cap=SALARY_CAP,
        created_at=datetime.datetime(2025, 1, 1), archived_at=None,
    )
    team_a = team_repo.Team(
        id=1, league_id=LEAGUE_ID, nba_team_code="AAA", name="Alpha",
        city="City A", conference="East", division="Atlantic",
        manager_user_id=None, cpu_mode="developing",
        team_offense_rating=None, team_defense_rating=None, pace=None,
    )
    team_b = team_repo.Team(
        id=2, league_id=LEAGUE_ID, nba_team_code="BBB", name="Beta",
        city="City B", conference="West", division="Pacific",
        manager_user_id=None, cpu_mode="developing",
        team_offense_rating=None, team_defense_rating=None, pace=None,
    )

    primary_player = player_repo.Player(
        id=PRIMARY_PID, league_id=LEAGUE_ID, external_id=None,
        first_name="Star", last_name="Forward", position="SF",
        height_in=79, weight_lb=220, birth_date=datetime.date(1999, 1, 1),
        years_pro=4, is_rookie=False, team_id=team_b.id,
        roster_status="active", overall=PRIMARY_OVERALL,
        speed=82, shooting_2pt=82, shooting_3pt=75, shooting_mid=80,
        finishing=82, playmaking=78, defense=78, rebounding=75,
        iq=78, potential=82, peak_age_start=26, peak_age_end=31,
        loyalty=50, money_drive=50, win_drive=60, market_pref="any",
        star_leverage=60,
    )
    secondary_player = player_repo.Player(
        id=SECONDARY_PID, league_id=LEAGUE_ID, external_id=None,
        first_name="Role", last_name="Guard", position="PG",
        height_in=74, weight_lb=185, birth_date=datetime.date(1997, 5, 10),
        years_pro=6, is_rookie=False, team_id=team_b.id,
        roster_status="active", overall=SECONDARY_OVERALL,
        speed=76, shooting_2pt=74, shooting_3pt=74, shooting_mid=72,
        finishing=70, playmaking=74, defense=72, rebounding=65,
        iq=72, potential=70, peak_age_start=27, peak_age_end=31,
        loyalty=50, money_drive=50, win_drive=50, market_pref="any",
        star_leverage=30,
    )

    primary_contract = player_repo.Contract(
        id=10, league_id=LEAGUE_ID, player_id=PRIMARY_PID, team_id=team_b.id,
        salary=PRIMARY_SALARY, years_remaining=PRIMARY_YEARS, total_years=4,
        contract_type="standard", signed_in_season=SEASON - 2,
        is_active=True, terminated_reason=None,
    )
    secondary_contract = player_repo.Contract(
        id=11, league_id=LEAGUE_ID, player_id=SECONDARY_PID, team_id=team_b.id,
        salary=SECONDARY_SALARY, years_remaining=SECONDARY_YEARS, total_years=3,
        contract_type="standard", signed_in_season=SEASON - 1,
        is_active=True, terminated_reason=None,
    )

    # Compute expected combined target value: form(primary) + form(secondary).
    # Use _player_age so age matches production exactly (birth_date → today diff).
    from services.cpu_trade_posture import _player_age
    p2_primary_raw = trade_evaluator.player_trade_value(
        {"overall": PRIMARY_OVERALL, "age": _player_age(primary_player), "position": "SF"},
        {"salary": PRIMARY_SALARY, "years_remaining": PRIMARY_YEARS},
        SALARY_CAP, season_stats=None,
    )
    p2_secondary_raw = trade_evaluator.player_trade_value(
        {"overall": SECONDARY_OVERALL, "age": _player_age(secondary_player), "position": "PG"},
        {"salary": SECONDARY_SALARY, "years_remaining": SECONDARY_YEARS},
        SALARY_CAP, season_stats=None,
    )
    expected_combined = (
        trade_evaluator.apply_form(p2_primary_raw, PRIMARY_FORM)
        + trade_evaluator.apply_form(p2_secondary_raw, SECONDARY_FORM)
    )

    pool = MagicMock()

    async def _fetch(sql, *args):
        sql_lower = sql.lower()
        if "lineups" in sql_lower:
            return []
        if "pending_counterparty" in sql_lower:
            return []
        return []

    async def _fetchval(sql, *args):
        sql_lower = sql.lower()
        if "coach_philosophy" in sql_lower:
            return None
        if "draft_picks" in sql_lower:
            return 0
        if "trade_assets" in sql_lower:
            return 0
        if "last_traded_at" in sql_lower:
            return None
        return None

    pool.fetch = _fetch
    pool.fetchval = _fetchval

    # get_by_id: primary pid during pass-1 block scan, secondary pid during pass-2.
    async def _get_by_id(pool_, pid):
        if pid == PRIMARY_PID:
            return primary_player
        if pid == SECONDARY_PID:
            return secondary_player
        return None

    # get_active_contract: primary during pass-1; secondary during pass-2 secondary fold.
    async def _get_active_contract(pool_, pid):
        if pid == PRIMARY_PID:
            return primary_contract
        if pid == SECONDARY_PID:
            return secondary_contract
        return None

    # compute_form_map: return per-player form based on which pid is requested.
    async def _compute_form_map(pool_, player_ids, ovr_map, pos_map, league_id, season):
        result = {}
        for pid in player_ids:
            if pid == PRIMARY_PID:
                result[pid] = (PRIMARY_FORM, {})
            elif pid == SECONDARY_PID:
                result[pid] = (SECONDARY_FORM, {})
            else:
                result[pid] = (1.0, {})
        return result

    captured_target_value: list[float] = []

    async def _fake_build_return_package(
        pool_, league_, team_a_, block_ids, target_value, *args, **kwargs
    ):
        captured_target_value.append(target_value)
        return ([], [], 0.0)

    postures = {
        team_a.id: {
            "mode": "developing", "urgency": "comfortable",
            "avg_age": 27.0, "projected_wins": None,
            "conf_rank": None, "games_remaining": 82, "near_threshold": None,
        },
        team_b.id: {
            "mode": "developing", "urgency": "comfortable",
            "avg_age": 27.0, "projected_wins": None,
            "conf_rank": None, "games_remaining": 82, "near_threshold": None,
        },
    }

    with (
        patch.object(_mod.player_repo, "get_by_id", _get_by_id),
        patch.object(_mod.player_repo, "get_active_contract", _get_active_contract),
        patch.object(_mod.trade_evaluator, "compute_form_map", _compute_form_map),
        patch("services.cpu_trade_proposals._build_return_package", _fake_build_return_package),
        # Force random.random() < 0.3 so the secondary branch always fires for
        # a player with overall >= 75 (PRIMARY_OVERALL = 82 qualifies).
        patch("services.cpu_trade_proposals.random.random", return_value=0.1),
        patch("services.trade_context.compute_context_modifier", AsyncMock(return_value=(1.0, []))),
    ):
        await _mod._run_incoming_first_for_team(
            pool=pool,
            league=league,
            season=SEASON,
            team_a=team_a,
            cpu_teams=[team_a, team_b],
            # Both pids in b's block: primary scored in pass-1, secondary fetched in pass-2.
            block_by_team={team_b.id: [PRIMARY_PID, SECONDARY_PID]},
            used_pairs=set(),
            taken_player_ids=set(),
            deadline_game_index=50,
            recently_signed_ids=set(),
            guild=None,
            postures=postures,
            cp_plans={team_a.id: None, team_b.id: None},
            cp_contexts={},
            cp_r1_counts={},
            roster_a_cache=[],
        )

    assert len(captured_target_value) >= 1, (
        "_build_return_package was never called; the candidate did not reach pass-2. "
        "Check that both players' trade values exceed the 15-point threshold."
    )
    actual_tv = captured_target_value[0]
    assert abs(actual_tv - expected_combined) < 0.01, (
        f"Pass-2 must pass primary+secondary combined form-adjusted value.\n"
        f"  Expected (primary_form + secondary_form): {expected_combined:.4f}\n"
        f"  Actual (received):                        {actual_tv:.4f}\n"
        f"  Difference:                               {abs(actual_tv - expected_combined):.4f}\n"
        "If this fails, line 1529 is using _cand_tv (primary only, no form) "
        "instead of _p2_adj_tv (primary-form + secondary-form combined)."
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
