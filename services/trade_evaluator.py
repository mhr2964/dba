from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)


def _age_multiplier(age: int) -> float:
    # Wider spread: a 22yo is ~3x more valuable than a 39yo all else equal.
    # Prime window (24-28) gets 1.0 as the reference; younger = upside premium, older = steep decay.
    if age <= 21:
        return 1.5   # raw upside
    if age <= 23:
        return 1.4
    if age <= 26:
        return 1.2
    if age <= 30:
        return 1.0   # prime
    if age <= 33:
        return 0.8
    if age <= 36:
        return 0.55
    return max(0.1, 0.4 - (age - 37) * 0.08)  # sharp cliff past 36


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


def player_trade_value(player: dict, contract: dict, salary_cap: int) -> float:
    """
    Score a player's trade value.
    Factors:
    - overall rating (weight 0.5)
    - age: younger = more value. peak at 26-29, drops sharply after 34.
      multiplier: age 18-23 -> 1.2, 24-28 -> 1.1, 29-33 -> 1.0, 34+ -> 0.75 - (age-34)*0.05
    - contract: bad contracts reduce value.
      salary_ratio = salary / (salary_cap * 0.25)  # 1.0 = max contract worth
      years_remaining: 1yr = 0.8 modifier, 2yr = 1.0, 3yr = 1.1, 4yr+ = 0.9 (long commitment risk)
      if salary_ratio > 1.5: value *= 0.6  # overpaid, hard to move
    - overall tiers: 90+ -> premium, 80-89 -> starter, 70-79 -> role, <70 -> bench
    Returns a float score (higher = more valuable).
    """
    overall = player.get("overall", 0)
    age = player.get("age", 28)
    salary = contract.get("salary", 0)
    years_remaining = contract.get("years_remaining", 1)

    base = overall * 0.5

    age_mult = _age_multiplier(age)
    contract_mod = _contract_modifier(salary, years_remaining, salary_cap)

    value = base * age_mult * contract_mod
    return round(value, 2)


def pick_trade_value(season: int, round_num: int, current_season: int) -> float:
    """
    Score a draft pick's value.
    - Round 1 future: base 40.0. Decreases slightly the further out: -5 per season gap.
    - Round 2: base 10.0, same decay.
    - Current season pick: multiply by 1.5 (known lottery position).
    Returns a float score.
    """
    base = 40.0 if round_num == 1 else 10.0
    season_gap = max(0, season - current_season)
    value = base - (5.0 * season_gap)
    value = max(0.0, value)
    if season_gap == 0:
        value *= 1.5
    return round(value, 2)


def evaluate_trade(
    side_a_players: list[dict],
    side_a_picks: list[dict],
    side_b_players: list[dict],
    side_b_picks: list[dict],
    salary_cap: int,
    current_season: int,
) -> dict:
    """
    Returns {
        "score_a": float,   # total value of assets team A gives up
        "score_b": float,   # total value of assets team B gives up
        "differential": float,  # abs(score_a - score_b)
        "is_fair": bool,    # differential < 20% of max(score_a, score_b)
        "rationale": str,   # human-readable summary
    }
    """
    score_a = sum(
        player_trade_value(p["player"], p["contract"], salary_cap)
        for p in side_a_players
    )
    score_a += sum(
        pick_trade_value(p["season"], p["round"], current_season)
        for p in side_a_picks
    )

    score_b = sum(
        player_trade_value(p["player"], p["contract"], salary_cap)
        for p in side_b_players
    )
    score_b += sum(
        pick_trade_value(p["season"], p["round"], current_season)
        for p in side_b_picks
    )

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


def cpu_should_accept(
    cpu_team_mode: str,
    assets_receiving: list,
    assets_giving: list,
    evaluation: dict,
    salary_cap: int,
    current_cap_used: int,
) -> tuple[bool, str]:
    """
    Returns (accept: bool, reason: str).
    Rules:
    - Reject if evaluation['differential'] > 25% of max side
    - Rebuilding CPU: prefers picks > vets. Accepts if receiving picks or youth (age < 26).
    - Contending CPU: prefers proven players. Reluctant to give picks.
    - Reject if accepting would put CPU over salary_cap.
    - Never give up a player with overall >= 88 unless rebuilding AND getting 2+ first-rounders.
    """
    score_a = evaluation["score_a"]
    score_b = evaluation["score_b"]
    max_side = max(score_a, score_b, 1.0)
    differential = evaluation["differential"]

    # CPU is the counterparty — score_b is what CPU gives, score_a is what CPU receives.
    # assets_receiving = what CPU gets, assets_giving = what CPU gives.

    if differential > max_side * 0.25:
        losing_side = score_b > score_a
        if losing_side:
            return False, "CPU evaluated the trade as too lopsided against its interests."

    giving_players = [a for a in assets_giving if a.get("asset_type") == "player"]
    receiving_picks = [a for a in assets_receiving if a.get("asset_type") == "pick"]
    receiving_players = [a for a in assets_receiving if a.get("asset_type") == "player"]

    for asset in giving_players:
        player = asset.get("player", {})
        if player.get("overall", 0) >= 88:
            if cpu_team_mode != "rebuilding":
                return False, "CPU refuses to trade away a franchise cornerstone."
            first_rounders_incoming = sum(
                1 for p in receiving_picks
                if p.get("pick", {}).get("round", 2) == 1
            )
            if first_rounders_incoming < 2:
                return False, "CPU won't give up a star player without at least 2 first-round picks in return."

    incoming_salary = sum(
        a.get("contract", {}).get("salary", 0)
        for a in assets_receiving
        if a.get("asset_type") == "player"
    )
    outgoing_salary = sum(
        a.get("contract", {}).get("salary", 0)
        for a in assets_giving
        if a.get("asset_type") == "player"
    )
    net_cap_change = incoming_salary - outgoing_salary
    if current_cap_used + net_cap_change > salary_cap:
        return False, "Accepting this trade would put CPU over the salary cap."

    if cpu_team_mode == "rebuilding":
        has_picks = len(receiving_picks) > 0
        has_youth = any(
            a.get("player", {}).get("age", 30) < 26
            for a in receiving_players
        )
        if has_picks or has_youth:
            return True, "CPU accepts — acquiring picks and young talent fits the rebuild."
        return False, "CPU declined — rebuilding teams need picks and youth, not veteran salaries."

    if cpu_team_mode == "contending":
        giving_picks = [a for a in assets_giving if a.get("asset_type") == "pick"]
        if giving_picks and not receiving_players:
            return False, "CPU declines — contending teams protect their future draft capital."
        high_value_incoming = any(
            a.get("player", {}).get("overall", 0) >= 80
            for a in receiving_players
        )
        if high_value_incoming:
            return True, "CPU accepts — acquiring a proven starter strengthens the contending roster."
        return False, "CPU declined — assets don't meaningfully improve the contending roster."

    # developing mode: balanced, accept fair trades
    if evaluation["is_fair"]:
        return True, "CPU accepts — trade is balanced and fits team development."
    return False, "CPU declined — trade doesn't offer sufficient value."


async def get_ai_reasoning(trade_summary: dict, api_key: str | None) -> dict[str, str]:
    """
    Generate one-paragraph AI reasoning per trade side using Claude Haiku.

    trade_summary keys:
        team_a_name, team_b_name  — full team names
        team_a_record             — e.g. "32-18"
        team_b_record             — e.g. "20-30"
        team_a_players            — list of {full_name, position, overall, age}
        team_b_players            — list of {full_name, position, overall, age}
        grade_a, grade_b          — letter grades already assigned

    Returns {"team_a": "...", "team_b": "..."} or {} on failure.
    Silent fallback — never raises.
    """
    if not api_key:
        return {}

    def _player_line(p: dict) -> str:
        return f"{p.get('full_name', '?')} ({p.get('position', '?')}, OVR {p.get('overall', '?')}, age {p.get('age', '?')})"

    players_a = ", ".join(_player_line(p) for p in trade_summary.get("team_a_players", [])) or "picks only"
    players_b = ", ".join(_player_line(p) for p in trade_summary.get("team_b_players", [])) or "picks only"

    prompt = (
        "Grade this NBA trade. Be direct, 1-2 sentences per side. No filler.\n\n"
        f"TRADE: {trade_summary['team_a_name']} gives {players_a} | "
        f"{trade_summary['team_b_name']} gives {players_b}\n"
        f"{trade_summary['team_a_name']} record: {trade_summary.get('team_a_record', '?')} | "
        f"{trade_summary['team_b_name']} record: {trade_summary.get('team_b_record', '?')}\n"
        f"Grades already assigned: {trade_summary['team_a_name']} = {trade_summary['grade_a']} | "
        f"{trade_summary['team_b_name']} = {trade_summary['grade_b']}\n\n"
        "For each team write: 1 sentence explaining why that grade fits, "
        "1 sentence about where this team is headed.\n"
        'Return JSON: {"teamA": "...", "teamB": "..."}'
    )

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=api_key)
        message = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        # Strip markdown code fences if the model wraps its JSON output.
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and "teamA" in parsed and "teamB" in parsed:
            return {"team_a": str(parsed["teamA"]), "team_b": str(parsed["teamB"])}
    except Exception as exc:
        log.warning(f"AI trade reasoning failed, skipping: {exc}")

    return {}
