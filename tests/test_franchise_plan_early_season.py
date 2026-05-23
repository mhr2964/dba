"""
Tests for _derive_goal_and_horizon early-season (games_played < 10) branch.

Focuses on the B7 fix: play_in_fringe + star should floor at ("win_now", 2)
instead of falling through to ("transition", 2).
"""
from __future__ import annotations

from services.franchise_plan_service import _derive_goal_and_horizon


# ---------------------------------------------------------------------------
# NYK shape: avg_age ≈ 28-29, 1 star (OVR 89), mode = play_in_fringe
# ---------------------------------------------------------------------------


def test_preseason_play_in_fringe_with_star_returns_win_now():
    """NYK preseason shape: avg_age=28.5, star OVR 89, mode play_in_fringe.

    Before the fix: falls through to ("transition", 2) because the early-season
    block only handled soft_rebuild, rebuilding, and contending.

    After the fix: play_in_fringe + has_any_star floors at ("win_now", 2).
    """
    goal, horizon = _derive_goal_and_horizon(
        projected_wins=0.0,  # preseason — no wins yet
        avg_age=28.5,
        has_young_star=False,
        has_elite_star=False,
        has_any_star=True,   # KAT OVR 89
        r1_picks_next3=0,
        games_played=0,      # preseason / very early season
        ovr_list=[89, 82, 80, 78, 76, 74, 72, 70, 68, 66, 64, 62],
        mode="play_in_fringe",
    )
    assert goal == "win_now", (
        f"Preseason play_in_fringe + star must return win_now, got goal={goal!r}"
    )
    assert horizon == 2, (
        f"Expected horizon=2, got {horizon}"
    )


def test_preseason_play_in_fringe_without_star_returns_transition():
    """play_in_fringe team with NO star at preseason still falls through to transition.

    The fix only applies when has_any_star=True.
    """
    goal, horizon = _derive_goal_and_horizon(
        projected_wins=0.0,
        avg_age=28.5,
        has_young_star=False,
        has_elite_star=False,
        has_any_star=False,  # no star
        r1_picks_next3=0,
        games_played=0,
        ovr_list=[82, 80, 78, 76, 74, 72, 70, 68, 66, 64, 62, 60],
        mode="play_in_fringe",
    )
    # Without a star, play_in_fringe should fall through to transition (catch-all)
    assert goal == "transition", (
        f"play_in_fringe without star should return transition, got goal={goal!r}"
    )


def test_preseason_contending_returns_win_now():
    """Contending mode at preseason still returns win_now (pre-existing path).

    Verifies the existing branch was not broken by the new play_in_fringe check.
    """
    goal, horizon = _derive_goal_and_horizon(
        projected_wins=0.0,
        avg_age=29.0,
        has_young_star=False,
        has_elite_star=False,
        has_any_star=True,
        r1_picks_next3=0,
        games_played=0,
        ovr_list=[90, 85, 82, 80, 78, 76, 74, 72, 70, 68, 66, 64],
        mode="contending",
    )
    assert goal == "win_now", (
        f"Contending mode at preseason must return win_now, got goal={goal!r}"
    )


def test_preseason_soft_rebuild_returns_rebuild():
    """soft_rebuild at preseason returns rebuild (pre-existing path).

    Verifies the existing branch was not broken by the play_in_fringe fix.
    """
    goal, horizon = _derive_goal_and_horizon(
        projected_wins=0.0,
        avg_age=26.0,
        has_young_star=False,
        has_elite_star=False,
        has_any_star=False,
        r1_picks_next3=2,
        games_played=0,
        ovr_list=[75, 74, 72, 70, 68, 66, 64, 62, 60, 58, 56, 54],
        mode="soft_rebuild",
    )
    assert goal == "rebuild", (
        f"soft_rebuild at preseason must return rebuild, got goal={goal!r}"
    )


def test_preseason_old_star_returns_win_now():
    """avg_age >= 30 + star at preseason triggers the oldest branch first.

    The avg_age >= 30 + has_any_star branch fires before play_in_fringe check.
    Result must still be win_now (horizon 1 on the older path).
    """
    goal, horizon = _derive_goal_and_horizon(
        projected_wins=0.0,
        avg_age=31.0,
        has_young_star=False,
        has_elite_star=False,
        has_any_star=True,
        r1_picks_next3=0,
        games_played=0,
        ovr_list=[89, 85, 82, 80, 78, 76, 74, 72, 70, 68, 66, 64],
        mode="play_in_fringe",
    )
    assert goal == "win_now", (
        f"avg_age>=30 + star must return win_now, got goal={goal!r}"
    )
    assert horizon == 1, (
        f"avg_age>=30 branch sets horizon=1, got {horizon}"
    )
