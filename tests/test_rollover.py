"""
Integration tests for services.rollover_service and data.repositories.history_repo.

No external service patching required — progression_service.run_progression was
removed from rollover_service in the 2026-05 refactor. run_rollover is now
self-contained: it ages contracts, archives history, and calls hof_service.
"""
from __future__ import annotations

from data.repositories import history_repo
from services import rollover_service


# ---------------------------------------------------------------------------
# Helper — minimal league + 2 teams + 1 player with a contract
# ---------------------------------------------------------------------------


async def _create_minimal_league(
    pool,
) -> tuple[int, int, int, int]:
    """
    Insert league (current_season=2025, phase=PROGRESSION_PENDING),
    two teams, one active player on team 1 with a 3-year contract.
    Returns (league_id, team_id, player_id, contract_id).
    """
    league_row = await pool.fetchrow(
        """
        INSERT INTO leagues (
            discord_guild_id, name, start_season_year, current_season,
            current_phase, commissioner_user_id
        ) VALUES (777001, 'Rollover Test League', 2025, 2025, 'PROGRESSION_PENDING', 99999)
        RETURNING id
        """,
    )
    league_id: int = league_row["id"]

    team_row = await pool.fetchrow(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'TST', 'Testers', 'Testville', 'East', 'Atlantic')
        RETURNING id
        """,
        league_id,
    )
    team_id: int = team_row["id"]

    await pool.execute(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'OPP', 'Opponents', 'Othertown', 'West', 'Pacific')
        """,
        league_id,
    )

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
            $1, 'Rollover', 'Guy', 'SG', $2,
            'active',
            80, 75, 70, 65, 68,
            72, 78, 71, 60, 77,
            85, 26, 31,
            55, 45, 65
        )
        RETURNING id
        """,
        league_id,
        team_id,
    )
    player_id: int = player_row["id"]

    contract_row = await pool.fetchrow(
        """
        INSERT INTO contracts (
            league_id, player_id, team_id,
            salary, years_remaining, total_years,
            contract_type, signed_in_season, is_active
        ) VALUES ($1, $2, $3, 5000000, 3, 3, 'standard', 2025, TRUE)
        RETURNING id
        """,
        league_id,
        player_id,
        team_id,
    )
    contract_id: int = contract_row["id"]

    return league_id, team_id, player_id, contract_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_contract_aging_decrements_years(db_pool):
    """_age_contracts reduces years_remaining by 1 for active contracts."""
    # why: progression_service.run_progression removed from rollover in 2026-05 refactor;
    #      no patch needed — run_rollover is now self-contained.
    league_id, team_id, player_id, contract_id = await _create_minimal_league(db_pool)

    await rollover_service.run_rollover(league_id)

    row = await db_pool.fetchrow(
        "SELECT years_remaining FROM contracts WHERE id = $1", contract_id
    )
    assert row["years_remaining"] == 2


async def test_expired_contract_frees_player(db_pool):
    """A contract with years_remaining=1 expires and the player becomes a free agent."""
    league_id, team_id, player_id, contract_id = await _create_minimal_league(db_pool)

    # Set contract to its final year
    await db_pool.execute(
        "UPDATE contracts SET years_remaining = 1 WHERE id = $1", contract_id
    )

    await rollover_service.run_rollover(league_id)

    contract_row = await db_pool.fetchrow(
        "SELECT is_active, years_remaining FROM contracts WHERE id = $1", contract_id
    )
    assert contract_row["is_active"] is False
    assert contract_row["years_remaining"] == 0

    player_row = await db_pool.fetchrow(
        "SELECT roster_status, team_id FROM players WHERE id = $1", player_id
    )
    assert player_row["roster_status"] == "free_agent"
    assert player_row["team_id"] is None


async def test_rollover_increments_season(db_pool):
    """run_rollover bumps current_season from 2025 to 2026."""
    league_id, _, _, _ = await _create_minimal_league(db_pool)

    await rollover_service.run_rollover(league_id)

    season = await db_pool.fetchval(
        "SELECT current_season FROM leagues WHERE id = $1", league_id
    )
    assert season == 2026


async def test_rollover_advances_phase(db_pool):
    """run_rollover sets current_phase to PROGRESSION_PENDING after rollover."""
    # why: phase was changed to PROGRESSION_PENDING (not PRESEASON_READY) in
    #      rollover_service — matches Phase.PROGRESSION_PENDING.value.
    league_id, _, _, _ = await _create_minimal_league(db_pool)

    await rollover_service.run_rollover(league_id)

    phase = await db_pool.fetchval(
        "SELECT current_phase FROM leagues WHERE id = $1", league_id
    )
    assert phase == "PROGRESSION_PENDING"


async def test_rollover_clears_standings_cache(db_pool):
    """run_rollover deletes all standings_cache rows for the league."""
    league_id, team_id, _, _ = await _create_minimal_league(db_pool)

    await db_pool.execute(
        """
        INSERT INTO standings_cache
            (league_id, season, team_id, wins, losses, conference, division)
        VALUES ($1, 2025, $2, 40, 42, 'East', 'Atlantic')
        """,
        league_id,
        team_id,
    )

    await rollover_service.run_rollover(league_id)

    count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM standings_cache WHERE league_id = $1", league_id
    )
    assert count == 0


async def test_history_record_created(db_pool):
    """run_rollover inserts a history_seasons row for the completed season."""
    league_id, _, _, _ = await _create_minimal_league(db_pool)

    await rollover_service.run_rollover(league_id)

    record = await history_repo.get_season(db_pool, league_id, 2025)
    assert record is not None
    assert record["league_id"] == league_id
    assert record["season"] == 2025


async def test_get_all_seasons_ordered(db_pool):
    """get_all_seasons returns records in descending season order."""
    league_id, _, _, _ = await _create_minimal_league(db_pool)

    await history_repo.record_season(db_pool, league_id, 2023)
    await history_repo.record_season(db_pool, league_id, 2024)

    records = await history_repo.get_all_seasons(db_pool, league_id)
    assert len(records) == 2
    assert records[0]["season"] == 2024
    assert records[1]["season"] == 2023


async def test_rollover_returns_summary_dict(db_pool):
    """run_rollover return value has the expected shape."""
    # why: progression_service removed from rollover in 2026-05; summary dict no
    #      longer includes 'players_progressed'. Now includes extensions_activated,
    #      picks_seeded, and hof_inducted.
    league_id, _, _, _ = await _create_minimal_league(db_pool)

    summary = await rollover_service.run_rollover(league_id)

    assert summary["season_archived"] == 2025
    assert summary["next_season"] == 2026
    assert isinstance(summary["contracts_expired"], int)
    assert isinstance(summary["extensions_activated"], int)
    assert isinstance(summary["picks_seeded"], int)
    assert isinstance(summary["hof_inducted"], list)


async def test_multiple_expired_contracts(db_pool):
    """All contracts at years_remaining=1 in the same league expire together."""
    league_id, team_id, player_id, contract_id = await _create_minimal_league(db_pool)

    # Add a second player with a final-year contract
    player2_row = await db_pool.fetchrow(
        """
        INSERT INTO players (
            league_id, first_name, last_name, position, team_id,
            roster_status,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq,
            potential, peak_age_start, peak_age_end,
            loyalty, money_drive, win_drive
        ) VALUES (
            $1, 'Second', 'Player', 'PF', $2,
            'active',
            75, 70, 65, 60, 63,
            68, 72, 66, 72, 70,
            80, 28, 33,
            50, 50, 60
        )
        RETURNING id
        """,
        league_id,
        team_id,
    )
    player2_id: int = player2_row["id"]

    await db_pool.execute(
        """
        INSERT INTO contracts (
            league_id, player_id, team_id,
            salary, years_remaining, total_years,
            contract_type, signed_in_season, is_active
        ) VALUES ($1, $2, $3, 3000000, 1, 4, 'standard', 2022, TRUE)
        """,
        league_id,
        player2_id,
        team_id,
    )
    # Player 1 has 3 years left; player 2 has 1 year left
    # Only player 2 should expire
    summary = await rollover_service.run_rollover(league_id)

    assert summary["contracts_expired"] == 1

    p2_row = await db_pool.fetchrow(
        "SELECT roster_status FROM players WHERE id = $1", player2_id
    )
    assert p2_row["roster_status"] == "free_agent"

    p1_row = await db_pool.fetchrow(
        "SELECT roster_status FROM players WHERE id = $1", player_id
    )
    assert p1_row["roster_status"] == "active"
