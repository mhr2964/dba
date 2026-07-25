"""
Tests for services.cpu_voter, covering PA6 (MVP team-component unification),
PA12 (DPOY center-penalty removal), and PA14 (real team-signal voter profile)
from the playoffs/awards/HOF realism audit
(docs/design/playoffs-awards-hof-logic-rules.md).

No dedicated test file existed for this module before this file. Pure
functions throughout -- no DB required.
"""
from __future__ import annotations

from services import awards_service
from services.cpu_voter import VoterProfile, get_cpu_profile, score_player_for_award


# ---------------------------------------------------------------------------
# PA14 -- get_cpu_profile derives a real, deterministic signal
# ---------------------------------------------------------------------------


def test_get_cpu_profile_winning_team_leans_winning():
    profile = get_cpu_profile(1, offense_rating=78, defense_rating=78, win_pct=0.65)
    assert profile == VoterProfile.WINNING


def test_get_cpu_profile_offense_heavy_team_leans_scorer():
    profile = get_cpu_profile(2, offense_rating=90, defense_rating=75, win_pct=0.45)
    assert profile == VoterProfile.SCORER


def test_get_cpu_profile_defense_heavy_team_leans_defense():
    profile = get_cpu_profile(3, offense_rating=75, defense_rating=90, win_pct=0.45)
    assert profile == VoterProfile.DEFENSE


def test_get_cpu_profile_balanced_non_winning_team_leans_efficiency():
    profile = get_cpu_profile(4, offense_rating=78, defense_rating=79, win_pct=0.45)
    assert profile == VoterProfile.EFFICIENCY


def test_get_cpu_profile_deterministic_for_same_inputs():
    """Same real signals in -> same profile out, regardless of team_id."""
    a = get_cpu_profile(11, offense_rating=90, defense_rating=75, win_pct=0.30)
    b = get_cpu_profile(97, offense_rating=90, defense_rating=75, win_pct=0.30)
    assert a == b == VoterProfile.SCORER


def test_get_cpu_profile_not_a_bare_modulo_of_team_id():
    """PA14 regression guard: two consecutive team_ids with IDENTICAL real
    signals must get the SAME profile -- pre-fix (`team_id % 4`), two
    consecutive ids would land on different enum members regardless of
    any real signal."""
    profiles = {
        get_cpu_profile(tid, offense_rating=75, defense_rating=75, win_pct=0.50)
        for tid in range(1, 9)
    }
    assert profiles == {VoterProfile.EFFICIENCY}


# ---------------------------------------------------------------------------
# PA12 -- DPOY no longer penalizes centers
# ---------------------------------------------------------------------------


def _dpoy_inputs(position: str) -> tuple[dict, dict, dict]:
    player = {"defense": 85, "fouls_per_game": 2.0, "position": position}
    stats = {"ppg": 15.0, "rpg": 10.0, "apg": 2.0, "spg": 1.0, "bpg": 2.5, "ts_pct": 0.58}
    team_record = {"wins": 45, "losses": 20}
    return player, stats, team_record


def test_dpoy_center_no_longer_penalized_vs_identical_wing_stats():
    """PA12: a center and a wing with IDENTICAL production/defense/fouls
    stats must score identically -- pre-fix, the center took a flat -3."""
    center_player, stats, team_record = _dpoy_inputs("C")
    wing_player, _, _ = _dpoy_inputs("SF")

    center_score = score_player_for_award(center_player, stats, team_record, "dpoy", VoterProfile.DEFENSE)
    wing_score = score_player_for_award(wing_player, stats, team_record, "dpoy", VoterProfile.DEFENSE)

    assert center_score == wing_score


# ---------------------------------------------------------------------------
# PA6 -- MVP branch now calls awards_service._mvp_team_adjustments
# ---------------------------------------------------------------------------


def test_mvp_score_uses_awards_service_team_adjustments():
    """The team-component of the MVP score must match
    awards_service._mvp_team_adjustments(win_pct, wins) exactly -- proving
    cpu_voter no longer duplicates the formula inline."""
    player = {"team_conf_rank": 3}
    stats = {"ppg": 25.0, "apg": 6.0, "rpg": 7.0, "spg": 1.0, "bpg": 0.5, "ts_pct": 0.60}
    team_record = {"wins": 50, "losses": 32}

    score = score_player_for_award(player, stats, team_record, "mvp", VoterProfile.EFFICIENCY)

    win_pct = 50 / 82
    expected_team_component = awards_service._mvp_team_adjustments(win_pct, 50)
    expected_base = 25.0 * 1.0 + 6.0 * 0.6 + 7.0 * 0.4 + 0.60 * 10 + expected_team_component
    # conf_rank=3 -> no conf_rank adjustment; profile EFFICIENCY adds ts_pct*0.5
    expected = expected_base + 0.60 * 0.5

    assert abs(score - expected) < 1e-9


def test_mvp_tank_team_floor_applies_via_shared_helper():
    """A sub-25-win team's MVP candidate must reflect the tank-team floor
    that now lives in awards_service._mvp_team_adjustments. team_conf_rank is
    set to a value that doesn't itself trigger the separate conf_rank gate,
    isolating the floor's own effect."""
    player = {"team_conf_rank": 3}
    stats = {"ppg": 30.0, "apg": 8.0, "rpg": 9.0, "spg": 1.0, "bpg": 0.5, "ts_pct": 0.60}
    team_record = {"wins": 15, "losses": 40}

    score = score_player_for_award(player, stats, team_record, "mvp", VoterProfile.EFFICIENCY)

    win_pct = 15 / 55
    expected_team_component = awards_service._mvp_team_adjustments(win_pct, 15)
    assert expected_team_component == -15.0, "wins < 25 should floor the team-component at -15"
    expected = (30.0 * 1.0 + 8.0 * 0.6 + 9.0 * 0.4 + 0.60 * 10 + expected_team_component) + 0.60 * 0.5

    assert abs(score - expected) < 1e-9
    # A tank-team stat-stuffer should land well below a real MVP-caliber score
    # (45-55 range per _mvp_team_adjustments' own docstring).
    assert score < 35.0
