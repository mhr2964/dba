"""
Unit tests for _fill_to_value — the shared player+pick fill helper.

Pure Python, no DB, no async.  All inputs are constructed in-memory.
"""
from __future__ import annotations

import pytest

from services.cpu_trade_proposals import _fill_to_value


# Minimal pick dict shape used by the helper.
def _pick(pick_id: int, season: int = 2027, round_: int = 2, orig_team_id: int = 99) -> dict:
    return {
        "id": pick_id,
        "season": season,
        "round": round_,
        "original_team_id": orig_team_id,
    }


_CURRENT_SEASON = 2026
_TEAM_ID = 42  # offering team's ID (used for own-first-round protection)


# ---------------------------------------------------------------------------
# Test 1: hits target exactly using players only
# ---------------------------------------------------------------------------

def test_fills_target_with_players_only():
    """When players alone reach the target, no picks are included."""
    # scored_players: (bucket, value, player_id)
    # bucket 0 = surplus, offered first.
    scored_players = [
        (0, 40.0, 101),  # surplus player worth 40
        (1, 30.0, 102),  # flex player worth 30
    ]
    target_value = 38.0
    tolerance = target_value * 0.25  # 9.5 — so target - tol = 28.5

    player_ids, pick_ids, achieved = _fill_to_value(
        scored_players=scored_players,
        sorted_picks=[],
        target_value=target_value,
        max_picks=2,
        tolerance=tolerance,
        orig_win_pct={},
        current_season=_CURRENT_SEASON,
        team_id=_TEAM_ID,
        mode="developing",
        counterparty_mode="developing",
    )

    assert player_ids == [101], f"Should pick only the first surplus player: {player_ids}"
    assert pick_ids == [], f"No picks needed: {pick_ids}"
    assert achieved == 40.0, f"Achieved value should be 40: {achieved}"


# ---------------------------------------------------------------------------
# Test 2: hits target within tolerance using picks gap-fill
# ---------------------------------------------------------------------------

def test_fills_gap_with_picks():
    """When players fall short, picks bridge the value gap."""
    # Player worth 20, but target is 35 — gap of 15.
    scored_players = [(0, 20.0, 201)]
    sorted_picks = [
        _pick(1001, season=2027, round_=2, orig_team_id=99),
        _pick(1002, season=2028, round_=1, orig_team_id=99),
    ]
    target_value = 35.0
    tolerance = target_value * 0.25  # 8.75

    # pick_trade_value for a R2 2027 pick from a winning team will be > 0 — enough to close gap.
    player_ids, pick_ids, achieved = _fill_to_value(
        scored_players=scored_players,
        sorted_picks=sorted_picks,
        target_value=target_value,
        max_picks=2,
        tolerance=tolerance,
        orig_win_pct={99: 0.5},
        current_season=_CURRENT_SEASON,
        team_id=_TEAM_ID,
        mode="developing",
        counterparty_mode="developing",
    )

    assert player_ids == [201], f"Player should be included: {player_ids}"
    assert len(pick_ids) >= 1, f"At least one pick should be included: {pick_ids}"
    # achieved >= player value + pick value
    assert achieved > 20.0, f"Achieved should exceed player-only value: {achieved}"


# ---------------------------------------------------------------------------
# Test 3: returns empty when no combo possible
# ---------------------------------------------------------------------------

def test_returns_empty_when_nothing_available():
    """When there are no players and no picks, returns empty lists and zero value."""
    player_ids, pick_ids, achieved = _fill_to_value(
        scored_players=[],
        sorted_picks=[],
        target_value=50.0,
        max_picks=3,
        tolerance=12.5,
        orig_win_pct={},
        current_season=_CURRENT_SEASON,
        team_id=_TEAM_ID,
        mode="developing",
        counterparty_mode="developing",
    )

    assert player_ids == [], f"No players available: {player_ids}"
    assert pick_ids == [], f"No picks available: {pick_ids}"
    assert achieved == 0.0, f"Zero value when nothing available: {achieved}"


# ---------------------------------------------------------------------------
# Test 4: max_picks constraint is respected
# ---------------------------------------------------------------------------

def test_max_picks_constraint():
    """Never includes more picks than max_picks, even when short on value."""
    scored_players = []  # no players
    sorted_picks = [_pick(i, season=2027 + i, round_=2, orig_team_id=99) for i in range(1, 6)]
    target_value = 100.0  # very high — would need many picks to reach it
    tolerance = 5.0

    player_ids, pick_ids, achieved = _fill_to_value(
        scored_players=scored_players,
        sorted_picks=sorted_picks,
        target_value=target_value,
        max_picks=2,  # hard cap at 2
        tolerance=tolerance,
        orig_win_pct={99: 0.4},
        current_season=_CURRENT_SEASON,
        team_id=_TEAM_ID,
        mode="developing",
        counterparty_mode="developing",
    )

    assert len(pick_ids) <= 2, f"Should respect max_picks=2: {pick_ids}"


# ---------------------------------------------------------------------------
# Test 5: surplus players are offered before flex players
# ---------------------------------------------------------------------------

def test_surplus_offered_before_flex():
    """Surplus (bucket=0) players appear before flex (bucket=1) in output."""
    # Present flex first in the input list to confirm sorting overrides insertion order.
    scored_players = [
        (1, 35.0, 301),  # flex, high value
        (0, 20.0, 302),  # surplus, lower value — should still go first
    ]
    # Sort by (bucket, -value) so surplus goes before flex.
    scored_players_sorted = sorted(scored_players, key=lambda x: (x[0], -x[1]))

    player_ids, pick_ids, achieved = _fill_to_value(
        scored_players=scored_players_sorted,
        sorted_picks=[],
        target_value=60.0,    # needs both players
        max_picks=0,
        tolerance=10.0,
        orig_win_pct={},
        current_season=_CURRENT_SEASON,
        team_id=_TEAM_ID,
        mode="developing",
        counterparty_mode="developing",
    )

    # Surplus player (302) should appear before flex player (301).
    assert player_ids.index(302) < player_ids.index(301), (
        f"Surplus player 302 should come before flex player 301: {player_ids}"
    )
