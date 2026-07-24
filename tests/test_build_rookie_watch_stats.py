"""
Unit tests for services.sim_content_pipeline._recency_weighted_avg and
_build_rookie_watch_stats (Finding #6 — rookie_watch's season-long stat
average had no recency window).

Previously the SQL query pulled the whole season with no recency window, so
a rookie who was excellent in October and has cooled off since still showed
a stat line dominated by early performance, framed by the column as current
form. These tests pin: equal weighting when a player has <= _RECENCY_WINDOW
games (no distortion possible), heavier weighting of the most recent games
once a player has more games than the window, sorting/capping at 10, and
grouping multiple rookies' rows independently.
"""
from __future__ import annotations

from services.sim_content_pipeline import (
    _RECENCY_WINDOW,
    _build_rookie_watch_stats,
    _recency_weighted_avg,
)


def _game(points=10, rebounds=5, assists=3, game_index=1):
    return {"points": points, "rebounds": rebounds, "assists": assists, "game_index": game_index}


def test_recency_weighted_avg_empty_is_zero():
    assert _recency_weighted_avg([], "points") == 0.0


def test_equal_weighting_when_all_games_within_window():
    """Fewer games than _RECENCY_WINDOW -> every game gets the same weight,
    so this is just a plain average."""
    games = [_game(points=p) for p in (10, 20, 30)]
    assert _recency_weighted_avg(games, "points") == 20.0


def test_older_games_beyond_window_count_less():
    """21 games: the most recent _RECENCY_WINDOW score 20, the rest (older,
    beyond the window) score 0 -- the weighted average should sit well above
    the flat average because the recent games are double-weighted."""
    recent = [_game(points=20) for _ in range(_RECENCY_WINDOW)]
    older = [_game(points=0) for _ in range(21 - _RECENCY_WINDOW)]
    games = recent + older  # already most-recent-first
    flat_avg = sum(g["points"] for g in games) / len(games)
    weighted = _recency_weighted_avg(games, "points")
    assert weighted > flat_avg


def test_build_stats_groups_by_player_and_computes_all_three_stats():
    box_rows = [
        {"player_id": 1, "name": "Rookie A", "team": "LAL", "points": 20, "rebounds": 8, "assists": 4, "game_index": 1},
        {"player_id": 1, "name": "Rookie A", "team": "LAL", "points": 24, "rebounds": 6, "assists": 5, "game_index": 2},
        {"player_id": 2, "name": "Rookie B", "team": "BOS", "points": 10, "rebounds": 3, "assists": 2, "game_index": 1},
    ]
    stats = _build_rookie_watch_stats(box_rows)
    by_id = {s["id"]: s for s in stats}

    assert by_id[1]["name"] == "Rookie A"
    assert by_id[1]["team"] == "LAL"
    assert by_id[1]["gp"] == 2
    assert by_id[1]["ppg"] == 22.0  # equal weight, both games within the recency window

    assert by_id[2]["gp"] == 1
    assert by_id[2]["ppg"] == 10.0


def test_build_stats_sorts_by_ppg_descending_and_caps_at_10():
    box_rows = [
        {"player_id": i, "name": f"Rookie {i}", "team": "LAL", "points": i, "rebounds": 1, "assists": 1, "game_index": 1}
        for i in range(1, 13)  # 12 distinct rookies
    ]
    stats = _build_rookie_watch_stats(box_rows)
    assert len(stats) == 10
    ppgs = [s["ppg"] for s in stats]
    assert ppgs == sorted(ppgs, reverse=True)
    # The two lowest-scoring rookies (ids 1 and 2) get cut by the top-10 cap.
    assert {s["id"] for s in stats} == set(range(3, 13))


def test_build_stats_empty_input_returns_empty_list():
    assert _build_rookie_watch_stats([]) == []
