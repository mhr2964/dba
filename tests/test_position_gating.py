"""
Unit tests for positional-need gating in trade_proposal_scoring.py.

_get_trade_target_positions / _position_matches_need had no prior test
coverage — the first block below is a characterization of their CURRENT
(pre-#2-fix) behavior. _is_stacked_without_upgrade is new (finding #2:
non-contender modes previously had zero positional targeting at all).
"""
from __future__ import annotations

from services.trade_proposal_scoring import (
    _get_trade_target_positions,
    _is_stacked_without_upgrade,
    _position_matches_need,
)


class _FakeTeam:
    def __init__(self, team_id: int = 1):
        self.id = team_id


class _FakeRosterPlayer:
    def __init__(self, position: str, overall: int):
        self.position = position
        self.overall = overall


# ---------------------------------------------------------------------------
# Characterization: _get_trade_target_positions (pre-existing behavior)
# ---------------------------------------------------------------------------


def test_contending_targets_single_weakest_position():
    """contending mode returns exactly the 1 weakest position."""
    pos_counts = {"PG": 3, "SG": 2, "SF": 4, "PF": 1, "C": 5}
    result = _get_trade_target_positions(_FakeTeam(), pos_counts, "contending")
    assert result == ["PF"], f"Expected weakest position PF; got {result}"


def test_play_in_fringe_targets_two_weakest_positions():
    """play_in_fringe mode returns the 2 weakest positions, ranked."""
    pos_counts = {"PG": 3, "SG": 2, "SF": 4, "PF": 1, "C": 5}
    result = _get_trade_target_positions(_FakeTeam(), pos_counts, "play_in_fringe")
    assert result == ["PF", "SG"], f"Expected 2 weakest [PF, SG]; got {result}"


def test_developing_mode_returns_all_five_positions():
    """Every other mode (developing/rebuilding/soft_rebuild/tanking) is
    unrestricted by _get_trade_target_positions — this is the gap finding #2
    closes at the hard-skip call site via _is_stacked_without_upgrade instead
    of restricting the target list itself."""
    pos_counts = {"PG": 3, "SG": 2, "SF": 4, "PF": 1, "C": 5}
    result = _get_trade_target_positions(_FakeTeam(), pos_counts, "developing")
    assert result == ["PG", "SG", "SF", "PF", "C"], f"Expected all 5 positions; got {result}"


def test_rebuilding_mode_returns_all_five_positions():
    pos_counts = {}
    result = _get_trade_target_positions(_FakeTeam(), pos_counts, "rebuilding")
    assert result == ["PG", "SG", "SF", "PF", "C"], f"Expected all 5 positions; got {result}"


# ---------------------------------------------------------------------------
# Characterization: _position_matches_need (pre-existing behavior)
# ---------------------------------------------------------------------------


def test_position_matches_need_exact_match():
    assert _position_matches_need("PF", ["PF"]) is True


def test_position_matches_need_no_match():
    assert _position_matches_need("C", ["PG"]) is False


def test_position_matches_need_adjacency_sf_to_pf_target():
    """SF is adjacent to a PF need (wing versatility)."""
    assert _position_matches_need("SF", ["PF"]) is True


def test_position_matches_need_adjacency_pg_to_sg_target():
    assert _position_matches_need("PG", ["SG"]) is True


def test_position_matches_need_no_adjacency_c_to_pg():
    assert _position_matches_need("C", ["PG"]) is False


# ---------------------------------------------------------------------------
# New (#2): _is_stacked_without_upgrade
# ---------------------------------------------------------------------------


def test_stacked_without_upgrade_blocks_lateral_add_at_stacked_position():
    """4 centers already rostered (>= stacked_floor 3); incoming C is not a
    clear upgrade over the weakest one (within upgrade_margin) — must block."""
    roster = [
        _FakeRosterPlayer("C", 70),
        _FakeRosterPlayer("C", 72),
        _FakeRosterPlayer("C", 74),
        _FakeRosterPlayer("C", 76),
    ]
    pos_count_map = {"C": 4}
    # Weakest C is 70; incoming 71 is within the default +3 margin — not a clear upgrade.
    assert _is_stacked_without_upgrade("C", 71, pos_count_map, roster) is True


def test_stacked_without_upgrade_allows_clear_upgrade():
    """Same stacked position, but incoming player clearly beats the weakest
    rostered player at that position (>= weakest + margin) — must NOT block."""
    roster = [
        _FakeRosterPlayer("C", 70),
        _FakeRosterPlayer("C", 72),
        _FakeRosterPlayer("C", 74),
        _FakeRosterPlayer("C", 76),
    ]
    pos_count_map = {"C": 4}
    assert _is_stacked_without_upgrade("C", 85, pos_count_map, roster) is False


def test_stacked_without_upgrade_allows_below_floor():
    """Only 2 rostered at the position (< stacked_floor 3) — never blocks,
    regardless of incoming OVR."""
    roster = [
        _FakeRosterPlayer("C", 70),
        _FakeRosterPlayer("C", 72),
    ]
    pos_count_map = {"C": 2}
    assert _is_stacked_without_upgrade("C", 60, pos_count_map, roster) is False


def test_stacked_without_upgrade_ignores_other_positions():
    """Position count map shows PF stacked, but the incoming player is a PG —
    must not block (checks the incoming player's own position only)."""
    roster = [_FakeRosterPlayer("PF", 70) for _ in range(4)]
    pos_count_map = {"PF": 4, "PG": 1}
    assert _is_stacked_without_upgrade("PG", 65, pos_count_map, roster) is False
