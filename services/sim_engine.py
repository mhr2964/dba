from __future__ import annotations

from random import Random
from typing import List

_POSITION_SCORING_WEIGHT = {
    "PG": 1.10,
    "SG": 1.15,
    "SF": 1.00,
    "PF": 0.85,
    "C":  0.85,
}

_POSITION_PLAYMAKING_WEIGHT = {
    "PG": 1.40,
    "SG": 1.20,
    "SF": 0.90,
    "PF": 0.70,
    "C":  0.60,
}

_POSITION_BLOCK_WEIGHT = {
    "PG": 0.30,
    "SG": 0.50,
    "SF": 0.90,
    "PF": 1.40,
    "C":  1.80,
}


def _distribute_proportional(rng: Random, total: int, weights: List[float]) -> List[int]:
    """Distribute total among buckets proportional to weights, rounding correctly."""
    if not weights or sum(weights) == 0:
        return [0] * len(weights)
    s = sum(weights)
    floats = [total * w / s for w in weights]
    result = [int(f) for f in floats]
    remainder = total - sum(result)
    fracs = [(floats[i] - result[i], i) for i in range(len(result))]
    fracs.sort(reverse=True)
    for _, i in fracs[:remainder]:
        result[i] += 1
    return result


def _player_fg_pct(player: dict) -> float:
    return 0.35 + (player.get("finishing", 50) + player.get("shooting_2pt", 50)) / 200.0 * 0.15


def _player_3pct(player: dict) -> float:
    return 0.30 + player.get("shooting_3pt", 50) / 200.0 * 0.15


def _build_player_line(
    rng: Random,
    player: dict,
    team_id: int,
    started: bool,
    minutes: float,
    team_score: int,
    all_players: List[dict],
    all_minutes: List[float],
    player_index: int,
    score_diff: int,
) -> dict:
    """Build one player's stat line. Points are pre-allocated by caller."""
    pts = player.get("_allocated_points", 0)

    fg_pct = _player_fg_pct(player)
    three_pct = _player_3pct(player)

    # Tendency-driven shot mix: tendency_3pt=50 → ~15% of pts from 3; 100 → ~35%; 0 → ~5%
    t3 = player.get("tendency_3pt", 50)
    three_share = 0.05 + (t3 / 100.0) * 0.30   # 5% to 35% of points from 3s
    tpm = max(0, round(pts * three_share / 3))
    tpa = max(tpm, round(tpm / max(three_pct, 0.01)))

    # Tendency-driven FT rate: tendency_drive=50 → ~13% of pts; 100 → ~20%; 0 → ~6%
    t_drive = player.get("tendency_drive", 50)
    ft_share = 0.06 + (t_drive / 100.0) * 0.14   # 6% to 20% of points from FTs
    ft_pts = max(0, round(pts * ft_share))
    ftm = ft_pts
    fta = max(ftm, round(ftm / 0.75))

    two_pts = max(0, pts - tpm * 3 - ftm)
    fgm_2 = max(0, round(two_pts / 2))
    fga_2 = max(fgm_2, round(fgm_2 / max(fg_pct, 0.01)))

    fgm = fgm_2 + tpm
    fga = fga_2 + tpa

    # Confirm pts roughly matches; adjust ftm to absorb rounding
    computed_pts = fgm_2 * 2 + tpm * 3 + ftm
    if computed_pts != pts:
        ftm = max(0, ftm + (pts - computed_pts))
        fta = max(ftm, fta)

    min_share = minutes / 240.0
    pm = round(score_diff * min_share)

    return {
        "player_id": player["id"],
        "team_id": team_id,
        "started": started,
        "minutes": round(minutes, 1),
        "points": pts,
        "rebounds_off": 0,
        "rebounds_def": 0,
        "assists": 0,
        "steals": 0,
        "blocks": 0,
        "turnovers": 0,
        "fouls": 0,
        "fga": fga,
        "fgm": fgm,
        "tpa": tpa,
        "tpm": tpm,
        "fta": fta,
        "ftm": ftm,
        "plus_minus": pm,
    }


def _assign_team_stats(
    rng: Random,
    lines: List[dict],
    players: List[dict],
    team_id: int,
    team_score: int,
    minutes_list: List[float],
    score_diff: int,
    turnover_adj: float = 0.0,
    foul_adj: float = 0.0,
) -> List[dict]:
    """Assign rebounds, assists, steals, blocks, turnovers, fouls to lines in place."""
    n = len(players)

    def w_minutes() -> List[float]:
        return [m for m in minutes_list]

    total_reb = rng.randint(40, 50)
    reb_off_total = round(total_reb * 0.28)
    reb_def_total = total_reb - reb_off_total

    reb_weights = [
        players[i].get("rebounding", 50) * minutes_list[i]
        for i in range(n)
    ]
    reb_offs = _distribute_proportional(rng, reb_off_total, reb_weights)
    reb_defs = _distribute_proportional(rng, reb_def_total, reb_weights)

    ast_total = rng.randint(20, 28)
    ast_weights = [
        players[i].get("playmaking", 50)
        * _POSITION_PLAYMAKING_WEIGHT.get(players[i].get("position", "SF"), 1.0)
        * (players[i].get("tendency_pass", 50) / 50.0)
        * minutes_list[i]
        for i in range(n)
    ]
    asts = _distribute_proportional(rng, ast_total, ast_weights)

    stl_total = rng.randint(5, 10)
    stl_weights = [
        players[i].get("defense", 50)
        * (players[i].get("defensive_effort", 50) / 50.0)
        * minutes_list[i]
        for i in range(n)
    ]
    stls = _distribute_proportional(rng, stl_total, stl_weights)

    blk_total = rng.randint(3, 7)
    blk_weights = [
        players[i].get("defense", 50)
        * _POSITION_BLOCK_WEIGHT.get(players[i].get("position", "SF"), 1.0)
        * (players[i].get("defensive_effort", 50) / 50.0)
        * minutes_list[i]
        for i in range(n)
    ]
    blks = _distribute_proportional(rng, blk_total, blk_weights)

    base_tov = rng.randint(10, 16)
    tov_total = max(0, round(base_tov + turnover_adj))
    pg_penalty = [1.2 if players[i].get("position") == "PG" else 1.0 for i in range(n)]
    tov_weights = [minutes_list[i] * pg_penalty[i] for i in range(n)]
    tovs = _distribute_proportional(rng, tov_total, tov_weights)

    base_foul = rng.randint(15, 22)
    foul_total = max(0, round(base_foul + foul_adj))
    foul_weights = w_minutes()
    fouls = _distribute_proportional(rng, foul_total, foul_weights)
    fouls = [min(f, 6) for f in fouls]

    for i, line in enumerate(lines):
        line["rebounds_off"] = reb_offs[i]
        line["rebounds_def"] = reb_defs[i]
        line["assists"] = asts[i]
        line["steals"] = stls[i]
        line["blocks"] = blks[i]
        line["turnovers"] = tovs[i]
        line["fouls"] = fouls[i]

    return lines


def _build_box_for_team(
    rng: Random,
    players: List[dict],
    team_id: int,
    team_score: int,
    score_diff: int,
    minutes_override: dict | None = None,
    star_usage_mult: float = 1.0,
    three_rate_adj: float = 0.0,
    turnover_adj: float = 0.0,
    foul_adj: float = 0.0,
) -> List[dict]:
    n = len(players)
    if n == 0:
        return []

    starters = players[:5]
    bench = players[5:]

    if minutes_override:
        # Use provided minutes; players not in override get a small fallback share.
        raw = [minutes_override.get(p.get("id", p.get("player_id", 0)), None) for p in players]
        missing_indices = [i for i, v in enumerate(raw) if v is None]
        assigned = sum(v for v in raw if v is not None)
        remainder = max(0.0, 240.0 - assigned)
        if missing_indices:
            per_missing = remainder / len(missing_indices)
            for idx in missing_indices:
                raw[idx] = per_missing
        total = sum(raw) or 1.0
        minutes_list = [v / total * 240.0 for v in raw]
    else:
        # Auto-allocate: starters 28-38, bench 8-22, must sum to 240.
        starter_weights = [rng.uniform(28, 38) for _ in starters]
        bench_weights = [rng.uniform(8, 22) for _ in bench] if bench else []
        all_weights = starter_weights + bench_weights
        total_w = sum(all_weights)
        minutes_list = [w / total_w * 240 for w in all_weights]

    # Points: weight by minutes * position_scoring_weight * composite_rating * usage_weight.
    team_avg_composite = sum(
        p.get("finishing", 50) + p.get("shooting_2pt", 50) + p.get("shooting_3pt", 50)
        for p in players
    ) / max(n, 1)

    scoring_weights = []
    for i, p in enumerate(players):
        pos_w = _POSITION_SCORING_WEIGHT.get(p.get("position", "SF"), 1.0)
        usage_w = p.get("usage_weight", 50) / 50.0  # 1.0 at default, up to 2.0 for 100
        composite = p.get("finishing", 50) + p.get("shooting_2pt", 50) + p.get("shooting_3pt", 50)
        rating_adj = composite / max(team_avg_composite, 1)
        base = (minutes_list[i] / 48.0) * pos_w * rating_adj * usage_w
        noise = rng.uniform(0.75, 1.25)
        scoring_weights.append(max(base * noise, 0.01))

    # Star usage: top-2 players by OVR get amplified scoring weight; bench absorbs the reduction.
    if star_usage_mult != 1.0 and n >= 2:
        ovrs = [(p.get("overall", p.get("finishing", 50)), i) for i, p in enumerate(players)]
        ovrs.sort(reverse=True)
        star_indices = {ovrs[0][1], ovrs[1][1]}
        total_w_before = sum(scoring_weights)
        star_total = sum(scoring_weights[i] for i in star_indices)
        bench_total = total_w_before - star_total
        star_boost = star_total * star_usage_mult
        # Redistribute: stars get boosted share; bench absorbs the difference.
        deficit = star_boost - star_total
        bench_factor = max(0.01, (bench_total - deficit) / bench_total) if bench_total > 0 else 1.0
        new_weights = list(scoring_weights)
        for i in range(n):
            if i in star_indices:
                new_weights[i] = scoring_weights[i] * star_usage_mult
            else:
                new_weights[i] = max(scoring_weights[i] * bench_factor, 0.01)
        scoring_weights = new_weights

    # Clutch adjustment: in close games, high-clutch players get more late-game usage.
    if abs(score_diff) < 12:
        clutch_adj = [(p.get("clutch_rating", 50) - 50) / 100.0 for p in players]  # -0.5 to +0.5
        scoring_weights = [max(scoring_weights[i] * (1 + clutch_adj[i] * 0.4), 0.01) for i in range(n)]

    pts_allocated = _distribute_proportional(rng, team_score, scoring_weights)
    for i, p in enumerate(players):
        p["_allocated_points"] = pts_allocated[i]

    lines = []
    for i, p in enumerate(players):
        started = i < 5
        line = _build_player_line(
            rng, p, team_id, started, minutes_list[i],
            team_score, players, minutes_list, i, score_diff,
        )
        # Apply three_rate_adj: scale tpa/tpm proportionally.
        if three_rate_adj != 0.0:
            adj_factor = max(0.0, 1.0 + three_rate_adj)
            line["tpa"] = max(line["tpm"], round(line["tpa"] * adj_factor))
        lines.append(line)

    lines = _assign_team_stats(
        rng, lines, players, team_id, team_score, minutes_list, score_diff,
        turnover_adj=turnover_adj, foul_adj=foul_adj,
    )
    return lines


_INJURY_PROBS: List[tuple[str, float]] = [
    ("season_ending", 0.0005),
    ("week_4_8",      0.002),
    ("week_2_4",      0.005),
    ("day_to_day",    0.02),
]


def _roll_injuries(
    rng: Random,
    box_lines: List[dict],
    team_id: int,
) -> List[dict]:
    """Roll injury outcomes for players who played > 10 minutes."""
    injuries: List[dict] = []
    for line in box_lines:
        if line["minutes"] <= 10:
            continue
        roll = rng.random()
        cumulative = 0.0
        for severity, prob in _INJURY_PROBS:
            cumulative += prob
            if roll < cumulative:
                injuries.append({
                    "player_id": line["player_id"],
                    "team_id": team_id,
                    "severity": severity,
                })
                break
    return injuries


def _simulate_overtime(rng: Random, home_score: int, away_score: int) -> tuple[int, int]:
    """Play OT periods until scores differ.  Max 4 OT; home team wins on 4OT tie."""
    MAX_OT = 4
    for _ in range(MAX_OT):
        if home_score != away_score:
            break
        # ~5-min OT period: each team scores 0–14 pts (regulation avg ~25/qtr → ~6 in 5 min,
        # ceiling raised so games don't always end 0-0 OT).
        home_ot = rng.randint(0, 14)
        away_ot = rng.randint(0, 14)
        home_score += home_ot
        away_score += away_ot
    # If still tied after max OT, home team wins (extremely rare edge case).
    if home_score == away_score:
        home_score += 1
    return home_score, away_score


def sim_game(
    home_team: dict,
    away_team: dict,
    home_players: List[dict],
    away_players: List[dict],
    rng_seed: int,
    fatigue: dict | None = None,
    home_strategy: dict | None = None,
    away_strategy: dict | None = None,
    home_minutes: dict | None = None,
    away_minutes: dict | None = None,
) -> dict:
    """Pure function. No DB calls. Returns structured result."""
    rng = Random(rng_seed)

    # Unpack strategy modifiers; absent strategy = neutral (no effect).
    h_strat = home_strategy or {}
    a_strat = away_strategy or {}

    home_pace_adj: float = h_strat.get("pace_adjustment", 0.0)
    away_pace_adj: float = a_strat.get("pace_adjustment", 0.0)

    home_pace = home_team.get("pace", 100.0) or 100.0
    away_pace = away_team.get("pace", 100.0) or 100.0
    avg_pace = (home_pace + away_pace) / 2.0 + (home_pace_adj + away_pace_adj) / 2.0
    total_possessions = int(rng.gauss(avg_pace * 2, 4))
    total_possessions = max(160, total_possessions)

    home_off = (home_team.get("offense_rating") or 75) + 3
    away_off = away_team.get("offense_rating") or 75
    home_def = home_team.get("defense_rating") or 75
    away_def = away_team.get("defense_rating") or 75

    def _ppp(off: float, opp_def: float) -> float:
        base = 1.05 + (off - 60) / (95 - 60) * 0.20
        base *= 1 - (opp_def - 60) / (95 - 60) * 0.10
        base *= rng.gauss(1.0, 0.05)
        return base

    home_poss = total_possessions // 2
    away_poss = total_possessions - home_poss

    home_ppp = _ppp(home_off, away_def)
    away_ppp = _ppp(away_off, home_def)

    # Offensive scheme multiplier — own team's offense, opponent's defense scheme.
    home_ppp *= h_strat.get("ppp_offense_mult", 1.0)
    home_ppp /= a_strat.get("ppp_defense_mult", 1.0)
    away_ppp *= a_strat.get("ppp_offense_mult", 1.0)
    away_ppp /= h_strat.get("ppp_defense_mult", 1.0)

    if fatigue:
        if fatigue.get("home_b2b"):
            home_ppp -= 0.03
        if fatigue.get("away_b2b"):
            away_ppp -= 0.03

    home_score = max(60, int(home_poss * home_ppp))
    away_score = max(60, int(away_poss * away_ppp))

    # Overtime: tied games cannot end — simulate up to 4 OT periods.
    # Each OT period is ~5 min (~25% of a quarter).  Score ceiling per team
    # is derived from their regulation points-per-quarter average scaled to 5 min.
    if home_score == away_score:
        home_score, away_score = _simulate_overtime(rng, home_score, away_score)

    winner_id = home_team["team_id"] if home_score > away_score else away_team["team_id"]
    home_diff = home_score - away_score
    away_diff = -home_diff

    home_box = _build_box_for_team(
        rng, home_players, home_team["team_id"], home_score, home_diff,
        minutes_override=home_minutes,
        star_usage_mult=h_strat.get("star_usage_mult", 1.0),
        three_rate_adj=h_strat.get("three_rate_adj", 0.0),
        turnover_adj=h_strat.get("turnover_adj", 0.0),
        foul_adj=h_strat.get("foul_adj", 0.0),
    )
    away_box = _build_box_for_team(
        rng, away_players, away_team["team_id"], away_score, away_diff,
        minutes_override=away_minutes,
        star_usage_mult=a_strat.get("star_usage_mult", 1.0),
        three_rate_adj=a_strat.get("three_rate_adj", 0.0),
        turnover_adj=a_strat.get("turnover_adj", 0.0),
        foul_adj=a_strat.get("foul_adj", 0.0),
    )

    injuries = _roll_injuries(rng, home_box, home_team["team_id"]) + _roll_injuries(rng, away_box, away_team["team_id"])

    return {
        "home_score": home_score,
        "away_score": away_score,
        "winner_team_id": winner_id,
        "home_box": home_box,
        "away_box": away_box,
        "injuries": injuries,
    }
