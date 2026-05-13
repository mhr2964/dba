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
        # All profiles converge on defensive stats; profile adds a small tiebreaker.
        base = spg * 3.0 + bpg * 2.5
        if profile == VoterProfile.SCORER:
            return base + ppg * 0.05
        if profile == VoterProfile.EFFICIENCY:
            return base + ts_pct * 5
        if profile == VoterProfile.DEFENSE:
            return base * 1.3
        # WINNING
        return base + wins * 0.1

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

    # MVP / All-NBA / All-Star — all use MVP weights
    if profile == VoterProfile.SCORER:
        return ppg * 0.5 + apg * 0.2 + wins * 0.3
    if profile == VoterProfile.EFFICIENCY:
        return ts_pct * 50 + apg * 0.3 + wins * 0.2
    if profile == VoterProfile.DEFENSE:
        return spg * 2.0 + bpg * 1.5 + wins * 0.3 + ppg * 0.1
    # WINNING
    return wins * 0.6 + ppg * 0.2 + apg * 0.2
