from __future__ import annotations

import datetime
import os
import random
from types import SimpleNamespace

from data.repositories import gameplan_repo

_HEADLESS = os.environ.get("DBA_HEADLESS_MODE") == "1"


async def decide_gameplans(
    pool,
    league_id: int,
    season: int,
    game_row: dict,
    home_players: list[dict],
    away_players: list[dict],
) -> tuple[dict, dict]:
    home_team_id: int = game_row["home_team_id"]
    away_team_id: int = game_row["away_team_id"]

    home_team_row = await pool.fetchrow(
        "SELECT manager_user_id FROM teams WHERE id = $1", home_team_id
    )
    away_team_row = await pool.fetchrow(
        "SELECT manager_user_id FROM teams WHERE id = $1", away_team_id
    )

    home_gameplan = await _resolve_gameplan(
        pool, league_id, season, game_row, home_team_id, home_players, away_players, home_team_row
    )
    away_gameplan = await _resolve_gameplan(
        pool, league_id, season, game_row, away_team_id, away_players, home_players, away_team_row
    )
    return home_gameplan, away_gameplan


async def _resolve_gameplan(
    pool,
    league_id: int,
    season: int,
    game_row: dict,
    team_id: int,
    self_players: list[dict],
    opp_players: list[dict],
    team_row,
) -> dict:
    manager_user_id = team_row["manager_user_id"] if team_row is not None else None
    strategy_row = await pool.fetchrow(
        "SELECT coach_mode FROM team_strategies WHERE league_id = $1 AND team_id = $2",
        league_id,
        team_id,
    )
    if strategy_row is not None:
        coach_mode = strategy_row["coach_mode"] or "manual"
    else:
        coach_mode = "manual" if manager_user_id is not None else "auto"
    is_human = manager_user_id is not None and coach_mode == "manual"
    if is_human:
        return await _load_human_gameplan(pool, league_id, team_id, self_players)
    return await _compute_cpu_gameplan(pool, league_id, season, game_row, team_id, self_players, opp_players)


async def _load_human_gameplan(pool, league_id: int, team_id: int, players: list[dict]) -> dict:
    # CA4 (coaching AI realism sweep): bench_leash/transition_aggression were
    # missing from this SELECT too (same class of bug as strategy_repo.get_strategy) --
    # a human coach's chosen leash/transition setting was silently dropped every game.
    strategy_row = await pool.fetchrow(
        """
        SELECT offensive_pace, offensive_scheme, defensive_scheme,
               defensive_intensity, star_usage, bench_leash, transition_aggression
        FROM team_strategies
        WHERE league_id = $1 AND team_id = $2
        """,
        league_id,
        team_id,
    )
    strategy = dict(strategy_row) if strategy_row else {
        "offensive_pace": "balanced",
        "offensive_scheme": "balanced",
        "defensive_scheme": "man_to_man",
        "defensive_intensity": "normal",
        "star_usage": 50,
        "bench_leash": "normal",
        "transition_aggression": "balanced",
    }

    directive_rows = await pool.fetch(
        """
        SELECT player_id, shot_diet, usage_mode, defense_mode, role_mode, clutch_mode
        FROM player_directives
        WHERE league_id = $1 AND player_id = ANY($2::int[])
        """,
        league_id,
        [p["id"] for p in players],
    )
    player_directives: dict[int, dict] = {
        r["player_id"]: {
            "shot_diet": r["shot_diet"] or "auto",
            "usage_mode": r["usage_mode"] or "normal",
            "defense_mode": r["defense_mode"] or "standard",
            "role_mode": r["role_mode"] or "spot_up",
            "clutch_mode": r["clutch_mode"] or "normal",
        }
        for r in directive_rows
    }
    return {
        "source": "human",
        "strategy": strategy,
        "player_directives": player_directives,
        "rationale": "",
    }


async def _compute_cpu_gameplan(
    pool,
    league_id: int,
    season: int,
    game_row: dict,
    team_id: int,
    self_players: list[dict],
    opp_players: list[dict],
) -> dict:
    rng = random.Random(game_row["id"] ^ team_id)

    top8_self = sorted(self_players, key=lambda p: p.get("overall", 50), reverse=True)[:8]
    top8_opp = sorted(opp_players, key=lambda p: p.get("overall", 50), reverse=True)[:8]

    self_analysis = _analyze_roster(top8_self)
    opp_analysis = _analyze_roster(top8_opp)

    form = await _get_form(pool, league_id, season, team_id)
    win_streak: int = form.get("win_streak", 0) or 0
    loss_streak: int = form.get("loss_streak", 0) or 0

    posture = await _classify_posture(pool, league_id, season, team_id)

    game_date = game_row.get("scheduled_date")
    back_to_back = await _is_back_to_back(pool, league_id, season, team_id, game_date)

    hot_cold = await _get_hot_cold(pool, top8_self[:5], season)

    # CA5 (coaching AI realism sweep): scheme stickiness. get_scheme_history
    # already existed for Coach Beat's columnist copy ("has this team run this
    # scheme before, did it work") but was never read here -- the CPU picked a
    # fresh scheme every game with zero memory of its own recent tendencies.
    # Degrades gracefully to {} (no bonus) early in a season with no simmed
    # games yet, same as its existing caller.
    scheme_history = await gameplan_repo.get_scheme_history(pool, league_id, season, team_id)

    strategy, rationale_parts = _decide_strategy(
        self_analysis, opp_analysis, posture, back_to_back, win_streak, loss_streak, rng,
        scheme_history=scheme_history,
    )

    player_directives = _decide_player_directives(
        top8_self, strategy, posture, hot_cold, rng
    )

    rationale = _build_rationale(rationale_parts, top8_self, strategy, posture, win_streak, loss_streak)

    if _HEADLESS:
        try:
            _team_row = await pool.fetchrow(
                "SELECT nba_team_code FROM teams WHERE id = $1", team_id
            )
            _opp_row = await pool.fetchrow(
                "SELECT nba_team_code FROM teams WHERE id = $1",
                game_row.get("home_team_id") if game_row.get("home_team_id") != team_id else game_row.get("away_team_id"),
            )
            _tc = _team_row["nba_team_code"] if _team_row else str(team_id)
            _oc = _opp_row["nba_team_code"] if _opp_row else "OPP"
            _strat = strategy
            _r_parts = "; ".join(rationale_parts) if rationale_parts else "no specific reasoning"
            print(
                f"CPU [{_tc}] — gameplan vs [{_oc}]\n"
                f"   offense: {_strat['offensive_scheme']} @ {_strat['offensive_pace']} pace\n"
                f"   defense: {_strat['defensive_scheme']} ({_strat['defensive_intensity']})\n"
                f"   posture: {posture} | star_usage: {_strat['star_usage']}\n"
                f"   rationale: {_r_parts}"
            )
        except Exception:
            pass  # never let logging break the sim

    return {
        "source": "cpu",
        "strategy": strategy,
        "player_directives": player_directives,
        "rationale": rationale,
    }


def _analyze_roster(top8: list[dict]) -> dict:
    if not top8:
        return {
            "avg_ovr": 70,
            "has_elite_passer": False,
            "has_elite_scorer": False,
            "has_elite_big": False,
            "three_point_count": 0,
            "has_isolation_star": False,
            "elite_passer_name": None,
            "isolation_star_name": None,
            "elite_big_name": None,
            "elite_scorer_name": None,
            "avg_speed": 50.0,
            "avg_defense": 50.0,
            "avg_defensive_effort": 50.0,
        }
    n = len(top8)

    def avg(attr: str) -> float:
        return sum(p.get(attr, 50) for p in top8) / n

    def _last(p: dict) -> str:
        full = p.get("name", "") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        return full.split()[-1] if full else ""

    avg_ovr = avg("overall")
    # Finding #1 (realism audit): personnel inputs for the defensive-scheme
    # gate in _decide_strategy -- press requires team speed, switch_all
    # requires team defense/versatility. Mirrors the >=74 raw-rating /
    # >=60 tendency-style thresholds auto_strategy.infer_archetype already
    # uses for its (dead-code, but pattern-proven) offensive archetype checks.
    avg_speed = avg("speed")
    avg_defense = avg("defense")
    avg_defensive_effort = avg("defensive_effort")

    _passer_p = next((p for p in top8 if p.get("playmaking", 50) > 85), None)
    has_elite_passer = _passer_p is not None
    elite_passer_name = _last(_passer_p) if _passer_p else None

    _scorer_p = next((p for p in top8 if p.get("overall", 50) > 88), None)
    has_elite_scorer = _scorer_p is not None
    elite_scorer_name = _last(_scorer_p) if _scorer_p else None

    _iso_p = next((p for p in top8 if p.get("overall", 50) >= 90), None)
    has_isolation_star = _iso_p is not None
    isolation_star_name = _last(_iso_p) if _iso_p else None

    _big_p = next(
        (p for p in top8 if p.get("position", "") in ("C", "PF") and p.get("finishing", 50) > 80),
        None,
    )
    has_elite_big = _big_p is not None
    elite_big_name = _last(_big_p) if _big_p else None

    three_point_count = sum(1 for p in top8 if p.get("shooting_3pt", 50) > 75)

    return {
        "avg_ovr": avg_ovr,
        "has_elite_passer": has_elite_passer,
        "has_elite_scorer": has_elite_scorer,
        "has_elite_big": has_elite_big,
        "has_isolation_star": has_isolation_star,
        "three_point_count": three_point_count,
        "elite_passer_name": elite_passer_name,
        "isolation_star_name": isolation_star_name,
        "elite_big_name": elite_big_name,
        "elite_scorer_name": elite_scorer_name,
        "avg_speed": avg_speed,
        "avg_defense": avg_defense,
        "avg_defensive_effort": avg_defensive_effort,
    }


async def _get_form(pool, league_id: int, season: int, team_id: int) -> dict:
    row = await pool.fetchrow(
        """
        SELECT wins, losses, win_streak, loss_streak
        FROM standings_cache
        WHERE league_id = $1 AND season = $2 AND team_id = $3
        """,
        league_id,
        season,
        team_id,
    )
    if row is None:
        return {"wins": 0, "losses": 0, "win_streak": 0, "loss_streak": 0}
    return dict(row)


async def _classify_posture(pool, league_id: int, season: int, team_id: int) -> str:
    """Classify coaching posture from the SAME live-posture source trades use.

    Finding #5 (realism audit): this function used to compute a local,
    record-only heuristic (win_pct/wins/pct_complete from standings_cache) that
    never read franchise_plan.goal -- exactly the defect B7 already fixed for
    the trade posture pipeline (services/team_intel.py::build_team_intel /
    compute_posture, which blends roster star-count + record + plan_goal via
    trade_context_builder.compute_team_mode). A team with a stated `win_now`
    plan on a losing skid could get classified "tanking" here and receive
    forced-slow-pace/bench-conserving gameplans that contradicted the front
    office's actual plan. Reusing compute_posture directly means gameplan
    posture and trade posture can never drift apart again.

    No circular import risk: team_intel imports trade_context_builder and
    franchise_plan_service, neither of which imports cpu_coach_service (the
    only importer of cpu_coach_service in services/ is sim_orchestrator.py).

    compute_posture only reads `league.id` and `league.current_season` off the
    League argument, so a lightweight stand-in is passed instead of fetching
    the full leagues row -- this avoids an extra query per team per game while
    still calling the real, shared posture logic.
    """
    from services import team_intel

    league_ref = SimpleNamespace(id=league_id, current_season=season)
    posture_data = await team_intel.compute_posture(pool, league_ref, team_id)
    mode = posture_data["mode"]

    if mode == "contending":
        return "contender"
    if mode in ("rebuilding", "soft_rebuild"):
        return "tanking"
    # play_in_fringe / developing (and any future bucket) -> mid: matches the
    # old heuristic's middle band, which drove "balanced" pace/intensity options.
    return "mid"


async def _is_back_to_back(pool, league_id: int, season: int, team_id: int, game_date) -> bool:
    if game_date is None:
        return False
    if isinstance(game_date, datetime.date):
        prev_date = game_date - datetime.timedelta(days=1)
    else:
        try:
            d = datetime.date.fromisoformat(str(game_date))
            prev_date = d - datetime.timedelta(days=1)
        except (ValueError, TypeError):
            return False
    row = await pool.fetchrow(
        """
        SELECT id FROM games
        WHERE league_id = $1 AND season = $2 AND status = 'simmed'
          AND (home_team_id = $3 OR away_team_id = $3)
          AND scheduled_date = $4
        LIMIT 1
        """,
        league_id,
        season,
        team_id,
        prev_date,
    )
    return row is not None


async def _get_hot_cold(pool, top5: list[dict], season: int) -> dict[int, str]:
    result: dict[int, str] = {}
    for p in top5:
        pid = p.get("id")
        if not pid:
            continue
        season_avg = await pool.fetchval(
            """
            SELECT AVG(points) FROM game_box_scores b
            JOIN games g ON g.id = b.game_id
            WHERE b.player_id = $1 AND g.season_type = 'regular' AND g.season = $2
            """,
            pid,
            season,
        )
        recent_avg = await pool.fetchval(
            """
            SELECT AVG(points) FROM (
                SELECT b.points FROM game_box_scores b
                JOIN games g ON g.id = b.game_id
                WHERE b.player_id = $1 AND g.season_type = 'regular' AND g.season = $2
                ORDER BY g.game_index DESC
                LIMIT 5
            ) sub
            """,
            pid,
            season,
        )
        if season_avg and recent_avg and float(season_avg) > 0:
            ratio = float(recent_avg) / float(season_avg)
            if ratio > 1.15:
                result[pid] = "hot"
            elif ratio < 0.85:
                result[pid] = "cold"
    return result


# Finding #1 personnel gates (realism audit): a team's own roster must be able
# to execute press / switch_all before either scheme can be selected at all --
# previously press fired purely off opponent OVR and switch_all purely off
# opponent archetype/posture, with zero read of the team's own speed or
# defensive personnel (the "slow team runs full-court press" bug).
#
# Thresholds mirror auto_strategy.infer_archetype's existing >=74-raw-rating /
# >=60-tendency convention (that module is dead code in the live sim path, but
# its thresholds are a proven reference point): speed and defense are raw 0-99
# ratings like shooting_3pt/finishing there, so >=74 is the same "clearly
# above average" bar; defensive_effort is tendency-shaped (0-100 behavioral
# leaning) like tendency_3pt/tendency_pass there, so >=60 matches.
_PRESS_SPEED_THRESHOLD = 74
_SWITCH_ALL_DEFENSE_THRESHOLD = 74
_SWITCH_ALL_EFFORT_THRESHOLD = 60


def _weighted_choice(rng: random.Random, options: list[tuple[str, int]]) -> str:
    total = sum(w for _, w in options)
    r = rng.random() * total
    cumulative = 0.0
    for val, w in options:
        cumulative += w
        if r <= cumulative:
            return val
    return options[-1][0]


# CA5 (coaching AI realism sweep) -- disclosed placeholder, not derived from a formula.
_SCHEME_HISTORY_BONUS = 2


def _apply_scheme_history_bonus(
    options: list[tuple[str, int]], historical_scheme: str | None
) -> list[tuple[str, int]]:
    """CA5 (coaching AI realism sweep): give a team's historically-favored
    scheme (from gameplan_repo.get_scheme_history) a flat weight bonus in the
    weighted-choice pool -- CPU teams picked a scheme fresh every single game
    with zero memory of what they'd actually been running. Only applies if
    the historical scheme is still one of the options on offer THIS game --
    for defense_options that's already been pruned by the finding #1
    personnel gate (a team that lost the personnel to run its old favorite
    scheme doesn't get it artificially propped back up).
    """
    if historical_scheme is None:
        return options
    return [
        (scheme, weight + _SCHEME_HISTORY_BONUS) if scheme == historical_scheme else (scheme, weight)
        for scheme, weight in options
    ]


def _decide_strategy(
    self_a: dict,
    opp_a: dict,
    posture: str,
    back_to_back: bool,
    win_streak: int,
    loss_streak: int,
    rng: random.Random,
    scheme_history: dict | None = None,
) -> tuple[dict, list[str]]:
    rationale_parts: list[str] = []
    _scheme_history = scheme_history or {}
    _historical_offense = _scheme_history.get("offensive_scheme", {}).get("scheme")
    _historical_defense = _scheme_history.get("defensive_scheme", {}).get("scheme")

    pace_options: list[tuple[str, int]]
    if self_a["has_elite_scorer"] and posture == "contender":
        pace_options = [("fast", 3), ("balanced", 2)]
        rationale_parts.append("pushing pace with elite scorer")
    elif posture == "tanking":
        pace_options = [("slow", 3)]
        rationale_parts.append("slow pace in rebuild mode")
    else:
        pace_options = [("balanced", 3), ("fast", 1)]

    offensive_pace = _weighted_choice(rng, pace_options)
    if back_to_back:
        _tier = {"run_and_gun": "fast", "fast": "balanced", "balanced": "slow", "slow": "slow"}
        offensive_pace = _tier.get(offensive_pace, "balanced")
        if "back-to-back fatigue" not in rationale_parts:
            rationale_parts.append("conserving legs on a back-to-back")

    scheme_options: list[tuple[str, int]]
    # When a team has BOTH an iso star and an elite passer, blend both triggers
    # into the weighted list so neither archetype always wins.
    if self_a["has_elite_passer"] and self_a["has_isolation_star"] and self_a["three_point_count"] >= 3:
        _iso_name = self_a.get("isolation_star_name") or "isolation star"
        _pass_name = self_a.get("elite_passer_name") or "elite passer"
        scheme_options = [("ball_movement", 2), ("isolation", 2), ("inside_out", 1)]
        if _iso_name == _pass_name:
            rationale_parts.append(f"{_iso_name} dual-threat (ball movement / iso)")
        else:
            rationale_parts.append(f"ball movement / {_iso_name} iso hybrid ({_pass_name} + {_iso_name})")
    elif self_a["has_elite_passer"] and self_a["three_point_count"] >= 3:
        _name = self_a.get("elite_passer_name") or "elite passer"
        scheme_options = [("ball_movement", 3), ("three_heavy", 2), ("inside_out", 1)]
        rationale_parts.append(f"ball movement off {_name}")
    elif self_a["has_isolation_star"]:
        _name = self_a.get("isolation_star_name") or "isolation star"
        scheme_options = [("isolation", 3), ("inside_out", 1), ("ball_movement", 1)]
        rationale_parts.append(f"{_name} isolation scheme")
    elif self_a["has_elite_big"] and not self_a["has_elite_scorer"]:
        _name = self_a.get("elite_big_name") or "elite big"
        scheme_options = [("inside_out", 3), ("ball_movement", 1)]
        rationale_parts.append(f"inside-out through {_name}")
    elif self_a["three_point_count"] >= 2:
        scheme_options = [("three_heavy", 2), ("balanced", 1)]
    else:
        scheme_options = [("balanced", 3), ("ball_movement", 1)]

    offensive_scheme = _weighted_choice(rng, _apply_scheme_history_bonus(scheme_options, _historical_offense))

    # Personnel gate: can THIS roster actually execute press / switch_all?
    # A slow team pressing full-court, or a poor-defense team switching every
    # screen, gives up more than it gains -- so those options are pruned from
    # the weighted list entirely rather than picked and left to hurt the team
    # with no roster-aware consequence (finding #1).
    can_press = self_a.get("avg_speed", 50) >= _PRESS_SPEED_THRESHOLD
    can_switch_all = (
        self_a.get("avg_defense", 50) >= _SWITCH_ALL_DEFENSE_THRESHOLD
        and self_a.get("avg_defensive_effort", 50) >= _SWITCH_ALL_EFFORT_THRESHOLD
    )

    defense_options: list[tuple[str, int]]
    if opp_a["has_isolation_star"]:
        defense_options = [("man_to_man", 3)]
        if can_switch_all:
            defense_options.append(("switch_all", 2))
        rationale_parts.append("locking down their isolation star")
    elif offensive_scheme == "three_heavy":
        defense_options = [("zone", 3)]
        if can_switch_all:
            defense_options.append(("switch_all", 1))
        rationale_parts.append("zone to disrupt their three-point attack")
    elif opp_a.get("avg_ovr", 70) > 82:
        defense_options = [("man_to_man", 2)]
        if can_press:
            defense_options.append(("press", 2))
        if can_switch_all:
            defense_options.append(("switch_all", 1))
    elif posture == "contender":
        defense_options = [("man_to_man", 2)]
        if can_switch_all:
            defense_options.append(("switch_all", 1))
        if can_press:
            defense_options.append(("press", 1))
    else:
        defense_options = [("man_to_man", 3), ("zone", 1)]
        if can_switch_all:
            defense_options.append(("switch_all", 1))

    defensive_scheme = _weighted_choice(rng, _apply_scheme_history_bonus(defense_options, _historical_defense))

    intensity_options: list[tuple[str, int]]
    if posture == "tanking":
        intensity_options = [("conservative", 3)]
    elif back_to_back:
        intensity_options = [("conservative", 2), ("normal", 1)]
    elif posture == "contender" and loss_streak >= 3:
        intensity_options = [("aggressive", 3)]
        rationale_parts.append(f"aggressive intensity after {loss_streak}-game skid")
    else:
        intensity_options = [("normal", 3)]

    defensive_intensity = _weighted_choice(rng, intensity_options)

    if offensive_scheme == "isolation":
        star_usage = rng.randint(75, 85)
    elif offensive_scheme == "ball_movement":
        star_usage = rng.randint(40, 55)
    elif posture == "tanking":
        star_usage = rng.randint(30, 45)
    else:
        star_usage = rng.randint(55, 65)

    # CA4 (coaching AI realism sweep): bench_leash/transition_aggression were
    # columns on team_strategies with a DB default and a get_sim_modifiers
    # pass-through, but no CPU coach ever set a real value -- every CPU team
    # ran the neutral default forever. Driven off the same `posture` signal
    # the rest of this function already uses: a tanking team pulls starters
    # faster (short leash) and plays it safe in transition (retreat); a
    # contender rides its rotation longer (long leash) and pushes the break
    # (crash). Everyone else gets the neutral default.
    if posture == "tanking":
        bench_leash = "short"
        transition_aggression = "retreat"
    elif posture == "contender":
        bench_leash = "long"
        transition_aggression = "crash"
    else:
        bench_leash = "normal"
        transition_aggression = "balanced"

    strategy = {
        "offensive_pace": offensive_pace,
        "offensive_scheme": offensive_scheme,
        "defensive_scheme": defensive_scheme,
        "defensive_intensity": defensive_intensity,
        "star_usage": star_usage,
        "bench_leash": bench_leash,
        "transition_aggression": transition_aggression,
    }
    return strategy, rationale_parts


def _decide_player_directives(
    top8: list[dict],
    strategy: dict,
    posture: str,
    hot_cold: dict[int, str],
    rng: random.Random,
) -> dict[int, dict]:
    directives: dict[int, dict] = {}
    scheme = strategy["offensive_scheme"]
    defense = strategy["defensive_scheme"]

    for rank, p in enumerate(top8):
        pid = p.get("id")
        if not pid:
            continue
        status = hot_cold.get(pid)
        ovr = p.get("overall", 50)
        playmaking = p.get("playmaking", 50)
        shooting_3pt = p.get("shooting_3pt", 50)
        finishing = p.get("finishing", 50)
        defense_attr = p.get("defense", 50)
        shooting_mid = p.get("shooting_mid", 50)
        position = p.get("position", "")

        if status == "hot" and posture == "contender":
            usage_mode = "feature"
        elif status == "cold":
            usage_mode = "conserve"
        elif rank == 0 and scheme == "isolation":
            usage_mode = "feature"
        elif posture == "tanking" and rank < 2:
            usage_mode = "conserve"
        else:
            usage_mode = "normal"

        # CA3 (coaching AI realism sweep): a second, redundant `if posture ==
        # "tanking":` block used to sit here and unconditionally overwrite the
        # decision above for every tanking team (e.g. a hot/isolation-star
        # "feature" from the branches above got silently reset to
        # "conserve"/"normal"). The tanking-aware `elif` above is now the sole
        # source of truth for tanking teams' usage_mode.

        if shooting_3pt > 80 and scheme == "three_heavy":
            shot_diet = "force_3s"
        elif finishing > 80 and position in ("C", "PF") and scheme == "inside_out":
            shot_diet = "attack_rim"
        elif shooting_mid > 80 and scheme == "isolation":
            shot_diet = "midrange"
        else:
            shot_diet = "auto"

        if defense_attr > 80 and defense == "man_to_man":
            defense_mode = "lockdown"
        elif posture == "tanking":
            defense_mode = "off"
        else:
            defense_mode = "standard"

        if playmaking > 85:
            role_mode = "creator"
        elif ovr > 87 and usage_mode == "feature":
            role_mode = "scorer"
        else:
            role_mode = "spot_up"

        if status == "hot" and posture == "contender":
            clutch_mode = "hero"
        elif status == "cold" or posture == "tanking":
            clutch_mode = "hide"
        else:
            clutch_mode = "normal"

        directives[pid] = {
            "shot_diet": shot_diet,
            "usage_mode": usage_mode,
            "defense_mode": defense_mode,
            "role_mode": role_mode,
            "clutch_mode": clutch_mode,
        }

    return directives


def _build_rationale(
    parts: list[str],
    top8: list[dict],
    strategy: dict,
    posture: str,
    win_streak: int,
    loss_streak: int,
) -> str:
    if not parts:
        if posture == "tanking":
            parts = ["rebuilding phase, conserving energy"]
        elif posture == "contender":
            parts = [f"{strategy['offensive_scheme']} scheme with {strategy['defensive_intensity']} defense"]
        else:
            parts = [f"{strategy['offensive_scheme']} offense, {strategy['defensive_scheme']} defense"]

    combined = "; ".join(parts[:2])
    sentence = combined[0].upper() + combined[1:] if combined else ""
    if not sentence.endswith("."):
        sentence += "."
    return sentence[:120]
