"""
Unit tests for services.trade_grading.

Pure Python — no DB, no async needed.
"""
from __future__ import annotations

from services import trade_grading

SALARY_CAP = 140_000_000


# ---------------------------------------------------------------------------
# evaluate_trade
# ---------------------------------------------------------------------------


def _make_player_asset(overall: int, age: int, salary: int, years: int) -> dict:
    return {
        "player": {"overall": overall, "age": age},
        "contract": {"salary": salary, "years_remaining": years},
    }


def _make_pick_asset(season: int, round_num: int) -> dict:
    return {"season": season, "round": round_num}


def test_fair_trade_is_fair():
    """Two sides with roughly equal player value produce is_fair=True."""
    side_a = [_make_player_asset(85, 27, int(SALARY_CAP * 0.22), 2)]
    side_b = [_make_player_asset(84, 26, int(SALARY_CAP * 0.21), 2)]
    result = trade_grading.evaluate_trade(
        side_a_players=side_a,
        side_a_picks=[],
        side_b_players=side_b,
        side_b_picks=[],
        salary_cap=SALARY_CAP,
        current_season=2025,
    )
    assert result["is_fair"] is True, (
        f"Expected fair trade: score_a={result['score_a']}, score_b={result['score_b']}, "
        f"diff={result['differential']}"
    )


def test_lopsided_trade_not_fair():
    """OVR 90 star for a bench player is not fair."""
    side_a = [_make_player_asset(90, 27, int(SALARY_CAP * 0.28), 3)]  # star going out
    side_b = [_make_player_asset(65, 30, int(SALARY_CAP * 0.05), 1)]  # bench piece coming in
    result = trade_grading.evaluate_trade(
        side_a_players=side_a,
        side_a_picks=[],
        side_b_players=side_b,
        side_b_picks=[],
        salary_cap=SALARY_CAP,
        current_season=2025,
    )
    assert result["is_fair"] is False, (
        f"Expected lopsided trade: score_a={result['score_a']}, score_b={result['score_b']}"
    )
    assert result["score_a"] > result["score_b"]
