from __future__ import annotations

from typing import Optional

import asyncpg

from data.repositories import records_repo, all_time_records_repo

_RECORD_FLOORS = {
    "most_pts_game_player": 30,
    "highest_team_score":   120,
    "biggest_blowout":      20,
}

# A new season record only announces if it improves the prior value by at least 5%
# (or the prior value is zero, i.e. first record of the season).
_SEASON_RECORD_IMPROVEMENT_GATE = 0.05

# Floor for all-time 3PM in a single game.
_ALL_TIME_3PM_FLOOR = 7


def _passes_improvement_gate(new_value: float, old_value: float) -> bool:
    """Return True if new_value beats old_value by at least 5%, or old_value is zero."""
    if old_value == 0:
        return True
    return new_value > old_value * (1 + _SEASON_RECORD_IMPROVEMENT_GATE)


async def check_and_update_records(
    pool: asyncpg.Pool,
    league_id: int,
    season: int,
    game_id: int,
    result: dict,
) -> tuple[list[str], list[str]]:
    """
    Check if this game set any season or all-time records.

    Returns (season_announcements, at_announcements).
    season_announcements: standard record strings (season scope).
    at_announcements: all-time records, prefixed with the hall-of-fame column emoji.

    Season records only announce if the new value improves the old one by >= 5%
    (or it is the first record of the season).

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

    if best_scorer is not None and best_pts > _RECORD_FLOORS["most_pts_game_player"]:
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
        if _passes_improvement_gate(float(best_scorer["points"]), current_pts_value):
            _pid = best_scorer["player_id"]
            _row = await pool.fetchrow("SELECT first_name, last_name FROM players WHERE id = $1", _pid)
            _player_name = f"{_row['first_name']} {_row['last_name']}" if _row else f"Player #{_pid}"
            announcements.append(
                f"New season record: {_player_name} scored {best_scorer['points']} points in a game!"
            )

    current_team_score = await records_repo.get_record(pool, league_id, season, "highest_team_score")
    current_team_value: float = current_team_score["value"] if current_team_score else 0.0

    high_score = max(home_score, away_score)
    if high_score > current_team_value and high_score > _RECORD_FLOORS["highest_team_score"]:
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
        if _passes_improvement_gate(float(high_score), current_team_value):
            _team_row = await pool.fetchrow("SELECT nba_team_code FROM teams WHERE id = $1", high_team_id)
            _team_code = _team_row["nba_team_code"] if _team_row else "???"
            announcements.append(
                f"New season record: {_team_code} dropped {high_score} points!"
            )

    margin = abs(home_score - away_score)
    current_blowout = await records_repo.get_record(pool, league_id, season, "biggest_blowout")
    current_blowout_value: float = current_blowout["value"] if current_blowout else 0.0

    if margin > current_blowout_value and margin > _RECORD_FLOORS["biggest_blowout"]:
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
        if _passes_improvement_gate(float(margin), current_blowout_value):
            _winner_row = await pool.fetchrow("SELECT nba_team_code FROM teams WHERE id = $1", winner_team_id)
            _winner_code = _winner_row["nba_team_code"] if _winner_row else "???"
            # Derive loser team from box score team IDs.
            _all_team_ids = list({l.get("team_id") for l in all_box if l.get("team_id")})
            _loser_team_id = next((tid for tid in _all_team_ids if tid != winner_team_id), None)
            if _loser_team_id:
                _loser_row = await pool.fetchrow("SELECT nba_team_code FROM teams WHERE id = $1", _loser_team_id)
                _loser_code = _loser_row["nba_team_code"] if _loser_row else "???"
            else:
                _loser_code = "???"
            announcements.append(
                f"New season record: {_winner_code} blew out {_loser_code} by {margin} points!"
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
            _pid = line["player_id"]
            _row = await pool.fetchrow("SELECT first_name, last_name FROM players WHERE id = $1", _pid)
            _player_name = f"{_row['first_name']} {_row['last_name']}" if _row else f"Player #{_pid}"
            announcements.append(
                f"Triple-double alert! {_player_name} recorded {pts}/{reb}/{ast} (pts/reb/ast)!"
            )

    at_announcements = await _check_all_time_records(pool, league_id, season, game_id, result, all_box)

    return announcements, at_announcements


async def _check_all_time_records(
    pool: asyncpg.Pool,
    league_id: int,
    season: int,
    game_id: int,
    result: dict,
    all_box: list[dict],
) -> list[str]:
    """Check and update all-time league records. Returns formatted announcement strings."""
    at_announcements: list[str] = []

    home_score: int = result["home_score"]
    away_score: int = result["away_score"]
    home_box: list[dict] = result.get("home_box", [])
    away_box: list[dict] = result.get("away_box", [])

    # Most points in a game by a player (all-time)
    best_line: Optional[dict] = None
    best_pts = 0
    for line in all_box:
        if line.get("points", 0) > best_pts:
            best_pts = line["points"]
            best_line = line

    if best_line is not None:
        current_at_pts = await all_time_records_repo.get_record(pool, league_id, "most_pts_game_player")
        current_at_pts_val: float = current_at_pts["value"] if current_at_pts else 0.0
        if float(best_pts) > current_at_pts_val:
            await all_time_records_repo.set_record(
                pool, league_id, "most_pts_game_player",
                float(best_pts),
                player_id=best_line["player_id"],
                team_id=best_line["team_id"],
                game_id=game_id,
                season_set=season,
            )
            _pid = best_line["player_id"]
            _row = await pool.fetchrow("SELECT first_name, last_name FROM players WHERE id = $1", _pid)
            _name = f"{_row['first_name']} {_row['last_name']}" if _row else f"Player #{_pid}"
            at_announcements.append(
                f"\U0001f3db️ ALL-TIME RECORD: {_name} scored {best_pts} points — new franchise high in league history!"
            )

    # Highest team score (all-time)
    high_score = max(home_score, away_score)
    current_at_team = await all_time_records_repo.get_record(pool, league_id, "highest_team_score")
    current_at_team_val: float = current_at_team["value"] if current_at_team else 0.0
    if float(high_score) > current_at_team_val:
        if home_score >= away_score:
            high_team_id = result.get("home_team_id") or (home_box[0]["team_id"] if home_box else None)
        else:
            high_team_id = result.get("away_team_id") or (away_box[0]["team_id"] if away_box else None)
        await all_time_records_repo.set_record(
            pool, league_id, "highest_team_score",
            float(high_score),
            team_id=high_team_id,
            game_id=game_id,
            season_set=season,
        )
        _team_row = await pool.fetchrow("SELECT nba_team_code FROM teams WHERE id = $1", high_team_id)
        _team_code = _team_row["nba_team_code"] if _team_row else "???"
        at_announcements.append(
            f"\U0001f3db️ ALL-TIME RECORD: {_team_code} dropped {high_score} points — highest team score in league history!"
        )

    # Biggest blowout (all-time) — only announce at or above the 20-point floor to
    # suppress trivial early-season blowout records from small margins.
    margin = abs(home_score - away_score)
    current_at_blowout = await all_time_records_repo.get_record(pool, league_id, "biggest_blowout")
    current_at_blowout_val: float = current_at_blowout["value"] if current_at_blowout else 0.0
    if float(margin) > current_at_blowout_val and margin >= 20:
        winner_team_id = result.get("winner_team_id")
        await all_time_records_repo.set_record(
            pool, league_id, "biggest_blowout",
            float(margin),
            team_id=winner_team_id,
            game_id=game_id,
            season_set=season,
        )
        _winner_row = await pool.fetchrow("SELECT nba_team_code FROM teams WHERE id = $1", winner_team_id)
        _winner_code = _winner_row["nba_team_code"] if _winner_row else "???"
        at_announcements.append(
            f"\U0001f3db️ ALL-TIME RECORD: {_winner_code} recorded the biggest blowout in league history — margin of {margin} points!"
        )

    # Most 3-pointers made in a game by a player (all-time, floor: 7)
    best_3pm_line: Optional[dict] = None
    best_3pm = _ALL_TIME_3PM_FLOOR - 1  # must exceed floor to count
    for line in all_box:
        if line.get("tpm", 0) > best_3pm:
            best_3pm = line["tpm"]
            best_3pm_line = line

    if best_3pm_line is not None:
        current_at_3pm = await all_time_records_repo.get_record(pool, league_id, "most_3pm_game")
        current_at_3pm_val: float = current_at_3pm["value"] if current_at_3pm else 0.0
        if float(best_3pm) > current_at_3pm_val:
            await all_time_records_repo.set_record(
                pool, league_id, "most_3pm_game",
                float(best_3pm),
                player_id=best_3pm_line["player_id"],
                team_id=best_3pm_line["team_id"],
                game_id=game_id,
                season_set=season,
            )
            _pid = best_3pm_line["player_id"]
            _row = await pool.fetchrow("SELECT first_name, last_name FROM players WHERE id = $1", _pid)
            _name = f"{_row['first_name']} {_row['last_name']}" if _row else f"Player #{_pid}"
            at_announcements.append(
                f"\U0001f3db️ ALL-TIME RECORD: {_name} hit {best_3pm} three-pointers — most in a single game in league history!"
            )

    # Longest active win streak tracked via standings_cache (all-time)
    # We silently update the all-time record in the DB but do NOT emit an announcement here.
    # batch_sim_runner already posts a milestone "Win Streak" embed (via game_repo notable_streak
    # at {5, 10, 15} games) — emitting a second message here causes every milestone to post twice.
    winner_team_id = result.get("winner_team_id")
    if winner_team_id:
        streak_row = await pool.fetchrow(
            "SELECT win_streak FROM standings_cache WHERE league_id = $1 AND team_id = $2",
            league_id, winner_team_id,
        )
        current_streak = streak_row["win_streak"] if streak_row else 0
        if current_streak >= 5:
            current_at_streak = await all_time_records_repo.get_record(pool, league_id, "longest_active_win_streak")
            current_at_streak_val: float = current_at_streak["value"] if current_at_streak else 0.0
            if float(current_streak) > current_at_streak_val:
                await all_time_records_repo.set_record(
                    pool, league_id, "longest_active_win_streak",
                    float(current_streak),
                    team_id=winner_team_id,
                    game_id=game_id,
                    season_set=season,
                )
                # No announcement — batch_sim_runner handles the user-facing milestone post.

    return at_announcements
