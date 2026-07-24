"""
Characterization + behavior tests for services.cpu_coach_service.

Zero coverage existed for this module before this file, despite it being the
LIVE per-game gameplan decision path (sim_orchestrator._sim_single_game calls
cpu_coach_service.decide_gameplans unconditionally; auto_strategy.infer_archetype
is dead code by comparison -- see docs/design for the realism-audit background).
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from services import cpu_coach_service as ccs
from services import team_intel

pytestmark = pytest.mark.asyncio


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
