from __future__ import annotations

import random

STORYLINE_TEMPLATES: dict[str, list[str]] = {
    "big_game": [
        "{player} erupts for {pts} points as the {team} {outcome} the {opp}",
        "{player} goes off for {pts} in a {margin}-point {outcome} over the {opp}",
    ],
    "blowout": [
        "{team} dominates the {opp} {score}, biggest win of the season",
        "Statement game: {team} destroys {opp} by {margin}",
    ],
    "clutch": [
        "{team} survives a scare against {opp}, winning {score}",
        "Nail-biter: {team} edges out {opp} {score} in a tight one",
    ],
    "triple_double": [
        "{player} records a triple-double ({pts}/{reb}/{ast}) in {team}'s win over {opp}",
    ],
    "streak": [
        "{team} extends their winning streak to {streak} games",
        "{team} snaps {opp}'s {streak}-game win streak",
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
    top_scorer = max(all_box, key=lambda l: l.get("points", 0), default=None)

    score_str = f"{home_score}-{away_score}"
    outcome = "defeat" if winner_id == home_team_id else "defeat"

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
                player=f"Player {line['player_id']}",
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
            player=f"Player {top_scorer['player_id']}",
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
    max_storylines: int = 3,
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
