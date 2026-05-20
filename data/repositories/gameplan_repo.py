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
