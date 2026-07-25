from __future__ import annotations

import json


async def record_gameplan(pool, game_id: int, team_id: int, gameplan: dict) -> None:
    strategy = gameplan.get("strategy", {})
    await pool.execute(
        """
        INSERT INTO game_cpu_gameplans (
            game_id, team_id, source,
            offensive_pace, offensive_scheme, defensive_scheme,
            defensive_intensity, star_usage,
            player_directives, rationale
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        ON CONFLICT (game_id, team_id) DO NOTHING
        """,
        game_id,
        team_id,
        gameplan.get("source", "cpu"),
        strategy.get("offensive_pace", "balanced"),
        strategy.get("offensive_scheme", "balanced"),
        strategy.get("defensive_scheme", "man_to_man"),
        strategy.get("defensive_intensity", "normal"),
        strategy.get("star_usage", 50),
        json.dumps({str(k): v for k, v in gameplan.get("player_directives", {}).items()}),
        gameplan.get("rationale", ""),
    )


async def get_scheme_history(pool, league_id: int, season: int, team_id: int) -> dict:
    """Aggregate this team's CPU-selected schemes across the season's simmed
    games, returning the most-used offensive and defensive scheme along with
    the team's W-L record while running each. Powers Coach Beat's
    scheme-history awareness (Phase 2 fix C3) -- "has this team run this
    scheme before, did it work."

    Returns {} when the team has no simmed games with a recorded gameplan yet
    this season (safe early-season default, same degrade-gracefully pattern
    as columnist_intel.py's history providers).
    """
    rows = await pool.fetch(
        """
        SELECT gp.offensive_scheme, gp.defensive_scheme,
               (g.winner_team_id = gp.team_id) AS won
        FROM game_cpu_gameplans gp
        JOIN games g ON g.id = gp.game_id
        WHERE gp.team_id = $1 AND g.league_id = $2 AND g.season = $3
          AND g.status = 'simmed'
        """,
        team_id,
        league_id,
        season,
    )
    if not rows:
        return {}

    def _most_used(column: str) -> dict | None:
        tallies: dict[str, dict[str, int]] = {}
        for row in rows:
            scheme = row[column]
            bucket = tallies.setdefault(scheme, {"games": 0, "wins": 0, "losses": 0})
            bucket["games"] += 1
            if row["won"]:
                bucket["wins"] += 1
            else:
                bucket["losses"] += 1
        if not tallies:
            return None
        scheme, stats = max(tallies.items(), key=lambda kv: kv[1]["games"])
        return {"scheme": scheme, **stats}

    result: dict = {}
    offensive = _most_used("offensive_scheme")
    if offensive is not None:
        result["offensive_scheme"] = offensive
    defensive = _most_used("defensive_scheme")
    if defensive is not None:
        result["defensive_scheme"] = defensive
    return result


async def get_gameplan(pool, game_id: int, team_id: int) -> dict | None:
    row = await pool.fetchrow(
        """
        SELECT source, offensive_pace, offensive_scheme, defensive_scheme,
               defensive_intensity, star_usage, player_directives, rationale
        FROM game_cpu_gameplans
        WHERE game_id = $1 AND team_id = $2
        """,
        game_id,
        team_id,
    )
    if row is None:
        return None
    directives_raw = row["player_directives"]
    if isinstance(directives_raw, str):
        directives_raw = json.loads(directives_raw)
    player_directives = {int(k): v for k, v in directives_raw.items()}
    return {
        "source": row["source"],
        "strategy": {
            "offensive_pace": row["offensive_pace"],
            "offensive_scheme": row["offensive_scheme"],
            "defensive_scheme": row["defensive_scheme"],
            "defensive_intensity": row["defensive_intensity"],
            "star_usage": row["star_usage"],
        },
        "player_directives": player_directives,
        "rationale": row["rationale"],
    }
