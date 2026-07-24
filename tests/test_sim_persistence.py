"""
Characterization + behavior tests for services.sim_persistence._stamp_role_data.

No DB required: get_or_derive_roles is monkeypatched so these tests exercise
the pure synergy-adjustment/renormalization math directly. Zero coverage
existed for this function before this file.
"""
from __future__ import annotations

import pytest

from services import sim_persistence

pytestmark = pytest.mark.asyncio


async def _fake_roles_movement_shooter_plus_filler(pool, league_id, team_id, season):
    """movement_shooter (scheme_synergy=["ball_movement"], touch_share=0.28)
    paired with glue_guy (scheme_synergy=[], touch_share=0.08) as a neutral
    control that should never be adjusted by offensive_scheme."""
    return [
        {"player_id": 1, "role": "movement_shooter", "touch_share": 0.28},
        {"player_id": 2, "role": "glue_guy", "touch_share": 0.08},
    ]


async def test_stamp_role_data_matching_scheme_applies_bonus(monkeypatch):
    """Baseline (unchanged by finding #3a): a role whose scheme_synergy
    includes the active offensive_scheme gets its raw touch_share multiplied
    by 1.15 before renormalization."""
    monkeypatch.setattr(sim_persistence, "get_or_derive_roles", _fake_roles_movement_shooter_plus_filler)
    sim_persistence.invalidate_role_cache(league_id=9001)

    players = [{"id": 1}, {"id": 2}]
    await sim_persistence._stamp_role_data(None, 9001, 1, 2025, players, "ball_movement")

    p1 = next(p for p in players if p["id"] == 1)
    p2 = next(p for p in players if p["id"] == 2)
    # 0.28*1.15 = 0.322; total = 0.322 + 0.08 = 0.402; share = 0.322/0.402
    expected_share = (0.28 * 1.15) / (0.28 * 1.15 + 0.08)
    assert abs(p1["_role_touch_share"] - round(expected_share, 4)) < 0.001
    assert p1["_role_touch_share"] > p2["_role_touch_share"]


async def test_stamp_role_data_mismatched_scheme_now_applies_penalty(monkeypatch):
    """Finding #3a fix: a role with a non-empty scheme_synergy list that does
    NOT include the active offensive_scheme (a real conflict -- movement_shooter
    wants ball_movement but the team is running isolation) now gets a small
    touch-share PENALTY (0.92x) instead of the old one-sided behavior where a
    mismatch was simply neutral (no bonus, no penalty).

    Pre-fix, this would have produced the same normalized share as the raw,
    unmodified ROLE_REGISTRY ratio (0.28 / (0.28 + 0.08) = 0.7778) since no
    adjustment applied to non-matching schemes at all. Post-fix it should be
    strictly lower than that unmodified baseline.
    """
    monkeypatch.setattr(sim_persistence, "get_or_derive_roles", _fake_roles_movement_shooter_plus_filler)
    sim_persistence.invalidate_role_cache(league_id=9002)

    players = [{"id": 1}, {"id": 2}]
    await sim_persistence._stamp_role_data(None, 9002, 1, 2025, players, "isolation")

    p1 = next(p for p in players if p["id"] == 1)
    unmodified_baseline = 0.28 / (0.28 + 0.08)
    assert p1["_role_touch_share"] < round(unmodified_baseline, 4)

    expected_share = (0.28 * 0.92) / (0.28 * 0.92 + 0.08)
    assert abs(p1["_role_touch_share"] - round(expected_share, 4)) < 0.001


async def _fake_roles_two_glue_guys(pool, league_id, team_id, season):
    """Two identical glue_guy players (scheme_synergy=[]) -- isolates whether
    an empty-synergy role's OWN adjustment factor ever moves, independent of
    any other player's bonus/penalty."""
    return [
        {"player_id": 1, "role": "glue_guy", "touch_share": 0.08},
        {"player_id": 2, "role": "glue_guy", "touch_share": 0.08},
    ]


async def test_stamp_role_data_empty_synergy_role_unaffected_by_scheme(monkeypatch):
    """A role with an empty scheme_synergy list (glue_guy) isn't scheme-committed
    at all -- it should get neither bonus nor penalty regardless of the active
    offensive_scheme. Two IDENTICAL glue_guy players should always split 50/50,
    for every offensive_scheme value, since neither the match-bonus nor the
    mismatch-penalty branch can ever fire for an empty synergy list."""
    monkeypatch.setattr(sim_persistence, "get_or_derive_roles", _fake_roles_two_glue_guys)

    for i, scheme in enumerate(("isolation", "ball_movement", "three_heavy", "inside_out", "balanced")):
        sim_persistence.invalidate_role_cache(league_id=9100 + i)
        players = [{"id": 1}, {"id": 2}]
        await sim_persistence._stamp_role_data(None, 9100 + i, 1, 2025, players, scheme)
        for p in players:
            assert abs(p["_role_touch_share"] - 0.5) < 0.001, (
                f"scheme={scheme}: expected 50/50 split for two identical empty-synergy "
                f"players, got {p['_role_touch_share']}"
            )
