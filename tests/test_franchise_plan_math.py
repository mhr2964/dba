"""
Characterization tests for the pure decision/classification functions now
in franchise_plan_math.py (originally written against
franchise_plan_service.py before the Phase 3 split; re-run unchanged
afterward -- see HANDOFF.md).

Pure functions -- no DB, no async, no I/O -- so no fakes/mocks needed.
_derive_goal_and_horizon already has dedicated coverage in
test_franchise_plan_early_season.py; this file covers the rest:
_should_pivot, _is_reassessment_checkpoint, _build_rationale,
_production_tier, _defensive_tier, _combined_tier, _categorise_players,
_project_wins, _calc_age. Zero coverage existed for any of these before
this file.
"""
from __future__ import annotations

import datetime

from services import franchise_plan_math as fps


# ---------------------------------------------------------------------------
# _project_wins
# ---------------------------------------------------------------------------

def test_project_wins_no_games_played_returns_wins():
    assert fps._project_wins(wins=5, losses=0, games_played=0) == 5.0


def test_project_wins_extrapolates_pace():
    # 10-5 through 15 games -> pace of 82 games
    result = fps._project_wins(wins=10, losses=5, games_played=15)
    assert abs(result - (10 / 15 * 82)) < 0.01


# ---------------------------------------------------------------------------
# _calc_age
# ---------------------------------------------------------------------------

def test_calc_age_none_birth_date_returns_none():
    assert fps._calc_age(None, 2025) is None


def test_calc_age_computes_relative_to_season_start():
    birth_date = datetime.date(2000, 10, 1)
    age = fps._calc_age(birth_date, 2025)
    assert abs(age - 25.0) < 0.01


# ---------------------------------------------------------------------------
# _is_reassessment_checkpoint
# ---------------------------------------------------------------------------

def test_checkpoint_initial_when_never_derived():
    should, reason = fps._is_reassessment_checkpoint(
        games_played=10, games_remaining=72, last_derived_game_index=None,
    )
    assert should is True
    assert reason == "initial"


def test_checkpoint_offseason_start():
    should, reason = fps._is_reassessment_checkpoint(
        games_played=82, games_remaining=0, last_derived_game_index=50,
    )
    assert should is True
    assert reason == "offseason_start"


def test_checkpoint_trade_deadline_window():
    should, reason = fps._is_reassessment_checkpoint(
        games_played=60, games_remaining=22, last_derived_game_index=10,
    )
    assert should is True
    assert reason == "trade_deadline"


def test_checkpoint_trade_deadline_already_derived_in_window_falls_through():
    # last_derived_game_index also in [55,65] -- already handled this window.
    should, reason = fps._is_reassessment_checkpoint(
        games_played=60, games_remaining=22, last_derived_game_index=58,
    )
    assert should is True
    assert reason == "mid_season_checkpoint"


def test_checkpoint_mid_season_checkpoint_after_pivot_min_games():
    should, reason = fps._is_reassessment_checkpoint(
        games_played=35, games_remaining=47, last_derived_game_index=10,
    )
    assert should is True
    assert reason == "mid_season_checkpoint"


def test_checkpoint_sticky_before_pivot_min_games():
    should, reason = fps._is_reassessment_checkpoint(
        games_played=15, games_remaining=67, last_derived_game_index=10,
    )
    assert should is False
    assert reason == "sticky"


# ---------------------------------------------------------------------------
# _should_pivot
# ---------------------------------------------------------------------------

def test_should_pivot_tank_failed_pivots_to_rebuild():
    old_plan = {"goal": "tank"}
    record = {"projected_wins": 40.0}
    should, new_goal, reason = fps._should_pivot(old_plan, record, r1_picks_banked=0)
    assert should is True
    assert new_goal == "rebuild"


def test_should_pivot_win_now_collapse_pivots_to_transition():
    old_plan = {"goal": "win_now"}
    record = {"projected_wins": 35.0}
    should, new_goal, reason = fps._should_pivot(old_plan, record, r1_picks_banked=0)
    assert should is True
    assert new_goal == "transition"


def test_should_pivot_transition_overperform_pivots_to_win_now():
    old_plan = {"goal": "transition"}
    record = {"projected_wins": 52.0}
    should, new_goal, reason = fps._should_pivot(old_plan, record, r1_picks_banked=0)
    assert should is True
    assert new_goal == "win_now"


def test_should_pivot_rebuild_with_banked_picks_pivots_to_tank():
    old_plan = {"goal": "rebuild"}
    record = {"projected_wins": 25.0}
    should, new_goal, reason = fps._should_pivot(old_plan, record, r1_picks_banked=2)
    assert should is True
    assert new_goal == "tank"


def test_should_pivot_no_condition_fires_returns_false():
    old_plan = {"goal": "win_now"}
    record = {"projected_wins": 50.0}
    should, new_goal, reason = fps._should_pivot(old_plan, record, r1_picks_banked=0)
    assert should is False
    assert new_goal is None


# ---------------------------------------------------------------------------
# _build_rationale
# ---------------------------------------------------------------------------

def test_build_rationale_win_now_with_star():
    text = fps._build_rationale(
        goal="win_now", star_name="Star Player", avg_age=28.0,
        projected_wins=55.0, r1_picks_next3=0, target_year=2026,
    )
    assert "Star Player" in text
    assert "55-win pace" in text


def test_build_rationale_tank_with_picks_banked():
    text = fps._build_rationale(
        goal="tank", star_name=None, avg_age=24.0,
        projected_wins=20.0, r1_picks_next3=2, target_year=2027,
    )
    assert "2 R1 picks banked" in text


def test_build_rationale_tank_no_picks_path():
    text = fps._build_rationale(
        goal="tank", star_name=None, avg_age=23.0,
        projected_wins=18.0, r1_picks_next3=0, target_year=2027,
    )
    assert "no OVR-88+ star" in text


def test_build_rationale_rebuild():
    text = fps._build_rationale(
        goal="rebuild", star_name=None, avg_age=29.0,
        projected_wins=22.0, r1_picks_next3=1, target_year=2027,
    )
    assert "full reset" in text


def test_build_rationale_transition():
    text = fps._build_rationale(
        goal="transition", star_name=None, avg_age=27.0,
        projected_wins=38.0, r1_picks_next3=0, target_year=2026,
    )
    assert "not contending" in text


# ---------------------------------------------------------------------------
# _production_tier / _defensive_tier / _combined_tier
# ---------------------------------------------------------------------------

def test_production_tier_unknown_low_gp():
    assert fps._production_tier({"gp": 5, "ppg": 30}) == "unknown"


def test_production_tier_star():
    assert fps._production_tier({"gp": 20, "ppg": 25, "apg": 2, "rpg": 3}) == "star"


def test_production_tier_producer():
    assert fps._production_tier({"gp": 20, "ppg": 17, "apg": 2, "rpg": 3}) == "producer"


def test_production_tier_role():
    assert fps._production_tier({"gp": 20, "ppg": 11, "apg": 2, "rpg": 3}) == "role"


def test_production_tier_depth():
    assert fps._production_tier({"gp": 20, "ppg": 4, "apg": 1, "rpg": 2}) == "depth"


def test_defensive_tier_unknown_low_minutes():
    assert fps._defensive_tier({"gp": 20, "mpg": 10, "bpg": 3}) == "unknown"


def test_defensive_tier_star():
    assert fps._defensive_tier({"gp": 20, "mpg": 30, "bpg": 2.5, "spg": 0.5, "drpg": 5}) == "star"


def test_defensive_tier_depth():
    assert fps._defensive_tier({"gp": 20, "mpg": 25, "bpg": 0.2, "spg": 0.3, "drpg": 2}) == "depth"


def test_combined_tier_takes_higher_of_the_two():
    assert fps._combined_tier("depth", "star") == "star"
    assert fps._combined_tier("producer", "depth") == "producer"


def test_combined_tier_archetype_bump():
    # def_tier "role" + defensive archetype -> best of the two ("role") gets
    # bumped one notch up to "producer".
    result = fps._combined_tier("depth", "role", archetype="rim_protector")
    assert result == "producer"


def test_combined_tier_archetype_bump_caps_at_star():
    result = fps._combined_tier("star", "star", archetype="wing_stopper")
    assert result == "star"


# ---------------------------------------------------------------------------
# _categorise_players
# ---------------------------------------------------------------------------

def _roster_player(pid, age, overall, position="SF"):
    return {"player_id": pid, "age": age, "overall": overall, "position": position}


def test_categorise_players_win_now_core_top3_in_age_window():
    roster = [
        _roster_player(1, 28, 90),
        _roster_player(2, 27, 85),
        _roster_player(3, 29, 80),
        _roster_player(4, 27, 76),
    ]
    core, flex, surplus, youth_overrides, shop_intent = fps._categorise_players(
        "win_now", roster, avg_age=28.0,
    )
    assert set(core) == {1, 2, 3}
    assert 4 in flex


def test_categorise_players_tank_core_is_young_and_good():
    roster = [
        _roster_player(1, 20, 80),  # U22, OVR>=78 -> core
        _roster_player(2, 29, 82),  # vet, OVR>=78 -> surplus
    ]
    core, flex, surplus, youth_overrides, shop_intent = fps._categorise_players(
        "tank", roster, avg_age=25.0,
    )
    assert core == [1]
    assert 2 in surplus
    assert shop_intent[2] == "age_misfit"


def test_categorise_players_rebuild_core_is_young_and_decent():
    roster = [
        _roster_player(1, 21, 76),  # U23, OVR>=75 -> core
        _roster_player(2, 30, 74),  # old -> surplus (age_misfit)
    ]
    core, flex, surplus, youth_overrides, shop_intent = fps._categorise_players(
        "rebuild", roster, avg_age=24.0,
    )
    assert core == [1]
    assert 2 in surplus


def test_categorise_players_production_override_forces_star_into_core():
    roster = [_roster_player(1, 30, 74)]  # would be flex/surplus by OVR alone
    production_map = {1: {"gp": 20, "ppg": 25, "apg": 2, "rpg": 3}}
    core, flex, surplus, youth_overrides, shop_intent = fps._categorise_players(
        "transition", roster, avg_age=27.0, production_map=production_map,
    )
    assert core == [1]


def test_categorise_players_youth_cornerstone_override_win_now():
    roster = [
        _roster_player(1, 24, 84),  # young cornerstone: OVR>=82, age<=26
        _roster_player(2, 28, 90),
    ]
    core, flex, surplus, youth_overrides, shop_intent = fps._categorise_players(
        "win_now", roster, avg_age=27.0,
    )
    assert 1 in core
    assert 1 in youth_overrides


def test_categorise_players_flip_asset_shop_intent():
    roster = [_roster_player(1, 33, 74)]  # old bench -> surplus
    core, flex, surplus, youth_overrides, shop_intent = fps._categorise_players(
        "win_now", roster, avg_age=28.0, recently_acquired_ids={1},
    )
    assert 1 in surplus
    assert shop_intent[1] == "flip_asset"
