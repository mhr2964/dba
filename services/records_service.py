from __future__ import annotations

from typing import Optional

import asyncpg

from data.repositories import records_repo


async def check_and_update_records(
    pool: asyncpg.Pool,
    league_id: int,
    season: int,
    game_id: int,
    result: dict,
) -> list[str]:
    """
    Check if this game set any season records. Update season_records table if so.
    Returns list of announcement strings for notable records broken.

    Checks:
    - Player points in a game: if any player scored > current 'most_pts_game_player' record
    - Team score: if home_score or away_score > current 'highest_team_score'
    - Blowout: if |home_score - away_score| > current 'biggest_blowout'
    - Triple-double: any player with pts>=10 AND reb>=10 AND ast>=10
    """
    announcements: list[str] = []

    home_score: int = result["home_score"]
    away_score: int = result["away_score"]
    home_box: list[dict] = result.get("home_box", [])
    away_box: list[dict] = result.get("away_box", [])
    all_box = home_box + away_box

    current_pts_record = await records_repo.get_record(pool, league_id, season, "most_pts_game_player")
    current_pts_value: float = current_pts_record["value"] if current_pts_record else 0.0

    best_scorer: Optional[dict] = None
    best_pts = current_pts_value
    for line in all_box:
        if line["points"] > best_pts:
            best_pts = line["points"]
            best_scorer = line

    if best_scorer is not None:
        await records_repo.set_record(
            pool,
            league_id,
            season,
            "most_pts_game_player",
            float(best_scorer["points"]),
            player_id=best_scorer["player_id"],
            team_id=best_scorer["team_id"],
            game_id=game_id,
        )
        announcements.append(
            f"New season record: Player {best_scorer['player_id']} "
            f"scored {best_scorer['points']} points in a game!"
        )

    current_team_score = await records_repo.get_record(pool, league_id, season, "highest_team_score")
    current_team_value: float = current_team_score["value"] if current_team_score else 0.0

    high_score = max(home_score, away_score)
    if high_score > current_team_value:
        # Determine which team scored highest
        if home_score >= away_score:
            high_team_id = result.get("home_team_id") or (home_box[0]["team_id"] if home_box else None)
        else:
            high_team_id = result.get("away_team_id") or (away_box[0]["team_id"] if away_box else None)
        await records_repo.set_record(
            pool,
            league_id,
            season,
            "highest_team_score",
            float(high_score),
            team_id=high_team_id,
            game_id=game_id,
        )
        announcements.append(
            f"New season record: Highest team score — {high_score} points!"
        )

    margin = abs(home_score - away_score)
    current_blowout = await records_repo.get_record(pool, league_id, season, "biggest_blowout")
    current_blowout_value: float = current_blowout["value"] if current_blowout else 0.0

    if margin > current_blowout_value:
        winner_team_id = result.get("winner_team_id")
        await records_repo.set_record(
            pool,
            league_id,
            season,
            "biggest_blowout",
            float(margin),
            team_id=winner_team_id,
            game_id=game_id,
        )
        announcements.append(
            f"New season record: Biggest blowout — {margin}-point margin!"
        )

    for line in all_box:
        reb = line.get("rebounds_off", 0) + line.get("rebounds_def", 0)
        pts = line.get("points", 0)
        ast = line.get("assists", 0)
        if pts >= 10 and reb >= 10 and ast >= 10:
            await records_repo.set_record(
                pool,
                league_id,
                season,
                "triple_double",
                float(pts),
                player_id=line["player_id"],
                team_id=line["team_id"],
                game_id=game_id,
            )
            announcements.append(
                f"Triple-double alert! Player {line['player_id']} "
                f"recorded {pts}/{reb}/{ast} (pts/reb/ast)!"
            )

    return announcements
