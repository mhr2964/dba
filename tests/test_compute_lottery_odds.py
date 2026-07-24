"""
Unit tests for services.sim_content_pipeline._compute_lottery_odds
(Finding #4 — Darius Cole's lottery odds were a hardcoded rank-position array).

Pins: odds are computed from actual win/loss records rather than a fixed
[14.0, 13.4, 12.7, 12.0, 10.5] spread, two teams with a real record gap get
visibly different odds, two teams close in record land close together, the
pool normalizes to 100%, and empty input is handled gracefully.
"""
from __future__ import annotations

from services.sim_content_pipeline import _compute_lottery_odds


def _row(team_id, wins, losses):
    return {"team_id": team_id, "wins": wins, "losses": losses}


def test_empty_pool_returns_empty_dict():
    assert _compute_lottery_odds([]) == {}


def test_odds_sum_to_100_percent():
    rows = [_row(1, 5, 40), _row(2, 10, 35), _row(3, 15, 30), _row(4, 20, 25)]
    odds = _compute_lottery_odds(rows)
    assert abs(sum(odds.values()) - 100.0) < 0.5


def test_worse_record_gets_higher_odds():
    rows = [_row(1, 5, 40), _row(2, 20, 25)]
    odds = _compute_lottery_odds(rows)
    assert odds[1] > odds[2]


def test_large_record_gap_produces_visibly_different_odds():
    """A team 10 games worse than another shouldn't land within a hair of
    them the way a fixed rank-only spread would."""
    rows = [_row(1, 5, 40), _row(2, 15, 30)]
    odds = _compute_lottery_odds(rows)
    assert odds[1] - odds[2] > 10.0


def test_small_record_gap_produces_close_odds():
    """Two teams half a game apart should land close together, unlike a
    fixed array that always assigns a ~0.6-1.3 point gap purely by rank."""
    rows = [_row(1, 20, 25), _row(2, 20, 24)]
    odds = _compute_lottery_odds(rows)
    assert abs(odds[1] - odds[2]) <= 1.0


def test_zero_games_played_does_not_divide_by_zero():
    rows = [_row(1, 0, 0), _row(2, 0, 0)]
    odds = _compute_lottery_odds(rows)
    assert odds[1] == odds[2] == 50.0
