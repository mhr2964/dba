"""Trade fairness scoring and letter grading, plus player-archetype
classification used to infer scheme fit.

Extracted from trade_evaluator.py (Phase 3 opportunistic split, see
HANDOFF.md).
"""
from __future__ import annotations

from collections import Counter

from services.trade_value_math import (
    asset_upside_modifier,
    pick_trade_value,
    player_team_specific_value,
    player_trade_value,
)


def evaluate_trade(
    side_a_players: list[dict],
    side_a_picks: list[dict],
    side_b_players: list[dict],
    side_b_picks: list[dict],
    salary_cap: int,
    current_season: int,
    receiving_team_context_a: dict | None = None,
    receiving_team_context_b: dict | None = None,
) -> dict:
    """
    Evaluate a trade from both teams' perspectives.

    When receiving_team_context_a/b are provided, uses player_team_specific_value
    so each side's valuation reflects their roster construction, cap situation,
    and rebuild window.  Falls back to player_market_value when context is None.

    score_a = what team A gives up (valued from team B's perspective when context_b provided)
    score_b = what team B gives up (valued from team A's perspective when context_a provided)

    This means: team A "receives" side_b assets and evaluates them via context_a;
                team B "receives" side_a assets and evaluates them via context_b.
    Fairness: both teams must feel their received value ≥ given value.

    Returns {
        "score_a": float,         # value of side_a assets (from team_b context)
        "score_b": float,         # value of side_b assets (from team_a context)
        "market_score_a": float,  # same without team context (abstract market)
        "market_score_b": float,
        "differential": float,    # abs(score_a - score_b) using team-specific values
        "is_fair": bool,          # team-specific differential < 20% of max side
        "rationale": str,
    }
    """
    # #5: B3's upside modifier previously only reached pass-1 proposal-rank
    # scoring (trade_proposal_scoring._run_incoming_first_for_team) — grading
    # never saw it, so a young/pedigree/award-race centerpiece could get graded
    # as a lopsided loss by the sim's own narrative layer. Applied here to both
    # the team-specific and abstract-market values so score_a/score_b (which
    # grade_trade and the fleecing-floor/B5 math downstream consume) and the
    # market_score_a/b fields agree with the same "this asset is worth more
    # than raw OVR" adjustment the search side already applies.
    def _player_val(p: dict, context: dict | None) -> float:
        if context is not None:
            base = player_team_specific_value(
                p["player"], p["contract"], context, salary_cap,
                p.get("season_stats"),
                p.get("form_modifier", 1.0),
            )
        else:
            base = player_trade_value(
                p["player"], p["contract"], salary_cap,
                p.get("season_stats"),
                p.get("form_modifier", 1.0),
            )
        return base * asset_upside_modifier(p["player"], current_season)

    def _market_val(p: dict) -> float:
        base = player_trade_value(
            p["player"], p["contract"], salary_cap,
            p.get("season_stats"),
            p.get("form_modifier", 1.0),
        )
        return base * asset_upside_modifier(p["player"], current_season)

    # Team-specific scores: side_a valued from team_b's perspective (they receive it)
    score_a = sum(_player_val(p, receiving_team_context_b) for p in side_a_players)
    score_a += sum(pick_trade_value(p["season"], p["round"], current_season) for p in side_a_picks)

    # side_b valued from team_a's perspective (they receive it)
    score_b = sum(_player_val(p, receiving_team_context_a) for p in side_b_players)
    score_b += sum(pick_trade_value(p["season"], p["round"], current_season) for p in side_b_picks)

    # Abstract market scores (always computed for display/logging)
    market_score_a = sum(_market_val(p) for p in side_a_players)
    market_score_a += sum(pick_trade_value(p["season"], p["round"], current_season) for p in side_a_picks)
    market_score_b = sum(_market_val(p) for p in side_b_players)
    market_score_b += sum(pick_trade_value(p["season"], p["round"], current_season) for p in side_b_picks)

    differential = abs(score_a - score_b)
    max_side = max(score_a, score_b, 1.0)
    is_fair = differential < (max_side * 0.20)

    if is_fair:
        rationale = f"Trade is roughly balanced (A gives {score_a:.1f}, B gives {score_b:.1f})."
    else:
        heavier = "A" if score_a > score_b else "B"
        rationale = (
            f"Team {heavier} gives significantly more value "
            f"(A: {score_a:.1f} vs B: {score_b:.1f}, gap {differential:.1f})."
        )

    return {
        "score_a": score_a,
        "score_b": score_b,
        "market_score_a": market_score_a,
        "market_score_b": market_score_b,
        "differential": differential,
        "is_fair": is_fair,
        "rationale": rationale,
    }


def grade_trade(score_a: float, score_b: float) -> tuple[str, str]:
    """
    Assign letter grades to both sides of a trade based on value differential.

    Returns (grade_for_team_a, grade_for_team_b) where team_a receives score_b
    assets and team_b receives score_a assets. Grades reflect who won the trade.

    Differential thresholds are expressed as a fraction of max(score_a, score_b):
      < 5%  → A / A  (even)
      5-15% → B+ / B- (slight edge)
      15-30% → B / C  (clear winner)
      30%+  → A / D  (lopsided)
    """
    max_side = max(score_a, score_b, 1.0)
    diff = abs(score_a - score_b)
    pct = diff / max_side

    if pct < 0.05:
        return "A", "A"

    winner_grade: str
    loser_grade: str
    if pct < 0.15:
        winner_grade, loser_grade = "B+", "B-"
    elif pct < 0.30:
        winner_grade, loser_grade = "B", "C"
    else:
        winner_grade, loser_grade = "A", "D"

    # score_a = total value team_a gives away (team_b receives)
    # score_b = total value team_b gives away (team_a receives)
    # team_a wins the trade if score_a < score_b (receives more than it gives)
    if score_b >= score_a:
        return winner_grade, loser_grade
    return loser_grade, winner_grade


def _player_archetype(player: dict) -> str | None:
    """
    Classify a player into one of five archetypes using tendency columns.
    player dict keys: overall, age, and optionally tendency_3pt, tendency_drive,
    tendency_pass, ast_tendency, reb_tendency, blk_tendency, stl_tendency, position.
    Returns the archetype string or None when no clear archetype matches.

    Priority order matters — a player can only have one label here:
    interior_big > two_way_wing > playmaker > slasher > shooter
    """
    pos = player.get("position", "")
    t3pt = player.get("tendency_3pt", 50)
    tdrive = player.get("tendency_drive", 50)
    tpass = player.get("tendency_pass", 50)
    tast = player.get("ast_tendency", 50)
    treb = player.get("reb_tendency", 50)
    tblk = player.get("blk_tendency", 50)
    tstl = player.get("stl_tendency", 50)
    tdef = (tblk + tstl) // 2  # composite defensive tendency proxy

    if treb > 70 and pos in ("C", "PF"):
        return "interior_big"
    if tdef > 70 and pos in ("SF", "SG"):
        return "two_way_wing"
    if tpass > 65 or tast > 80:
        return "playmaker"
    if tdrive > 65:
        return "slasher"
    if t3pt > 65:
        return "shooter"
    return None


def _team_primary_scheme(receiving_assets: list[dict]) -> str | None:
    """
    Infer the receiving team's primary scheme from the players they're giving away.
    This gives a rough signal of what archetype they already have/need.
    Returns an archetype string or None.
    """
    archetypes: list[str] = []
    for a in receiving_assets:
        if a.get("asset_type") != "player":
            continue
        arch = _player_archetype(a.get("player", {}))
        if arch:
            archetypes.append(arch)
    if not archetypes:
        return None
    # Most common archetype among what they're giving up is what they have plenty of —
    # the complement is what they need, but we return what they have for scheme match.
    return Counter(archetypes).most_common(1)[0][0]
