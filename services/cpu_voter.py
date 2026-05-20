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
        # Eligibility: players with defense < 65 are excluded upstream
        # in _get_eligible_players. Defense attribute is passed via player dict.
        # Age is NOT a DPOY eligibility factor — voters evaluate actual defensive output.
        defense_attr = player.get("defense", 75)
        # Halved from *8 → *4: attribute alone shouldn't beat a stat-producer.
        # A 90-rated defender gets +3.6 vs a 75-rated one at +3.0 — small spread
        # that preserves ordering without drowning out spg/bpg production.
        defense_boost = (defense_attr / 99) * 4

        # --- Foul-rate penalty ---
        # Foul-prone players are poor DPOY candidates.
        fouls_pg = float(player.get("fouls_per_game") or 0.0)
        foul_penalty = max(0.0, (fouls_pg - 3.0) * 2.5)

        # --- Center positional penalty ---
        # Kept but reduced: position alone shouldn't override role/stats.
        position = (player.get("position") or "").upper()
        position_penalty = -3 if position == "C" else 0

        # Raised stat weights: produced defensive stats now clearly outrank
        # attribute-only reputation. spg: 3.5 → 4.5, bpg: 3.0 → 4.0.
        base = spg * 4.5 + bpg * 4.0 + defense_boost + position_penalty
        base -= foul_penalty

        # Team defensive rating boost: softened from +5 → +3.
        # Being on a good-defense team is correlated but not causal;
        # a full +5 was too large relative to individual stat contributions.
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
