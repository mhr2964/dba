from __future__ import annotations

import json
import logging
import os
import random
from typing import Optional

log = logging.getLogger(__name__)

STORYLINE_TEMPLATES: dict[str, list[str]] = {
    "big_game": [
        "{player} erupts for {pts} points as the {team} {outcome} the {opp}",
        "{player} goes off for {pts} in a {margin}-point {outcome} over the {opp}",
        "{player} pours in {pts} to carry the {team} past the {opp}",
        "{player} drops {pts} and the {team} make it look easy against the {opp}",
    ],
    "blowout": [
        "{team} dominates the {opp} {score}, biggest win of the season",
        "Statement game: {team} dismantles {opp} by {margin}",
        "{team} leaves no doubt, rolling past {opp} {score}",
        "{opp} had no answer as {team} cruised to a {margin}-point win",
    ],
    "clutch": [
        "{team} survives a late push from {opp}, escaping {score}",
        "Nail-biter: {team} edges out {opp} {score} in a tight one",
        "{team} holds on down the stretch to turn back {opp} {score}",
        "{opp} couldn't complete the comeback — {team} takes it {score}",
    ],
    "triple_double": [
        "{player} records a triple-double ({pts}/{reb}/{ast}) in {team}'s win over {opp}",
        "{player} does a little of everything — {pts}/{reb}/{ast} — as {team} top {opp}",
        "Triple-double night for {player}: {pts} pts, {reb} reb, {ast} ast in {team}'s victory",
    ],
    "streak": [
        "{team} extends their winning streak to {streak} games",
        "{team} snaps {opp}'s {streak}-game win streak",
        "{streak} straight — {team} keep rolling and show no signs of slowing down",
        "{team} cooling off? Not yet — win No. {streak} in a row",
    ],
}


def _interest_score(game: dict) -> float:
    home_score: int = game["home_score"]
    away_score: int = game["away_score"]
    margin = abs(home_score - away_score)

    all_box: list[dict] = game.get("home_box", []) + game.get("away_box", [])
    top_pts = max((line.get("points", 0) for line in all_box), default=0)

    # Blowouts are interesting; close games are interesting; big scorers are interesting.
    # Low margin → high interest for clutch; high margin → blowout interest.
    clutch_score = max(0.0, 10.0 - margin)  # 0–5pt margin gives 5–10
    blowout_score = max(0.0, margin - 20.0)  # only above 20-pt margins
    scorer_score = float(top_pts)

    return clutch_score + blowout_score + scorer_score


def _format_score(home_score: int, away_score: int, home_team: str, away_team: str) -> str:
    return f"{home_score}-{away_score}"


def _generate_for_game(game: dict, teams_by_id: dict, rng: random.Random) -> str | None:
    home_score: int = game["home_score"]
    away_score: int = game["away_score"]
    margin = abs(home_score - away_score)

    home_team_id = game.get("home_team_id")
    away_team_id = game.get("away_team_id")
    winner_id = game.get("winner_team_id")
    loser_id = away_team_id if winner_id == home_team_id else home_team_id

    home_name = teams_by_id.get(home_team_id, {}).get("name", f"Team {home_team_id}")
    away_name = teams_by_id.get(away_team_id, {}).get("name", f"Team {away_team_id}")
    winner_name = teams_by_id.get(winner_id, {}).get("name", f"Team {winner_id}")
    loser_name = teams_by_id.get(loser_id, {}).get("name", f"Team {loser_id}")

    all_box: list[dict] = game.get("home_box", []) + game.get("away_box", [])
    top_scorer = max(all_box, key=lambda row: row.get("points", 0), default=None)

    # Always show winner score first so "X-Y" means winner-loser, never a confusing tie.
    winner_score = home_score if winner_id == home_team_id else away_score
    loser_score = away_score if winner_id == home_team_id else home_score
    score_str = f"{winner_score}-{loser_score}"
    outcome = "defeat"

    # Triple-double takes priority
    for line in all_box:
        reb = line.get("rebounds_off", 0) + line.get("rebounds_def", 0)
        pts = line.get("points", 0)
        ast = line.get("assists", 0)
        if pts >= 10 and reb >= 10 and ast >= 10:
            tmpl = rng.choice(STORYLINE_TEMPLATES["triple_double"])
            player_team_id = line.get("team_id")
            player_team_name = teams_by_id.get(player_team_id, {}).get("name", f"Team {player_team_id}")
            opp_name = away_name if player_team_id == home_team_id else home_name
            return tmpl.format(
                player=line.get("player_name") or f"Player #{line['player_id']}",
                pts=pts,
                reb=reb,
                ast=ast,
                team=player_team_name,
                opp=opp_name,
            )

    if margin >= 25 and top_scorer:
        tmpl = rng.choice(STORYLINE_TEMPLATES["blowout"])
        return tmpl.format(
            team=winner_name,
            opp=loser_name,
            score=score_str,
            margin=margin,
        )

    if top_scorer and top_scorer.get("points", 0) >= 35:
        tmpl = rng.choice(STORYLINE_TEMPLATES["big_game"])
        player_team_id = top_scorer.get("team_id")
        player_team_name = teams_by_id.get(player_team_id, {}).get("name", f"Team {player_team_id}")
        opp_id = away_team_id if player_team_id == home_team_id else home_team_id
        opp_name = teams_by_id.get(opp_id, {}).get("name", f"Team {opp_id}")
        return tmpl.format(
            player=top_scorer.get("player_name") or f"Player #{top_scorer['player_id']}",
            pts=top_scorer["points"],
            team=player_team_name,
            opp=opp_name,
            margin=margin,
            outcome=outcome,
        )

    if margin <= 5:
        tmpl = rng.choice(STORYLINE_TEMPLATES["clutch"])
        return tmpl.format(
            team=winner_name,
            opp=loser_name,
            score=score_str,
        )

    return None


def generate_storylines(
    game_results: list[dict],
    teams_by_id: dict,
    max_storylines: int = 5,
) -> list[str]:
    """
    Picks the most interesting games from a batch and generates natural-language storylines.
    Interest score: blowout margin, top scorer's points, close game (< 5 point margin).
    Returns list of formatted strings ready to post.
    """
    if not game_results:
        return []

    rng = random.Random(sum(g.get("home_score", 0) + g.get("away_score", 0) for g in game_results))

    ranked = sorted(game_results, key=_interest_score, reverse=True)
    storylines: list[str] = []

    for game in ranked:
        if len(storylines) >= max_storylines:
            break
        line = _generate_for_game(game, teams_by_id, rng)
        if line:
            storylines.append(line)

    return storylines


def _build_game_summary(game: dict, teams_by_id: dict) -> Optional[str]:
    """Build a compact one-line game summary for the AI prompt."""
    home_id = game.get("home_team_id")
    away_id = game.get("away_team_id")
    home_name = teams_by_id.get(home_id, {}).get("name", "?")
    away_name = teams_by_id.get(away_id, {}).get("name", "?")
    home_score = game.get("home_score", 0)
    away_score = game.get("away_score", 0)
    winner_id = game.get("winner_team_id")
    winner = home_name if winner_id == home_id else away_name
    loser = away_name if winner_id == home_id else home_name

    all_box = game.get("home_box", []) + game.get("away_box", [])
    top_players = sorted(all_box, key=lambda row: row.get("points", 0), reverse=True)[:3]
    stat_parts = []
    for p in top_players:
        name = p.get("player_name") or f"Player#{p.get('player_id','?')}"
        pts = p.get("points", 0)
        reb = p.get("rebounds_off", 0) + p.get("rebounds_def", 0)
        ast = p.get("assists", 0)
        stat_parts.append(f"{name} {pts}pts/{reb}reb/{ast}ast")

    stats_str = "; ".join(stat_parts) if stat_parts else "no stats"
    margin = abs(home_score - away_score)
    return f"{winner} def. {loser} {max(home_score,away_score)}-{min(home_score,away_score)} ({margin}-pt margin) — {stats_str}"


async def generate_storylines_ai(
    game_results: list[dict],
    teams_by_id: dict,
    max_storylines: int = 5,
) -> list[str]:
    """
    AI-powered headline generation using Claude Haiku. Batches top games into one prompt.
    Falls back to template-based generate_storylines on any failure.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return generate_storylines(game_results, teams_by_id, max_storylines)

    ranked = sorted(game_results, key=_interest_score, reverse=True)
    top_games = ranked[:max_storylines]

    summaries = []
    for g in top_games:
        s = _build_game_summary(g, teams_by_id)
        if s:
            summaries.append(s)

    if not summaries:
        return generate_storylines(game_results, teams_by_id, max_storylines)

    prompt = (
        "You are a terse NBA beat writer. Write one punchy headline per game below. "
        "Each headline must be under 90 characters, read like a real sports brief, and vary in phrasing. "
        "No filler words. No player full names — use last name only. "
        "Return ONLY a valid JSON array of strings, no markdown, no code fences.\n\n"
        + "\n".join(f"{i+1}. {s}" for i, s in enumerate(summaries))
    )

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=api_key)
        message = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        # Strip markdown code fences if the model wraps its JSON output.
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        lines = json.loads(raw)
        if isinstance(lines, list) and all(isinstance(line, str) for line in lines):
            return [line for line in lines if line][:max_storylines]
    except Exception as exc:
        raw_preview = locals().get("raw", "<no response>")[:120]
        log.warning(f"AI storylines failed, falling back to templates: {exc} | response preview: {raw_preview!r}")

    return generate_storylines(game_results, teams_by_id, max_storylines)
