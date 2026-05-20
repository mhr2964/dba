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


async def test_cpu_rebuilding_accepts_picks_for_vets():
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

    accept, reason = await trade_evaluator.cpu_should_accept(
        cpu_team_mode="rebuilding",
        assets_receiving=assets_receiving,
        assets_giving=assets_giving,
        evaluation=evaluation,
        salary_cap=SALARY_CAP,
        current_cap_used=int(SALARY_CAP * 0.50),
    )
    assert accept is True, f"Rebuilding CPU should accept picks for vets: {reason}"


async def test_cpu_rejects_lopsided_trade():
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

    accept, reason = await trade_evaluator.cpu_should_accept(
        cpu_team_mode="developing",
        assets_receiving=assets_receiving,
        assets_giving=assets_giving,
        evaluation=evaluation,
        salary_cap=SALARY_CAP,
        current_cap_used=int(SALARY_CAP * 0.50),
    )
    assert accept is False, f"CPU should reject lopsided trade: {reason}"


async def test_cpu_rejects_if_over_cap():
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

    accept, reason = await trade_evaluator.cpu_should_accept(
        cpu_team_mode="contending",
        assets_receiving=assets_receiving,
        assets_giving=assets_giving,
        evaluation=evaluation,
        salary_cap=SALARY_CAP,
        current_cap_used=current_cap_used,
    )
    assert accept is False, f"CPU should reject cap-busting trade: {reason}"
    assert "cap" in reason.lower(), f"Reason should mention cap: {reason}"


# ---------------------------------------------------------------------------
# Guard 1: Age + OVR asymmetry
# ---------------------------------------------------------------------------


async def test_guard1_blocks_selling_low_on_younger_better_player():
    """Guard 1: giving OVR 81 age-25 for OVR 76 age-30 + a 2nd-round pick is a hard reject.

    TOR/RJ-Barrett-style scenario: younger and higher-OVR player going out,
    only older, lower-OVR player plus a 2nd coming back.
    ovr_gap = 81 - 76 = 5 (>= 3); age_gap = 30 - 25 = 5 (>= 2.0).
    Guard 1 must fire BEFORE the value-ratio acceptance path.
    """
    assets_giving = [
        {
            "asset_type": "player",
            "player": {"id": 1, "overall": 81, "age": 25, "first_name": "RJ", "last_name": "Barrett"},
            "contract": {"salary": int(SALARY_CAP * 0.18), "years_remaining": 3},
        }
    ]
    assets_receiving = [
        {
            "asset_type": "player",
            "player": {"id": 2, "overall": 76, "age": 30, "first_name": "Mikal", "last_name": "Bridges"},
            "contract": {"salary": int(SALARY_CAP * 0.16), "years_remaining": 2},
        },
        {
            "asset_type": "pick",
            "round": 2,
            "season": 2027,
            "pick": {"round": 2},
        },
    ]
    # Score roughly balanced so the existing fleecing floor would NOT block it
    evaluation = _build_evaluation(score_a=45.0, score_b=48.0)

    accept, reason = await trade_evaluator.cpu_should_accept(
        cpu_team_mode="developing",
        assets_receiving=assets_receiving,
        assets_giving=assets_giving,
        evaluation=evaluation,
        salary_cap=SALARY_CAP,
        current_cap_used=int(SALARY_CAP * 0.60),
    )
    assert accept is False, f"Guard 1 should block this trade: {reason}"
    assert "selling low" in reason, f"Reason should mention 'selling low': {reason}"
    assert "hard reject" in reason, f"Reason should be a hard reject: {reason}"


async def test_guard1_allows_symmetric_age_ovr():
    """Guard 1 does NOT block when OVR and age are roughly symmetric."""
    assets_giving = [
        {
            "asset_type": "player",
            "player": {"id": 10, "overall": 80, "age": 27},
            "contract": {"salary": int(SALARY_CAP * 0.18), "years_remaining": 2},
        }
    ]
    assets_receiving = [
        {
            "asset_type": "player",
            "player": {"id": 11, "overall": 78, "age": 26},
            "contract": {"salary": int(SALARY_CAP * 0.16), "years_remaining": 2},
        },
        {
            "asset_type": "pick",
            "round": 1,
            "season": 2027,
            "pick": {"round": 1},
        },
    ]
    # ovr_gap = 2 (< 3 threshold) — Guard 1 should NOT fire
    evaluation = _build_evaluation(score_a=52.0, score_b=50.0)

    accept, reason = await trade_evaluator.cpu_should_accept(
        cpu_team_mode="developing",
        assets_receiving=assets_receiving,
        assets_giving=assets_giving,
        evaluation=evaluation,
        salary_cap=SALARY_CAP,
        current_cap_used=int(SALARY_CAP * 0.55),
    )
    # Guard 1 should not fire; developing mode accepts balanced trades
    assert accept is True, f"Guard 1 should not block this symmetric trade: {reason}"


# ---------------------------------------------------------------------------
# Guard 2: Lead-role player requires equivalent return
# ---------------------------------------------------------------------------


async def test_guard2_blocks_iso_scorer_for_second_round_pick():
    """Guard 2: giving up an iso_scorer for only a 2nd-round pick is a hard reject.

    No starter-quality player (OVR >= 78) and no 1st-round pick arriving —
    Guard 2 must block this regardless of score math.
    """
    assets_giving = [
        {
            "asset_type": "player",
            "player": {"id": 100, "overall": 82, "age": 26, "first_name": "Devin", "last_name": "Booker"},
            "contract": {"salary": int(SALARY_CAP * 0.22), "years_remaining": 3},
        }
    ]
    assets_receiving = [
        {
            "asset_type": "pick",
            "round": 2,
            "season": 2027,
            "pick": {"round": 2},
        },
    ]
    giving_role_map = {100: "iso_scorer"}
    # Score is close enough that the existing fleecing floor (< 0.85 ratio) does NOT
    # fire — Guard 2 must be what blocks the trade.
    evaluation = _build_evaluation(score_a=46.0, score_b=50.0)

    accept, reason = await trade_evaluator.cpu_should_accept(
        cpu_team_mode="rebuilding",
        assets_receiving=assets_receiving,
        assets_giving=assets_giving,
        evaluation=evaluation,
        salary_cap=SALARY_CAP,
        current_cap_used=int(SALARY_CAP * 0.40),
        giving_role_map=giving_role_map,
    )
    assert accept is False, f"Guard 2 should block iso_scorer-for-2nd trade: {reason}"
    assert "hard reject" in reason, f"Reason should be a hard reject: {reason}"
    assert "lead-role" in reason, f"Reason should mention lead-role: {reason}"


async def test_guard2_allows_lead_role_for_first_round_pick():
    """Guard 2 does NOT block when a non-elite lead-role player is traded for a 1st-round pick.

    For ELITE players (OVR >= 82), Guard 2 now requires BOTH a starter-quality player
    AND a 1st-round pick. For non-elite lead-role players (OVR < 82), a 1st-round
    pick alone is sufficient to pass Guard 2.
    """
    # why: Guard 2 raised the bar for elite (OVR>=82) players in 2026-05 — they require
    #      BOTH a starter AND a 1st-round pick. OVR 79 is non-elite so the OR branch
    #      applies: 1st-round pick alone satisfies Guard 2. Updated player from OVR 82
    #      (elite, blocked) to OVR 79 (non-elite, allowed).
    assets_giving = [
        {
            "asset_type": "player",
            "player": {"id": 101, "overall": 79, "age": 26, "first_name": "Devin", "last_name": "Booker"},
            "contract": {"salary": int(SALARY_CAP * 0.22), "years_remaining": 3},
        }
    ]
    assets_receiving = [
        {
            "asset_type": "pick",
            "round": 1,
            "season": 2027,
            "pick": {"round": 1},
        },
    ]
    giving_role_map = {101: "iso_scorer"}
    # Rebuilding accepts picks — score must also survive the fleecing floor
    evaluation = _build_evaluation(score_a=28.0, score_b=32.0)

    accept, reason = await trade_evaluator.cpu_should_accept(
        cpu_team_mode="rebuilding",
        assets_receiving=assets_receiving,
        assets_giving=assets_giving,
        evaluation=evaluation,
        salary_cap=SALARY_CAP,
        current_cap_used=int(SALARY_CAP * 0.40),
        giving_role_map=giving_role_map,
    )
    assert accept is True, f"Guard 2 should allow non-elite lead-role-for-1st-round: {reason}"


async def test_guard2_skips_when_role_map_is_none():
    """Guard 2 silently skips when giving_role_map is None (backward compat)."""
    assets_giving = [
        {
            "asset_type": "player",
            "player": {"id": 102, "overall": 82, "age": 26},
            "contract": {"salary": int(SALARY_CAP * 0.22), "years_remaining": 3},
        }
    ]
    assets_receiving = [
        {
            "asset_type": "pick",
            "round": 2,
            "season": 2027,
            "pick": {"round": 2},
        },
    ]
    # No role map provided — Guard 2 must not fire, Guard 1 should also not fire
    # (only one player on each side; giving has no player counterpart to compare)
    evaluation = _build_evaluation(score_a=10.0, score_b=50.0)

    accept, reason = await trade_evaluator.cpu_should_accept(
        cpu_team_mode="rebuilding",
        assets_receiving=assets_receiving,
        assets_giving=assets_giving,
        evaluation=evaluation,
        salary_cap=SALARY_CAP,
        current_cap_used=int(SALARY_CAP * 0.40),
        giving_role_map=None,
    )
    # Guard 2 skips; rebuilding still rejects because no picks/youth quality here
    # but NOT because of Guard 2 — the reason must NOT mention "lead-role"
    assert "lead-role" not in reason, f"Guard 2 should not have fired: {reason}"


# ---------------------------------------------------------------------------
# Fleecing floor: "rebuild buys young" exemption
# ---------------------------------------------------------------------------


async def test_rebuild_buys_young_accepts_at_0_78_ratio():
    """CHI scenario: transition CPU, Vassell (age 25 OVR 76) incoming, ratio 0.78.

    Default floor is 0.85 — this would hard-reject.  Rebuild-buys-young exemption
    drops the floor to 0.70, so 0.78 now PASSES.

    Setup: CPU gives a value of 50 (score_b=50), receives 39 (score_a=39).
    Effective ratio = 39/50 = 0.78.  Mode = "soft_rebuild".
    """
    # Vucevic (aging, given away) side — CPU is the counterparty giving this
    assets_giving = [
        {
            "asset_type": "player",
            "player": {"id": 10, "overall": 82, "age": 35, "first_name": "Nikola", "last_name": "Vucevic"},
            "contract": {"salary": int(SALARY_CAP * 0.18), "years_remaining": 1},
        }
    ]
    # Vassell (25yo OVR 76) + CP3 throw-in — CPU is receiving this
    assets_receiving = [
        {
            "asset_type": "player",
            "player": {"id": 20, "overall": 76, "age": 25, "first_name": "Devin", "last_name": "Vassell"},
            "contract": {"salary": int(SALARY_CAP * 0.14), "years_remaining": 3},
        },
        {
            "asset_type": "player",
            "player": {"id": 21, "overall": 72, "age": 37, "first_name": "Chris", "last_name": "Paul"},
            "contract": {"salary": int(SALARY_CAP * 0.04), "years_remaining": 1},
        },
        {
            "asset_type": "pick",
            "round": 2,
            "season": 2027,
            "pick": {"round": 2},
        },
    ]
    # score_a=39, score_b=50 → ratio = 0.78 (above 0.70, below 0.85)
    evaluation = _build_evaluation(score_a=39.0, score_b=50.0)

    accept, reason = await trade_evaluator.cpu_should_accept(
        cpu_team_mode="soft_rebuild",
        assets_receiving=assets_receiving,
        assets_giving=assets_giving,
        evaluation=evaluation,
        salary_cap=SALARY_CAP,
        current_cap_used=int(SALARY_CAP * 0.55),
    )
    assert accept is True, (
        f"Rebuild-buys-young exemption should allow ratio 0.78 for soft_rebuild "
        f"receiving Vassell (age 25, OVR 76): {reason}"
    )


async def test_rebuild_buys_young_still_rejects_at_0_65_ratio():
    """Same CHI scenario but ratio 0.65 — still below the soft 0.70 floor.

    The exemption applies (mode=soft_rebuild, Vassell age 25 OVR 76 incoming),
    but 0.65 < 0.70 so the trade must still be rejected.  The reason should
    mention the soft floor and include "[rebuild softened]".
    """
    assets_giving = [
        {
            "asset_type": "player",
            "player": {"id": 10, "overall": 82, "age": 35},
            "contract": {"salary": int(SALARY_CAP * 0.18), "years_remaining": 1},
        }
    ]
    assets_receiving = [
        {
            "asset_type": "player",
            "player": {"id": 20, "overall": 76, "age": 25},
            "contract": {"salary": int(SALARY_CAP * 0.14), "years_remaining": 3},
        },
    ]
    # score_a=32.5, score_b=50 → ratio = 0.65 (below the 0.70 soft floor)
    evaluation = _build_evaluation(score_a=32.5, score_b=50.0)

    accept, reason = await trade_evaluator.cpu_should_accept(
        cpu_team_mode="soft_rebuild",
        assets_receiving=assets_receiving,
        assets_giving=assets_giving,
        evaluation=evaluation,
        salary_cap=SALARY_CAP,
        current_cap_used=int(SALARY_CAP * 0.55),
    )
    assert accept is False, (
        f"Ratio 0.65 is below the 0.70 soft floor — must still reject: {reason}"
    )
    assert "fleecing floor" in reason, f"Reason should mention fleecing floor: {reason}"
    assert "0.70" in reason, f"Reason should show the soft floor value: {reason}"
    assert "[rebuild softened]" in reason, f"Reason should include softened label: {reason}"


async def test_win_now_hard_floor_not_softened():
    """Win-now CPU at ratio 0.78 receives the same young-quality player but still rejects.

    The rebuild-buys-young exemption must NOT apply to contending / play_in_fringe /
    any non-rebuild mode.  Vassell (age 25 OVR 76) arriving doesn't help a win-now team
    bypass the 0.85 default floor.
    """
    assets_giving = [
        {
            "asset_type": "player",
            "player": {"id": 30, "overall": 82, "age": 28},
            "contract": {"salary": int(SALARY_CAP * 0.22), "years_remaining": 2},
        }
    ]
    assets_receiving = [
        {
            "asset_type": "player",
            "player": {"id": 31, "overall": 76, "age": 25},
            "contract": {"salary": int(SALARY_CAP * 0.14), "years_remaining": 3},
        },
    ]
    # ratio = 39/50 = 0.78 — passes soft floor but must fail the hard 0.85 default
    evaluation = _build_evaluation(score_a=39.0, score_b=50.0)

    accept, reason = await trade_evaluator.cpu_should_accept(
        cpu_team_mode="contending",
        assets_receiving=assets_receiving,
        assets_giving=assets_giving,
        evaluation=evaluation,
        salary_cap=SALARY_CAP,
        current_cap_used=int(SALARY_CAP * 0.75),
    )
    assert accept is False, (
        f"Win-now CPU should reject ratio 0.78 regardless of incoming youth: {reason}"
    )
    assert "fleecing floor" in reason, f"Reason should mention fleecing floor: {reason}"
    assert "[rebuild softened]" not in reason, (
        f"Reason must NOT include softened label for win-now team: {reason}"
    )
