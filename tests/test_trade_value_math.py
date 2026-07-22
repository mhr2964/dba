"""
Unit tests for services.trade_value_math.

Pure Python — no DB, no async needed.
"""
from __future__ import annotations

from services import trade_value_math

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
    value = trade_value_math.player_trade_value(player, contract, SALARY_CAP)

    bench_player = {"overall": 65, "age": 30}
    bench_contract = {"salary": int(SALARY_CAP * 0.05), "years_remaining": 1}
    bench_value = trade_value_math.player_trade_value(bench_player, bench_contract, SALARY_CAP)

    assert value > 50, f"Star player value should exceed 50, got {value}"
    assert value > bench_value * 1.5, (
        f"Star ({value}) should be >50% more valuable than bench ({bench_value})"
    )


def test_bad_contract_reduces_value():
    """Same OVR 80 player on an overpaid contract is worth less than on a fair deal."""
    player = {"overall": 80, "age": 28}
    good_contract = {"salary": int(SALARY_CAP * 0.20), "years_remaining": 2}
    bad_contract = {"salary": int(SALARY_CAP * 0.40), "years_remaining": 2}

    good_value = trade_value_math.player_trade_value(player, good_contract, SALARY_CAP)
    bad_value = trade_value_math.player_trade_value(player, bad_contract, SALARY_CAP)

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

    young_value = trade_value_math.player_trade_value(player_young, contract, SALARY_CAP)
    old_value = trade_value_math.player_trade_value(player_old, contract, SALARY_CAP)

    assert young_value > old_value, (
        f"Young player should be worth more: young={young_value}, old={old_value}"
    )


def test_age_multiplier_decline():
    """Age 37 player has significantly reduced value compared to same player at 28."""
    player_prime = {"overall": 85, "age": 28}
    player_decline = {"overall": 85, "age": 37}
    contract = {"salary": int(SALARY_CAP * 0.20), "years_remaining": 2}

    prime_value = trade_value_math.player_trade_value(player_prime, contract, SALARY_CAP)
    decline_value = trade_value_math.player_trade_value(player_decline, contract, SALARY_CAP)

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
    r1 = trade_value_math.pick_trade_value(2026, 1, current)
    r2 = trade_value_math.pick_trade_value(2026, 2, current)
    assert r1 > r2, f"R1={r1} should exceed R2={r2}"


def test_near_term_more_valuable():
    """Next season's pick is worth more than a pick four years out."""
    current = 2025
    near = trade_value_math.pick_trade_value(current + 1, 1, current)
    far = trade_value_math.pick_trade_value(current + 4, 1, current)
    assert near > far, f"Near-term pick ({near}) should be worth more than distant pick ({far})"
