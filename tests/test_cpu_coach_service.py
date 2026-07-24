"""
Characterization + behavior tests for services.cpu_coach_service.

Zero coverage existed for this module before this file, despite it being the
LIVE per-game gameplan decision path (sim_orchestrator._sim_single_game calls
cpu_coach_service.decide_gameplans unconditionally; auto_strategy.infer_archetype
is dead code by comparison -- see docs/design for the realism-audit background).
"""
from __future__ import annotations

import random
from unittest.mock import AsyncMock

from services import cpu_coach_service as ccs
from services import team_intel

# asyncio_mode = auto (pytest.ini) already covers the async tests below --
# no blanket pytestmark, since this file also has plain sync tests for the
# pure-function helpers (_analyze_roster, _decide_strategy).


# ---------------------------------------------------------------------------
# _classify_posture — now reuses team_intel.compute_posture (B7 pattern) so
# gameplan posture and trade posture share one live-posture source instead of
# two independently-drifting heuristics (finding #5).
# ---------------------------------------------------------------------------


async def test_classify_posture_contending_mode_maps_to_contender(monkeypatch):
    async def _fake_compute_posture(pool, league, team_id):
        return {"mode": "contending", "urgency": "pushing"}

    monkeypatch.setattr(team_intel, "compute_posture", _fake_compute_posture)

    result = await ccs._classify_posture(AsyncMock(), league_id=1, season=2025, team_id=1)
    assert result == "contender"


async def test_classify_posture_rebuilding_mode_maps_to_tanking(monkeypatch):
    async def _fake_compute_posture(pool, league, team_id):
        return {"mode": "rebuilding", "urgency": "tanking"}

    monkeypatch.setattr(team_intel, "compute_posture", _fake_compute_posture)

    result = await ccs._classify_posture(AsyncMock(), league_id=1, season=2025, team_id=1)
    assert result == "tanking"


async def test_classify_posture_soft_rebuild_mode_maps_to_tanking(monkeypatch):
    async def _fake_compute_posture(pool, league, team_id):
        return {"mode": "soft_rebuild", "urgency": "tanking"}

    monkeypatch.setattr(team_intel, "compute_posture", _fake_compute_posture)

    result = await ccs._classify_posture(AsyncMock(), league_id=1, season=2025, team_id=1)
    assert result == "tanking"


async def test_classify_posture_play_in_fringe_maps_to_mid(monkeypatch):
    async def _fake_compute_posture(pool, league, team_id):
        return {"mode": "play_in_fringe", "urgency": "pushing"}

    monkeypatch.setattr(team_intel, "compute_posture", _fake_compute_posture)

    result = await ccs._classify_posture(AsyncMock(), league_id=1, season=2025, team_id=1)
    assert result == "mid"


async def test_classify_posture_developing_maps_to_mid(monkeypatch):
    async def _fake_compute_posture(pool, league, team_id):
        return {"mode": "developing", "urgency": "comfortable"}

    monkeypatch.setattr(team_intel, "compute_posture", _fake_compute_posture)

    result = await ccs._classify_posture(AsyncMock(), league_id=1, season=2025, team_id=1)
    assert result == "mid"


async def test_classify_posture_respects_franchise_plan_goal_via_team_intel(monkeypatch):
    """A contend-plan team on a losing stretch should NOT be forced to 'tanking'.

    This is the exact B7-style regression finding #5 targets: previously
    _classify_posture only read win_pct/wins/pct_complete and had zero awareness
    of franchise_plan.goal. Now that it defers to team_intel.compute_posture,
    the plan_goal floor (already proven correct for trades) applies here too.
    Simulated by returning the mode team_intel would produce for a win_now
    plan floor (play_in_fringe, not rebuilding/soft_rebuild) despite a bad record.
    """
    async def _fake_compute_posture(pool, league, team_id):
        # team_intel.compute_posture would apply the plan_goal floor and return
        # play_in_fringe here instead of soft_rebuild/rebuilding.
        return {"mode": "play_in_fringe", "urgency": "desperate"}

    monkeypatch.setattr(team_intel, "compute_posture", _fake_compute_posture)

    result = await ccs._classify_posture(AsyncMock(), league_id=1, season=2025, team_id=1)
    assert result == "mid"
    assert result != "tanking"


# ---------------------------------------------------------------------------
# _analyze_roster — avg_speed / avg_defense / avg_defensive_effort computed
# for the finding #1 personnel gate.
# ---------------------------------------------------------------------------


def test_analyze_roster_computes_avg_speed_and_defense():
    players = [
        {"overall": 80, "speed": 90, "defense": 70, "defensive_effort": 60},
        {"overall": 80, "speed": 70, "defense": 50, "defensive_effort": 40},
    ]
    analysis = ccs._analyze_roster(players)
    assert analysis["avg_speed"] == 80.0
    assert analysis["avg_defense"] == 60.0
    assert analysis["avg_defensive_effort"] == 50.0


def test_analyze_roster_empty_returns_neutral_defaults():
    analysis = ccs._analyze_roster([])
    assert analysis["avg_speed"] == 50.0
    assert analysis["avg_defense"] == 50.0
    assert analysis["avg_defensive_effort"] == 50.0


# ---------------------------------------------------------------------------
# _decide_strategy — finding #1 personnel gate: press/switch_all must be
# pruned from the weighted defense_options entirely when the team's own
# roster can't execute them, not merely picked and left to hurt the team.
# ---------------------------------------------------------------------------


def _self_analysis(**overrides) -> dict:
    base = {
        "avg_ovr": 75.0, "has_elite_passer": False, "has_elite_scorer": False,
        "has_elite_big": False, "has_isolation_star": False, "three_point_count": 0,
        "elite_passer_name": None, "isolation_star_name": None, "elite_big_name": None,
        "elite_scorer_name": None,
        "avg_speed": 50.0, "avg_defense": 50.0, "avg_defensive_effort": 50.0,
    }
    base.update(overrides)
    return base


def _opp_analysis(**overrides) -> dict:
    base = {
        "avg_ovr": 90.0, "has_elite_passer": False, "has_elite_scorer": False,
        "has_elite_big": False, "has_isolation_star": False, "three_point_count": 0,
        "elite_passer_name": None, "isolation_star_name": None, "elite_big_name": None,
        "elite_scorer_name": None,
    }
    base.update(overrides)
    return base


_SEEDS = range(200)


def test_decide_strategy_slow_team_never_presses():
    """avg_speed=50 (league-average) is below the press gate -- press should
    never be selected even in the branch that would normally offer it
    (opponent OVR > 82)."""
    self_a = _self_analysis(avg_speed=50.0)
    opp_a = _opp_analysis(avg_ovr=90.0)
    for seed in _SEEDS:
        strategy, _ = ccs._decide_strategy(
            self_a, opp_a, "mid", False, 0, 0, random.Random(seed)
        )
        assert strategy["defensive_scheme"] != "press"


def test_decide_strategy_fast_team_can_press():
    """A genuinely fast roster (avg_speed >= 74) should still be able to press
    in the same branch."""
    self_a = _self_analysis(avg_speed=80.0)
    opp_a = _opp_analysis(avg_ovr=90.0)
    presses = sum(
        1 for seed in _SEEDS
        if ccs._decide_strategy(self_a, opp_a, "mid", False, 0, 0, random.Random(seed))[0]["defensive_scheme"] == "press"
    )
    assert presses > 0


def test_decide_strategy_poor_defense_team_never_switches_all():
    """avg_defense/avg_defensive_effort at league-average is below the
    switch_all gate -- switch_all should never fire even against an
    isolation-star opponent (the branch that would normally favor it)."""
    self_a = _self_analysis(avg_defense=50.0, avg_defensive_effort=50.0)
    opp_a = _opp_analysis(has_isolation_star=True, isolation_star_name="Star")
    for seed in _SEEDS:
        strategy, _ = ccs._decide_strategy(
            self_a, opp_a, "mid", False, 0, 0, random.Random(seed)
        )
        assert strategy["defensive_scheme"] != "switch_all"


def test_decide_strategy_elite_defense_team_can_switch_all():
    """A genuinely versatile, high-defense roster (avg_defense >= 74,
    avg_defensive_effort >= 60) should still be able to switch_all."""
    self_a = _self_analysis(avg_defense=85.0, avg_defensive_effort=75.0)
    opp_a = _opp_analysis(has_isolation_star=True, isolation_star_name="Star")
    switches = sum(
        1 for seed in _SEEDS
        if ccs._decide_strategy(self_a, opp_a, "mid", False, 0, 0, random.Random(seed))[0]["defensive_scheme"] == "switch_all"
    )
    assert switches > 0


def test_decide_strategy_always_returns_a_valid_defensive_scheme():
    """Pruning press/switch_all from defense_options must never leave the
    weighted-choice list empty -- every branch keeps a non-gated fallback
    (man_to_man or zone)."""
    self_a = _self_analysis(avg_speed=30.0, avg_defense=30.0, avg_defensive_effort=30.0)
    for opp_kwargs, posture in [
        ({"has_isolation_star": True, "isolation_star_name": "Star"}, "mid"),
        ({"avg_ovr": 90.0}, "mid"),
        ({}, "contender"),
        ({}, "mid"),
    ]:
        opp_a = _opp_analysis(**opp_kwargs)
        for seed in range(20):
            strategy, _ = ccs._decide_strategy(
                self_a, opp_a, posture, False, 0, 0, random.Random(seed)
            )
            assert strategy["defensive_scheme"] in ("man_to_man", "zone", "press", "switch_all")
