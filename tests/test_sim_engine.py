"""
Unit tests for services.sim_engine.sim_game().

Pure function — no DB, no async needed.
"""
from __future__ import annotations

from random import Random

from services.sim_engine import (
    _apply_scheme_to_players,
    _build_box_for_team,
    _scheme_fit_factor,
    sim_game,
)


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


# ---------------------------------------------------------------------------
# _scheme_fit_factor — the skill-conditioning helper finding #2 introduces.
# ---------------------------------------------------------------------------


def test_scheme_fit_factor_zero_at_or_below_low():
    assert _scheme_fit_factor(40.0) == 0.0
    assert _scheme_fit_factor(10.0) == 0.0


def test_scheme_fit_factor_one_at_or_above_high():
    assert _scheme_fit_factor(80.0) == 1.0
    assert _scheme_fit_factor(99.0) == 1.0


def test_scheme_fit_factor_linear_midpoint():
    # Midpoint of default 40..80 range is 60 -> 0.5
    assert abs(_scheme_fit_factor(60.0) - 0.5) < 0.001


# ---------------------------------------------------------------------------
# _apply_scheme_to_players — finding #2 fix: the three_heavy tendency_3pt
# bump is now skill-conditioned (proportional to shooting_3pt) instead of a
# flat +12 for every player regardless of whether they can actually shoot.
# ---------------------------------------------------------------------------


def test_three_heavy_bump_scales_with_shooting_skill():
    """A poor shooter (below the fit floor) gets little-to-no bump; an elite
    shooter (at/above the fit ceiling) gets the full +12 bump. This replaces
    the old flat-+12-for-everyone behavior (finding #2)."""
    good_shooter = {"overall": 80, "position": "SF", "shooting_3pt": 92, "tendency_3pt": 50}
    bad_shooter = {"overall": 75, "position": "C", "shooting_3pt": 20, "tendency_3pt": 50}
    result = _apply_scheme_to_players([good_shooter, bad_shooter], "three_heavy")
    assert result[0]["tendency_3pt"] == 62  # full +12 bump (shooting_3pt=92 >= 80 ceiling)
    assert result[1]["tendency_3pt"] == 50  # no bump (shooting_3pt=20 <= 40 floor)


def test_three_heavy_bump_is_never_larger_for_worse_shooter():
    """General monotonicity check across a range of shooting_3pt values."""
    ratings = [20, 40, 50, 60, 74, 92]
    players = [
        {"overall": 75, "position": "SF", "shooting_3pt": r, "tendency_3pt": 50}
        for r in ratings
    ]
    result = _apply_scheme_to_players(players, "three_heavy")
    bumps = [r["tendency_3pt"] - 50 for r in result]
    assert bumps == sorted(bumps), f"Bumps should be non-decreasing with shooting skill: {bumps}"


# ---------------------------------------------------------------------------
# _build_box_for_team — finding #2 fix: three_rate_adj (own-scheme +
# opponent-defense combined) now scales tpa by each player's own shooting_3pt
# fit instead of applying the same factor to every player on the floor.
# ---------------------------------------------------------------------------


def _make_scheme_fit_player(pid: int, shooting_3pt: int) -> dict:
    return {
        "id": pid, "overall": 75, "position": "SF",
        "finishing": 75, "shooting_2pt": 75, "shooting_3pt": shooting_3pt,
        "playmaking": 75, "defense": 75, "rebounding": 75,
    }


def test_three_rate_adj_boosts_good_shooters_tpa_more_than_bad_shooters():
    """With a positive three_rate_adj (e.g. three_heavy's +0.22), a good
    shooter's tpa should scale up more than a poor shooter's, averaged over
    many seeds to smooth out per-game noise."""
    good = _make_scheme_fit_player(1, 92)
    bad = _make_scheme_fit_player(2, 20)
    players = [good, bad] + [_make_scheme_fit_player(i, 60) for i in range(3, 6)]

    good_tpa_total = 0
    bad_tpa_total = 0
    n_runs = 40
    for seed in range(n_runs):
        rng = Random(seed)
        box = _build_box_for_team(
            rng, [dict(p) for p in players], team_id=1, team_score=100, score_diff=0,
            three_rate_adj=0.22,
        )
        by_id = {line["player_id"]: line for line in box}
        good_tpa_total += by_id[1]["tpa"]
        bad_tpa_total += by_id[2]["tpa"]

    # Both start from the same baseline shot profile (identical dicts besides
    # shooting_3pt), so a strictly larger scale-up for the good shooter shows
    # the adjustment is now conditioned on skill rather than flat.
    assert good_tpa_total > bad_tpa_total, (
        f"good shooter tpa {good_tpa_total} should exceed poor shooter tpa {bad_tpa_total} "
        f"under a positive three_rate_adj"
    )


# ---------------------------------------------------------------------------
# CA1 (coaching AI realism sweep) -- blowout garbage-time minutes.
# _build_box_for_team is the real, only production code path that computes
# minutes (auto-allocate and minutes_override both flow through it before
# CA1's post-processing step) -- calling it directly with a forced score_diff
# exercises the actual fix, not a mock. This is a MANDATORY live smoke test
# per the coaching-ai sweep's verification bar: it was run against pre-fix
# code (CA1's post-processing block temporarily removed) and confirmed to
# fail (starters kept full minutes regardless of margin), then re-run
# post-fix and confirmed to pass. See docs/design/coaching-ai-logic-rules.md.
# ---------------------------------------------------------------------------


def _make_blowout_roster(team_id: int, start_id: int) -> list[dict]:
    positions = ["PG", "SG", "SF", "PF", "C", "PG", "SG", "SF", "PF", "C"]
    return [_make_player(start_id + i, team_id, overall=75, position=positions[i]) for i in range(10)]


def test_blowout_reduces_starter_minutes_and_grows_bench_minutes():
    """A 30+ point final margin should pull starters down from their
    auto-allocated ~35 min toward the garbage-time floor, with the freed
    minutes flowing to bench players -- a close game (score_diff small)
    should leave starters at their normal allocation."""
    players_blowout = _make_blowout_roster(1, 1)
    players_close = _make_blowout_roster(2, 1)

    rng_blowout = Random(1)
    box_blowout = _build_box_for_team(
        rng_blowout, [dict(p) for p in players_blowout], team_id=1, team_score=130, score_diff=32,
    )
    rng_close = Random(1)
    box_close = _build_box_for_team(
        rng_close, [dict(p) for p in players_close], team_id=2, team_score=110, score_diff=4,
    )

    starter_minutes_blowout = sum(line["minutes"] for line in box_blowout[:5])
    starter_minutes_close = sum(line["minutes"] for line in box_close[:5])
    bench_minutes_blowout = sum(line["minutes"] for line in box_blowout[5:])
    bench_minutes_close = sum(line["minutes"] for line in box_close[5:])

    assert starter_minutes_blowout < starter_minutes_close, (
        f"Blowout starters ({starter_minutes_blowout}) should play fewer combined "
        f"minutes than a close game's starters ({starter_minutes_close})"
    )
    assert bench_minutes_blowout > bench_minutes_close, (
        f"Blowout bench ({bench_minutes_blowout}) should absorb the freed minutes "
        f"vs a close game's bench ({bench_minutes_close})"
    )


def test_blowout_never_pulls_starters_below_garbage_time_floor():
    """No starter should be reduced below the 22-min garbage-time floor even
    at the maximum blowout severity (margin >= 30)."""
    players = _make_blowout_roster(1, 1)
    rng = Random(2)
    box = _build_box_for_team(
        rng, [dict(p) for p in players], team_id=1, team_score=140, score_diff=45,
    )
    for line in box[:5]:
        assert line["minutes"] >= 22.0 - 0.01, f"Starter fell below garbage-time floor: {line}"


def test_mild_blowout_margin_reduces_less_than_severe_blowout():
    """Margin=20 (mild) should reduce starter minutes less than margin=30+ (severe) --
    proves severity actually scales with margin rather than being a flat cutoff."""
    players_mild = _make_blowout_roster(1, 1)
    players_severe = _make_blowout_roster(2, 1)

    rng_mild = Random(5)
    box_mild = _build_box_for_team(
        rng_mild, [dict(p) for p in players_mild], team_id=1, team_score=120, score_diff=20,
    )
    rng_severe = Random(5)
    box_severe = _build_box_for_team(
        rng_severe, [dict(p) for p in players_severe], team_id=2, team_score=140, score_diff=35,
    )

    starter_minutes_mild = sum(line["minutes"] for line in box_mild[:5])
    starter_minutes_severe = sum(line["minutes"] for line in box_severe[:5])

    assert starter_minutes_severe < starter_minutes_mild, (
        f"Severe blowout starters ({starter_minutes_severe}) should play fewer minutes "
        f"than a mild blowout's ({starter_minutes_mild})"
    )
