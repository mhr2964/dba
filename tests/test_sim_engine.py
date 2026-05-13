"""
Unit tests for services.sim_engine.sim_game().

Pure function — no DB, no async needed.
"""
from __future__ import annotations

from services.sim_engine import sim_game


# ---------------------------------------------------------------------------
# Helpers — minimal valid team/player dicts
# ---------------------------------------------------------------------------


def _make_team(team_id: int, offense: int = 75, defense: int = 75, pace: float = 100.0) -> dict:
    return {
        "team_id": team_id,
        "offense_rating": offense,
        "defense_rating": defense,
        "pace": pace,
    }


def _make_player(player_id: int, team_id: int, overall: int = 75, position: str = "SF") -> dict:
    """Minimal player dict that sim_game's box-builder will accept."""
    return {
        "id": player_id,
        "team_id": team_id,
        "overall": overall,
        "position": position,
        "finishing": overall,
        "shooting_2pt": overall,
        "shooting_3pt": overall,
        "playmaking": overall,
        "defense": overall,
        "rebounding": overall,
    }


def _make_roster(team_id: int, overall: int = 75, start_id: int = 1) -> list[dict]:
    """10-player roster for a team (5 starters, 5 bench)."""
    positions = ["PG", "SG", "SF", "PF", "C", "PG", "SG", "SF", "PF", "C"]
    return [
        _make_player(start_id + i, team_id, overall, positions[i])
        for i in range(10)
    ]


HOME_ID = 1
AWAY_ID = 2
HOME_PLAYERS = _make_roster(HOME_ID, overall=75, start_id=1)
AWAY_PLAYERS = _make_roster(AWAY_ID, overall=75, start_id=101)


# ---------------------------------------------------------------------------
# Structure & contract tests
# ---------------------------------------------------------------------------


def test_sim_game_returns_valid_structure():
    """sim_game output contains all required top-level keys."""
    home = _make_team(HOME_ID)
    away = _make_team(AWAY_ID)
    result = sim_game(home, away, HOME_PLAYERS[:], AWAY_PLAYERS[:], rng_seed=42)

    assert "home_score" in result
    assert "away_score" in result
    assert "winner_team_id" in result
    assert "home_box" in result
    assert "away_box" in result


def test_sim_game_is_deterministic():
    """Same seed produces identical output twice."""
    home = _make_team(HOME_ID)
    away = _make_team(AWAY_ID)
    r1 = sim_game(home, away, HOME_PLAYERS[:], AWAY_PLAYERS[:], rng_seed=7)
    r2 = sim_game(home, away, HOME_PLAYERS[:], AWAY_PLAYERS[:], rng_seed=7)

    assert r1["home_score"] == r2["home_score"]
    assert r1["away_score"] == r2["away_score"]
    assert r1["winner_team_id"] == r2["winner_team_id"]
    # Compare full box entries
    assert r1["home_box"] == r2["home_box"]
    assert r1["away_box"] == r2["away_box"]


def test_different_seeds_produce_different_results():
    """A seed gap of 1000 should (almost always) produce different scores."""
    home = _make_team(HOME_ID)
    away = _make_team(AWAY_ID)
    r1 = sim_game(home, away, HOME_PLAYERS[:], AWAY_PLAYERS[:], rng_seed=1)
    r2 = sim_game(home, away, HOME_PLAYERS[:], AWAY_PLAYERS[:], rng_seed=1001)

    assert (r1["home_score"], r1["away_score"]) != (r2["home_score"], r2["away_score"]), (
        "1000-seed gap should virtually always produce different scores"
    )


def test_home_court_advantage():
    """Home team wins more than 50% when both teams are equal over 100 simulations."""
    home = _make_team(HOME_ID, offense=75, defense=75)
    away = _make_team(AWAY_ID, offense=75, defense=75)
    home_wins = sum(
        sim_game(home, away, HOME_PLAYERS[:], AWAY_PLAYERS[:], rng_seed=seed)["winner_team_id"] == HOME_ID
        for seed in range(100)
    )
    assert home_wins > 50, f"Home should win majority with equal teams; won {home_wins}/100"


def test_better_team_wins_more():
    """OVR 90 team vs OVR 60 team: strong side wins >80% over 100 sims."""
    strong_id = 10
    weak_id = 20
    strong_players = _make_roster(strong_id, overall=90, start_id=200)
    weak_players = _make_roster(weak_id, overall=60, start_id=300)
    strong_team = _make_team(strong_id, offense=90, defense=90)
    weak_team = _make_team(weak_id, offense=60, defense=60)

    strong_wins = sum(
        sim_game(strong_team, weak_team, strong_players[:], weak_players[:], rng_seed=seed)["winner_team_id"] == strong_id
        for seed in range(100)
    )
    assert strong_wins > 80, (
        f"Strong team (OVR 90) should win >80% vs OVR 60; won {strong_wins}/100"
    )


def test_box_scores_sum_to_team_score():
    """Sum of points in home_box == home_score, same for away."""
    home = _make_team(HOME_ID)
    away = _make_team(AWAY_ID)
    result = sim_game(home, away, HOME_PLAYERS[:], AWAY_PLAYERS[:], rng_seed=99)

    home_pts = sum(line["points"] for line in result["home_box"])
    away_pts = sum(line["points"] for line in result["away_box"])

    assert home_pts == result["home_score"], (
        f"Home box points {home_pts} != home_score {result['home_score']}"
    )
    assert away_pts == result["away_score"], (
        f"Away box points {away_pts} != away_score {result['away_score']}"
    )


def test_all_players_get_minutes():
    """Every player in both box scores has minutes > 0."""
    home = _make_team(HOME_ID)
    away = _make_team(AWAY_ID)
    result = sim_game(home, away, HOME_PLAYERS[:], AWAY_PLAYERS[:], rng_seed=55)

    for line in result["home_box"] + result["away_box"]:
        assert line["minutes"] > 0, (
            f"Player {line['player_id']} got 0 minutes"
        )


def test_player_stats_non_negative():
    """All numeric stat fields in every box-score line are >= 0."""
    home = _make_team(HOME_ID)
    away = _make_team(AWAY_ID)
    result = sim_game(home, away, HOME_PLAYERS[:], AWAY_PLAYERS[:], rng_seed=123)

    stat_fields = [
        "points", "rebounds_off", "rebounds_def", "assists",
        "steals", "blocks", "turnovers", "fouls",
        "fga", "fgm", "tpa", "tpm", "fta", "ftm", "minutes",
    ]
    for line in result["home_box"] + result["away_box"]:
        for field in stat_fields:
            assert line[field] >= 0, (
                f"Player {line['player_id']} has negative {field}={line[field]}"
            )
