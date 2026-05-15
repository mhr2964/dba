from __future__ import annotations

import asyncpg

from data.repositories import strategy_repo
from services.auto_strategy import infer_archetype

# Module-level inference cache keyed by (league_id, team_id).
# Cleared at the start of each batch run via clear_archetype_cache().
_archetype_cache: dict[tuple[int, int], dict] = {}


def clear_archetype_cache() -> None:
    """Call once at the start of a batch sim run so stale inferences don't persist."""
    _archetype_cache.clear()


async def get_sim_modifiers(
    pool: asyncpg.Pool,
    league_id: int,
    team_id: int,
    players: list[dict] | None = None,
) -> dict:
    """
    Returns a dict of modifier values that sim_engine applies on top of base math.
    All values default to zero/one (no effect) when no strategy row exists.

    When `players` is provided and the team's strategy is all defaults (indicating
    no custom strategy has been set), auto-strategy inference is applied instead.
    The returned dict includes `archetype_label` for Pat Chen enrichment.
    """
    strategy = await strategy_repo.get_strategy(pool, league_id, team_id)

    # Auto-strategy inference: if the team has no custom strategy (all fields are
    # defaults) and a player list was provided, infer from roster attributes.
    archetype_label: str | None = None
    _is_default = (
        strategy["offensive_pace"] == "balanced"
        and strategy["offensive_scheme"] == "balanced"
        and strategy["defensive_scheme"] == "man_to_man"
        and strategy["defensive_intensity"] == "normal"
    )
    if _is_default and players:
        cache_key = (league_id, team_id)
        if cache_key not in _archetype_cache:
            _archetype_cache[cache_key] = infer_archetype(players)
        inferred = _archetype_cache[cache_key]
        # Overlay inferred values on top of the defaults so any partial custom
        # setting would still take precedence (though here all are default).
        strategy = dict(strategy)
        strategy["offensive_scheme"] = inferred["offensive_scheme"]
        strategy["offensive_pace"] = inferred["offensive_pace"]
        strategy["defensive_scheme"] = inferred["defensive_scheme"]
        strategy["defensive_intensity"] = inferred["defensive_intensity"]
        strategy["star_usage"] = inferred["star_usage"]
        archetype_label = inferred["archetype_label"]

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
        "archetype_label": archetype_label,
    }


def get_team_archetype_label(league_id: int, team_id: int) -> str | None:
    """Return the cached archetype label for a team if inference was run this batch."""
    return _archetype_cache.get((league_id, team_id), {}).get("archetype_label")
