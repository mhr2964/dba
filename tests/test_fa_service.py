"""
Integration tests for services.fa_service.

Tests run against dba_test with a real asyncpg pool.
patch_get_pool (autouse) routes all service get_pool() calls to db_pool.
advance_to_responses requires a discord.Guild mock; mock_guild from conftest
returns None for all guild.get_channel() calls, so Discord sends are skipped.
"""
from __future__ import annotations

import pytest

from core.errors import DBAError
from services import fa_service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _setup(pool) -> tuple[int, int, int]:
    """
    Insert league + team + player (roster_status='active', OVR 80, age 28)
    with an active contract that has years_remaining=0 so open_fa will expire
    it and convert the player to a free_agent.
    Returns (league_id, team_id, player_id).
    """
    league_row = await pool.fetchrow(
        """
        INSERT INTO leagues
            (discord_guild_id, name, start_season_year, current_season,
             commissioner_user_id, salary_cap)
        VALUES (888001, 'FA Test League', 2025, 2025, 99999, 140000000)
        RETURNING id
        """
    )
    league_id: int = league_row["id"]

    team_row = await pool.fetchrow(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'FAT', 'FA Testers', 'Testville', 'East', 'Atlantic')
        RETURNING id
        """,
        league_id,
    )
    team_id: int = team_row["id"]

    player_row = await pool.fetchrow(
        """
        INSERT INTO players (
            league_id, team_id, first_name, last_name, position,
            birth_date, years_pro,
            roster_status,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq,
            potential, peak_age_start, peak_age_end,
            loyalty, money_drive, win_drive
        ) VALUES (
            $1, $2, 'Free', 'Agent', 'SF',
            '1997-01-01', 7,
            'active',
            80, 78, 72, 68, 70,
            74, 76, 73, 65, 80,
            85, 26, 31,
            50, 50, 60
        )
        RETURNING id
        """,
        league_id,
        team_id,
    )
    player_id: int = player_row["id"]

    # Expired contract — years_remaining=0 triggers open_fa to convert player to free_agent.
    await pool.execute(
        """
        INSERT INTO contracts
            (league_id, player_id, team_id, salary, years_remaining, total_years,
             contract_type, signed_in_season, is_active)
        VALUES ($1, $2, $3, 10000000, 0, 3, 'standard', 2022, TRUE)
        """,
        league_id,
        player_id,
        team_id,
    )

    return league_id, team_id, player_id


async def _setup_fa_open(pool) -> tuple[int, int, int]:
    """
    Full setup: league + team + player already converted to free_agent via open_fa.
    Returns (league_id, team_id, player_id).
    """
    league_id, team_id, player_id = await _setup(pool)
    await fa_service.open_fa(league_id, 2025)
    return league_id, team_id, player_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_open_fa_initializes_state(db_pool):
    """open_fa creates an fa_state row with current_day=1 and phase='offers_open'."""
    league_id, team_id, player_id = await _setup(db_pool)

    state = await fa_service.open_fa(league_id, 2025)

    assert state["league_id"] == league_id
    assert state["current_day"] == 1
    assert state["phase"] == "offers_open"


async def test_open_fa_converts_expired_contracts_to_free_agent(db_pool):
    """open_fa sets active players with years_remaining=0 contract to free_agent."""
    league_id, team_id, player_id = await _setup(db_pool)

    # Player starts as 'active' before open_fa.
    before = await db_pool.fetchval(
        "SELECT roster_status FROM players WHERE id = $1", player_id
    )
    assert before == "active"

    await fa_service.open_fa(league_id, 2025)

    after = await db_pool.fetchval(
        "SELECT roster_status FROM players WHERE id = $1", player_id
    )
    assert after == "free_agent"


async def test_submit_offer_creates_offer_row(db_pool):
    """submit_offer inserts an fa_offers row with status='submitted' for the correct player."""
    league_id, team_id, player_id = await _setup_fa_open(db_pool)

    offer_id = await fa_service.submit_offer(
        league_id, 2025, team_id, player_id, salary=15_000_000, years=2
    )

    row = await db_pool.fetchrow("SELECT * FROM fa_offers WHERE id = $1", offer_id)
    assert row is not None
    assert row["player_id"] == player_id
    assert row["team_id"] == team_id
    assert row["status"] == "submitted"
    assert row["salary_per_year"] == 15_000_000
    assert row["years"] == 2


async def test_submit_offer_enforces_daily_limit(db_pool):
    """
    After DAILY_OFFER_LIMIT offers are submitted in one FA day, the next raises DBAError
    about the daily limit.
    """
    league_id, team_id, player_id = await _setup_fa_open(db_pool)

    # We need DAILY_OFFER_LIMIT distinct free-agent players to submit to.
    # Insert extra players to hit the limit without re-using one player.
    extra_player_ids: list[int] = [player_id]

    for i in range(fa_service.DAILY_OFFER_LIMIT):  # DAILY_OFFER_LIMIT = 3
        pid = await db_pool.fetchval(
            """
            INSERT INTO players (
                league_id, first_name, last_name, position,
                years_pro, roster_status,
                overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
                finishing, playmaking, defense, rebounding, iq,
                potential, peak_age_start, peak_age_end,
                loyalty, money_drive, win_drive
            ) VALUES (
                $1, 'Extra', $2, 'PG',
                3, 'free_agent',
                72, 70, 68, 65, 67,
                70, 73, 69, 60, 75,
                80, 25, 30,
                50, 50, 50
            )
            RETURNING id
            """,
            league_id,
            f"Player{i}",
        )
        extra_player_ids.append(pid)

    # Submit DAILY_OFFER_LIMIT offers (one per unique FA player, same team, same day).
    for pid in extra_player_ids[: fa_service.DAILY_OFFER_LIMIT]:
        await fa_service.submit_offer(
            league_id, 2025, team_id, pid, salary=5_000_000, years=1
        )

    # The (DAILY_OFFER_LIMIT + 1)-th offer to a different free-agent player must fail.
    overflow_player_id = extra_player_ids[fa_service.DAILY_OFFER_LIMIT]
    with pytest.raises(DBAError, match="offers today"):
        await fa_service.submit_offer(
            league_id, 2025, team_id, overflow_player_id, salary=5_000_000, years=1
        )


async def test_submit_offer_requires_free_agent(db_pool):
    """submit_offer raises DBAError when the target player has roster_status='active'."""
    league_id, team_id, player_id = await _setup_fa_open(db_pool)

    # Insert a separate player that remains 'active' (never exposed to open_fa expiry).
    active_pid = await db_pool.fetchval(
        """
        INSERT INTO players (
            league_id, team_id, first_name, last_name, position,
            years_pro, roster_status,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq,
            potential, peak_age_start, peak_age_end,
            loyalty, money_drive, win_drive
        ) VALUES (
            $1, $2, 'Active', 'Player', 'C',
            5, 'active',
            78, 72, 66, 60, 64,
            70, 60, 75, 80, 74,
            82, 26, 31,
            55, 45, 65
        )
        RETURNING id
        """,
        league_id,
        team_id,
    )

    with pytest.raises(DBAError, match="not a free agent"):
        await fa_service.submit_offer(
            league_id, 2025, team_id, active_pid, salary=10_000_000, years=2
        )


async def test_advance_to_responses_processes_decisions(db_pool, mock_guild):
    """
    After submitting an offer and calling advance_to_responses, the offer status
    changes from 'submitted' to any terminal value (signed/declined/waiting/countered).
    """
    league_id, team_id, player_id = await _setup_fa_open(db_pool)

    offer_id = await fa_service.submit_offer(
        league_id, 2025, team_id, player_id, salary=15_000_000, years=3
    )

    results = await fa_service.advance_to_responses(league_id, 2025, mock_guild)

    # advance_to_responses must return at least one result dict.
    assert len(results) >= 1

    # Offer status must have changed — not still 'submitted'.
    row = await db_pool.fetchrow("SELECT status FROM fa_offers WHERE id = $1", offer_id)
    assert row["status"] != "submitted"


async def test_advance_day_increments_day(db_pool, mock_guild):
    """
    After open_fa (day=1) + advance_to_responses + advance_day,
    either current_day increments to 2 or phase transitions past offers_open.
    """
    league_id, team_id, player_id = await _setup_fa_open(db_pool)

    # Submit an offer so advance_to_responses has something to process.
    await fa_service.submit_offer(
        league_id, 2025, team_id, player_id, salary=12_000_000, years=2
    )
    await fa_service.advance_to_responses(league_id, 2025, mock_guild)

    state_before = await db_pool.fetchrow(
        "SELECT current_day, phase FROM fa_state WHERE league_id = $1", league_id
    )
    day_before = state_before["current_day"]

    new_state = await fa_service.advance_day(league_id, 2025)

    # Either day advanced or the FA closed (phase changed).
    advanced_day = new_state.get("current_day", 0) == day_before + 1
    closed = new_state.get("phase") in ("complete", "closed")
    assert advanced_day or closed, (
        f"Expected day to increment or FA to close, got state: {new_state}"
    )


async def test_claim_waiver_signs_player(db_pool):
    """
    claim_waiver on a waived player signs them at minimum salary:
    roster_status becomes 'active' and an active contract for team_id exists.
    """
    league_id, team_id, player_id = await _setup(db_pool)

    # Force player to waived (skip the full FA cycle).
    await db_pool.execute(
        "UPDATE players SET roster_status = 'waived', team_id = NULL WHERE id = $1",
        player_id,
    )
    # Deactivate the expired contract so cap math is clean.
    await db_pool.execute(
        "UPDATE contracts SET is_active = FALSE WHERE player_id = $1",
        player_id,
    )

    await fa_service.claim_waiver(league_id, team_id, player_id)

    status = await db_pool.fetchval(
        "SELECT roster_status FROM players WHERE id = $1", player_id
    )
    assert status == "active"

    contract = await db_pool.fetchrow(
        """
        SELECT * FROM contracts
        WHERE player_id = $1 AND team_id = $2 AND is_active = TRUE
        """,
        player_id,
        team_id,
    )
    assert contract is not None
    assert contract["salary"] == fa_service._MIN_SALARY
    assert contract["years_remaining"] == 1


async def test_claim_waiver_rejects_non_waived_player(db_pool):
    """claim_waiver raises DBAError when the player is not on waivers."""
    league_id, team_id, player_id = await _setup_fa_open(db_pool)

    # Player is now a free_agent after open_fa, not waived.
    with pytest.raises(DBAError, match="not on waivers"):
        await fa_service.claim_waiver(league_id, team_id, player_id)
