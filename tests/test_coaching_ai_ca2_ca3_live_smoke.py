"""
Live smoke test for CA2+CA3 (coaching AI realism sweep) -- these two fixes
MUST SHIP TOGETHER: CA2 wires usage_weight/star_usage_mult into
sim_engine._build_box_for_team's role-based touch-share calculation (dead
code before this fix -- a "feature"/"conserve" directive had zero effect on
scoring share once player_roles data was stamped). CA3 removes a redundant
second `if posture == "tanking":` block in cpu_coach_service._decide_player_directives
that unconditionally overwrote the FIRST if/elif chain's decision for every
tanking team, stomping a legitimate "feature" call (e.g. the team's lone
isolation-scheme star) back down to "conserve" on every single tanking-team
game. Shipping CA2 without CA3 would do nothing for tanking teams (their
usage_mode never survives to reach sim_engine); shipping CA3 without CA2
would let "feature" survive but still have zero effect on the box score.

This drives the REAL production pipeline end to end: a real two-team league,
seeded in the test DB, real lineups, sim_orchestrator._sim_single_game (the
actual regular-season entry point -- same function sim_until_rival/sim_range
call per game), cpu_coach_service.decide_gameplans running for real (not
mocked), sim_engine.sim_game running for real, and game_box_scores read back
from the DB afterward. team_intel.compute_posture is monkeypatched (same
pattern tests/test_cpu_coach_service.py already uses for this exact function)
purely to force a deterministic "tanking" posture without needing to hand-seed
a full season of standings history -- everything downstream of that single
posture value is real, unmocked code.

Two dispositive assertions, both proven to fail against pre-fix code (see
docstrings on each test):

1. test_tanking_teams_isolation_star_can_still_get_featured_directive --
   CA3's bug reproduces as a hard 100%-to-0% flip: pre-fix, the redundant
   block stomps the star's usage_mode to "conserve" on literally every
   tanking-team game regardless of scheme, so across N real games "feature"
   NEVER appears for this player. Post-fix, on any game where the CPU's real
   scheme choice happens to land on "isolation" (the branch that assigns
   "feature" to a tanking team's rank-0 isolation star), "feature" survives.

2. test_featured_directive_produces_measurably_more_points -- within the same
   player, across the same set of real games, points scored on games where
   the persisted directive was "feature" average measurably higher than
   points scored on games where it was "conserve"/"normal" -- proving CA2
   actually moved real box-score points, not just a directive string.
"""
from __future__ import annotations

import datetime

import pytest

from data.repositories import game_repo
from services import sim_orchestrator, team_intel

pytestmark = pytest.mark.asyncio

_N_GAMES = 30


async def _seed_team(db_pool, league_id: int, code: str, overalls: list[int]) -> tuple[int, list[int]]:
    team_id = await db_pool.fetchval(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, $2, $3, $4, 'East', 'Atlantic')
        RETURNING id
        """,
        league_id, code, f"{code} City", code,
    )
    positions = ["PG", "SG", "SF", "PF", "C", "PG", "SF", "C"]
    player_ids: list[int] = []
    for i, (pos, ovr) in enumerate(zip(positions, overalls)):
        pid = await db_pool.fetchval(
            """
            INSERT INTO players (
                league_id, team_id, first_name, last_name, position,
                birth_date, years_pro, roster_status,
                overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
                finishing, playmaking, defense, rebounding, iq,
                potential, peak_age_start, peak_age_end,
                loyalty, money_drive, win_drive
            ) VALUES (
                $1, $2, $3, $4, $5,
                '1995-01-01', 6, 'active',
                $6, 70, 65, 60, 60,
                65, 60, 65, 65, 70,
                $6, 26, 31,
                50, 50, 50
            )
            RETURNING id
            """,
            league_id, team_id, code, f"Player{i}", pos, ovr,
        )
        player_ids.append(pid)
    for slot, pid in enumerate(player_ids, start=1):
        await db_pool.execute(
            """
            INSERT INTO lineups (league_id, team_id, is_starter, slot, player_id, set_by)
            VALUES ($1, $2, $3, $4, $5, NULL)
            """,
            league_id, team_id, slot <= 5, slot, pid,
        )
    return team_id, player_ids


async def _run_games(db_pool, league_id: int, season: int, home_team_id: int, away_team_id: int, star_id: int):
    """Run _N_GAMES real games between the two teams, returning per-game
    (usage_mode, points) pairs for `star_id` on the home team."""
    base_date = datetime.date(2025, 11, 1)
    observations: list[tuple[str, int]] = []
    for i in range(_N_GAMES):
        game_id = await game_repo.insert_game(db_pool, {
            "league_id": league_id,
            "season": season,
            "season_type": "regular",
            "game_index": i,
            "home_team_id": home_team_id,
            "away_team_id": away_team_id,
            "scheduled_date": base_date + datetime.timedelta(days=i * 3),
            "status": "scheduled",
            "is_user_matchup": False,
            "rng_seed": 5000 + i,
        })
        game_row = dict(await db_pool.fetchrow("SELECT * FROM games WHERE id = $1", game_id))

        sim_result = await sim_orchestrator._sim_single_game(
            db_pool, game_row, league_id, season, news_channel=None,
        )
        assert sim_result is not None

        home_gameplan = sim_result["home_gameplan"]
        usage_mode = home_gameplan["player_directives"].get(star_id, {}).get("usage_mode", "normal")

        points = await db_pool.fetchval(
            "SELECT points FROM game_box_scores WHERE game_id = $1 AND player_id = $2",
            game_id, star_id,
        )
        observations.append((usage_mode, points or 0))
    return observations


async def test_tanking_teams_isolation_star_can_still_get_featured_directive(db_pool, monkeypatch):
    """CA3: pre-fix, the redundant `if posture == "tanking":` block stomped
    EVERY tanking team's rank<2 usage_mode to "conserve" no matter what the
    first if/elif chain decided -- across _N_GAMES real games this player's
    usage_mode would be "feature" in ZERO of them. Post-fix, whenever the
    CPU's real (unmocked) scheme choice for this game lands on "isolation",
    the first chain's `rank == 0 and scheme == "isolation" -> feature` branch
    survives -- "feature" must appear at least once across _N_GAMES games,
    each independently seeded so the scheme choice varies game to game."""
    league_row = await db_pool.fetchrow(
        """
        INSERT INTO leagues (
            discord_guild_id, name, start_season_year, current_season,
            current_phase, commissioner_user_id
        ) VALUES (999201, 'CA2 CA3 Smoke League', 2025, 2025, 'REGULAR_SEASON_ACTIVE', 12345)
        RETURNING id
        """
    )
    league_id: int = league_row["id"]
    season = 2025

    # Tanking roster: rank-0 player (overall 93) is the lone star -- no other
    # top-8 player has playmaking > 85 or overall >= 90, so `_analyze_roster`
    # reports has_isolation_star=True and has_elite_passer=False, steering
    # `_decide_strategy`'s scheme_options toward [("isolation", 3),
    # ("inside_out", 1), ("ball_movement", 1)] -- isolation is likely but not
    # guaranteed per game, which is exactly why this test runs _N_GAMES
    # independently-seeded games rather than asserting on a single one.
    home_overalls = [93, 78, 74, 70, 68, 64, 60, 56]
    home_team_id, home_player_ids = await _seed_team(db_pool, league_id, "TNK", home_overalls)
    away_overalls = [80, 78, 76, 74, 72, 70, 68, 66]
    away_team_id, _ = await _seed_team(db_pool, league_id, "OPP", away_overalls)
    star_id = home_player_ids[0]

    async def _fake_compute_posture(pool, league, team_id):
        if team_id == home_team_id:
            return {"mode": "rebuilding", "urgency": "tanking"}
        return {"mode": "developing", "urgency": "comfortable"}

    monkeypatch.setattr(team_intel, "compute_posture", _fake_compute_posture)

    observations = await _run_games(db_pool, league_id, season, home_team_id, away_team_id, star_id)

    feature_games = [pts for mode, pts in observations if mode == "feature"]
    assert feature_games, (
        f"Expected at least one 'feature' game for the tanking team's isolation star "
        f"across {_N_GAMES} games; got usage_modes={[m for m, _ in observations]}. "
        f"Zero here means CA3's redundant tanking override is back (or reintroduced)."
    )


async def test_featured_directive_produces_measurably_more_points(db_pool, monkeypatch):
    """CA2: within the SAME player, games where the real persisted directive
    was "feature" should average measurably more points than games where it
    was "conserve"/"normal" -- proving usage_weight's touch-share nudge (and
    the star_usage_mult targeted-star nudge) actually reach the box score,
    not just that the directive string changed."""
    league_row = await db_pool.fetchrow(
        """
        INSERT INTO leagues (
            discord_guild_id, name, start_season_year, current_season,
            current_phase, commissioner_user_id
        ) VALUES (999202, 'CA2 Points Smoke League', 2025, 2025, 'REGULAR_SEASON_ACTIVE', 12345)
        RETURNING id
        """
    )
    league_id: int = league_row["id"]
    season = 2025

    home_overalls = [93, 78, 74, 70, 68, 64, 60, 56]
    home_team_id, home_player_ids = await _seed_team(db_pool, league_id, "TNK", home_overalls)
    away_overalls = [80, 78, 76, 74, 72, 70, 68, 66]
    away_team_id, _ = await _seed_team(db_pool, league_id, "OPP", away_overalls)
    star_id = home_player_ids[0]

    async def _fake_compute_posture(pool, league, team_id):
        if team_id == home_team_id:
            return {"mode": "rebuilding", "urgency": "tanking"}
        return {"mode": "developing", "urgency": "comfortable"}

    monkeypatch.setattr(team_intel, "compute_posture", _fake_compute_posture)

    observations = await _run_games(db_pool, league_id, season, home_team_id, away_team_id, star_id)

    feature_pts = [pts for mode, pts in observations if mode == "feature"]
    other_pts = [pts for mode, pts in observations if mode != "feature"]

    assert feature_pts, "Need at least one 'feature' game to compare (see the CA3 test for that assertion alone)"
    assert other_pts, "Need at least one non-'feature' game to compare against"

    avg_feature = sum(feature_pts) / len(feature_pts)
    avg_other = sum(other_pts) / len(other_pts)

    assert avg_feature > avg_other, (
        f"'feature'-directive games should average more points ({avg_feature:.1f}) than "
        f"'conserve'/'normal' games ({avg_other:.1f}) for the same player -- CA2's "
        f"usage_weight/star_usage_mult wiring should have produced a real scoring bump."
    )
