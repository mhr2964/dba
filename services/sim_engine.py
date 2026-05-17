from __future__ import annotations

from random import Random
from typing import List


def _split_quarters(total: int, game_seed: int, side: str) -> list[int]:
    """Distribute `total` into 4 quarters using seeded noise.

    side is a short string ('home'/'away') so home and away draws are independent.
    """
    rng_q = Random(game_seed + hash(side))
    weights = [rng_q.gauss(1.0, 0.3) for _ in range(4)]
    weights = [max(0.5, w) for w in weights]  # no zero-point quarters
    factor = sum(weights)
    splits = [round(total * w / factor) for w in weights]
    # Last quarter absorbs any rounding drift so the sum is exact.
    splits[3] += total - sum(splits)
    return splits  # [q1, q2, q3, q4]

_POSITION_SCORING_WEIGHT = {
    "PG": 1.05,
    "SG": 1.10,
    "SF": 1.00,
    "PF": 0.95,
    "C":  0.90,
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

# Position-based rebounding multiplier.  reb_tendency is scaled relative to the
# position-group average (50 = average for G/F/C), so a guard with reb_tendency=81
# means "above-average guard rebounder" — not "above-average rebounder overall."
# Without this multiplier, Curry (G, reb_tendency=81) gets as many boards as an
# average PF because the raw weight values are similar.  The multiplier re-introduces
# the absolute positional gap: centers rebound far more than guards regardless of
# their position-relative tendency value.
_POSITION_REBOUND_WEIGHT = {
    "PG": 0.42,
    "SG": 0.70,
    "SF": 0.85,
    "PF": 1.10,
    "C":  1.40,
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

    # Apply per-game shooting noise so FG% varies instead of always hitting the expected rate.
    raw_fg = _player_fg_pct(player)
    fg_pct = max(0.25, min(0.65, raw_fg * rng.gauss(1.0, 0.08)))
    raw_3p = _player_3pct(player)
    three_pct = max(0.20, min(0.55, raw_3p * rng.gauss(1.0, 0.10)))

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

    # Reduced from randint(40,50) to randint(32,42) — the minutes fix raised starter
    # minutes from ~27 to ~36, which would otherwise inflate individual RPG by ~33%.
    # Pulling the pool down ~20% compensates so absolute RPG targets remain realistic.
    total_reb = rng.randint(32, 42)
    reb_off_total = round(total_reb * 0.28)
    reb_def_total = total_reb - reb_off_total

    # reb_tendency is position-relative (50 = average for G/F/C group), so a guard
    # with reb_tendency=81 means "above-average guard" — not "above-average overall."
    # _POSITION_REBOUND_WEIGHT reintroduces the absolute positional gap so that an
    # elite guard (reb_tendency=95, C-weight=1.40 vs SG-weight=0.70) gets ~6 RPG,
    # while a true center with the same tendency gets ~12 RPG.
    reb_weights = [
        (players[i].get("reb_tendency", 50) / 50.0)
        * _POSITION_REBOUND_WEIGHT.get(players[i].get("position", "SF"), 0.85)
        * minutes_list[i]
        for i in range(n)
    ]
    reb_offs = _distribute_proportional(rng, reb_off_total, reb_weights)
    reb_defs = _distribute_proportional(rng, reb_def_total, reb_weights)

    ast_total = rng.randint(20, 28)
    ast_weights = [
        players[i].get("playmaking", 50)
        * _POSITION_PLAYMAKING_WEIGHT.get(players[i].get("position", "SF"), 1.0)
        * (players[i].get("tendency_pass", 50) / 50.0)
        # Exponent on ast_tendency widens the gap between elite passers and role players:
        # ast_tendency=95 → (1.9)^1.6 ≈ 2.72x; =70 → (1.4)^1.6 ≈ 1.60x; =30 → (0.6)^1.6 ≈ 0.44x.
        # Linear scaling was capping elite PGs (Trae Young) at ~7 APG; this targets 10–12.
        * (players[i].get("ast_tendency", 50) / 50.0) ** 1.6
        * minutes_list[i]
        for i in range(n)
    ]
    asts = _distribute_proportional(rng, ast_total, ast_weights)

    stl_total = rng.randint(6, 10)
    stl_weights = [
        # defense_tendency is derived from actual stl+blk per game (not raw OVR),
        # so bad-defender stars like Luka don't steal at an elite rate.
        # Fall back to defense attribute if defense_tendency is absent (old rows).
        players[i].get("defense_tendency", players[i].get("defense", 50))
        * (players[i].get("stl_tendency", 50) / 50.0)
        * (players[i].get("defensive_effort", 50) / 50.0)
        * minutes_list[i]
        for i in range(n)
    ]
    stls = _distribute_proportional(rng, stl_total, stl_weights)

    blk_total = rng.randint(4, 8)
    blk_weights = [
        players[i].get("defense_tendency", players[i].get("defense", 50))
        * (players[i].get("blk_tendency", 50) / 50.0)
        * (players[i].get("defensive_effort", 50) / 50.0)
        * _POSITION_BLOCK_WEIGHT.get(players[i].get("position", "SF"), 1.0)
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
        # Auto-allocate minutes so starters land ~33-38 min and bench shares the rest.
        # Weights are chosen so that after normalization to 240 total, a 5-starter
        # roster with 7 bench players produces ~35 min per starter.  The ratio
        # starter_weight / bench_weight ≈ 4:1 achieves this at typical roster sizes.
        starter_weights = [rng.uniform(50, 65) for _ in starters]
        bench_weights = [rng.uniform(10, 18) for _ in bench] if bench else []
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
        # Exponential usage curve: (usage/50)^2.0 widens the gap between stars
        # and role players significantly more than ^1.5.
        # usage=95 → (1.9)^2.0 * 0.55 ≈ 1.98x; usage=80 → 1.41x; usage=35 → 0.27x.
        # Multiplied by 0.55 (down from 0.65) to keep league-average PPG ~15-16.
        usage_w = (p.get("usage_weight", 50) / 50.0) ** 2.0 * 0.55
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

    # Star scoring bump: top-2 players by weight get an extra allocation multiplier
    # applied BEFORE normalization so the effect is additive against the rest of the roster.
    # Cap: no single player can exceed 30% of total team scoring weight.
    if n >= 2:
        indexed_weights = sorted(enumerate(scoring_weights), key=lambda x: x[1], reverse=True)
        top_idx = indexed_weights[0][0]
        second_idx = indexed_weights[1][0]
        scoring_weights[top_idx] *= 1.08
        scoring_weights[second_idx] *= 1.03
        # Enforce 33% cap on the top player (raised from 30% to free up share for secondary
        # stars and reach ~17 players at 25+ PPG league-wide).
        # At a ~115-pt team average, 33% ≈ 38 PPG ceiling — stars who organically hit
        # above that (usage=95+ on a low-competition roster) get clipped here.
        total_w_star = sum(scoring_weights)
        if total_w_star > 0 and scoring_weights[top_idx] / total_w_star > 0.33:
            scoring_weights[top_idx] = (sum(scoring_weights) - scoring_weights[top_idx]) * 0.33 / 0.67

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
        # Base raised from 1.08 → 1.11 to add ~2.5 PPG league-wide.
        # Each 0.01 increase in base yields ~0.8 PPG (80 possessions × 0.01).
        # Offense scale raised from 0.22 → 0.38 so an 8-OVR gap (e.g. 84 vs 76)
        # produces ~7-10 pts/game margin, giving a ~70% per-game win rate for the
        # top seed — enough to yield ~95% series win probability in 7 games.
        # Defense scale raised from 0.10 → 0.14 for complementary defensive impact.
        base = 1.11 + (off - 60) / (95 - 60) * 0.38
        base *= 1 - (opp_def - 60) / (95 - 60) * 0.14
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

    home_score_reg = max(60, int(home_poss * home_ppp))
    away_score_reg = max(60, int(away_poss * away_ppp))

    # Quarter splits are based on regulation totals and seeded independently of the main rng.
    game_seed = (rng_seed ^ 0xDEAD) & 0xFFFFFF
    home_quarters = _split_quarters(home_score_reg, game_seed, "home")  # [q1,q2,q3,q4]
    away_quarters = _split_quarters(away_score_reg, game_seed, "away")

    home_score = home_score_reg
    away_score = away_score_reg

    # OT points are whatever exceeds the regulation quarter sum.
    ot_home: int = 0
    ot_away: int = 0

    # Overtime: tied games cannot end — simulate up to 4 OT periods.
    # Each OT period is ~5 min (~25% of a quarter).  Score ceiling per team
    # is derived from their regulation points-per-quarter average scaled to 5 min.
    if home_score == away_score:
        home_score, away_score = _simulate_overtime(rng, home_score, away_score)
        ot_home = home_score - home_score_reg
        ot_away = away_score - away_score_reg

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
        "q1_home": home_quarters[0],
        "q1_away": away_quarters[0],
        "q2_home": home_quarters[1],
        "q2_away": away_quarters[1],
        "q3_home": home_quarters[2],
        "q3_away": away_quarters[2],
        "q4_home": home_quarters[3],
        "q4_away": away_quarters[3],
        "ot_home": ot_home if ot_home > 0 else None,
        "ot_away": ot_away if ot_away > 0 else None,
    }
