"""
Characterization tests for the pure sentence-builder functions now in
trade_narrative_lines.py (originally written against ra_reasoning.py
before the Phase 3 split; re-run unchanged afterward -- see HANDOFF.md).

These are pure functions (no DB, no async) so no fakes/mocks are needed --
just representative inputs per branch, pinning current output so the move
can be verified byte-for-byte. Zero coverage existed for any of this
narrative-generation logic before this file.
"""
from __future__ import annotations

from services import trade_narrative_lines as rl


# ---------------------------------------------------------------------------
# _window_line
# ---------------------------------------------------------------------------

def test_window_line_win_now_short_horizon():
    plan = {"goal": "win_now", "horizon_seasons": 1, "asset_targets": ["veterans"]}
    posture = {"projected_wins": 55, "conf_rank": 2, "wins": 40, "losses": 20}
    line = rl._window_line(plan, posture)
    assert "Window's closing" in line
    assert "55-win pace" in line


def test_window_line_win_now_open_window():
    plan = {"goal": "win_now", "horizon_seasons": 3, "asset_targets": []}
    posture = {"projected_wins": 50, "conf_rank": 3, "wins": 30, "losses": 15}
    line = rl._window_line(plan, posture)
    assert "Window's open" in line


def test_window_line_tank():
    plan = {"goal": "tank", "horizon_seasons": 2, "asset_targets": ["young_u23"]}
    posture = {"projected_wins": 20, "conf_rank": 14, "wins": 10, "losses": 30}
    line = rl._window_line(plan, posture)
    assert "full tank mode" in line


def test_window_line_rebuild():
    plan = {"goal": "rebuild", "horizon_seasons": 3, "asset_targets": []}
    posture = {"projected_wins": 25, "conf_rank": 13, "wins": 15, "losses": 25}
    line = rl._window_line(plan, posture)
    assert "rebuild" in line
    assert "Year one" in line


def test_window_line_transition():
    plan = {"goal": "transition", "horizon_seasons": 2, "asset_targets": []}
    posture = {"projected_wins": 38, "conf_rank": 8, "wins": 20, "losses": 20}
    line = rl._window_line(plan, posture)
    assert "Threading the needle" in line


def test_window_line_soft_rebuild_fallback():
    plan = {"goal": "soft_rebuild", "horizon_seasons": 2, "asset_targets": []}
    posture = {"projected_wins": None, "conf_rank": None, "wins": 5, "losses": 5}
    line = rl._window_line(plan, posture)
    assert "Retooling" in line
    assert "5-5 right now" in line


# ---------------------------------------------------------------------------
# _posture_mode_label
# ---------------------------------------------------------------------------

def test_posture_mode_label_known():
    assert rl._posture_mode_label("play_in_fringe", "comfortable") == "play-in fringe/comfortable"


def test_posture_mode_label_unknown_falls_back_to_raw():
    assert rl._posture_mode_label("mystery_mode", "urgent") == "mystery_mode/urgent"


# ---------------------------------------------------------------------------
# _bucket_for_player
# ---------------------------------------------------------------------------

def test_bucket_for_player_core():
    plan = {"core_player_ids": [1, 2], "flex_player_ids": [3], "surplus_player_ids": [4]}
    assert rl._bucket_for_player(1, plan) == "core"


def test_bucket_for_player_flex():
    plan = {"core_player_ids": [1], "flex_player_ids": [3], "surplus_player_ids": [4]}
    assert rl._bucket_for_player(3, plan) == "flex"


def test_bucket_for_player_surplus():
    plan = {"core_player_ids": [1], "flex_player_ids": [3], "surplus_player_ids": [4]}
    assert rl._bucket_for_player(4, plan) == "surplus"


def test_bucket_for_player_unlisted():
    plan = {"core_player_ids": [1], "flex_player_ids": [3], "surplus_player_ids": [4]}
    assert rl._bucket_for_player(99, plan) == "unlisted"


# ---------------------------------------------------------------------------
# _key_tendency_label
# ---------------------------------------------------------------------------

def test_key_tendency_label_picks_highest():
    player = {"tendency_pass": 90, "tendency_3pt": 40, "tendency_drive": 30, "blk_tendency": 20, "reb_tendency": 10}
    label, value = rl._key_tendency_label(player)
    assert label == "pass tendency"
    assert value == 90


def test_key_tendency_label_defaults_to_50_when_missing():
    label, value = rl._key_tendency_label({})
    assert value == 50


# ---------------------------------------------------------------------------
# _overperforming_line_incoming / _overperforming_line_outgoing
# ---------------------------------------------------------------------------

def test_overperforming_line_incoming_fires_above_threshold():
    line = rl._overperforming_line_incoming("Star Player", 1.15, 82)
    assert line is not None
    assert "buy on the trend" in line


def test_overperforming_line_incoming_none_below_threshold():
    assert rl._overperforming_line_incoming("Star Player", 1.05, 82) is None


def test_overperforming_line_outgoing_fires_above_threshold():
    line = rl._overperforming_line_outgoing("Star Player", 1.12, 82)
    assert line is not None
    assert "sell-high window" in line


def test_overperforming_line_outgoing_none_below_threshold():
    assert rl._overperforming_line_outgoing("Star Player", 1.05, 82) is None


# ---------------------------------------------------------------------------
# _role_fit_line
# ---------------------------------------------------------------------------

def test_role_fit_line_mismatch():
    player = {"tendency_pass": 30, "ast_tendency": 30}
    line = rl._role_fit_line(player, "primary_initiator")
    assert line is not None
    assert "miscast" in line


def test_role_fit_line_match():
    player = {"tendency_pass": 90, "ast_tendency": 90}
    line = rl._role_fit_line(player, "primary_initiator")
    assert line is not None
    assert "clean fit" in line


def test_role_fit_line_unknown_role_returns_none():
    assert rl._role_fit_line({}, "not_a_real_role") is None


def test_role_fit_line_no_role_returns_none():
    assert rl._role_fit_line({}, "") is None


# ---------------------------------------------------------------------------
# _defense_quality_line
# ---------------------------------------------------------------------------

def test_defense_quality_line_elite_rim_protector():
    player = {"defensive_archetype": "rim_protector", "blk_tendency": 80}
    line = rl._defense_quality_line(player)
    assert line is not None
    assert "elite shot-blocker" in line


def test_defense_quality_line_tagged_but_numbers_dont_back_it_up():
    player = {"defensive_archetype": "rim_protector", "blk_tendency": 20}
    line = rl._defense_quality_line(player)
    assert line is not None
    assert "doesn't back it up" in line


def test_defense_quality_line_no_archetype_returns_none():
    assert rl._defense_quality_line({"defensive_archetype": "non_defender"}) is None
    assert rl._defense_quality_line({}) is None


# ---------------------------------------------------------------------------
# _window_fit_line_incoming / _window_fit_line_outgoing
# ---------------------------------------------------------------------------

def test_window_fit_line_incoming_rebuild_prime_alignment():
    player = {"age": 22}
    plan = {"horizon_seasons": 3, "goal": "rebuild"}
    line = rl._window_fit_line_incoming(player, plan)
    assert line is not None
    assert "prime years align" in line


def test_window_fit_line_incoming_rebuild_past_prime():
    player = {"age": 32}
    plan = {"horizon_seasons": 3, "goal": "rebuild"}
    line = rl._window_fit_line_incoming(player, plan)
    assert line is not None
    assert "past his prime" in line


def test_window_fit_line_incoming_win_now_prime():
    player = {"age": 27}
    plan = {"horizon_seasons": 2, "goal": "win_now"}
    line = rl._window_fit_line_incoming(player, plan)
    assert line is not None
    assert "in prime now" in line


def test_window_fit_line_incoming_win_now_rental():
    player = {"age": 34}
    plan = {"horizon_seasons": 2, "goal": "win_now"}
    line = rl._window_fit_line_incoming(player, plan)
    assert line is not None
    assert "rental" in line


def test_window_fit_line_incoming_no_match_returns_none():
    # win_now branch only fires for age <= 28 or age >= 33 -- 30 falls
    # in the gap and rebuild/tank isn't the goal, so nothing fires.
    player = {"age": 30}
    plan = {"horizon_seasons": 2, "goal": "win_now"}
    assert rl._window_fit_line_incoming(player, plan) is None


def test_window_fit_line_outgoing_rebuild_old_vet():
    player = {"age": 33}
    plan = {"goal": "rebuild"}
    line = rl._window_fit_line_outgoing(player, plan)
    assert line is not None
    assert "doesn't see our window" in line


def test_window_fit_line_outgoing_win_now_young_asset():
    player = {"age": 22}
    plan = {"goal": "win_now"}
    line = rl._window_fit_line_outgoing(player, plan)
    assert line is not None
    assert "future asset" in line


def test_window_fit_line_outgoing_no_match_returns_none():
    assert rl._window_fit_line_outgoing({"age": 27}, {"goal": "transition"}) is None


# ---------------------------------------------------------------------------
# _scheme_implication_line
# ---------------------------------------------------------------------------

def test_scheme_implication_line_primary_initiator_egalitarian():
    line = rl._scheme_implication_line("primary_initiator", "egalitarian")
    assert line is not None
    assert "egalitarian system" in line


def test_scheme_implication_line_no_match_returns_none():
    assert rl._scheme_implication_line("primary_initiator", "star_maxer") is None


def test_scheme_implication_line_missing_inputs_returns_none():
    assert rl._scheme_implication_line(None, "egalitarian") is None
    assert rl._scheme_implication_line("primary_initiator", None) is None


# ---------------------------------------------------------------------------
# _pick_context_bullet
# ---------------------------------------------------------------------------

def test_pick_context_bullet_returns_first_non_none():
    result = rl._pick_context_bullet({}, [None, None, "second candidate", "third"])
    assert result == "second candidate"


def test_pick_context_bullet_all_none_returns_none():
    assert rl._pick_context_bullet({}, [None, None]) is None


# ---------------------------------------------------------------------------
# _motivation_incoming
# ---------------------------------------------------------------------------

def _incoming_player(**overrides):
    base = {"first_name": "Test", "last_name": "Player", "position": "SG", "overall": 80, "age": 27}
    base.update(overrides)
    return base


def test_motivation_incoming_lead_role_upgrade_no_incumbent():
    line = rl._motivation_incoming(
        player=_incoming_player(overall=85),
        form_mod=1.0, stats={"ppg": 20.0, "rpg": 5.0, "apg": 4.0},
        plan={}, posture={}, depth=3, top_at_pos=None,
        current_team_code="LAL", current_role="unknown",
    )
    assert "slides right in as our primary" in line


def test_motivation_incoming_scarcity_fires_when_thin():
    line = rl._motivation_incoming(
        player=_incoming_player(overall=76),
        form_mod=1.0, stats={"ppg": 5.0, "rpg": 2.0, "apg": 1.0},
        plan={}, posture={}, depth=1, top_at_pos=None,
        current_team_code="LAL", current_role="unknown",
    )
    assert "thin at" in line


def test_motivation_incoming_rebuild_young_upside():
    line = rl._motivation_incoming(
        player=_incoming_player(age=22, overall=76, tendency_pass=70),
        form_mod=1.0, stats={"ppg": 5.0, "rpg": 2.0, "apg": 1.0},
        plan={"goal": "rebuild"}, posture={}, depth=3, top_at_pos={"overall": 90, "name": "Star"},
        current_team_code="LAL", current_role="unknown",
    )
    assert "rebuild needs" in line


def test_motivation_incoming_buy_low_fires():
    line = rl._motivation_incoming(
        player=_incoming_player(age=28, overall=80),
        form_mod=0.85, stats={"ppg": 5.0, "rpg": 2.0, "apg": 1.0},
        plan={"goal": "transition"}, posture={}, depth=3, top_at_pos={"overall": 90, "name": "Star"},
        current_team_code="LAL", current_role="unknown",
    )
    assert "market's undervaluing" in line


def test_motivation_incoming_fallback_rotation_line():
    line = rl._motivation_incoming(
        player=_incoming_player(age=28, overall=76),
        form_mod=1.0, stats={"ppg": 9.0, "rpg": 3.0, "apg": 2.0},
        plan={"goal": "transition"}, posture={}, depth=3, top_at_pos={"overall": 90, "name": "Star"},
        current_team_code="LAL", current_role="rotation",
    )
    assert "rounds out our rotation" in line


# ---------------------------------------------------------------------------
# _motivation_outgoing
# ---------------------------------------------------------------------------

def _outgoing_player(**overrides):
    base = {"first_name": "Test", "last_name": "Player", "position": "SG", "overall": 80, "age": 27}
    base.update(overrides)
    return base


def test_motivation_outgoing_underperforming_fires_first():
    line = rl._motivation_outgoing(
        player=_outgoing_player(), form_mod=0.85, plan={"goal": "transition"},
        posture={}, bucket="unlisted", pos_depth=2,
    )
    assert line is not None
    assert "underperforming" in line


def test_motivation_outgoing_age_timeline_mismatch():
    line = rl._motivation_outgoing(
        player=_outgoing_player(age=32), form_mod=1.0, plan={"goal": "rebuild", "horizon_seasons": 3},
        posture={}, bucket="unlisted", pos_depth=2,
    )
    assert line is not None
    assert "prime years are wasted" in line


def test_motivation_outgoing_surplus_bucket():
    line = rl._motivation_outgoing(
        player=_outgoing_player(age=27, overall=76), form_mod=1.0, plan={"goal": "transition"},
        posture={}, bucket="surplus", pos_depth=2,
    )
    assert line is not None
    assert "bucketed surplus" in line


def test_motivation_outgoing_core_or_unlisted_returns_none():
    line = rl._motivation_outgoing(
        player=_outgoing_player(age=27, overall=76), form_mod=1.0, plan={"goal": "transition"},
        posture={}, bucket="core", pos_depth=2,
    )
    assert line is None
