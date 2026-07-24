"""Characterization tests for _description_limit (Phase 1 fix A6).

Pins the per-category Discord embed description truncation ceiling that
replaced the old flat [:2000] slice used at every columnist post call site.
"""
from __future__ import annotations

from services.sim_content_pipeline import _description_limit


def test_sunday_column_keeps_the_generous_long_form_ceiling():
    assert _description_limit("sunday_column") == 2000


def test_short_form_categories_get_the_tighter_default_ceiling():
    for category in (
        "power_rankings", "rookie_watch", "front_office_grade", "award_race",
        "injury_report", "game_recap", "tank_watch", "series_preview",
        "playoff_recap", "player_of_the_month", "coaching_beat",
    ):
        limit = _description_limit(category)
        assert limit == 800, f"{category} expected 800, got {limit}"
        assert limit < 2000


def test_unknown_category_falls_back_to_default_not_the_old_2000():
    assert _description_limit("some_new_category") == 800
