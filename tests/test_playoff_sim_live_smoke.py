"""
Live smoke test for PA1 (playoffs/awards/HOF realism audit) -- drives an
ACTUAL playoff series game end-to-end via playoff_service.sim_series_game,
not a mocked/isolated call.

tests/test_playoff_service.py::test_run_one_game_wires_pre_sim_inputs_into_sim_game
already proves the wiring *in isolation* (every sub-dependency of
_build_pre_sim_inputs is individually mocked). That test cannot prove
playoff_service.py's real call site is actually wired to the real
sim_orchestrator._build_pre_sim_inputs against a real database -- which is
exactly the class of gap PA1 slipped through once already (the pre-fix
code called sim_engine.sim_game with only 5 positional args, and no test
existed to catch it because zero test coverage existed for this module at
all before this sweep).

This test seeds a real two-team league directly in the test database,
creates a playoff series row (skipping the full 82-game regular season and
bracket-generation machinery -- neither is what PA1 touches), and calls
sim_series_game for real. It asserts against a table PA1's fix is the only
code path in playoff_service.py that writes to: game_cpu_gameplans. Before
this fix, playoff_service._run_one_game never called
cpu_coach_service.decide_gameplans / gameplan_repo.record_gameplan at all
(it called sim_engine.sim_game directly with 5 positional args) -- so this
table would have zero rows for the resulting game_id. This is a clean,
deterministic, non-flaky proof that the real pre-sim pipeline (gameplans,
directives, strategy modifiers, role-stamping, minutes plan, fatigue) ran
against a real playoff game, not just that a helper function exists.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from data.repositories import series_repo
from services import playoff_service

pytestmark = pytest.mark.asyncio


async def _seed_team_with_roster(db_pool, league_id: int, code: str, city: str) -> int:
    team_id = await db_pool.fetchval(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, $2, $3, $4, 'East', 'Atlantic')
        RETURNING id
        """,
        league_id, code, f"{city} {code}", city,
    )

    # 8 players: enough for _compute_team_ovr's top-8 average and a full
    # starter+bench lineup, with varied overall/position so role derivation
    # (and therefore _role_touch_share stamping) produces a real, differentiated
    # rotation rather than a degenerate single-player case.
    positions = ["PG", "SG", "SF", "PF", "C", "PG", "SF", "C"]
    overalls = [88, 82, 79, 76, 74, 68, 65, 60]
    player_ids = []
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

    return team_id


async def test_sim_series_game_wires_real_pre_sim_pipeline(db_pool):
    league_row = await db_pool.fetchrow(
        """
        INSERT INTO leagues (
            discord_guild_id, name, start_season_year, current_season,
            current_phase, commissioner_user_id
        ) VALUES (999001, 'Playoff Smoke League', 2025, 2025, 'PLAYOFFS_ACTIVE', 12345)
        RETURNING id
        """
    )
    league_id: int = league_row["id"]
    season = 2025

    high_team_id = await _seed_team_with_roster(db_pool, league_id, "HIS", "Hightown")
    low_team_id = await _seed_team_with_roster(db_pool, league_id, "LOS", "Lowville")

    series = await series_repo.create_series(
        db_pool, league_id, season, "nba_finals",
        high_seed_id=high_team_id, low_seed_id=low_team_id, games_needed=7,
    )

    mock_guild = MagicMock()
    mock_guild.get_channel = MagicMock(return_value=None)  # skip Discord embed posting

    # Deterministically skip the ~30%-chance playoff recap article branch --
    # it would otherwise make a real LLM call, which has nothing to do with
    # PA1 and would make this test flaky/expensive.
    with patch("services.playoff_service.random.random", return_value=0.99):
        outcome = await playoff_service.sim_series_game(league_id, series.id, mock_guild, season)

    assert outcome["game_result"]["winner_team_id"] in (high_team_id, low_team_id)

    # sim_series_game's return dict (game_result/series/series_over/winner_team_id)
    # doesn't include the raw game_id, so fetch it directly -- there's exactly one
    # game row for this league/season/series after the sim.
    game_row = await db_pool.fetchrow(
        "SELECT id FROM games WHERE league_id = $1 AND season = $2 AND series_id = $3",
        league_id, season, series.id,
    )
    game_id = game_row["id"]

    # --- The dispositive PA1 assertion ---
    # game_cpu_gameplans is written ONLY by gameplan_repo.record_gameplan, which is
    # ONLY called from sim_orchestrator._build_pre_sim_inputs. Pre-fix,
    # playoff_service._run_one_game never called that function at all -- it called
    # sim_engine.sim_game directly with 5 positional args. This assertion fails
    # against that code path (zero rows) and passes only if the real pre-sim
    # pipeline (gameplan decision, directive application, strategy modifiers,
    # role-stamping, minutes plan, back-to-back fatigue) genuinely ran.
    gameplan_rows = await db_pool.fetch(
        "SELECT team_id, source, offensive_scheme, defensive_scheme FROM game_cpu_gameplans WHERE game_id = $1",
        game_id,
    )
    gameplan_team_ids = {r["team_id"] for r in gameplan_rows}
    assert gameplan_team_ids == {high_team_id, low_team_id}, (
        f"Expected a game_cpu_gameplans row for both teams (proving the real "
        f"pre-sim pipeline ran), got rows for {gameplan_team_ids}"
    )
    for row in gameplan_rows:
        assert row["source"] == "cpu"
        assert row["offensive_scheme"]
        assert row["defensive_scheme"]

    # --- Corroborating signal: role-driven, non-degenerate minutes distribution ---
    # sim_engine's legacy fallback (the pre-fix dead path) and the real
    # role-stamped path both produce *some* distribution, so this alone
    # wouldn't catch the regression -- it's a secondary sanity check that the
    # game actually produced a normal box score, not a crash/empty result.
    box_rows = await db_pool.fetch(
        "SELECT player_id, minutes FROM game_box_scores WHERE game_id = $1", game_id
    )
    assert len(box_rows) >= 10, "Expected box score lines for both teams' rotations"
    minutes_values = {float(r["minutes"]) for r in box_rows}
    assert len(minutes_values) > 1, "All players got identical minutes -- rotation/role data did not vary output"

    # --- Persisted game row reflects a completed playoff game ---
    game_row = await db_pool.fetchrow("SELECT status, season_type FROM games WHERE id = $1", game_id)
    assert game_row["status"] == "simmed"
    assert game_row["season_type"] == "playoff"
