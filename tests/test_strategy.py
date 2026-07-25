"""
Tests for team strategy, player minutes, sim integration, and contract extensions.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

from data.repositories import extension_repo, strategy_repo
from services import strategy_service
from services.sim_engine import sim_game


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_team(team_id: int, pace: float = 100.0) -> dict:
    return {
        "team_id": team_id,
        "offense_rating": 75,
        "defense_rating": 75,
        "pace": pace,
    }


def _make_player(player_id: int, team_id: int, overall: int = 75, position: str = "SF") -> dict:
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
    positions = ["PG", "SG", "SF", "PF", "C", "PG", "SG", "SF", "PF", "C"]
    return [_make_player(start_id + i, team_id, overall, positions[i]) for i in range(10)]


async def _create_league_team_player(pool) -> tuple[int, int, int]:
    """Insert minimal league, team, player. Returns (league_id, team_id, player_id)."""
    league_row = await pool.fetchrow(
        """
        INSERT INTO leagues (
            discord_guild_id, name, start_season_year, current_season,
            current_phase, commissioner_user_id
        ) VALUES (888001, 'Strategy Test League', 2025, 2025, 'REGULAR_SEASON_ACTIVE', 12345)
        RETURNING id
        """
    )
    league_id: int = league_row["id"]

    team_row = await pool.fetchrow(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'STT', 'Strategytesters', 'Testville', 'East', 'Atlantic')
        RETURNING id
        """,
        league_id,
    )
    team_id: int = team_row["id"]

    player_row = await pool.fetchrow(
        """
        INSERT INTO players (
            league_id, first_name, last_name, position, team_id,
            roster_status,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq,
            potential, peak_age_start, peak_age_end,
            loyalty, money_drive, win_drive
        ) VALUES (
            $1, 'Test', 'Player', 'SG', $2, 'active',
            80, 75, 70, 65, 68, 72, 78, 71, 60, 77,
            85, 26, 31, 55, 45, 65
        )
        RETURNING id
        """,
        league_id,
        team_id,
    )
    player_id: int = player_row["id"]
    return league_id, team_id, player_id


# ---------------------------------------------------------------------------
# Service unit tests (no DB required)
# ---------------------------------------------------------------------------


async def test_get_sim_modifiers_defaults():
    """Team with no strategy row returns zero/neutral adjustments."""
    mock_pool = AsyncMock()
    mock_pool.fetchrow = AsyncMock(return_value=None)

    modifiers = await strategy_service.get_sim_modifiers(mock_pool, league_id=1, team_id=99)

    assert modifiers["pace_adjustment"] == 0.0
    assert modifiers["ppp_offense_mult"] == 1.0
    assert modifiers["ppp_defense_mult"] == 1.0
    assert modifiers["three_rate_adj"] == 0.0
    assert modifiers["turnover_adj"] == 0.0
    assert modifiers["foul_adj"] == 0.0
    # star_usage default is 50 → 0.80 + 0.50 * 0.50 = 1.05
    assert abs(modifiers["star_usage_mult"] - 1.05) < 0.001


async def test_isolation_increases_star_usage():
    """Isolation scheme produces a star_usage_mult greater than the balanced baseline."""
    mock_pool = AsyncMock()

    async def _fetchrow_isolation(*args, **kwargs):
        return {
            "offensive_pace": "balanced",
            "offensive_scheme": "isolation",
            "defensive_scheme": "man_to_man",
            "defensive_intensity": "normal",
            "star_usage": 50,
        }

    mock_pool.fetchrow = _fetchrow_isolation

    modifiers = await strategy_service.get_sim_modifiers(mock_pool, league_id=1, team_id=1)

    # Isolation applies 1.25x multiplier on top of base star_usage_mult (1.05 at default 50)
    assert modifiers["star_usage_mult"] > 1.05, (
        f"Isolation should increase star_usage_mult above balanced baseline, got {modifiers['star_usage_mult']}"
    )


async def test_press_increases_pace():
    """Press defense adds pace adjustment."""
    mock_pool = AsyncMock()

    async def _fetchrow_press(*args, **kwargs):
        return {
            "offensive_pace": "balanced",
            "offensive_scheme": "balanced",
            "defensive_scheme": "press",
            "defensive_intensity": "normal",
            "star_usage": 50,
        }

    mock_pool.fetchrow = _fetchrow_press

    modifiers = await strategy_service.get_sim_modifiers(mock_pool, league_id=1, team_id=1)

    # why: press pace bump updated from +3 to +4 in strategy_service 2026-05 calibration pass.
    assert modifiers["pace_adjustment"] == 4.0, (
        f"Press defense should add +4 pace, got {modifiers['pace_adjustment']}"
    )


async def test_press_with_slow_roster_adds_foul_downside_and_shrinks_turnover_benefit():
    """Finding #2: press used to have zero downside and a flat benefit
    regardless of personnel. A slow roster (avg speed well below the 50-70
    fit band) should now get a real foul-rate cost and a reduced (not
    eliminated) turnover-forcing benefit."""
    mock_pool = AsyncMock()

    async def _fetchrow_press(*args, **kwargs):
        return {
            "offensive_pace": "balanced",
            "offensive_scheme": "balanced",
            "defensive_scheme": "press",
            "defensive_intensity": "normal",
            "star_usage": 50,
        }

    mock_pool.fetchrow = _fetchrow_press
    slow_roster = [{"overall": 70, "speed": 40} for _ in range(8)]

    modifiers = await strategy_service.get_sim_modifiers(
        mock_pool, league_id=1, team_id=1, players=slow_roster
    )

    assert modifiers["foul_adj"] > 0.0, "Slow roster pressing should incur a real foul-rate cost"
    assert modifiers["opp_turnover_adj"] < 0.05, (
        "Slow roster pressing should get a reduced turnover-forcing benefit vs the flat 0.05 baseline"
    )


async def test_press_with_fast_roster_gets_full_benefit_and_no_foul_downside():
    """A genuinely fast roster (avg speed >= 70) should get the full turnover
    benefit and no foul-rate penalty -- the personnel actually fits the scheme."""
    mock_pool = AsyncMock()

    async def _fetchrow_press(*args, **kwargs):
        return {
            "offensive_pace": "balanced",
            "offensive_scheme": "balanced",
            "defensive_scheme": "press",
            "defensive_intensity": "normal",
            "star_usage": 50,
        }

    mock_pool.fetchrow = _fetchrow_press
    fast_roster = [{"overall": 80, "speed": 85} for _ in range(8)]

    modifiers = await strategy_service.get_sim_modifiers(
        mock_pool, league_id=1, team_id=1, players=fast_roster
    )

    assert modifiers["foul_adj"] == 0.0
    assert abs(modifiers["opp_turnover_adj"] - 0.05) < 0.001


async def test_switch_all_with_poor_defense_roster_shrinks_benefit():
    """Finding #2: switch_all used to apply the same flat ppp_defense_mult/
    opp_three_rate_adj regardless of the team's own defensive personnel. A
    roster at league-average defense (well below the 50-74/40-60 fit bands)
    should get the full flat concession, not a reduced one."""
    mock_pool = AsyncMock()

    async def _fetchrow_switch(*args, **kwargs):
        return {
            "offensive_pace": "balanced",
            "offensive_scheme": "balanced",
            "defensive_scheme": "switch_all",
            "defensive_intensity": "normal",
            "star_usage": 50,
        }

    mock_pool.fetchrow = _fetchrow_switch
    poor_defense_roster = [{"overall": 70, "defense": 50, "defensive_effort": 40} for _ in range(8)]

    modifiers = await strategy_service.get_sim_modifiers(
        mock_pool, league_id=1, team_id=1, players=poor_defense_roster
    )

    assert abs(modifiers["ppp_defense_mult"] - 0.98) < 0.001
    assert abs(modifiers["opp_three_rate_adj"] - 0.07) < 0.001


async def test_switch_all_with_elite_defense_roster_gets_near_neutral_concession():
    """An elite, versatile defensive roster (avg defense >= 74, avg
    defensive_effort >= 60 -- the same bar finding #1's selection gate uses)
    should switch with minimal cost: concession shrinks toward 1.0 (no
    concession) and opponent 3PA rate opens up much less."""
    mock_pool = AsyncMock()

    async def _fetchrow_switch(*args, **kwargs):
        return {
            "offensive_pace": "balanced",
            "offensive_scheme": "balanced",
            "defensive_scheme": "switch_all",
            "defensive_intensity": "normal",
            "star_usage": 50,
        }

    mock_pool.fetchrow = _fetchrow_switch
    elite_defense_roster = [{"overall": 85, "defense": 85, "defensive_effort": 75} for _ in range(8)]

    modifiers = await strategy_service.get_sim_modifiers(
        mock_pool, league_id=1, team_id=1, players=elite_defense_roster
    )

    assert modifiers["ppp_defense_mult"] > 0.98
    assert modifiers["opp_three_rate_adj"] < 0.07


async def test_run_and_gun_adds_turnovers():
    """Run-and-gun pace adds turnover adjustment."""
    mock_pool = AsyncMock()

    async def _fetchrow_rng(*args, **kwargs):
        return {
            "offensive_pace": "run_and_gun",
            "offensive_scheme": "balanced",
            "defensive_scheme": "man_to_man",
            "defensive_intensity": "normal",
            "star_usage": 50,
        }

    mock_pool.fetchrow = _fetchrow_rng

    modifiers = await strategy_service.get_sim_modifiers(mock_pool, league_id=1, team_id=1)

    assert modifiers["pace_adjustment"] == 10.0
    assert modifiers["turnover_adj"] == 1.5


# ---------------------------------------------------------------------------
# Repository tests (require DB)
# ---------------------------------------------------------------------------


async def test_minutes_plan_sums_to_240(db_pool):
    """get_team_minutes_plan sums to 240 when roster is large enough to absorb cap overflow.

    Roster must have more than 5 players so bench players (30-min cap) can absorb
    overflow from starters (42-min cap). With exactly 5 players all classified as
    starters, overflow is irredistributable and total falls short of 240.
    Using 10 players (5 starters + 5 bench) ensures caps can be respected and
    the total stays at 240.
    """
    # why: minutes algorithm added per-player caps (42 starters / 30 bench) in 2026-05;
    #      with only 5 players all classified as starters the plan sums to 5*42=210,
    #      not 240. 10-player roster gives bench slots to absorb overflow.
    league_id, team_id, player_id = await _create_league_team_player(db_pool)

    # Add 9 more players: starters (OVR 80) and bench (OVR 65) so overflow redistributes.
    player_ids = [player_id]
    for i in range(9):
        ovr = 80 if i < 4 else 65  # 5 starters total (including the base player), 5 bench
        row = await db_pool.fetchrow(
            """
            INSERT INTO players (
                league_id, first_name, last_name, position, team_id,
                roster_status, overall, speed, shooting_2pt, shooting_3pt,
                shooting_mid, finishing, playmaking, defense, rebounding, iq,
                potential, peak_age_start, peak_age_end, loyalty, money_drive, win_drive
            ) VALUES ($1, 'Extra', $2, 'PF', $3, 'active', $4, 65, 60, 55,
                      58, 62, 68, 61, 70, 67, 75, 26, 31, 50, 50, 50)
            RETURNING id
            """,
            league_id,
            str(i),
            team_id,
            ovr,
        )
        player_ids.append(row["id"])

    plan = await strategy_repo.get_team_minutes_plan(db_pool, league_id, team_id, player_ids)
    total = sum(plan.values())

    assert abs(total - 240.0) < 0.01, f"Plan should sum to 240, got {total}"


async def test_minutes_plan_uses_targets(db_pool):
    """A player with target_minutes=40 gets at most 42 minutes (their starter cap).

    The targeted 40 is honored directly, but cap-overflow redistribution from other
    starters can push it up to the 42-minute starter ceiling. Bench players (9 total
    added at OVR 65) absorb most overflow so the targeted player stays near 40.
    The assertion checks the target is respected within the clamping tolerance.
    """
    # why: minutes algorithm added per-player caps in 2026-05; with only 5 starters
    #      cap overflow redistributes onto the targeted player (40 → 42). 10-player
    #      roster gives bench players to absorb overflow instead, keeping targeted
    #      player in the 40-42 range (at or near target, within starter cap).
    league_id, team_id, player_id = await _create_league_team_player(db_pool)

    # Set target of 40 minutes for this one player; all others are auto.
    await strategy_repo.set_player_minutes(db_pool, league_id, team_id, player_id, 40)

    # 9 additional players: 4 starters (OVR 80) + 5 bench (OVR 65) so overflow
    # can be redistributed to bench without hitting the targeted player.
    other_ids = [player_id]
    for i in range(9):
        ovr = 80 if i < 4 else 65
        row = await db_pool.fetchrow(
            """
            INSERT INTO players (
                league_id, first_name, last_name, position, team_id,
                roster_status, overall, speed, shooting_2pt, shooting_3pt,
                shooting_mid, finishing, playmaking, defense, rebounding, iq,
                potential, peak_age_start, peak_age_end, loyalty, money_drive, win_drive
            ) VALUES ($1, 'Auto', $2, 'C', $3, 'active', $4, 65, 60, 55,
                      58, 62, 68, 61, 70, 67, 75, 26, 31, 50, 50, 50)
            RETURNING id
            """,
            league_id,
            str(i),
            team_id,
            ovr,
        )
        other_ids.append(row["id"])

    plan = await strategy_repo.get_team_minutes_plan(db_pool, league_id, team_id, other_ids)

    # Target 40 is preserved; overflow redistribution may add up to the 42-min starter cap.
    assert 39.9 <= plan[player_id] <= 42.0, (
        f"Player with target 40 should get 40-42 minutes (starter cap), got {plan[player_id]}"
    )


# ---------------------------------------------------------------------------
# Sim engine integration tests (pure function, no DB)
# ---------------------------------------------------------------------------


def test_sim_game_uses_strategy():
    """
    Fast-pace strategy produces more total possessions than slow-pace strategy
    averaged over 5 runs with the same seeds.
    """
    home = _make_team(1)
    away = _make_team(2)
    home_players = _make_roster(1, start_id=1)
    away_players = _make_roster(2, start_id=101)

    fast_strategy = {
        "pace_adjustment": 10.0,
        "ppp_offense_mult": 1.0,
        "ppp_defense_mult": 1.0,
        "three_rate_adj": 0.0,
        "turnover_adj": 0.0,
        "foul_adj": 0.0,
        "star_usage_mult": 1.0,
    }
    slow_strategy = {
        "pace_adjustment": -6.0,
        "ppp_offense_mult": 1.0,
        "ppp_defense_mult": 1.0,
        "three_rate_adj": 0.0,
        "turnover_adj": 0.0,
        "foul_adj": 0.0,
        "star_usage_mult": 1.0,
    }

    seeds = [10, 20, 30, 40, 50]
    fast_possessions = []
    slow_possessions = []

    for seed in seeds:
        fast_result = sim_game(
            home, away, [p.copy() for p in home_players], [p.copy() for p in away_players],
            rng_seed=seed,
            home_strategy=fast_strategy,
            away_strategy=fast_strategy,
        )
        slow_result = sim_game(
            home, away, [p.copy() for p in home_players], [p.copy() for p in away_players],
            rng_seed=seed,
            home_strategy=slow_strategy,
            away_strategy=slow_strategy,
        )
        # Total possessions are reflected in scores (higher pace → more total points on average)
        fast_possessions.append(fast_result["home_score"] + fast_result["away_score"])
        slow_possessions.append(slow_result["home_score"] + slow_result["away_score"])

    avg_fast = sum(fast_possessions) / len(fast_possessions)
    avg_slow = sum(slow_possessions) / len(slow_possessions)

    assert avg_fast > avg_slow, (
        f"Fast pace should produce higher scoring than slow pace; fast={avg_fast:.1f} slow={avg_slow:.1f}"
    )


# ---------------------------------------------------------------------------
# Extension repository tests (require DB)
# ---------------------------------------------------------------------------


async def test_extension_creates_row(db_pool):
    """create_extension inserts a row retrievable by get_extension."""
    league_id, team_id, player_id = await _create_league_team_player(db_pool)

    ext_id = await extension_repo.create_extension(
        db_pool,
        league_id=league_id,
        player_id=player_id,
        team_id=team_id,
        salary=10_000_000,
        years=3,
        season=2025,
        activates_after=2027,
    )

    assert ext_id is not None and ext_id > 0

    row = await extension_repo.get_extension(db_pool, league_id, player_id)
    assert row is not None
    assert row["new_salary"] == 10_000_000
    assert row["new_years"] == 3
    assert row["activates_after_season"] == 2027


async def test_process_extensions_activates(db_pool):
    """process_extensions_for_season converts a pending extension into an active contract."""
    league_id, team_id, player_id = await _create_league_team_player(db_pool)

    # Give the player an active contract that expires at end of season 2025.
    await db_pool.execute(
        """
        INSERT INTO contracts (league_id, player_id, team_id, salary, years_remaining,
                               total_years, contract_type, signed_in_season, is_active)
        VALUES ($1, $2, $3, 8000000, 1, 2, 'fa', 2024, TRUE)
        """,
        league_id,
        player_id,
        team_id,
    )

    # Create extension activating after season 2025 (when current contract runs out).
    await extension_repo.create_extension(
        db_pool,
        league_id=league_id,
        player_id=player_id,
        team_id=team_id,
        salary=12_000_000,
        years=4,
        season=2025,
        activates_after=2025,
    )

    count = await extension_repo.process_extensions_for_season(
        db_pool, league_id, season=2025, signed_in_season=2026
    )
    assert count == 1

    # Old contract should be deactivated.
    old_active = await db_pool.fetchval(
        "SELECT COUNT(*) FROM contracts WHERE player_id = $1 AND salary = 8000000 AND is_active = TRUE",
        player_id,
    )
    assert old_active == 0

    # New contract should be active with the extension salary, anchored to the
    # season it actually starts in (signed_in_season), not the season param
    # used to match activates_after_season.
    new_row = await db_pool.fetchrow(
        "SELECT * FROM contracts WHERE player_id = $1 AND is_active = TRUE",
        player_id,
    )
    assert new_row is not None
    assert new_row["salary"] == 12_000_000
    assert new_row["years_remaining"] == 4
    assert new_row["contract_type"] == "extension"
    assert new_row["signed_in_season"] == 2026

    # Extension row should be deleted.
    ext_row = await extension_repo.get_extension(db_pool, league_id, player_id)
    assert ext_row is None
