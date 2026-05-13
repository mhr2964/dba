"""
Unit tests for services.trade_evaluator.

Pure Python — no DB, no async needed.
"""
from __future__ import annotations

from services import trade_evaluator

SALARY_CAP = 140_000_000


# ---------------------------------------------------------------------------
# player_trade_value
# ---------------------------------------------------------------------------


def test_star_player_high_value():
    """OVR 92, age 28, reasonable 3-year contract produces a notably higher value than a bench player.

    The formula: base = 92 * 0.5 = 46, age_mult = 1.1, contract_mod = 1.1
    gives 46 * 1.1 * 1.1 = 55.66, clearly above a 65 OVR bench piece (~35.75).
    We assert the star is worth more than 50 points, and more than 50% above bench value.
    """
    player = {"overall": 92, "age": 28}
    contract = {"salary": int(SALARY_CAP * 0.25), "years_remaining": 3}
    value = trade_evaluator.player_trade_value(player, contract, SALARY_CAP)

    bench_player = {"overall": 65, "age": 30}
    bench_contract = {"salary": int(SALARY_CAP * 0.05), "years_remaining": 1}
    bench_value = trade_evaluator.player_trade_value(bench_player, bench_contract, SALARY_CAP)

    assert value > 50, f"Star player value should exceed 50, got {value}"
    assert value > bench_value * 1.5, (
        f"Star ({value}) should be >50% more valuable than bench ({bench_value})"
    )


def test_bad_contract_reduces_value():
    """Same OVR 80 player on an overpaid contract is worth less than on a fair deal."""
    player = {"overall": 80, "age": 28}
    good_contract = {"salary": int(SALARY_CAP * 0.20), "years_remaining": 2}
    bad_contract = {"salary": int(SALARY_CAP * 0.40), "years_remaining": 2}

    good_value = trade_evaluator.player_trade_value(player, good_contract, SALARY_CAP)
    bad_value = trade_evaluator.player_trade_value(player, bad_contract, SALARY_CAP)

    assert bad_value < good_value, (
        f"Bad contract should reduce value: good={good_value}, bad={bad_value}"
    )
    # The overpaid multiplier is 0.6, so the delta should be substantial
    assert good_value - bad_value > 10, "Reduction should be meaningful, not marginal"


def test_age_multiplier_young():
    """Age 21 player has higher value than same OVR age 35 player."""
    player_young = {"overall": 80, "age": 21}
    player_old = {"overall": 80, "age": 35}
    contract = {"salary": int(SALARY_CAP * 0.20), "years_remaining": 2}

    young_value = trade_evaluator.player_trade_value(player_young, contract, SALARY_CAP)
    old_value = trade_evaluator.player_trade_value(player_old, contract, SALARY_CAP)

    assert young_value > old_value, (
        f"Young player should be worth more: young={young_value}, old={old_value}"
    )


def test_age_multiplier_decline():
    """Age 37 player has significantly reduced value compared to same player at 28."""
    player_prime = {"overall": 85, "age": 28}
    player_decline = {"overall": 85, "age": 37}
    contract = {"salary": int(SALARY_CAP * 0.20), "years_remaining": 2}

    prime_value = trade_evaluator.player_trade_value(player_prime, contract, SALARY_CAP)
    decline_value = trade_evaluator.player_trade_value(player_decline, contract, SALARY_CAP)

    # age 37 multiplier: max(0.1, 0.75 - (37-34)*0.05) = max(0.1, 0.60) = 0.60
    # age 28 multiplier: 1.1
    assert decline_value < prime_value * 0.70, (
        f"Age 37 player should be significantly cheaper: prime={prime_value}, decline={decline_value}"
    )


# ---------------------------------------------------------------------------
# pick_trade_value
# ---------------------------------------------------------------------------


def test_first_round_more_valuable_than_second():
    """Round 1 pick in the same season is worth more than a Round 2 pick."""
    current = 2025
    r1 = trade_evaluator.pick_trade_value(2026, 1, current)
    r2 = trade_evaluator.pick_trade_value(2026, 2, current)
    assert r1 > r2, f"R1={r1} should exceed R2={r2}"


def test_near_term_more_valuable():
    """Next season's pick is worth more than a pick four years out."""
    current = 2025
    near = trade_evaluator.pick_trade_value(current + 1, 1, current)
    far = trade_evaluator.pick_trade_value(current + 4, 1, current)
    assert near > far, f"Near-term pick ({near}) should be worth more than distant pick ({far})"


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
    result = trade_evaluator.evaluate_trade(
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
    result = trade_evaluator.evaluate_trade(
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


# ---------------------------------------------------------------------------
# cpu_should_accept
# ---------------------------------------------------------------------------


def _build_evaluation(score_a: float, score_b: float) -> dict:
    differential = abs(score_a - score_b)
    max_side = max(score_a, score_b, 1.0)
    return {
        "score_a": score_a,
        "score_b": score_b,
        "differential": differential,
        "is_fair": differential < max_side * 0.20,
        "rationale": "test",
    }


def test_cpu_rebuilding_accepts_picks_for_vets():
    """Rebuilding CPU accepts trading a 78 OVR vet for two first-round picks."""
    assets_receiving = [
        {"asset_type": "pick", "pick": {"round": 1}, "season": 2026, "round": 1},
        {"asset_type": "pick", "pick": {"round": 1}, "season": 2027, "round": 1},
    ]
    assets_giving = [
        {
            "asset_type": "player",
            "player": {"overall": 78, "age": 30},
            "contract": {"salary": int(SALARY_CAP * 0.15)},
        }
    ]
    evaluation = _build_evaluation(score_a=80.0, score_b=85.0)

    accept, reason = trade_evaluator.cpu_should_accept(
        cpu_team_mode="rebuilding",
        assets_receiving=assets_receiving,
        assets_giving=assets_giving,
        evaluation=evaluation,
        salary_cap=SALARY_CAP,
        current_cap_used=int(SALARY_CAP * 0.50),
    )
    assert accept is True, f"Rebuilding CPU should accept picks for vets: {reason}"


def test_cpu_rejects_lopsided_trade():
    """CPU rejects when it clearly gives more value than it receives (score_b >> score_a)."""
    assets_receiving = [
        {
            "asset_type": "player",
            "player": {"overall": 60, "age": 32},
            "contract": {"salary": int(SALARY_CAP * 0.05)},
        }
    ]
    assets_giving = [
        {
            "asset_type": "player",
            "player": {"overall": 88, "age": 27},
            "contract": {"salary": int(SALARY_CAP * 0.28)},
        }
    ]
    # score_a (CPU receives) is low, score_b (CPU gives) is high — lopsided against CPU
    evaluation = _build_evaluation(score_a=30.0, score_b=110.0)

    accept, reason = trade_evaluator.cpu_should_accept(
        cpu_team_mode="developing",
        assets_receiving=assets_receiving,
        assets_giving=assets_giving,
        evaluation=evaluation,
        salary_cap=SALARY_CAP,
        current_cap_used=int(SALARY_CAP * 0.50),
    )
    assert accept is False, f"CPU should reject lopsided trade: {reason}"


def test_cpu_rejects_if_over_cap():
    """CPU rejects a trade that would push it over the salary cap."""
    # CPU gives out a cheap player and receives an expensive one
    big_salary = int(SALARY_CAP * 0.35)
    small_salary = int(SALARY_CAP * 0.05)
    assets_receiving = [
        {
            "asset_type": "player",
            "player": {"overall": 85, "age": 27},
            "contract": {"salary": big_salary},
        }
    ]
    assets_giving = [
        {
            "asset_type": "player",
            "player": {"overall": 70, "age": 30},
            "contract": {"salary": small_salary},
        }
    ]
    # Net salary change: +big_salary - small_salary pushed over cap
    current_cap_used = int(SALARY_CAP * 0.80)  # already at 80%
    evaluation = _build_evaluation(score_a=50.0, score_b=40.0)

    accept, reason = trade_evaluator.cpu_should_accept(
        cpu_team_mode="contending",
        assets_receiving=assets_receiving,
        assets_giving=assets_giving,
        evaluation=evaluation,
        salary_cap=SALARY_CAP,
        current_cap_used=current_cap_used,
    )
    assert accept is False, f"CPU should reject cap-busting trade: {reason}"
    assert "cap" in reason.lower(), f"Reason should mention cap: {reason}"
