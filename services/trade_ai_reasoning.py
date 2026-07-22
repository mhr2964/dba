"""LLM-generated one-paragraph reasoning per trade side, shown alongside the
letter grade in the trade-completed embed.

Extracted from trade_evaluator.py (Phase 3 opportunistic split, see
HANDOFF.md).
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)


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
        "Grade this DBA trade. Be direct. No filler. No paragraph blocks.\n\n"
        f"TRADE: {trade_summary['team_a_name']} gives {players_a} | "
        f"{trade_summary['team_b_name']} gives {players_b}\n"
        f"{trade_summary['team_a_name']} record: {trade_summary.get('team_a_record', '?')} | "
        f"{trade_summary['team_b_name']} record: {trade_summary.get('team_b_record', '?')}\n"
        f"Grades: {trade_summary['team_a_name']} = **{trade_summary['grade_a']}** | "
        f"{trade_summary['team_b_name']} = **{trade_summary['grade_b']}**\n\n"
        "MANDATORY FORMAT — each team gets exactly two sentences:\n"
        "Sentence 1: the asymmetry (what one side got vs the other, bolding the key player/number).\n"
        "Sentence 2: the rationale (why this fits or hurts that team's situation).\n"
        "No openers. No conclusions. Two sentences per team, period.\n"
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
