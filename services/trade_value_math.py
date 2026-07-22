"""Pure trade-valuation math: player value, pick value, and their modifier
components. No DB access, no async, no discord -- these are the formulas
that everything else in the trade system compounds on top of.

Extracted from trade_evaluator.py (Phase 3 opportunistic split, see
HANDOFF.md) along with trade_context_builder.py, trade_grading.py,
cpu_trade_acceptance.py, and trade_ai_reasoning.py.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# Fleecing-floor constants
# ---------------------------------------------------------------------------
# DEFAULT floor (0.85) applies to all win-now and fringe-playoff teams.
# REBUILD floor (0.70) kicks in for rebuild-adjacent teams ONLY when the
# incoming side contains a young, quality player — they're buying age, not value.
_REBUILD_MODES: frozenset[str] = frozenset({"rebuilding", "soft_rebuild", "developing"})


DEFAULT_FLEECING_FLOOR: float = 0.85


REBUILD_BUYS_YOUNG_FLOOR: float = 0.78


# OVR normalization constant so OVR 80 anchors at 40.0 in player_trade_value.
# Derived: 80 ** 1.45 = 488.94  →  _OVR_NORM = 488.94 / 40 = 12.2236
_OVR_NORM: float = 12.2236


# OVR floor for "elite" lead-role players requiring both a starter AND a 1st-round
# pick as return. Guards against cornerstone fire-sales via value math loopholes.
_ELITE_OVR_FLOOR: int = 82


def _effective_fleecing_floor(
    cpu_team_mode: str,
    assets_receiving: list[dict],
) -> tuple[float, bool]:
    """Return (floor, is_rebuild_softened).

    Softens the fleecing floor from 0.85 → 0.70 when:
      1. cpu_team_mode is in _REBUILD_MODES (rebuilding / soft_rebuild / developing)
      2. At least one incoming player is age ≤ 25 AND overall ≥ 75

    Both conditions must hold simultaneously.  Win-now / play_in_fringe teams always
    get the hard 0.85 floor regardless of who is coming back.
    """
    if cpu_team_mode not in _REBUILD_MODES:
        return DEFAULT_FLEECING_FLOOR, False
    has_young_buy = any(
        a.get("asset_type") == "player"
        and (
            (
                (a.get("player") or {}).get("age", 99) <= 23
                and (a.get("player") or {}).get("overall", 0) >= 78
            )
            or (
                (a.get("player") or {}).get("age", 99) <= 24
                and (a.get("player") or {}).get("overall", 0) >= 80
            )
        )
        for a in assets_receiving
    )
    if has_young_buy:
        return REBUILD_BUYS_YOUNG_FLOOR, True
    return DEFAULT_FLEECING_FLOOR, False


def _expected_ppg(ovr: int) -> float:
    """Rough expected PPG from OVR, calibrated to observed sim distribution.

    OVR 95 → ~33 PPG, OVR 90 → ~28 PPG, OVR 85 → ~22 PPG,
    OVR 80 → ~15 PPG, OVR 75 → ~10 PPG, OVR 65 → ~4 PPG.
    Linear from OVR 65 baseline (was OVR 70 floor — that caused sub-70 players
    to compute expected=0, which made any positive actual PPG produce ratio=inf
    and form_modifier=1.15 across the board, inflating bench players in
    trade evaluation. Now sub-70s get a real-but-low expected PPG.)
    """
    return max(0.5, (ovr - 65) * 1.0)


def _expected_apg(ovr: int, position: str) -> float:
    """Rough expected APG for ball-handlers (PG/SG) — used for assist bonus."""
    if position in ("PG", "SG"):
        return max(0.0, (ovr - 70) * 0.18)
    return 0.0


def _ratio_to_modifier(ratio: float) -> float:
    """Map actual/expected ratio → multiplier clamped to [0.85, 1.30].

    Elite production earns a meaningful bump above the old 1.15 ceiling:
      ratio >= 1.50 → 1.30  (elite: 50%+ above expected)
      ratio >= 1.30 → 1.20  (great: 30-50% above)
      ratio >= 1.15 → 1.10  (good: 15-30% above)
      ratio == 1.00 → 1.00  (neutral)
      ratio <= 0.80 → 0.85  (underperforming — floor unchanged)

    Linear interpolation between each adjacent pair of breakpoints.

    Example — Harden OVR 88: expected_ppg = 23, actual 28 → ratio 1.22
      Falls in [1.15, 1.30]: lerp → ≈ 1.11 from PPG alone; assist bonus can
      push to 1.13–1.16, giving a meaningful star-production premium.
    """
    if ratio >= 1.50:
        return 1.30
    if ratio >= 1.30:
        # lerp [1.30, 1.50] → [1.20, 1.30]
        return 1.20 + (ratio - 1.30) / 0.20 * 0.10
    if ratio >= 1.15:
        # lerp [1.15, 1.30] → [1.10, 1.20]
        return 1.10 + (ratio - 1.15) / 0.15 * 0.10
    if ratio >= 1.0:
        # lerp [1.00, 1.15] → [1.00, 1.10]
        return 1.0 + (ratio - 1.0) / 0.15 * 0.10
    if ratio <= 0.8:
        return 0.85
    # lerp [0.80, 1.00] → [0.85, 1.00]
    return 1.0 - (1.0 - ratio) / 0.20 * 0.15


def apply_form(base_value: float, form_modifier: float) -> float:
    """Apply a pre-computed form modifier to a base trade value."""
    return round(base_value * form_modifier, 2)


def experience_premium(
    age: int,
    position: str,
    gp: int | None,
    ppg: float | None,
    apg: float | None,
    ovr: int,
) -> float:
    """
    Proven veteran premium — up to +15% on top of base × form_modifier.

    A player earns a premium based on:
    - NBA experience (proxy: age − 22 = years_exp). 4+ yrs = candidate.
    - Durability this season: GP ≥ 50 = proven durable; GP ≥ 70 = workhorse.
    - Playmaker production above expectation (PG/SG with APG above heuristic + GP ≥ 50).

    Formula:
      exp_bump       = min(0.10, years_exp × 0.012)  — caps at 10% by ~year 8
      durability_bump = 0.03 if GP ≥ 70 else 0.02 if GP ≥ 50 else 0
      play_bump      = 0.05 for PG/SG overperforming expected APG with 50+ GP
      total premium = 1.0 + exp_bump + durability_bump + play_bump  (cap 1.15)

    Returns 1.0 for players with < 4 years experience or fewer than 30 GP.

    Example — VanVleet (age 32, OVR 81, 66 GP, 8.2 APG, PG):
      years_exp = 10  → exp_bump = min(0.10, 0.12) = 0.10
      durability = GP 66 ≥ 50 → 0.02
      expected APG ≈ max(2, (81−70)×0.4) = 4.4; 8.2 ≥ 4.4×1.15 → play_bump = 0.05
      premium = 1.0 + 0.10 + 0.02 + 0.05 = 1.17  (≤ 1.15 cap → 1.15)
    """
    years_exp = max(0, age - 22)
    # Premium only applies to established contributors (OVR ≥ 75).
    # Sub-75 role players are already OVR-discounted; a veteran premium would
    # inflate fringe bench players above their actual market value.
    if years_exp < 4 or gp is None or gp < 30 or ovr < 75:
        return 1.0

    exp_bump = min(0.10, years_exp * 0.012)
    durability_bump = 0.03 if gp >= 70 else (0.02 if gp >= 50 else 0.0)

    play_bump = 0.0
    if position in ("PG", "SG") and apg is not None and gp >= 50:
        expected_apg = max(2.0, (ovr - 70) * 0.4)
        if apg >= expected_apg * 1.15:
            play_bump = 0.05

    return min(1.15, 1.0 + exp_bump + durability_bump + play_bump)


def _age_multiplier(age: int, overall: int = 75) -> float:
    """Age value multiplier, tier-scaled by OVR.

    Stars age more gracefully than role players — a 35-year-old superstar
    putting up elite numbers retains most of their trade value (LeBron-tier),
    while a 35-year-old bench player has none. Previously the curve was flat
    by OVR which crushed late-career stars like Harden (age 35 OVR 88 ended
    up at 0.55 multiplier — same as a 35-year-old 12th man).

    Three tiers:
    - Star tier (OVR 85+): gentle decline, retains 80%+ through age 36
    - Quality tier (OVR 80-84): moderate decline
    - Role tier (OVR < 80): original steep curve (career-arc reality for non-stars)
    """
    # Granularity added in 24-26 band (was flat) and post-30 decline steepened
    # so young+good carries a meaningfully higher premium than older+same-OVR.
    if overall >= 85:
        if age <= 21:
            return 1.45
        if age <= 23:
            return 1.35
        if age == 24:
            return 1.25
        if age == 25:
            return 1.20
        if age == 26:
            return 1.15
        if age <= 30:
            return 1.0
        if age <= 33:
            return 0.92
        if age <= 36:
            return 0.78
        return max(0.55, 0.70 - (age - 37) * 0.05)
    if overall >= 80:
        if age <= 21:
            return 1.50
        if age <= 23:
            return 1.38
        if age == 24:
            return 1.28
        if age == 25:
            return 1.22
        if age == 26:
            return 1.15
        if age <= 29:
            return 1.0
        if age == 30:
            return 0.95
        if age <= 33:
            return 0.82
        if age <= 36:
            return 0.60
        return max(0.30, 0.50 - (age - 37) * 0.08)
    # Role / bench tier — original brutal curve, slightly steeper at the edges
    if age <= 21:
        return 1.55
    if age <= 23:
        return 1.42
    if age == 24:
        return 1.28
    if age == 25:
        return 1.20
    if age == 26:
        return 1.12
    if age <= 30:
        return 1.0
    if age <= 33:
        return 0.78
    if age <= 36:
        return 0.55
    return max(0.1, 0.4 - (age - 37) * 0.08)


def _upside_modifier(age: int, overall: int) -> float:
    """Return a trajectory multiplier in [0.85, 1.10] based on age/OVR upside profile.

    Captures the market reality that two players with identical OVR/age can have
    very different trade value depending on whether the league expects growth.

    Tiers (evaluated in order):
    1. Young upside (age ≤ 24): 1.10 — still developing, breakout possible.
    2. Mid-career star (age 25-30, OVR ≥ 84): 1.05 — high ceiling, prime window.
    3. Mid-career role player (age 25-30, OVR < 82): 0.90 — "DiVincenzo case."
       Established role player; no realistic breakout upside.
    4. Mid-career middle tier (age 25-30, OVR 82-83): 1.0 — ambiguous trajectory.
    5. Veteran (age 31+): 1.0 — no upside discount; age_multiplier already handles
       decline. Applying an additional penalty here would double-count.

    Example — DiVincenzo (OVR 79, age 27):
      age 27 in [25-30], OVR 79 < 82 → upside_modifier = 0.90 (~10% discount)

    Example — 27yo with OVR 86:
      age 27 in [25-30], OVR 86 ≥ 84 → upside_modifier = 1.05 (unchanged tier)
    """
    if age <= 24:
        return 1.10
    if age <= 30:
        if overall >= 84:
            return 1.05
        if overall < 82:
            return 0.90
        # OVR 82-83: neutral
        return 1.0
    # age 31+
    return 1.0


def defensive_impact_modifier(
    player: dict,
    season_stats: dict | None = None,
) -> float:
    """Bump value for defensive aces whose impact isn't captured in PPG/APG.

    Returns a multiplier in [1.0, 1.20].

    Two eligibility paths (either triggers the modifier):

    Path A — tendency-based (formula-driven):
      defense_tendency >= 75 AND avg(blk_tendency, stl_tendency) >= 70.
      Works well for players whose tendency formula captured their defense.

    Path B — static-attribute fallback:
      player.defense >= 85.  Catches elite defenders (Gobert, Bam, AD, etc.)
      whose per-game stl/blk numbers are suppressed by the center-average baseline
      even though their curated defense attribute correctly marks them as elite.
      Multipliers from the attribute tier:
        defense >= 92 → 1.20 (DPOY-tier)
        defense 88-91 → 1.15
        defense 85-87 → 1.10
      If season stats also show a defensive_score (bpg*1.5 + spg*1.2) >= 3.0,
      the stat-path cap of 1.20 applies (no double-bump above that ceiling).

    Caps at 1.20 — a true DPOY-tier player gets +20% value, not double their OVR.
    Non-eligible players always return 1.0 so the modifier is invisible to everyone else.
    """
    defense_tendency = player.get("defense_tendency", 0)
    blk_tendency = player.get("blk_tendency", 0)
    stl_tendency = player.get("stl_tendency", 0)
    defense_attr = player.get("defense", 0) or 0

    def_avg = (blk_tendency + stl_tendency) / 2.0

    # Path A: tendency-based eligibility gate.
    tendency_eligible = defense_tendency >= 75 and def_avg >= 70

    # Path B: static defense-attribute gate for curated elite defenders.
    attr_eligible = defense_attr >= 85

    if not tendency_eligible and not attr_eligible:
        return 1.0

    # Compute stat-based modifier when sufficient games are available.
    # This applies to both paths — a big block/steal season earns full 1.20.
    if season_stats is not None and season_stats.get("games_played", 0) >= 30:
        bpg = season_stats.get("bpg", 0.0) or 0.0
        spg = season_stats.get("spg", 0.0) or 0.0
        # Blocks weighted more (rim protection counts more than poke-steals).
        defensive_score = bpg * 1.5 + spg * 1.2
        if defensive_score >= 3.0:
            return 1.20
        if defensive_score >= 2.0:
            return 1.10
        if defensive_score >= 1.5:
            return 1.05

        # Stat path didn't clear minimum threshold. Fall through to attribute path
        # if that's what made the player eligible — don't return 1.0 on a bad
        # sample for someone with defense=90.
        if tendency_eligible:
            return 1.0
        # attr_eligible path: attribute tier applies even when stat sample is weak.

    # Attribute-tier multiplier (Path B or early season with tendency eligibility).
    if attr_eligible:
        if defense_attr >= 92:
            return 1.20
        if defense_attr >= 88:
            return 1.15
        # 85-87
        return 1.10

    # tendency_eligible, no useful stats — benefit of the doubt.
    return 1.08


def _contract_modifier(salary: int, years_remaining: int, salary_cap: int) -> float:
    salary_ratio = salary / (salary_cap * 0.25)

    if years_remaining == 1:
        years_mod = 0.8
    elif years_remaining == 2:
        years_mod = 1.0
    elif years_remaining == 3:
        years_mod = 1.1
    else:
        years_mod = 0.9

    modifier = years_mod
    if salary_ratio > 1.5:
        modifier *= 0.6
    return modifier


def player_trade_value(
    player: dict,
    contract: dict,
    salary_cap: int,
    season_stats: dict | None = None,
    form_modifier: float = 1.0,
) -> float:
    """
    Score a player's trade value — the abstract market value for a fair trade.

    Factors (all compound):
    - overall rating (exponential curve: overall ** 1.45, normalized so OVR 80 anchors
      at 40, matching the R1 pick base value).  The steeper exponent concentrates value
      at the top tier: the OVR 89 vs 88 adjacent gap is ~1.7% (vs 1.5% at ^1.3), and
      the OVR 95 vs 80 span is ~30% (vs 25% at ^1.3).  Bench players (< 80) get a
      larger discount.  Picks stay in scale because the normalization keeps the
      OVR-80 anchor at ~40.
      Normalization: 80 ** 1.45 = 488.94  →  _OVR_NORM = 488.94 / 40 = 12.2236
    - age: younger = more value. peak at 24-26, drops sharply after 36.
    - upside: trajectory modifier based on age/OVR tier (see _upside_modifier).
      Mid-career role players (age 25-30, OVR < 82) get 0.90 — "no-growth" discount.
    - contract: bad contracts reduce value.
      salary_ratio = salary / (salary_cap * 0.25)  # 1.0 = max contract worth
      years_remaining: 1yr = 0.8 modifier, 2yr = 1.0, 3yr = 1.1, 4yr+ = 0.9 (long commitment risk)
      if salary_ratio > 1.5: value *= 0.6  # overpaid, hard to move
    - season_stats (optional): dict with keys ppg, apg, games_played.
      When provided, applies experience_premium() — up to +15% for proven veterans
      with strong durability and playmaking track record.
    - form_modifier (optional, default 1.0): pre-computed PPG-based performance modifier.
      Pass the value from compute_form_map to include in-season form in the final value.
      Now capped at 1.30 (was 1.15) for elite production.

    Formula: base × age_mult × upside_mod × contract_mod × form_modifier × exp_premium × defensive_impact_mod.

    defensive_impact_mod (1.0–1.20): bumps elite defenders whose value isn't
    captured in PPG/APG.  Two paths: tendency gate (defense_tendency >= 75 AND
    avg(blk,stl) >= 70) OR static-attribute gate (player.defense >= 85).
    Attribute tier: >=92→1.20, 88-91→1.15, 85-87→1.10.
    Returns 1.0 for all other players — invisible to them.

    Example — VanVleet (OVR 81, age 32, 66 GP, 8.2 APG, PG, form_modifier=1.15):
      base ≈ 47.9; age_mult = 0.8; upside_mod = 1.0 (age 32 → veteran tier)
      contract_mod = 0.8 → raw ≈ 30.6
      form_modifier = 1.15; exp_premium = 1.15 → final ≈ 40.5

    Example — DiVincenzo (OVR 79, age 27, role player):
      base ≈ 43.7; age_mult = 1.0 (role tier, age 27-30)
      upside_mod = 0.90 (age 25-30, OVR < 82 — "filled in" role player)
      contract_mod ≈ 1.0 → after upside: ~39.3 vs ~43.7 without the modifier
    """
    overall = player.get("overall", 0)
    age = player.get("age", 28)
    position = player.get("position", "")
    salary = contract.get("salary", 0)
    years_remaining = contract.get("years_remaining", 1)

    # Exponential OVR curve normalized so OVR 80 anchors at 40.0.  See _OVR_NORM.
    base = (overall ** 1.45) / _OVR_NORM

    age_mult = _age_multiplier(age, overall)
    upside_mod = _upside_modifier(age, overall)
    contract_mod = _contract_modifier(salary, years_remaining, salary_cap)
    def_mod = defensive_impact_modifier(player, season_stats)

    value = base * age_mult * upside_mod * contract_mod * form_modifier * def_mod

    # Apply proven-veteran experience premium when season stats are available.
    if season_stats is not None:
        gp = season_stats.get("games_played")
        ppg = season_stats.get("ppg")
        apg = season_stats.get("apg")
        exp_mult = experience_premium(age, position, gp, ppg, apg, overall)
        value *= exp_mult

    return round(value, 2)


# player_trade_value is the canonical market-value function.
# Layer 3 adds player_team_specific_value on top; callers that only need the
# abstract fair-market price should continue calling player_trade_value directly.
player_market_value = player_trade_value


def player_team_specific_value(
    player: dict,
    contract: dict,
    receiving_team_context: dict | None,
    salary_cap: int,
    season_stats: dict | None = None,
    form_modifier: float = 1.0,
) -> float:
    """
    Return how much this player is worth specifically to the receiving team.

    Starts from player_market_value, then compounds team-context modifiers:

    1. Cap fit: if contract.salary + team_current_payroll > salary_cap * 1.20,
       multiply by 0.80 — team can't absorb the player without major cuts.

    2. Roster construction fit:
       - If the player's position has 3+ OVR-75+ players already: × 0.85 (saturated)
       - If the player's position has 0-1 OVR-75+ players: × 1.10 (needed)
       - Otherwise: × 1.0

    3. Window match (receiving team mode × player age):
       - contending + age ≥ 33: × 1.05  (win-now help worth premium)
       - rebuilding + age ≥ 30: × 0.70  (old vet doesn't fit timeline)
       - rebuilding + age ≤ 24: × 1.15  (young asset, perfect fit)
       - soft_rebuild + age ≥ 32: × 0.80
       - all other combos: × 1.0

    All three modifiers compound on market value.
    Falls back to market value when receiving_team_context is None.

    receiving_team_context dict keys:
        team_id (int)
        mode (str): one of contending / play_in_fringe / soft_rebuild / rebuilding / developing
        current_payroll (int): sum of all active salaries on the receiving roster
        position_counts (dict[str, int]): position → count of OVR-75+ players on roster
    """
    market_val = player_market_value(player, contract, salary_cap, season_stats, form_modifier)

    if receiving_team_context is None:
        return market_val

    age = player.get("age", 28)
    position = player.get("position", "")
    salary = contract.get("salary", 0)
    mode = receiving_team_context.get("mode", "developing")
    current_payroll = receiving_team_context.get("current_payroll", 0)
    position_counts = receiving_team_context.get("position_counts", {})

    # 1. Cap fit modifier
    if salary + current_payroll > salary_cap * 1.20:
        cap_fit = 0.80
    else:
        cap_fit = 1.0

    # 2. Roster construction fit modifier
    pos_count = position_counts.get(position, 0)
    if pos_count >= 3:
        roster_fit = 0.85
    elif pos_count <= 1:
        roster_fit = 1.10
    else:
        roster_fit = 1.0

    # 3. Window match modifier
    if mode == "contending" and age >= 33:
        window_match = 1.05
    elif mode == "rebuilding" and age >= 30:
        window_match = 0.70
    elif mode == "rebuilding" and age <= 24:
        window_match = 1.15
    elif mode == "soft_rebuild" and age >= 32:
        window_match = 0.80
    else:
        window_match = 1.0

    team_value = market_val * cap_fit * roster_fit * window_match
    return round(team_value, 2)


def pick_trade_value(
    season: int,
    round_num: int,
    current_season: int,
    team_win_pct: float | None = None,
) -> float:
    """
    Score a draft pick's value.

    Base values (recalibrated to match realistic NBA pick market):
    - R1 base: 28.0  (most R1s are starter-or-bust; top-3 slots are the rare elite)
    - R2 base:  4.0  (dart throw; fringe rotation player if hit)
    - Decay: −5 per season gap from current, floor at 1.0 (far-future picks never zero)
    - Current-season multiplier: ×1.3 (knowing position helps, but not dramatically)
    - team_win_pct modifier for R1 picks only (lottery vs contender slot matters):
        win_pct 0.25 → ×~1.35  (lottery team, high pick)
        win_pct 0.50 → ×1.0    (average, neutral)
        win_pct 0.70 → ×~0.73  (contender, late pick)

    Reference values:
      R2 current season:            4.0 × 1.3            = 5.2
      R1 current, contender (0.70): 28.0 × 1.3 × 0.73   = 26.6
      R1 next season, average:      (28.0 − 5) × 1.0     = 23.0
      R2 3 yrs out:                 max(1.0, 4.0 − 15)   = 1.0
      R1 3 yrs out:                 max(1.0, 28.0 − 15)  = 13.0

    Pass team_win_pct=None to skip record adjustment (unknown team or pre-season).
    Returns a float score.
    """
    base = 28.0 if round_num == 1 else 4.0
    season_gap = max(0, season - current_season)
    value = base - (5.0 * season_gap)
    value = max(1.0, value)  # far-future picks never decay to zero
    if season_gap == 0:
        value *= 1.3

    # Apply team-quality discount/premium for 1st-round picks when record is known.
    # win_pct 0.25  → multiplier ~1.35  (lottery team, high pick)
    # win_pct 0.50  → multiplier ~1.0   (average, neutral)
    # win_pct 0.70  → multiplier ~0.73  (contender, low pick)
    if round_num == 1 and team_win_pct is not None:
        # Linear: 1.0 + (0.5 - win_pct) * 0.70, clamped [0.50, 1.60]
        record_mult = 1.0 + (0.5 - team_win_pct) * 0.70
        record_mult = max(0.50, min(1.60, record_mult))
        value *= record_mult

    return round(value, 2)
