from __future__ import annotations

import datetime
from enum import Enum


class VoterProfile(str, Enum):
    SCORER = "scorer"
    EFFICIENCY = "efficiency"
    DEFENSE = "defense"
    WINNING = "winning"


def get_cpu_profile(team_id: int) -> VoterProfile:
    """Deterministic from team_id so it doesn't change between sessions."""
    profiles = list(VoterProfile)
    return profiles[team_id % len(profiles)]


def score_player_for_award(
    player: dict,
    box_score_stats: dict,
    team_record: dict,
    award_type: str,
    profile: VoterProfile,
) -> float:
    """
    Returns a float score for this player for this award under this profile.

    box_score_stats keys: ppg, rpg, apg, spg, bpg, fg_pct, ts_pct
    team_record keys: wins, losses
    """
    ppg = box_score_stats.get("ppg", 0.0)
    apg = box_score_stats.get("apg", 0.0)
    spg = box_score_stats.get("spg", 0.0)
    bpg = box_score_stats.get("bpg", 0.0)
    ts_pct = box_score_stats.get("ts_pct", 0.0)
    wins = team_record.get("wins", 0)

    if award_type == "dpoy":
        # Eligibility: players with defense < 65 and age > 37 are excluded upstream
        # in _get_eligible_players. Defense attribute is passed via player dict.
        defense_attr = player.get("defense", 75)
        defense_boost = (defense_attr / 99) * 8

        # --- Age curve: subtract (age - 33)^1.5 for players over 33 ---
        # LeBron at 40 → -(7^1.5) ≈ -18.5; 36yo → -(3^1.5) ≈ -5.2; ≤33 → 0.
        age_penalty = 0.0
        birth = player.get("birth_date")
        if birth is not None:
            season_val = player.get("_season", datetime.date.today().year)
            age = (datetime.date(season_val, 10, 1) - birth).days / 365.25
            age_penalty = max(0.0, (age - 33) ** 1.5)

        # --- Role-based penalties / boosts ---
        # Offensive-first roles don't belong on DPOY ballots.
        _OFFENSIVE_ROLE_PENALTY_TAGS = frozenset({
            "iso_scorer", "primary_initiator", "slashing_lead", "movement_shooter",
            "secondary_creator", "wing_creator", "spark_plug_scorer",
            "transition_engine", "catch_and_shoot", "floor_spacer",
        })
        # Defense-first and two-way roles get a boost.
        _DEFENSIVE_ROLE_BOOST_TAGS = frozenset({
            "rim_protector", "wing_stopper", "on_ball_pest", "switching_big",
            "two_way_wing", "two_way_big",
        })
        role_tag = player.get("role_tag") or ""
        if role_tag in _OFFENSIVE_ROLE_PENALTY_TAGS:
            role_adjust = -12.0
        elif role_tag in _DEFENSIVE_ROLE_BOOST_TAGS:
            role_adjust = 8.0
        else:
            role_adjust = 0.0

        # --- Foul-rate penalty ---
        # Foul-prone players are poor DPOY candidates.
        fouls_pg = float(player.get("fouls_per_game") or 0.0)
        foul_penalty = max(0.0, (fouls_pg - 3.0) * 2.5)

        # --- Center positional penalty ---
        # Kept but reduced: position alone shouldn't override role/stats.
        position = (player.get("position") or "").upper()
        position_penalty = -3 if position == "C" else 0

        # Bumped stat weights: steal and block leaders now clearly outrank
        # general-rep candidates.
        base = spg * 3.5 + bpg * 3.0 + defense_boost + position_penalty
        base -= age_penalty + foul_penalty
        base += role_adjust

        # Team defensive rating boost: added by caller when team is top-10 in
        # points allowed; the voter profiles add small profile-specific tiebreakers.
        team_def_boost = player.get("_team_def_boost", 0)
        base += team_def_boost
        if profile == VoterProfile.SCORER:
            return base + ppg * 0.05
        if profile == VoterProfile.EFFICIENCY:
            return base + ts_pct * 0.5
        if profile == VoterProfile.DEFENSE:
            return base * 1.1
        # WINNING
        total_games = (wins + player.get("_team_losses", 0)) or 1
        win_pct = wins / total_games
        return base + win_pct * 0.5

    if award_type == "roy":
        # ROY: ppg-centric; eligibility (is_rookie) enforced upstream
        base = ppg * 0.6 + apg * 0.2 + spg * 0.5
        if profile == VoterProfile.EFFICIENCY:
            return ts_pct * 30 + apg * 0.3
        if profile == VoterProfile.WINNING:
            return base + wins * 0.2
        return base

    if award_type == "6moy":
        # Bench scoring; eligibility (not majority starter) enforced upstream
        base = ppg * 0.7 + apg * 0.15
        if profile == VoterProfile.EFFICIENCY:
            return ts_pct * 40 + apg * 0.2
        if profile == VoterProfile.WINNING:
            return base + wins * 0.15
        return base

    # MVP / All-NBA / All-Star — all profiles use the canonical MVP composite as
    # the base so race leaders and final vote agree on top candidates.
    #
    # Canonical formula (kept in sync with awards_service._mvp_team_adjustments):
    #   ppg*1.0 + apg*0.6 + rpg*0.4 + ts_pct*10 + win_pct*32
    #   sub-.500 penalty: -(0.500 - win_pct)*25 when win_pct < 0.500
    #   tank-team cap: score capped at 35 when wins < 25
    rpg = box_score_stats.get("rpg", 0.0)
    losses = team_record.get("losses", 0)
    total_games = (wins + losses) or 1
    win_pct = wins / total_games

    team_component = win_pct * 32
    if win_pct < 0.500:
        team_component -= (0.500 - win_pct) * 25

    base_mvp = ppg * 1.0 + apg * 0.6 + rpg * 0.4 + ts_pct * 10 + team_component

    # Tank-team hard cap: prevents stat-padders on terrible teams from winning.
    if wins < 25:
        base_mvp = min(base_mvp, 35.0)

    # Profile adds a small tiebreaker (5% swing) without overriding rankings.
    if profile == VoterProfile.SCORER:
        return base_mvp + ppg * 0.05
    if profile == VoterProfile.EFFICIENCY:
        return base_mvp + ts_pct * 0.5
    if profile == VoterProfile.DEFENSE:
        return base_mvp + (spg + bpg) * 0.1
    # WINNING
    return base_mvp + win_pct * 0.5
