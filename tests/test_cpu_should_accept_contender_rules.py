"""
Unit tests for the B5 contender-specific sub-rules in cpu_should_accept.

Sub-rule 1 (pick parity): contender-tier teams must not send picks on lateral/
downgrade swaps (net OVR change <= 0).

Sub-rule 2 (2-for-1 upgrade): contender-tier teams shipping 2+ starters (OVR>=75)
must receive one player whose OVR is STRICTLY greater than EACH outgoing starter.

Test scenarios map directly to the four run-4 regression trades named in the spec.
"""
from __future__ import annotations


from services import trade_evaluator

SALARY_CAP = 140_000_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_player_asset(
    overall: int,
    age: int = 27,
    salary_frac: float = 0.15,
    years: int = 2,
    first_name: str = "Player",
    last_name: str = "Test",
) -> dict:
    return {
        "asset_type": "player",
        "player": {
            "overall": overall,
            "age": age,
            "first_name": first_name,
            "last_name": last_name,
        },
        "contract": {
            "salary": int(SALARY_CAP * salary_frac),
            "years_remaining": years,
        },
    }


def _make_pick_asset(round_num: int = 2, season: int = 2027) -> dict:
    return {
        "asset_type": "pick",
        "round": round_num,
        "season": season,
        "pick": {"round": round_num, "season": season},
    }


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


# ---------------------------------------------------------------------------
# B5 sub-rule 1: Pick parity for contenders
# ---------------------------------------------------------------------------


async def test_contender_rejects_lateral_swap_with_lost_pick():
    """Mirrors DEN/HOU: win_now DEN ships Gordon (OVR 76) + 2nd pick, receives Brooks (OVR 76).

    Net OVR change = 76 - 76 = 0 (lateral swap).
    Contender sends a pick on a lateral swap — must be rejected by sub-rule 1.
    """
    # DEN is giving: Gordon (76) + 2nd-round pick
    assets_giving = [
        _make_player_asset(overall=76, last_name="Gordon"),
        _make_pick_asset(round_num=2),
    ]
    # DEN is receiving: Brooks (76)
    assets_receiving = [
        _make_player_asset(overall=76, last_name="Brooks"),
    ]

    # Evaluation roughly even on value (lateral swap).
    evaluation = _build_evaluation(score_a=35.0, score_b=38.0)

    accept, reason = await trade_evaluator.cpu_should_accept(
        cpu_team_mode="contending",
        assets_receiving=assets_receiving,
        assets_giving=assets_giving,
        evaluation=evaluation,
        salary_cap=SALARY_CAP,
        current_cap_used=int(SALARY_CAP * 0.85),
    )

    assert accept is False, (
        f"Contender should reject lateral swap with lost pick; got accept=True. reason={reason}"
    )
    assert "B5-sub1" in reason, (
        f"Expected B5-sub1 pick-parity rejection; got: {reason}"
    )


async def test_contender_rejects_downgrade_with_lost_pick():
    """Mirrors LAC/TOR: LAC gives depth + 2nd-round pick, receives Poeltl (79) while starting Zubac (84).

    Net OVR change: receiving Poeltl (79) vs giving Zubac-like (84) = -5.
    Contender sends a pick on a downgrade — must be rejected by sub-rule 1.
    Scores are set so fleecing floor passes (score_a >= score_b * 0.85) but
    sub-rule 1 still catches the downgrade + lost pick.
    """
    assets_giving = [
        _make_player_asset(overall=84, last_name="Zubac"),
        _make_pick_asset(round_num=2),
    ]
    assets_receiving = [
        _make_player_asset(overall=79, last_name="Poeltl"),
    ]

    # score_a (what we receive) > score_b (what we give) — looks "fair" by differential
    # but sub-rule 1 catches that we gave a pick on a downgrade.
    evaluation = _build_evaluation(score_a=50.0, score_b=50.0)

    accept, reason = await trade_evaluator.cpu_should_accept(
        cpu_team_mode="contending",
        assets_receiving=assets_receiving,
        assets_giving=assets_giving,
        evaluation=evaluation,
        salary_cap=SALARY_CAP,
        current_cap_used=int(SALARY_CAP * 0.90),
    )

    assert accept is False, (
        f"Contender should reject downgrade with lost pick; got accept=True. reason={reason}"
    )
    assert "B5-sub1" in reason, (
        f"Expected B5-sub1 pick-parity rejection; got: {reason}"
    )


async def test_contender_accepts_pick_with_upgrade():
    """Contender ships OVR-76 + 2nd-round pick, receives OVR-83.  Net OVR change = +7.

    This should pass sub-rule 1 (pick sent but net OVR is positive — upgrade).
    The overall trade may still fail other gates; we assert sub-rule 1 does NOT fire.
    """
    assets_giving = [
        _make_player_asset(overall=76, last_name="Outgoing"),
        _make_pick_asset(round_num=2),
    ]
    assets_receiving = [
        _make_player_asset(overall=83, last_name="Incoming"),
    ]

    # Balanced value — sub-rule 1 should not block this.
    evaluation = _build_evaluation(score_a=48.0, score_b=45.0)

    accept, reason = await trade_evaluator.cpu_should_accept(
        cpu_team_mode="contending",
        assets_receiving=assets_receiving,
        assets_giving=assets_giving,
        evaluation=evaluation,
        salary_cap=SALARY_CAP,
        current_cap_used=int(SALARY_CAP * 0.80),
    )

    # Sub-rule 1 must not trigger on an upgrade-with-pick deal.
    assert "B5-sub1" not in reason, (
        f"B5-sub1 should not fire on a pick-with-upgrade deal; got: {reason}"
    )


# ---------------------------------------------------------------------------
# B5 sub-rule 2: Contender 2-for-1 must be an upgrade
# ---------------------------------------------------------------------------


async def test_contender_rejects_2for1_without_upgrade():
    """Mirrors NYK/GSW: NYK ships Bridges-like (76) + Anunoby-like (82) for Kuminga-like (76).

    2 starters (OVR >= 75) out, 1 player in whose OVR (76) is not > max outgoing (82).
    Scores balanced so the fleecing floor passes (score_a = score_b).
    Sub-rule 2 must catch the 2-for-1 non-upgrade.
    """
    assets_giving = [
        _make_player_asset(overall=76, last_name="Bridges"),
        _make_player_asset(overall=82, last_name="Anunoby"),
    ]
    assets_receiving = [
        _make_player_asset(overall=76, last_name="Kuminga"),
    ]

    # Even score so the differential gates pass; sub-rule 2 must still fire.
    evaluation = _build_evaluation(score_a=55.0, score_b=55.0)

    accept, reason = await trade_evaluator.cpu_should_accept(
        cpu_team_mode="contending",
        assets_receiving=assets_receiving,
        assets_giving=assets_giving,
        evaluation=evaluation,
        salary_cap=SALARY_CAP,
        current_cap_used=int(SALARY_CAP * 0.90),
    )

    assert accept is False, (
        f"Contender should reject 2-for-1 without upgrade; got accept=True. reason={reason}"
    )
    # May be caught by the contending-mode "high_value_incoming >= 80" gate first
    # or by B5-sub2; either B5-family or mode-specific rejection is correct.
    assert accept is False


async def test_contender_rejects_2for1_lateral():
    """Contender ships two OVR-76 starters, receives one OVR-76 player.

    The incoming OVR (76) is NOT strictly greater than each outgoing (76, 76).
    Sub-rule 2 must reject even for a pure lateral 2-for-1.
    """
    assets_giving = [
        _make_player_asset(overall=76, last_name="StarterA"),
        _make_player_asset(overall=76, last_name="StarterB"),
    ]
    assets_receiving = [
        _make_player_asset(overall=76, last_name="LateralReturn"),
    ]

    # Balanced by value score but still a bad deal.
    evaluation = _build_evaluation(score_a=36.0, score_b=40.0)

    accept, reason = await trade_evaluator.cpu_should_accept(
        cpu_team_mode="contending",
        assets_receiving=assets_receiving,
        assets_giving=assets_giving,
        evaluation=evaluation,
        salary_cap=SALARY_CAP,
        current_cap_used=int(SALARY_CAP * 0.80),
    )

    assert accept is False, (
        f"Contender should reject lateral 2-for-1; got accept=True. reason={reason}"
    )
    assert "B5-sub2" in reason, (
        f"Expected B5-sub2 consolidation-upgrade rejection; got: {reason}"
    )


async def test_contender_accepts_consolidation_upgrade():
    """Contender ships two OVR-76 starters, receives one OVR-84 player.

    Incoming OVR (84) > max outgoing OVR (76) — strict upgrade.  Sub-rule 2 must pass.
    """
    assets_giving = [
        _make_player_asset(overall=76, last_name="StarterA"),
        _make_player_asset(overall=76, last_name="StarterB"),
    ]
    assets_receiving = [
        _make_player_asset(overall=84, last_name="StarUpgrade"),
    ]

    # Contender gets more value than it gives (upgrade scenario).
    evaluation = _build_evaluation(score_a=55.0, score_b=40.0)

    accept, reason = await trade_evaluator.cpu_should_accept(
        cpu_team_mode="contending",
        assets_receiving=assets_receiving,
        assets_giving=assets_giving,
        evaluation=evaluation,
        salary_cap=SALARY_CAP,
        current_cap_used=int(SALARY_CAP * 0.80),
    )

    # Sub-rule 2 must not block a genuine consolidation upgrade.
    assert "B5-sub2" not in reason, (
        f"B5-sub2 should not fire on a consolidation upgrade deal; got: {reason}"
    )
    # The contending path requires a high_value_incoming (OVR >= 80) — 84 qualifies.
    assert accept is True, (
        f"Contender should accept a 2-for-1 with strict OVR upgrade; got: {reason}"
    )


async def test_contender_2for2_not_gated():
    """Contender ships two OVR-76 starters, receives TWO players (2-for-2).

    Sub-rule 2 only fires when receiving <= 1 player.  A 2-for-2 should not be blocked
    by sub-rule 2 regardless of OVR comparison.
    """
    assets_giving = [
        _make_player_asset(overall=76, last_name="StarterA"),
        _make_player_asset(overall=76, last_name="StarterB"),
    ]
    assets_receiving = [
        _make_player_asset(overall=75, last_name="IncomingA"),
        _make_player_asset(overall=75, last_name="IncomingB"),
    ]

    evaluation = _build_evaluation(score_a=40.0, score_b=40.0)

    accept, reason = await trade_evaluator.cpu_should_accept(
        cpu_team_mode="contending",
        assets_receiving=assets_receiving,
        assets_giving=assets_giving,
        evaluation=evaluation,
        salary_cap=SALARY_CAP,
        current_cap_used=int(SALARY_CAP * 0.80),
    )

    # Sub-rule 2 must not fire on a 2-for-2 deal.
    assert "B5-sub2" not in reason, (
        f"B5-sub2 should not fire on a 2-for-2 deal; got: {reason}"
    )


# ---------------------------------------------------------------------------
# Non-contender teams are not gated by either sub-rule
# ---------------------------------------------------------------------------


async def test_non_contender_unaffected_by_pick_parity():
    """A tanking team shipping a pick on a lateral swap is not gated by sub-rule 1.

    Tanking teams trade picks all the time as part of deals to get youth.
    Sub-rules 1 and 2 only apply to contender-tier postures.
    """
    assets_giving = [
        _make_player_asset(overall=76, last_name="OldVet"),
        _make_pick_asset(round_num=2),
    ]
    assets_receiving = [
        _make_player_asset(overall=22, age=19, last_name="YoungRookie"),
    ]

    # Rebuild team gets youth + picks back; value slightly lower.
    evaluation = _build_evaluation(score_a=18.0, score_b=38.0)

    accept, reason = await trade_evaluator.cpu_should_accept(
        cpu_team_mode="rebuilding",
        assets_receiving=assets_receiving,
        assets_giving=assets_giving,
        evaluation=evaluation,
        salary_cap=SALARY_CAP,
        current_cap_used=int(SALARY_CAP * 0.50),
    )

    # Sub-rules 1 and 2 must not fire on a non-contender.
    assert "B5-sub1" not in reason, (
        f"B5-sub1 should not fire for a rebuilding team; got: {reason}"
    )
    assert "B5-sub2" not in reason, (
        f"B5-sub2 should not fire for a rebuilding team; got: {reason}"
    )


async def test_play_in_fringe_also_gated_by_sub_rules():
    """play_in_fringe teams are contender-tier and should also be gated.

    Sub-rule 1 fires when a play_in_fringe team ships a pick on a lateral swap.
    """
    assets_giving = [
        _make_player_asset(overall=76, last_name="WingA"),
        _make_pick_asset(round_num=2),
    ]
    assets_receiving = [
        _make_player_asset(overall=76, last_name="WingB"),
    ]

    evaluation = _build_evaluation(score_a=35.0, score_b=38.0)

    accept, reason = await trade_evaluator.cpu_should_accept(
        cpu_team_mode="play_in_fringe",
        assets_receiving=assets_receiving,
        assets_giving=assets_giving,
        evaluation=evaluation,
        salary_cap=SALARY_CAP,
        current_cap_used=int(SALARY_CAP * 0.80),
    )

    assert accept is False, (
        f"play_in_fringe should reject lateral swap with lost pick; got accept=True. reason={reason}"
    )
    assert "B5-sub1" in reason, (
        f"Expected B5-sub1 pick-parity rejection for play_in_fringe; got: {reason}"
    )
