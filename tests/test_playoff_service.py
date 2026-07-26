"""
Tests for services.playoff_service, covering the PA1-PA5 fixes from the
playoffs/awards/HOF realism audit (docs/design/playoffs-awards-hof-logic-rules.md).

Zero dedicated test coverage existed for this module before this file.

test_run_one_game_wires_pre_sim_inputs_into_sim_game is the core PA1
regression guard: it mocks every DB-touching sub-dependency of
sim_orchestrator._build_pre_sim_inputs (decide_gameplans, record_gameplan,
get_sim_modifiers, _stamp_role_data, get_team_minutes_plan, is_back_to_back)
individually and lets the REAL _build_pre_sim_inputs function run, then
asserts sim_engine.sim_game receives the resulting fatigue/strategy/minutes
kwargs. Pre-fix, _run_one_game never called any of these functions and
called sim_game with only 5 positional args -- this test fails against that
code path (every mocked dependency would report zero calls, and sim_game's
call_kwargs would be empty).
"""
from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data.repositories import series_repo
from services import playoff_service

pytestmark = pytest.mark.asyncio


class _FakeTeam:
    def __init__(
        self,
        team_id,
        offense=80,
        defense=80,
        pace=100.0,
        conference="East",
        division="Atlantic",
        full_name=None,
        nba_team_code=None,
    ):
        self.id = team_id
        self.team_offense_rating = offense
        self.team_defense_rating = defense
        self.pace = pace
        self.conference = conference
        self.division = division
        self.full_name = full_name or f"Team {team_id}"
        self.nba_team_code = nba_team_code or f"T{team_id}"


# ---------------------------------------------------------------------------
# PA1/PA4/PA5 -- _run_one_game wiring (mock-based, no DB required)
# ---------------------------------------------------------------------------


async def test_run_one_game_wires_pre_sim_inputs_into_sim_game():
    """PA1: gameplans/directives/strategy/role-stamps/minutes-plan/fatigue
    must be built via sim_orchestrator._build_pre_sim_inputs and threaded
    into sim_engine.sim_game as kwargs -- not the pre-fix 5-positional-arg
    call that silently ran sim_engine's legacy fallback path.

    PA4/PA5 (bundled -- same function, same root cause): injuries and
    all-time records must be persisted directly, without routing through
    sim_persistence._persist_game_result (which would touch standings).
    """
    home_team = _FakeTeam(1)
    away_team = _FakeTeam(2)
    home_players = [{"id": 10, "first_name": "Home", "last_name": "Star"}]
    away_players = [{"id": 20, "first_name": "Away", "last_name": "Star"}]

    home_gameplan = {"source": "cpu", "strategy": {"offensive_scheme": "balanced"}, "player_directives": {}}
    away_gameplan = {"source": "cpu", "strategy": {"offensive_scheme": "three_heavy"}, "player_directives": {}}

    home_strategy_mod = {"tag": "home_strategy"}
    away_strategy_mod = {"tag": "away_strategy"}
    home_minutes_plan = {10: 36.0}
    away_minutes_plan = {20: 34.0}

    sim_result = {
        "home_score": 101,
        "away_score": 97,
        "winner_team_id": 1,
        "home_box": [],
        "away_box": [],
        "injuries": [],
    }

    pool = MagicMock()

    with (
        patch("services.playoff_service._load_lineup", AsyncMock(side_effect=[home_players, away_players])),
        patch("services.playoff_service._compute_team_ovr", AsyncMock(side_effect=[82, 79])),
        patch("services.playoff_service._get_playoff_sim_date", AsyncMock(return_value=datetime.date(2025, 6, 1))),
        patch("services.playoff_service.game_repo.insert_game", AsyncMock(return_value=555)),
        patch("services.playoff_service.game_repo.mark_simmed", AsyncMock()),
        patch("services.playoff_service.game_repo.insert_box_scores", AsyncMock()),
        patch("services.playoff_service.game_repo.insert_team_game_stats", AsyncMock()),
        patch("services.playoff_service._persist_injuries", AsyncMock()) as mock_persist_injuries,
        patch(
            "services.playoff_service.records_service.check_and_update_records",
            AsyncMock(return_value=([], [])),
        ) as mock_records,
        patch("services.sim_orchestrator.game_repo.is_back_to_back", AsyncMock(side_effect=[True, False])) as mock_b2b,
        patch(
            "services.sim_orchestrator.cpu_coach_service.decide_gameplans",
            AsyncMock(return_value=(home_gameplan, away_gameplan)),
        ) as mock_decide,
        patch("services.sim_orchestrator.gameplan_repo.record_gameplan", AsyncMock()) as mock_record_gameplan,
        patch(
            "services.sim_orchestrator.strategy_service.get_sim_modifiers",
            AsyncMock(side_effect=[home_strategy_mod, away_strategy_mod]),
        ) as mock_get_mods,
        patch("services.sim_orchestrator._stamp_role_data", AsyncMock()) as mock_stamp,
        patch(
            "services.sim_orchestrator.strategy_repo.get_team_minutes_plan",
            AsyncMock(side_effect=[home_minutes_plan, away_minutes_plan]),
        ) as mock_minutes,
        patch("services.playoff_service.sim_engine.sim_game", MagicMock(return_value=sim_result)) as mock_sim_game,
    ):
        game_id, result = await playoff_service._run_one_game(
            pool,
            league_id=1,
            season=2025,
            home_team=home_team,
            away_team=away_team,
            series_id=999,
            season_type="playoff",
            guild=None,
        )

    assert game_id == 555
    assert result is sim_result

    # The call site must actually thread through what the shared helper built.
    mock_sim_game.assert_called_once()
    _, call_kwargs = mock_sim_game.call_args
    assert call_kwargs["fatigue"] == {"home_b2b": True, "away_b2b": False}
    assert call_kwargs["home_strategy"] is home_strategy_mod
    assert call_kwargs["away_strategy"] is away_strategy_mod
    assert call_kwargs["home_minutes"] is home_minutes_plan
    assert call_kwargs["away_minutes"] is away_minutes_plan

    # The shared helper's own sub-steps must have actually run.
    mock_decide.assert_awaited_once()
    assert mock_record_gameplan.await_count == 2
    assert mock_get_mods.await_count == 2
    assert mock_stamp.await_count == 2
    assert mock_minutes.await_count == 2
    assert mock_b2b.await_count == 2

    # PA4/PA5: wired in directly, not through _persist_game_result.
    mock_persist_injuries.assert_awaited_once()
    mock_records.assert_awaited_once()
    records_call_args = mock_records.call_args[0]
    assert records_call_args[3] == 555  # game_id
    assert result["home_team_id"] == 1
    assert result["away_team_id"] == 2


async def test_run_one_game_resolves_injury_channel_when_guild_provided():
    """PA4: when a guild is passed, the injuries channel (same "injuries"
    lookup the regular-season path uses) is resolved and forwarded to
    _persist_injuries so playoff injuries are announced, not just recorded."""
    home_team = _FakeTeam(1)
    away_team = _FakeTeam(2)
    home_players = [{"id": 10, "first_name": "Home", "last_name": "Star"}]
    away_players = [{"id": 20, "first_name": "Away", "last_name": "Star"}]

    home_gameplan = {"source": "cpu", "strategy": {"offensive_scheme": "balanced"}, "player_directives": {}}
    away_gameplan = {"source": "cpu", "strategy": {"offensive_scheme": "balanced"}, "player_directives": {}}

    sim_result = {
        "home_score": 100, "away_score": 90, "winner_team_id": 1,
        "home_box": [], "away_box": [], "injuries": [],
    }

    pool = MagicMock()
    fake_guild = MagicMock()
    fake_channel = MagicMock()

    with (
        patch("services.playoff_service._load_lineup", AsyncMock(side_effect=[home_players, away_players])),
        patch("services.playoff_service._compute_team_ovr", AsyncMock(side_effect=[80, 80])),
        patch("services.playoff_service._get_playoff_sim_date", AsyncMock(return_value=datetime.date(2025, 6, 1))),
        patch("services.playoff_service.game_repo.insert_game", AsyncMock(return_value=556)),
        patch("services.playoff_service.game_repo.mark_simmed", AsyncMock()),
        patch("services.playoff_service.game_repo.insert_box_scores", AsyncMock()),
        patch("services.playoff_service.game_repo.insert_team_game_stats", AsyncMock()),
        patch("services.playoff_service._persist_injuries", AsyncMock()) as mock_persist_injuries,
        patch(
            "services.playoff_service.records_service.check_and_update_records",
            AsyncMock(return_value=([], [])),
        ),
        patch("services.sim_orchestrator.game_repo.is_back_to_back", AsyncMock(side_effect=[False, False])),
        patch(
            "services.sim_orchestrator.cpu_coach_service.decide_gameplans",
            AsyncMock(return_value=(home_gameplan, away_gameplan)),
        ),
        patch("services.sim_orchestrator.gameplan_repo.record_gameplan", AsyncMock()),
        patch(
            "services.sim_orchestrator.strategy_service.get_sim_modifiers",
            AsyncMock(side_effect=[{}, {}]),
        ),
        patch("services.sim_orchestrator._stamp_role_data", AsyncMock()),
        patch(
            "services.sim_orchestrator.strategy_repo.get_team_minutes_plan",
            AsyncMock(side_effect=[{}, {}]),
        ),
        patch("services.playoff_service.sim_engine.sim_game", MagicMock(return_value=sim_result)),
        patch("services.sim_channel_announcer._get_injury_channel", AsyncMock(return_value=fake_channel)) as mock_get_ch,
    ):
        await playoff_service._run_one_game(
            pool, league_id=1, season=2025, home_team=home_team, away_team=away_team,
            series_id=999, season_type="playoff", guild=fake_guild,
        )

    mock_get_ch.assert_awaited_once_with(fake_guild, pool, 1)
    mock_persist_injuries.assert_awaited_once()
    _, kwargs = mock_persist_injuries.call_args
    assert kwargs["injury_channel"] is fake_channel
    assert kwargs["guild"] is fake_guild


# ---------------------------------------------------------------------------
# Cross-league series access (Finding #1, playoff-audit sweep) -- a
# commissioner in league A must not be able to sim a game in league B's
# series by passing league B's series_id.
# ---------------------------------------------------------------------------


async def test_sim_series_game_rejects_cross_league_series_id(db_pool):
    """sim_series_game(league_a.id, series_b_id, ...) must raise the same
    'not found' ValueError it raises for a genuinely nonexistent series_id --
    it must not silently succeed and mutate league B's series/standings."""
    league_a, _, _ = await _seed_league_two_teams(db_pool, 646301)
    league_b, east_b, west_b = await _seed_league_two_teams(db_pool, 646302)

    series_b = await series_repo.create_series(
        db_pool,
        league_id=league_b,
        season=2025,
        round="first_round",
        high_seed_id=east_b,
        low_seed_id=west_b,
        games_needed=7,
    )

    with pytest.raises(ValueError, match=f"Series {series_b.id} not found"):
        await playoff_service.sim_series_game(league_a, series_b.id, MagicMock(), 2025)

    # The series must be completely untouched -- no game was simmed, no wins recorded.
    untouched = await series_repo.get_series(db_pool, league_b, series_b.id)
    assert untouched.wins_high == 0
    assert untouched.wins_low == 0
    assert untouched.status == "active"


# ---------------------------------------------------------------------------
# PA2 -- Finals home court
# ---------------------------------------------------------------------------


async def _seed_league_two_teams(pool, guild_id: int) -> tuple[int, int, int]:
    league_id = await pool.fetchval(
        """
        INSERT INTO leagues (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES ($1, 'Playoff Test League', 2025, 2025, 11111)
        RETURNING id
        """,
        guild_id,
    )
    east_team = await pool.fetchval(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'EAT', 'Eastside', 'East City', 'East', 'Atlantic')
        RETURNING id
        """,
        league_id,
    )
    west_team = await pool.fetchval(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'WES', 'Westside', 'West City', 'West', 'Pacific')
        RETURNING id
        """,
        league_id,
    )
    return league_id, east_team, west_team


async def test_finals_home_seed_picks_west_when_west_has_better_record(db_pool):
    """PA2: the West winner earns home court when it has the better record --
    pre-fix, the East winner was unconditionally the high seed."""
    league_id, east_team, west_team = await _seed_league_two_teams(db_pool, 646101)
    await db_pool.execute(
        """
        INSERT INTO standings_cache (league_id, season, team_id, wins, losses, conference, division)
        VALUES ($1, 2025, $2, 40, 20, 'East', 'Atlantic'),
               ($1, 2025, $3, 55, 10, 'West', 'Pacific')
        """,
        league_id, east_team, west_team,
    )

    high, low = await playoff_service._finals_home_seed(db_pool, league_id, 2025, east_team, west_team)

    assert high == west_team
    assert low == east_team


async def test_finals_home_seed_keeps_east_when_east_has_better_record(db_pool):
    """PA2 sanity check: East still gets home court when it legitimately earned it."""
    league_id, east_team, west_team = await _seed_league_two_teams(db_pool, 646102)
    await db_pool.execute(
        """
        INSERT INTO standings_cache (league_id, season, team_id, wins, losses, conference, division)
        VALUES ($1, 2025, $2, 58, 8, 'East', 'Atlantic'),
               ($1, 2025, $3, 44, 22, 'West', 'Pacific')
        """,
        league_id, east_team, west_team,
    )

    high, low = await playoff_service._finals_home_seed(db_pool, league_id, 2025, east_team, west_team)

    assert high == east_team
    assert low == west_team


async def test_finals_home_seed_falls_back_when_standings_missing(db_pool):
    """PA2 edge case: missing standings data falls back to the pre-PA2
    East-as-high-seed default rather than crashing the Finals-creation path."""
    league_id, east_team, west_team = await _seed_league_two_teams(db_pool, 646103)

    high, low = await playoff_service._finals_home_seed(db_pool, league_id, 2025, east_team, west_team)

    assert high == east_team
    assert low == west_team


# ---------------------------------------------------------------------------
# PA3 -- series MVP / stats queries must not leak regular-season games
# ---------------------------------------------------------------------------


async def _seed_playoff_series_with_regular_season_leak_case(pool, guild_id: int):
    """Seeds a playoff series between team_a/team_b plus ONE playoff game and
    ONE regular-season game between the exact same two teams -- the regular-
    season game must never count toward series stats (PA3)."""
    league_id = await pool.fetchval(
        """
        INSERT INTO leagues (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES ($1, 'PA3 Test League', 2025, 2025, 11111)
        RETURNING id
        """,
        guild_id,
    )
    team_a = await pool.fetchval(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'TMA', 'Team A', 'City A', 'East', 'Atlantic') RETURNING id
        """,
        league_id,
    )
    team_b = await pool.fetchval(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'TMB', 'Team B', 'City B', 'East', 'Atlantic') RETURNING id
        """,
        league_id,
    )
    player_id = await pool.fetchval(
        """
        INSERT INTO players (
            league_id, first_name, last_name, position, team_id,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq, potential,
            peak_age_start, peak_age_end, loyalty, money_drive, win_drive
        ) VALUES (
            $1, 'Series', 'Star', 'SF', $2,
            88, 80, 80, 75, 75, 80, 85, 75, 65, 80, 90, 25, 30, 50, 50, 70
        ) RETURNING id
        """,
        league_id, team_a,
    )

    series_id = await pool.fetchval(
        """
        INSERT INTO series (league_id, season, round, high_seed_team_id, low_seed_team_id)
        VALUES ($1, 2025, 'r1_east', $2, $3)
        RETURNING id
        """,
        league_id, team_a, team_b,
    )

    # Playoff game -- genuinely part of the series.
    playoff_game_id = await pool.fetchval(
        """
        INSERT INTO games (league_id, season, season_type, series_id, game_index,
                            home_team_id, away_team_id, scheduled_date, status)
        VALUES ($1, 2025, 'playoff', $2, 0, $3, $4, '2025-05-01', 'simmed')
        RETURNING id
        """,
        league_id, series_id, team_a, team_b,
    )
    await pool.execute(
        """
        INSERT INTO game_box_scores
            (game_id, player_id, team_id, started, minutes, points, rebounds_off, rebounds_def, assists, steals, blocks)
        VALUES ($1, $2, $3, TRUE, 36, 30, 2, 6, 5, 1, 0)
        """,
        playoff_game_id, player_id, team_a,
    )

    # Regular-season head-to-head between the SAME two teams -- pre-fix, the
    # old EXISTS clause (matching only on team pairs) let this leak into
    # "series" stats. It has no series_id and season_type='regular'.
    regular_game_id = await pool.fetchval(
        """
        INSERT INTO games (league_id, season, season_type, series_id, game_index,
                            home_team_id, away_team_id, scheduled_date, status)
        VALUES ($1, 2025, 'regular', NULL, 5, $2, $3, '2025-01-01', 'simmed')
        RETURNING id
        """,
        league_id, team_a, team_b,
    )
    await pool.execute(
        """
        INSERT INTO game_box_scores
            (game_id, player_id, team_id, started, minutes, points, rebounds_off, rebounds_def, assists, steals, blocks)
        VALUES ($1, $2, $3, TRUE, 36, 100, 20, 20, 20, 10, 10)
        """,
        regular_game_id, player_id, team_a,
    )

    return league_id, series_id, team_a, player_id


async def test_compute_series_mvp_excludes_regular_season_leak(db_pool):
    league_id, series_id, team_a, player_id = await _seed_playoff_series_with_regular_season_leak_case(
        db_pool, 646201
    )

    mvp = await playoff_service._compute_series_mvp(db_pool, league_id, 2025, series_id, team_a)

    assert mvp is not None
    assert mvp["player_id"] == player_id


async def test_get_series_stats_for_player_excludes_regular_season_leak(db_pool):
    """Direct check of the underlying stats: 1 game / 30 ppg (the playoff
    game only), not 2 games / 65 ppg (leaking the 100-point regular-season
    game in). This is the assertion that fails against pre-fix code."""
    league_id, series_id, team_a, player_id = await _seed_playoff_series_with_regular_season_leak_case(
        db_pool, 646202
    )

    stats = await playoff_service._get_series_stats_for_player(db_pool, league_id, 2025, series_id, player_id)

    assert stats["games"] == 1
    assert stats["ppg"] == 30.0
