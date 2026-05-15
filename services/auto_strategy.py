"""
Infer a team's best-fit strategy based on its current roster's attribute profile.
Called at sim time when a team has no explicit strategy set (auto/balanced defaults).
"""
from __future__ import annotations


def infer_archetype(players: list[dict]) -> dict:
    """
    players: list of player attribute dicts (overall, shooting_3pt, finishing,
             playmaking, defense, tendency_3pt, tendency_post, tendency_pass,
             tendency_drive, defensive_effort, rebounding, usage_weight)

    Uses the top-8 players by overall (or all players if fewer than 8).
    Returns a strategy dict matching strategy_service expected keys, plus:
      - archetype_label: human-readable label for display / Pat Chen enrichment.
    """
    if not players:
        return _fallback_strategy()

    # Sort by overall rating descending; use top 8 for archetype detection.
    sorted_players = sorted(players, key=lambda p: p.get("overall", p.get("finishing", 50)), reverse=True)
    top = sorted_players[:8]

    n = len(top)

    def avg(attr: str) -> float:
        return sum(p.get(attr, 50) for p in top) / n

    # ---- Offensive archetype (priority: isolation > three_heavy > inside_out > ball_movement) ----
    offensive_scheme: str
    star_usage: int
    offensive_pace: str
    off_label: str

    # Isolation: any single player is elite (OVR >= 90) AND is a high-usage option.
    has_isolation_star = any(
        p.get("overall", p.get("finishing", 50)) >= 90
        and p.get("usage_weight", 50) >= 70
        for p in top
    )
    if has_isolation_star:
        offensive_scheme = "isolation"
        star_usage = 80
        offensive_pace = "balanced"
        off_label = "Isolation-Heavy"
    elif avg("shooting_3pt") >= 74 and avg("tendency_3pt") >= 58:
        offensive_scheme = "three_heavy"
        star_usage = 55
        offensive_pace = "fast"
        off_label = "Three-Point Barrage"
    elif avg("tendency_post") >= 55 or (avg("finishing") >= 74 and avg("shooting_3pt") < 65):
        offensive_scheme = "inside_out"
        star_usage = 55
        offensive_pace = "slow"
        off_label = "Inside-Out"
    elif avg("playmaking") >= 74 and avg("tendency_pass") >= 55:
        offensive_scheme = "ball_movement"
        star_usage = 40
        offensive_pace = "fast"
        off_label = "Ball Movement"
    else:
        offensive_scheme = "balanced"
        star_usage = 50
        offensive_pace = "balanced"
        off_label = "Balanced"

    # ---- Defensive archetype (independent of offensive) ----
    defensive_scheme: str
    defensive_intensity: str
    def_label: str

    if avg("defense") >= 74 and avg("defensive_effort") >= 60:
        defensive_scheme = "switch_all"
        defensive_intensity = "aggressive"
        def_label = "Switch-All"
    elif avg("rebounding") >= 74:
        defensive_scheme = "man_to_man"
        defensive_intensity = "normal"
        def_label = "Physical"
    else:
        defensive_scheme = "man_to_man"
        defensive_intensity = "normal"
        def_label = "Man-to-Man"

    archetype_label = f"{off_label} / {def_label} (Auto)"

    return {
        "offensive_scheme": offensive_scheme,
        "star_usage": star_usage,
        "offensive_pace": offensive_pace,
        "defensive_scheme": defensive_scheme,
        "defensive_intensity": defensive_intensity,
        "archetype_label": archetype_label,
    }


def _fallback_strategy() -> dict:
    return {
        "offensive_scheme": "balanced",
        "star_usage": 50,
        "offensive_pace": "balanced",
        "defensive_scheme": "man_to_man",
        "defensive_intensity": "normal",
        "archetype_label": "Balanced / Man-to-Man (Auto)",
    }
