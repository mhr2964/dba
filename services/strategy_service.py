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
    override_strategy: dict | None = None,
) -> dict:
    """
    Returns a dict of modifier values that sim_engine applies on top of base math.
    All values default to zero/one (no effect) when no strategy row exists.

    When `override_strategy` is provided, the DB read and archetype cache are
    skipped entirely — modifiers are computed directly from the override dict.
    This path is used by the CPU coach so the DB is not consulted mid-sim.

    When `players` is provided and the team's strategy is all defaults (indicating
    no custom strategy has been set), auto-strategy inference is applied instead.
    The returned dict includes `archetype_label` for Pat Chen enrichment.
    """
    archetype_label: str | None = None

    if override_strategy is not None:
        strategy = override_strategy
    else:
        strategy = await strategy_repo.get_strategy(pool, league_id, team_id)

        # Auto-strategy inference: if the team has no custom strategy (all fields are
        # defaults) and a player list was provided, infer from roster attributes.
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
    # Opponent-facing adjustments written by defensive schemes so they land on the
    # right team's box in sim_game (zone suppresses OPPONENT 3s, not own team's).
    opp_three_rate_adj: float = 0.0
    opp_turnover_adj: float = 0.0

    # PACE
    pace = strategy["offensive_pace"]
    if pace == "slow":
        pace_adjustment += -6.0
    elif pace == "fast":
        pace_adjustment += 5.0
    elif pace == "run_and_gun":
        pace_adjustment += 10.0
        turnover_adj += 1.5

    # OFFENSIVE SCHEME — calibrated 2026-05-18 to keep top scorers in the
    # NBA-realistic 28-35 PPG band. Previous magnitudes pushed elites to 40+ PPG.
    scheme = strategy["offensive_scheme"]
    if scheme == "isolation":
        ppp_offense_mult *= 1.04
        three_rate_adj += -0.08
    elif scheme == "ball_movement":
        ppp_offense_mult *= 1.06
        three_rate_adj += 0.06
    elif scheme == "three_heavy":
        ppp_offense_mult *= 0.98
        three_rate_adj += 0.22
    elif scheme == "inside_out":
        ppp_offense_mult *= 1.03
        three_rate_adj += -0.12
    # balanced: no adjustments (ppp_offense_mult stays 1.0, three_rate_adj stays 0)

    # DEFENSIVE SCHEME — opponent-facing effects go to opp_* fields so sim_game
    # can apply them to the correct (opposing) team's box build.
    defense = strategy["defensive_scheme"]
    if defense == "zone":
        # Zone shells up under 3-point line: suppresses opponent 3-pt rate, cuts ppp.
        ppp_defense_mult *= 1.05
        opp_three_rate_adj += -0.12
    elif defense == "press":
        # Full-court press raises pace and forces extra turnovers on the opponent.
        pace_adjustment += 4.0
        opp_turnover_adj += 0.05
    elif defense == "switch_all":
        # Switch-all creates mismatches: slight ppp concession, opens more 3s for opponent.
        ppp_defense_mult *= 0.98
        opp_three_rate_adj += 0.07
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

    # isolation stacks on top of the base star_usage_mult — calibrated lower
    # twice (1.30 → 1.18 → 1.12) to keep top scorers in real NBA's 28-33 band.
    if scheme == "isolation":
        star_usage_mult *= 1.12
    elif scheme == "ball_movement":
        star_usage_mult *= 0.85

    return {
        "pace_adjustment": pace_adjustment,
        "ppp_offense_mult": ppp_offense_mult,
        "ppp_defense_mult": ppp_defense_mult,
        "three_rate_adj": three_rate_adj,
        "opp_three_rate_adj": opp_three_rate_adj,
        "turnover_adj": turnover_adj,
        "opp_turnover_adj": opp_turnover_adj,
        "foul_adj": foul_adj,
        "star_usage_mult": star_usage_mult,
        "archetype_label": archetype_label,
        "offensive_scheme": scheme,
        "defensive_scheme": defense,
        "transition_aggression": strategy.get("transition_aggression", "balanced"),
        "bench_leash": strategy.get("bench_leash", "normal"),
    }


def get_team_archetype_label(league_id: int, team_id: int) -> str | None:
    """Return the cached archetype label for a team if inference was run this batch."""
    return _archetype_cache.get((league_id, team_id), {}).get("archetype_label")
