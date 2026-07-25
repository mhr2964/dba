from __future__ import annotations
# sim_engine v3 — role-based touch share (Phase 2: player_roles drives scoring weights)
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
    # Lowered PG 1.40→1.15 and SG 1.20→1.05: the quadratic ast_tendency formula
    # already provides within-position differentiation; large position weights
    # compounded with that to produce unrealistic PG APG totals (LaMelo 13.4,
    # Lillard 11.9 when real averages are ~8 APG).  The ordering is preserved —
    # PGs still assist more than Cs — but the absolute gap is tighter.
    "PG": 1.15,
    "SG": 1.05,
    "SF": 0.85,
    "PF": 0.70,
    # C raised from 0.60 → 0.85 so elite-passing centers (Jokić, Sabonis)
    # reach realistic APG.  ast_tendency (position-relative, clamped 5–95)
    # already carries the within-position differentiation — the position weight
    # only needs to reflect that an average C assists less than an average PG,
    # not penalize every center to half a PG's rate.
    "C":  0.85,
}

_POSITION_BLOCK_WEIGHT = {
    "PG": 0.75,
    "SG": 0.90,
    "SF": 0.90,
    "PF": 1.00,
    "C":  1.30,  # bumped from 1.20 — centers should out-block PFs more clearly
}

_POSITION_STEAL_WEIGHT = {
    "PG": 1.25,
    "SG": 1.10,
    "SF": 0.90,
    "PF": 0.70,
    "C":  0.55,
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


def _role_tendency_fg_adj(player: dict) -> float:
    """Compute a FG% efficiency adjustment from role/tendency fit.

    Phase 2: roles carry a list of tendencies they amplify (tendencies_boosted).
    When a player's tendency value exceeds 50, give a small efficiency bump
    (+2% FG% per 10 points above 50, capped at +6%).  When below 30, apply a
    small penalty for skill/role mismatch (-2% FG% per 10 below 30, cap -4%).

    This rewards casting players in roles matching their skillset and slightly
    punishes mismatches — Phase 3 "chaos" coaches deliberately mis-cast players.
    """
    boosted: list = player.get("_role_tendencies", [])
    if not boosted:
        return 0.0
    total_adj = 0.0
    for attr in boosted:
        val = player.get(attr, 50)
        if val > 50:
            total_adj += min((val - 50) / 10.0 * 0.02, 0.06)
        elif val < 30:
            total_adj += max((val - 30) / 10.0 * 0.02, -0.04)
    # Average across all boosted tendencies so a role with 3 tendencies doesn't triple-stack
    return total_adj / len(boosted)


def _player_fg_pct(player: dict) -> float:
    finishing = player.get("finishing", 50)
    shooting_2pt = player.get("shooting_2pt", 50)
    base = 0.38 + (finishing * 0.4 + shooting_2pt * 0.6) / 99 * 0.27
    return max(0.25, min(0.70, base + _role_tendency_fg_adj(player)))


def _player_3pct(player: dict) -> float:
    base = 0.28 + player.get("shooting_3pt", 50) / 99 * 0.22
    # Tendency amplification applies to 3PT% as well when tendency_3pt is boosted
    adj = _role_tendency_fg_adj(player) if "tendency_3pt" in player.get("_role_tendencies", []) else 0.0
    return max(0.20, min(0.60, base + adj))


def _scheme_fit_factor(skill: float, low: float = 40.0, high: float = 80.0) -> float:
    """Linear 0..1 fit factor for skill-conditioned scheme bumps (finding #2).

    skill <= low  -> 0.0 (mismatch -- no bump/suppression applied)
    skill >= high -> 1.0 (full bump/suppression applied)
    Linear ramp in between.

    Mirrors _role_tendency_fg_adj's fit-vs-mismatch shape (bonus for high
    tendency, muted-to-none for low) but keyed off a raw skill rating
    (e.g. shooting_3pt) instead of a role tendency, so a scheme-level
    attempt-rate/tendency bump can be scaled by whether a given player can
    actually convert on the extra volume a coach would be asking for.
    """
    if skill <= low:
        return 0.0
    if skill >= high:
        return 1.0
    return (skill - low) / (high - low)


def _apply_scheme_to_players(players: List[dict], scheme: str) -> List[dict]:
    """Return shallow-copied player list with tendency values nudged by offensive scheme.

    Tweaks ride ON TOP of each player's base tendencies — player identity is preserved
    because these are per-game copies, not mutations of the source dicts.
    All tendencies are capped at [0, 100] after applying.
    """
    if scheme == "balanced" or not scheme:
        # Shallow copy for consistency even when no tweak is applied.
        return [dict(p) for p in players]

    result = [dict(p) for p in players]

    def _clamp(val: float) -> int:
        return max(0, min(100, round(val)))

    if scheme == "ball_movement":
        # Find the star (highest OVR) to lightly reduce their 3-pt tendency.
        # The -5 nerf in the initial implementation suppressed elite-passer
        # stars (Haliburton, Jokic) too aggressively — they ended up at
        # 8-17 PPG when their real-world avg is 18-27. Dialed to -2.
        star_idx = max(range(len(result)), key=lambda i: result[i].get("overall", result[i].get("finishing", 50)))
        for i, p in enumerate(result):
            p["tendency_pass"] = _clamp(p.get("tendency_pass", 50) + 10)
            p["ast_tendency"] = _clamp(p.get("ast_tendency", 50) + 5)
            if i == star_idx:
                p["tendency_3pt"] = _clamp(p.get("tendency_3pt", 50) - 2)

    elif scheme == "isolation":
        star_idx = max(range(len(result)), key=lambda i: result[i].get("overall", result[i].get("finishing", 50)))
        for i, p in enumerate(result):
            if i == star_idx:
                p["tendency_3pt"] = _clamp(p.get("tendency_3pt", 50) + 10)
                p["tendency_drive"] = _clamp(p.get("tendency_drive", 50) + 10)
            else:
                p["tendency_pass"] = _clamp(p.get("tendency_pass", 50) - 5)
                p["tendency_3pt"] = _clamp(p.get("tendency_3pt", 50) - 3)

    elif scheme == "three_heavy":
        # Finding #2 (realism audit): the +12 tendency_3pt bump used to be
        # flat for every player regardless of shooting_3pt -- a 30-rated
        # shooting center got pushed to jack up 3s just like the team's best
        # shooter (he just missed more, since make% was already skill-gated
        # via _player_3pct; only attempt VOLUME was scheme-flat). A real coach
        # wouldn't force a non-shooter into this scheme's shot diet, so the
        # bump now scales with each player's own shooting_3pt via the same
        # fit-vs-mismatch shape _role_tendency_fg_adj uses for role fit.
        for p in result:
            fit = _scheme_fit_factor(p.get("shooting_3pt", 50))
            p["tendency_3pt"] = _clamp(p.get("tendency_3pt", 50) + 12 * fit)

    elif scheme == "inside_out":
        star_idx = max(range(len(result)), key=lambda i: result[i].get("overall", result[i].get("finishing", 50)))
        for i, p in enumerate(result):
            pos = p.get("position", "")
            if pos in ("C", "PF"):
                p["tendency_drive"] = _clamp(p.get("tendency_drive", 50) + 8)
            if i == star_idx:
                p["tendency_3pt"] = _clamp(p.get("tendency_3pt", 50) - 5)

    return result


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

    # Phase 2: Role-based shot mix — 3PA fraction driven by role's fga_3pa_pct,
    # modulated by the player's individual tendency_3pt relative to 30 (the neutral point).
    # This makes a post_anchor (fga_3pa_pct=0.05) stay near the basket regardless of
    # their tendency_3pt value, while a movement_shooter (0.65) fires threes consistently.
    role_3pa_pct = player.get("_role_fga_3pa_pct", None)
    if role_3pa_pct is not None:
        t3 = player.get("tendency_3pt", 50)
        # Modulate ±0-15% around role baseline by individual tendency
        p_three = role_3pa_pct * (1 + (t3 - 30) / 100.0)
        p_three = max(0.02, min(0.92, p_three))
    else:
        # Legacy fallback (no role data stamped)
        t3 = player.get("tendency_3pt", 50)
        p_three = 0.05 + (t3 / 100.0) * 0.30

    # Distribute points: what fraction come from 3s vs 2s vs FTs
    # p_three is fraction of FGA that are 3-pointers
    # three_pts = tpm * 3;  two_pts = fgm_2 * 2;  ft_pts = ftm
    # We start from FGA budget implied by pts and FG%, then split
    # using the role's 3PA fraction.
    role_fta_per_fga = player.get("_role_fta_per_fga", None)
    if role_fta_per_fga is not None:
        t_drive = player.get("tendency_drive", 50)
        # modulate fta_per_fga by drive tendency — high drivers get slight bump
        fta_per_fga = role_fta_per_fga * (1 + (t_drive - 30) / 200.0)
        fta_per_fga = max(0.02, min(0.70, fta_per_fga))
    else:
        t_drive = player.get("tendency_drive", 50)
        fta_per_fga = 0.06 + (t_drive / 100.0) * 0.14

    # Estimate approximate FGA from pts given FG% and ft rate
    # pts ≈ fga * (p_three*three_pct*3 + (1-p_three)*fg_pct*2) + fga*fta_per_fga*0.75
    # Solve for fga, then derive shot counts
    pts_per_fga = (
        p_three * three_pct * 3
        + (1.0 - p_three) * fg_pct * 2
        + fta_per_fga * 0.75
    )
    if pts_per_fga < 0.1:
        pts_per_fga = 0.1
    fga_est = pts / pts_per_fga if pts > 0 else 0
    tpa = max(0, round(fga_est * p_three))
    fga_2 = max(0, round(fga_est * (1.0 - p_three)))
    fta = max(0, round(fga_est * fta_per_fga))

    tpm = max(0, round(tpa * three_pct))
    fgm_2 = max(0, round(fga_2 * fg_pct))
    ftm = max(0, round(fta * 0.75))

    # Recompute pts from derived shot counts; absorb rounding difference into FTM
    computed_pts = tpm * 3 + fgm_2 * 2 + ftm
    if computed_pts != pts:
        delta = pts - computed_pts
        ftm = max(0, ftm + delta)
        fta = max(ftm, fta)

    fgm = fgm_2 + tpm
    fga = fga_2 + tpa

    # Guard: ensure fga >= fgm (can't make more than you attempt)
    if fgm > fga:
        fga = fgm

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

    # Phase 2: Defensive role multipliers — anchor/perimeter/general/passive
    # modulate block, steal, and rebound weights.
    def _def_role_mults(p: dict) -> tuple[float, float, float]:
        """Return (blk_mult, stl_mult, reb_mult) for a player's defensive role."""
        dr = p.get("_role_def_role", "general")
        if dr == "anchor":
            return (1.50, 0.85, 1.15)  # blk_mult bumped 1.20→1.50: rim protectors need a bigger share
        if dr == "perimeter":
            return (0.85, 1.15, 0.90)
        if dr == "passive":
            return (0.85, 0.85, 0.85)
        return (1.0, 1.0, 1.0)  # general

    # reb_tendency is position-relative (50 = average for G/F/C group), so a guard
    # with reb_tendency=81 means "above-average guard" — not "above-average overall."
    # _POSITION_REBOUND_WEIGHT reintroduces the absolute positional gap so that an
    # elite guard (reb_tendency=95, C-weight=1.40 vs SG-weight=0.70) gets ~6 RPG,
    # while a true center with the same tendency gets ~12 RPG.
    reb_weights = [
        (players[i].get("reb_tendency", 50) / 50.0)
        * _POSITION_REBOUND_WEIGHT.get(players[i].get("position", "SF"), 0.85)
        * _def_role_mults(players[i])[2]   # reb_mult from defensive role
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
        players[i].get("defense_tendency", players[i].get("defense", 50))
        * (players[i].get("stl_tendency", 50) / 50.0)
        * (players[i].get("defensive_effort", 50) / 50.0)
        * _POSITION_STEAL_WEIGHT.get(players[i].get("position", "SF"), 0.90)
        * _def_role_mults(players[i])[1]   # stl_mult from defensive role
        * minutes_list[i]
        for i in range(n)
    ]
    stls = _distribute_proportional(rng, stl_total, stl_weights)

    # Team block total: 5-9 (avg 7) — bumped from 4-8 (avg 6). NBA league
    # avg per team is ~4.8 but our sim's compressed distribution means league
    # leaders couldn't reach NBA-realistic 3.5+ bpg at the old total.
    blk_total = rng.randint(5, 9)
    blk_weights = [
        players[i].get("defense_tendency", players[i].get("defense", 50))
        # blk_tendency divisor tightened 50→45 so elite blockers (tendency 85+)
        # get a meaningfully bigger share than average defenders. At /45,
        # a 90-tendency rim protector is 2.0x weight vs 1.0x baseline.
        * (players[i].get("blk_tendency", 50) / 45.0)
        * (players[i].get("defensive_effort", 50) / 50.0)
        * _POSITION_BLOCK_WEIGHT.get(players[i].get("position", "SF"), 1.0)
        * _def_role_mults(players[i])[0]   # blk_mult from defensive role
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
        # Apply the same 42-min starter cap as the auto-allocation branch.
        _STARTER_CAP = 42.0
        overflow = 0.0
        for i in range(len(starters)):
            if minutes_list[i] > _STARTER_CAP:
                overflow += minutes_list[i] - _STARTER_CAP
                minutes_list[i] = _STARTER_CAP
        if overflow > 0.0 and bench:
            bench_start = len(starters)
            bench_total = sum(minutes_list[bench_start:]) or 1.0
            for i in range(bench_start, len(minutes_list)):
                share = minutes_list[i] / bench_total * overflow
                minutes_list[i] = min(minutes_list[i] + share, 38.0)
        # Hard ceiling: no player exceeds 48 minutes.
        minutes_list = [min(m, 48.0) for m in minutes_list]
    else:
        # Auto-allocate minutes so starters land ~33-38 min and bench shares the rest.
        # Weights are chosen so that after normalization to 240 total, a 5-starter
        # roster with 7 bench players produces ~35 min per starter.  The ratio
        # starter_weight / bench_weight ≈ 4:1 achieves this at typical roster sizes.
        #
        # Sparse-lineup guard: teams with 0-2 bench players would give starters 42-48
        # min with pure normalization.  After computing proportional minutes we clamp
        # each starter at 42 min and push the excess onto bench players (capped at 38
        # each) so the total always sums to exactly 240.
        _STARTER_CAP = 42.0
        _BENCH_CAP = 38.0
        starter_weights = [rng.uniform(50, 65) for _ in starters]
        bench_weights = [rng.uniform(10, 18) for _ in bench] if bench else []
        all_weights = starter_weights + bench_weights
        total_w = sum(all_weights)
        minutes_list = [w / total_w * 240 for w in all_weights]
        # Clamp starters and collect overflow.
        overflow = 0.0
        for i in range(len(starters)):
            if minutes_list[i] > _STARTER_CAP:
                overflow += minutes_list[i] - _STARTER_CAP
                minutes_list[i] = _STARTER_CAP
        # Distribute overflow to bench players, capped at _BENCH_CAP.
        if overflow > 0.0 and bench:
            bench_start = len(starters)
            bench_total = sum(minutes_list[bench_start:]) or 1.0
            for i in range(bench_start, len(minutes_list)):
                share = minutes_list[i] / bench_total * overflow
                minutes_list[i] = min(minutes_list[i] + share, _BENCH_CAP)
        # If there is no bench and overflow remains, distribute evenly across starters
        # (up to the hard cap — this handles extreme cases like a 5-man roster).
        elif overflow > 0.0:
            per_starter = overflow / len(starters)
            for i in range(len(starters)):
                minutes_list[i] = min(minutes_list[i] + per_starter, _STARTER_CAP)

    # CA1 (coaching AI realism sweep): blowout garbage-time minutes.
    # sim_game is a single-shot function -- final score margin is already known
    # by the time minutes are computed, so this is a post-processing pass over
    # whatever minutes_list the override/auto-allocate branches above produced,
    # not a live in-game substitution decision the engine has no clock to drive.
    # _BLOWOUT_THRESHOLD/_BLOWOUT_MAX_THRESHOLD/_GARBAGE_TIME_STARTER_FLOOR are
    # disclosed placeholders -- not derived from a formula, tuned by feel.
    _BLOWOUT_THRESHOLD = 20.0
    _BLOWOUT_MAX_THRESHOLD = 30.0
    _GARBAGE_TIME_STARTER_FLOOR = 22.0
    _GARBAGE_TIME_BENCH_CAP = 38.0  # matches the bench cap already used above

    abs_diff = abs(score_diff)
    if abs_diff >= _BLOWOUT_THRESHOLD and bench:
        severity = min(
            1.0, (abs_diff - _BLOWOUT_THRESHOLD) / (_BLOWOUT_MAX_THRESHOLD - _BLOWOUT_THRESHOLD)
        )
        freed = 0.0
        n_starters = len(starters)
        for i in range(n_starters):
            m = minutes_list[i]
            if m > _GARBAGE_TIME_STARTER_FLOOR:
                # Reduction scales from ~15% of excess minutes at the mild end
                # (margin==20) to ~50% at the severe end (margin>=30).
                reduction = (m - _GARBAGE_TIME_STARTER_FLOOR) * (0.15 + 0.35 * severity)
                minutes_list[i] = m - reduction
                freed += reduction
        if freed > 0.0:
            bench_start = n_starters
            bench_total = sum(minutes_list[bench_start:]) or 1.0
            for i in range(bench_start, len(minutes_list)):
                share = minutes_list[i] / bench_total * freed
                minutes_list[i] = min(minutes_list[i] + share, _GARBAGE_TIME_BENCH_CAP)

    # Phase 2: Role-based touch share drives scoring weight distribution.
    # touch_share from player_roles replaces the old OVR × usage_weight^1.55 formula.
    # star_usage_mult and the star-debuff-target logic are neutralised below.
    #
    # Step 1: Base touch share from role (stamped onto player dicts by sim_orchestrator).
    # Step 2: Minutes-tier eligibility penalties (bench can't get starter touches).
    # Step 3: Per-game form noise (keeps game-to-game variance realistic).
    # Step 4: Clutch adjustment (unchanged from Phase 1).
    # Step 5: Renormalize to sum=1 so absolute weights stay consistent.

    _has_role_data = any(p.get("_role_touch_share") is not None for p in players)

    # CA2 (coaching AI realism sweep): usage_weight/star_usage_mult exponent.
    # Softer than the legacy fallback formula's 1.55 -- role-based touch_share
    # already does most of the concentration work here, so usage only needs to
    # nudge it, not drive it. Disclosed placeholder.
    _USAGE_WEIGHT_TS_EXPONENT = 0.6

    if _has_role_data:
        scoring_weights = []
        for i, p in enumerate(players):
            ts = p.get("_role_touch_share", 0.08)
            minutes_tier = p.get("_role_minutes_tier", "rotation")
            # Key penalties off actual allocated minutes, not roster slot index.
            # Slot-based keying was wrong: a benched starter (slot 6) with 25 min
            # played still deserved starter touches; a blowout sub with 4 min didn't.
            player_minutes = minutes_list[i] if i < len(minutes_list) else 0.0

            # Minutes-tier eligibility:
            #   - Starter-role player who played < 24 min: half touch share.
            #   - Non-depth player who played < 12 min: 30% touch share.
            #   (Thresholds: 24 min ≈ typical bench/garbage-time boundary;
            #    12 min ≈ spot-minute contributor.)
            if minutes_tier == "starter" and player_minutes < 24.0:
                ts *= 0.50
            elif minutes_tier != "depth" and player_minutes < 12.0:
                ts *= 0.30

            # CA2: usage_weight is set upstream by cpu_coach_service's usage_mode
            # directives (via sim_persistence._apply_directives -- "feature" x1.4,
            # "conserve" x0.6) but was dead-wired here: this branch never read it,
            # so a "feature" directive had zero effect on scoring share once role
            # data was stamped. Softer exponent than the legacy formula's 1.55
            # since touch_share already carries most of the concentration.
            usage_weight = p.get("usage_weight", 50)
            ts *= (usage_weight / 50.0) ** _USAGE_WEIGHT_TS_EXPONENT

            # Per-game noise (same range as legacy formula, keeps game variance)
            noise = rng.uniform(0.80, 1.20)
            scoring_weights.append(max(ts * noise, 0.001))

        # CA2: star_usage_mult now targets ONLY the single highest-OVR player's
        # weight, applied before renormalization -- a uniform team-wide scalar
        # would cancel out entirely once _distribute_proportional allocates the
        # fixed team score by each player's RELATIVE share of the total weight.
        # Scaling just one player's weight actually shifts that ratio.
        if players and star_usage_mult != 1.0:
            star_idx = max(
                range(len(players)),
                key=lambda i: players[i].get("overall", players[i].get("finishing", 50)),
            )
            scoring_weights[star_idx] *= 1 + (star_usage_mult - 1) * 0.6

        # Renormalize so weights sum to 1.0 (touch share fractions must be proportional)
        _total_ts = sum(scoring_weights)
        if _total_ts > 0:
            scoring_weights = [w / _total_ts for w in scoring_weights]
    else:
        # Legacy fallback: no role data stamped — use original OVR × usage formula.
        # This path should not be reached in normal operation post-Phase-1.
        team_avg_composite = sum(
            p.get("finishing", 50) + p.get("shooting_2pt", 50) + p.get("shooting_3pt", 50)
            for p in players
        ) / max(n, 1)
        scoring_weights = []
        for i, p in enumerate(players):
            pos_w = _POSITION_SCORING_WEIGHT.get(p.get("position", "SF"), 1.0)
            usage_w = (p.get("usage_weight", 50) / 50.0) ** 1.55 * 0.55
            composite = p.get("finishing", 50) + p.get("shooting_2pt", 50) + p.get("shooting_3pt", 50)
            rating_adj = composite / max(team_avg_composite, 1)
            base = (minutes_list[i] / 48.0) * pos_w * rating_adj * usage_w
            noise = rng.uniform(0.75, 1.25)
            scoring_weights.append(max(base * noise, 0.01))

    # star_usage_mult: applied above (CA2) as a targeted nudge to the highest-OVR
    # player's weight when role data is present -- no longer a no-op (was dead-wired
    # pre-CA2). The legacy fallback branch above never read this parameter either
    # (its own usage_w term comes straight from p["usage_weight"], not this
    # function's star_usage_mult arg) -- unchanged, since that path "should not be
    # reached in normal operation" per its own comment.
    # _find_star_debuff_targets: Also a no-op when role data is stamped — the debuff
    # flag is still applied below (man_to_man defense still matters), but touch share
    # concentration is no longer driven by OVR rank.

    # Clutch adjustment: in close games, high-clutch players get more late-game usage.
    if abs(score_diff) < 12:
        clutch_adj = [(p.get("clutch_rating", 50) - 50) / 100.0 for p in players]  # -0.5 to +0.5
        scoring_weights = [max(scoring_weights[i] * (1 + clutch_adj[i] * 0.4), 0.01) for i in range(n)]

    pts_allocated = _distribute_proportional(rng, team_score, scoring_weights)
    for i, p in enumerate(players):
        # Man-to-man star debuff — reduce this player's point allocation by 8%.
        # The flag is set in sim_game when the opposing team plays man_to_man against
        # a star (OVR >= 88).  The debuff is per-game only (set on the adjusted copy).
        debuff = 0.92 if p.get("_star_debuff") else 1.0
        p["_allocated_points"] = max(0, round(pts_allocated[i] * debuff))

    lines = []
    for i, p in enumerate(players):
        started = p.get("is_starter", i < 5)
        line = _build_player_line(
            rng, p, team_id, started, minutes_list[i],
            team_score, players, minutes_list, i, score_diff,
        )
        # Apply three_rate_adj: scale tpa/tpm proportionally, conditioned on
        # this player's own shooting_3pt (finding #2) -- three_rate_adj bundles
        # both the team's own offensive-scheme rate change (e.g. three_heavy's
        # +0.22) and the opponent's defensive-scheme rate change (e.g. zone's
        # -0.12), and previously applied identically to every player on the
        # floor. A coach wouldn't ask a non-shooter to change their 3PA volume
        # either way, so a low-shooting_3pt player now gets little-to-no
        # change while a genuine shooter gets close to the full adjustment.
        if three_rate_adj != 0.0:
            fit = _scheme_fit_factor(p.get("shooting_3pt", 50))
            adj_factor = max(0.0, 1.0 + three_rate_adj * fit)
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
        # Base 1.11 → ~88.8 PPG per side at avg pace (~80 poss), giving league avg ~112.
        # Offense scale bumped to 0.30 on 2026-05-20 — league scoring was
        # tracking ~105 with leading scorers at 24 ppg (NBA leaders are 30+).
        # 0.30 gives ~110 team avg, preserving most of the close-game
        # calibration from the 0.25 cut (down from 0.38 which produced
        # blowouts). At 0.30: 90-OVR → ~108 pts, 70-OVR → ~96 pts, 12-pt
        # gap → ~70% per-game win rate.
        # Defense scale kept at 0.14 for complementary defensive impact.
        base = 1.11 + (off - 60) / (95 - 60) * 0.30
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

    # Fix 2: Per-player tendency nudges based on offensive scheme (per-game copies only).
    home_scheme = h_strat.get("offensive_scheme", "balanced") if h_strat else "balanced"
    away_scheme = a_strat.get("offensive_scheme", "balanced") if a_strat else "balanced"
    home_players_adj = _apply_scheme_to_players(home_players, home_scheme)
    away_players_adj = _apply_scheme_to_players(away_players, away_scheme)

    # Fix 3: Man-to-man star debuff — if the defending team plays man_to_man and the
    # opposing team has a star with OVR >= 88, that star receives an 8% scoring debuff.
    # We tag the target player id in the adjusted list so _build_box_for_team can apply it.
    home_defense = h_strat.get("defensive_scheme", "") if h_strat else ""
    away_defense = a_strat.get("defensive_scheme", "") if a_strat else ""

    def _find_star_debuff_targets(defending_scheme: str, attacking_players: List[dict]) -> set:
        """Return player_ids of opposing stars to debuff under man_to_man defense.

        Phase 2 note: this function's role in concentrating touches is replaced by
        role-based touch_share (player_roles).  It is kept because the −8% scoring
        debuff still models defensive assignment quality — man_to_man coverage on a
        star SHOULD reduce their scoring efficiency, even if touch share no longer
        flows from OVR rank.  The star_usage_mult pathway in _build_box_for_team is
        now a no-op when role data is stamped.
        """
        if defending_scheme != "man_to_man" or not attacking_players:
            return set()
        return {
            p.get("id", p.get("player_id"))
            for p in attacking_players
            if p.get("overall", p.get("finishing", 0)) >= 88
        }

    # Home defense vs away stars; away defense vs home stars.
    away_star_debuff_ids = _find_star_debuff_targets(home_defense, away_players_adj)
    home_star_debuff_ids = _find_star_debuff_targets(away_defense, home_players_adj)

    # Mark debuff targets in adjusted player dicts.
    if away_star_debuff_ids:
        for p in away_players_adj:
            if p.get("id", p.get("player_id")) in away_star_debuff_ids:
                p["_star_debuff"] = True
    if home_star_debuff_ids:
        for p in home_players_adj:
            if p.get("id", p.get("player_id")) in home_star_debuff_ids:
                p["_star_debuff"] = True

    # Defensive opp_* fields from each team's strategy are applied to the OPPONENT's box build.
    home_box = _build_box_for_team(
        rng, home_players_adj, home_team["team_id"], home_score, home_diff,
        minutes_override=home_minutes,
        star_usage_mult=h_strat.get("star_usage_mult", 1.0),
        three_rate_adj=h_strat.get("three_rate_adj", 0.0) + a_strat.get("opp_three_rate_adj", 0.0),
        turnover_adj=h_strat.get("turnover_adj", 0.0) + a_strat.get("opp_turnover_adj", 0.0),
        foul_adj=h_strat.get("foul_adj", 0.0),
    )
    away_box = _build_box_for_team(
        rng, away_players_adj, away_team["team_id"], away_score, away_diff,
        minutes_override=away_minutes,
        star_usage_mult=a_strat.get("star_usage_mult", 1.0),
        three_rate_adj=a_strat.get("three_rate_adj", 0.0) + h_strat.get("opp_three_rate_adj", 0.0),
        turnover_adj=a_strat.get("turnover_adj", 0.0) + h_strat.get("opp_turnover_adj", 0.0),
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
