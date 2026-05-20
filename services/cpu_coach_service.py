from __future__ import annotations

import datetime
import os
import random

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
    strategy_row = await pool.fetchrow(
        """
        SELECT offensive_pace, offensive_scheme, defensive_scheme,
               defensive_intensity, star_usage
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
    wins: int = form.get("wins", 0) or 0
    losses: int = form.get("losses", 0) or 0
    win_pct: float = wins / (wins + losses) if (wins + losses) > 0 else 0.0

    total_games = await pool.fetchval(
        "SELECT COUNT(*) FROM games WHERE league_id = $1 AND season = $2 AND season_type = 'regular'",
        league_id,
        season,
    ) or 82
    game_index: int = game_row.get("game_index", 0) or 0
    pct_complete: float = game_index / total_games if total_games > 0 else 0.0

    posture = _classify_posture(win_pct, wins, pct_complete)

    game_date = game_row.get("scheduled_date")
    back_to_back = await _is_back_to_back(pool, league_id, season, team_id, game_date)

    hot_cold = await _get_hot_cold(pool, top8_self[:5], season)

    strategy, rationale_parts = _decide_strategy(
        self_analysis, opp_analysis, posture, back_to_back, win_streak, loss_streak, rng
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
        }
    n = len(top8)

    def avg(attr: str) -> float:
        return sum(p.get(attr, 50) for p in top8) / n

    def _last(p: dict) -> str:
        full = p.get("name", "") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        return full.split()[-1] if full else ""

    avg_ovr = avg("overall")

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


def _classify_posture(win_pct: float, wins: int, pct_complete: float) -> str:
    if win_pct > 0.55 or (wins > 35 and pct_complete > 0.50):
        return "contender"
    if win_pct < 0.30 and pct_complete > 0.60:
        return "tanking"
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


def _weighted_choice(rng: random.Random, options: list[tuple[str, int]]) -> str:
    total = sum(w for _, w in options)
    r = rng.random() * total
    cumulative = 0.0
    for val, w in options:
        cumulative += w
        if r <= cumulative:
            return val
    return options[-1][0]


def _decide_strategy(
    self_a: dict,
    opp_a: dict,
    posture: str,
    back_to_back: bool,
    win_streak: int,
    loss_streak: int,
    rng: random.Random,
) -> tuple[dict, list[str]]:
    rationale_parts: list[str] = []

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

    offensive_scheme = _weighted_choice(rng, scheme_options)

    defense_options: list[tuple[str, int]]
    if opp_a["has_isolation_star"]:
        defense_options = [("man_to_man", 3), ("switch_all", 2)]
        rationale_parts.append("locking down their isolation star")
    elif offensive_scheme == "three_heavy":
        defense_options = [("zone", 3), ("switch_all", 1)]
        rationale_parts.append("zone to disrupt their three-point attack")
    elif opp_a.get("avg_ovr", 70) > 82:
        defense_options = [("press", 2), ("man_to_man", 2), ("switch_all", 1)]
    elif posture == "contender":
        defense_options = [("man_to_man", 2), ("switch_all", 1), ("press", 1)]
    else:
        defense_options = [("man_to_man", 3), ("switch_all", 1), ("zone", 1)]

    defensive_scheme = _weighted_choice(rng, defense_options)

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

    strategy = {
        "offensive_pace": offensive_pace,
        "offensive_scheme": offensive_scheme,
        "defensive_scheme": defensive_scheme,
        "defensive_intensity": defensive_intensity,
        "star_usage": star_usage,
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

        if posture == "tanking":
            usage_mode = "conserve" if rank < 2 else "normal"

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
