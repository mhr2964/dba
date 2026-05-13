from __future__ import annotations

import asyncpg

from data.repositories import strategy_repo


async def get_sim_modifiers(pool: asyncpg.Pool, league_id: int, team_id: int) -> dict:
    """
    Returns a dict of modifier values that sim_engine applies on top of base math.
    All values default to zero/one (no effect) when no strategy row exists.
    """
    strategy = await strategy_repo.get_strategy(pool, league_id, team_id)

    pace_adjustment: float = 0.0
    ppp_offense_mult: float = 1.0
    ppp_defense_mult: float = 1.0
    three_rate_adj: float = 0.0
    turnover_adj: float = 0.0
    foul_adj: float = 0.0

    # PACE
    pace = strategy["offensive_pace"]
    if pace == "slow":
        pace_adjustment += -6.0
    elif pace == "fast":
        pace_adjustment += 5.0
    elif pace == "run_and_gun":
        pace_adjustment += 10.0
        turnover_adj += 1.5

    # OFFENSIVE SCHEME
    scheme = strategy["offensive_scheme"]
    if scheme == "isolation":
        ppp_offense_mult *= 1.03
        three_rate_adj += -0.05
    elif scheme == "ball_movement":
        ppp_offense_mult *= 1.05
        three_rate_adj += 0.05
    elif scheme == "three_heavy":
        ppp_offense_mult *= 0.97
        three_rate_adj += 0.18
    elif scheme == "inside_out":
        three_rate_adj += -0.08
    # balanced: no adjustments

    # DEFENSIVE SCHEME
    defense = strategy["defensive_scheme"]
    if defense == "zone":
        ppp_defense_mult *= 1.03
        three_rate_adj += -0.08
    elif defense == "press":
        pace_adjustment += 3.0
    elif defense == "switch_all":
        ppp_defense_mult *= 1.05
        three_rate_adj += 0.05
    # man_to_man: no adjustment

    # DEFENSIVE INTENSITY
    intensity = strategy["defensive_intensity"]
    if intensity == "conservative":
        foul_adj += -1.5
        ppp_defense_mult *= 0.97
    elif intensity == "aggressive":
        foul_adj += 1.5
        ppp_defense_mult *= 1.04
        turnover_adj += 0.8
    # normal: no adjustment

    # STAR USAGE: 0–100 maps to star_usage_mult 0.80–1.30
    star_usage_val: int = strategy["star_usage"]
    star_usage_mult: float = 0.80 + (star_usage_val / 100.0) * 0.50

    # isolation stacks on top of the base star_usage_mult
    if scheme == "isolation":
        star_usage_mult *= 1.25
    elif scheme == "ball_movement":
        star_usage_mult *= 0.85

    return {
        "pace_adjustment": pace_adjustment,
        "ppp_offense_mult": ppp_offense_mult,
        "ppp_defense_mult": ppp_defense_mult,
        "three_rate_adj": three_rate_adj,
        "turnover_adj": turnover_adj,
        "foul_adj": foul_adj,
        "star_usage_mult": star_usage_mult,
    }
