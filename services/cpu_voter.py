from __future__ import annotations

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
        # Eligibility: players with defense < 65 are excluded upstream in
        # _get_eligible_players. Defense attribute is passed via player dict.
        defense_attr = player.get("defense", 75)
        defense_boost = (defense_attr / 99) * 8
        # Center positional penalty: real DPOY almost always goes to a wing or
        # rim protector, not a center who isn't elite defensively.
        position = (player.get("position") or "").upper()
        position_penalty = -3 if position == "C" else 0
        base = spg * 2.5 + bpg * 2.0 + defense_boost + position_penalty
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
    # Canonical: ppg*1.0 + apg*0.6 + rpg*0.4 + team_win_pct*20 + ts_pct*10
    rpg = box_score_stats.get("rpg", 0.0)
    total_games = (wins + team_record.get("losses", 0)) or 1
    win_pct = wins / total_games
    base_mvp = ppg * 1.0 + apg * 0.6 + rpg * 0.4 + win_pct * 20 + ts_pct * 10
    # Profile adds a small tiebreaker (5% swing) without overriding rankings.
    if profile == VoterProfile.SCORER:
        return base_mvp + ppg * 0.05
    if profile == VoterProfile.EFFICIENCY:
        return base_mvp + ts_pct * 0.5
    if profile == VoterProfile.DEFENSE:
        return base_mvp + (spg + bpg) * 0.1
    # WINNING
    return base_mvp + win_pct * 0.5
