from __future__ import annotations

import asyncpg

from data.repositories import strategy_repo
from services.sim_engine import _scheme_fit_factor


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

    When `override_strategy` is provided, the DB read is skipped entirely --
    modifiers are computed directly from the override dict. This path is used
    by the CPU coach so the DB is not consulted mid-sim.

    CA7 (coaching AI realism sweep): this used to also run auto-strategy
    inference (services/auto_strategy.py, since deleted) when a team's
    strategy was all defaults and a player list was provided -- confirmed
    dead code, since the only real caller that ever passes `players=`
    (sim_orchestrator._build_pre_sim_inputs) ALWAYS also passes
    `override_strategy`, which short-circuits before that branch could ever
    run. `archetype_label`/`get_team_archetype_label` are kept as an
    always-None stub below since sim_content_pipeline.py's Pat Chen
    enrichment still calls the latter and already handles a None result.

    When `players` is provided (regardless of override_strategy), it is used
    to condition the press/switch_all defensive-scheme modifiers on the team's own
    roster (finding #2, realism audit): press's opp_turnover_adj benefit and its
    new foul_adj downside scale with the roster's average `speed`; switch_all's
    ppp_defense_mult/opp_three_rate_adj scale with the roster's average `defense`/
    `defensive_effort`. Without `players`, both schemes fall back to their old flat
    magnitudes (preserves behavior for callers that don't have roster data handy,
    e.g. bot/cogs/strategy_cog.py's preview embeds).
    """
    if override_strategy is not None:
        strategy = override_strategy
    else:
        strategy = await strategy_repo.get_strategy(pool, league_id, team_id)

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

    # Finding #2 personnel conditioning: own-roster averages for the press/
    # switch_all magnitude gates below. None when no roster was supplied.
    roster_avg_speed: float | None = None
    roster_avg_defense: float | None = None
    roster_avg_defensive_effort: float | None = None
    if players:
        _top8 = sorted(players, key=lambda p: p.get("overall", 50), reverse=True)[:8]
        _n = len(_top8)
        roster_avg_speed = sum(p.get("speed", 50) for p in _top8) / _n
        roster_avg_defense = sum(p.get("defense", 50) for p in _top8) / _n
        roster_avg_defensive_effort = sum(p.get("defensive_effort", 50) for p in _top8) / _n

    # PACE
    pace = strategy["offensive_pace"]
    if pace == "slow":
        pace_adjustment += -6.0
    elif pace == "fast":
        pace_adjustment += 5.0
    elif pace == "run_and_gun":
        pace_adjustment += 10.0
        turnover_adj += 1.5

    # TRANSITION AGGRESSION (CA4, coaching AI realism sweep): this column
    # existed on team_strategies with a DB default and was passed through this
    # function's return dict, but never converted into an actual pace/turnover
    # modifier -- every team ran neutral regardless of setting. Magnitudes are
    # disclosed placeholders, deliberately smaller than offensive_pace's primary
    # lever above (+-5/+-6, up to +10 for run_and_gun) since this is a secondary,
    # transition-specific nudge on top of the team's main pace choice.
    transition = strategy.get("transition_aggression", "balanced")
    if transition == "crash":
        pace_adjustment += 2.0
        turnover_adj += 0.3
    elif transition == "retreat":
        pace_adjustment += -2.0
        turnover_adj += -0.3

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
        if roster_avg_speed is not None:
            # Finding #2: press had zero downside and a flat benefit regardless of
            # personnel -- a team of slow bigs pressed just as "effectively" as a
            # track team. speed_fit scales the turnover-forcing benefit down (never
            # to zero -- even a slow team rushes the opponent a little) and adds a
            # real foul-rate cost (fatigue/closeout scramble) for a team that can't
            # actually keep up full-court. 50/70 mirrors the switch_all gate's
            # low/high shape at a lower band, since speed's raw-rating scale here
            # is being read continuously rather than as a hard pass/fail cutoff.
            speed_fit = _scheme_fit_factor(roster_avg_speed, low=50.0, high=70.0)
            opp_turnover_adj += 0.05 * (0.4 + 0.6 * speed_fit)
            foul_adj += 1.5 * (1.0 - speed_fit)
        else:
            opp_turnover_adj += 0.05
    elif defense == "switch_all":
        # Switch-all creates mismatches: slight ppp concession, opens more 3s for opponent.
        if roster_avg_defense is not None and roster_avg_defensive_effort is not None:
            # Finding #2: this used to be the same flat concession for every
            # roster. A genuinely versatile, high-defense roster (the same
            # >=74 defense / >=60 defensive_effort bar the finding #1 selection
            # gate uses) can switch across positions with minimal cost -- the
            # concession shrinks toward neutral (1.00) and the extra open looks
            # shrink toward zero. A roster that only barely cleared the
            # selection gate keeps the full flat concession as its floor.
            defense_fit = min(
                _scheme_fit_factor(roster_avg_defense, low=50.0, high=74.0),
                _scheme_fit_factor(roster_avg_defensive_effort, low=40.0, high=60.0),
            )
            ppp_defense_mult *= 0.98 + 0.02 * defense_fit
            opp_three_rate_adj += 0.07 * (1.0 - defense_fit)
        else:
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
        # CA7: archetype inference (services/auto_strategy.py) was deleted as
        # confirmed dead code -- this key is always None now. Kept in the
        # return shape so existing callers (e.g. sim_content_pipeline.py's
        # Pat Chen enrichment) don't need a contract change.
        "archetype_label": None,
        "offensive_scheme": scheme,
        "defensive_scheme": defense,
        "transition_aggression": strategy.get("transition_aggression", "balanced"),
        "bench_leash": strategy.get("bench_leash", "normal"),
    }


def get_team_archetype_label(league_id: int, team_id: int) -> str | None:
    """Always None post-CA7 -- auto-strategy archetype inference (the only
    thing that ever populated this) was deleted as confirmed dead code.
    Kept as a stub so sim_content_pipeline.py's Pat Chen enrichment (its only
    caller) doesn't need a contract change; it already treats a None result
    as "no archetype label available" and filters it out."""
    return None
