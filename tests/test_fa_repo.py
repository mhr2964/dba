"""
Unit tests for data.repositories.fa_repo.

All operations run against dba_test via db_pool (real asyncpg).
patch_get_pool autouse fixture is active but not needed here — we call
repo functions directly, passing the pool.
"""
from __future__ import annotations

from data.repositories import fa_repo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _setup(pool) -> tuple[int, int, int]:
    """
    Insert league + team + player rows.
    Returns (league_id, team_id, player_id).
    """
    league_row = await pool.fetchrow(
        """
        INSERT INTO leagues
            (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES (888002, 'FA Repo Test League', 2025, 2025, 99999)
        RETURNING id
        """
    )
    league_id: int = league_row["id"]

    team_row = await pool.fetchrow(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'RPO', 'Repo Testers', 'Repoville', 'West', 'Pacific')
        RETURNING id
        """,
        league_id,
    )
    team_id: int = team_row["id"]

    player_row = await pool.fetchrow(
        """
        INSERT INTO players (
            league_id, first_name, last_name, position,
            years_pro, roster_status,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq,
            potential, peak_age_start, peak_age_end,
            loyalty, money_drive, win_drive
        ) VALUES (
            $1, 'Repo', 'Player', 'PF',
            4, 'free_agent',
            77, 72, 68, 64, 66,
            71, 65, 74, 78, 76,
            83, 26, 31,
            50, 50, 55
        )
        RETURNING id
        """,
        league_id,
    )
    player_id: int = player_row["id"]

    # Ensure fa_state exists for season 2025.
    await fa_repo.get_or_create_fa_state(pool, league_id, 2025)
    await fa_repo.update_fa_state(pool, league_id, "offers_open", current_day=1)

    return league_id, team_id, player_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_submit_offer_and_retrieve(db_pool):
    """submit_offer persists a row retrievable via get_offers_for_player."""
    league_id, team_id, player_id = await _setup(db_pool)

    offer_id = await fa_repo.submit_offer(
        db_pool, league_id, 2025, fa_day=1,
        team_id=team_id, player_id=player_id,
        salary=12_000_000, years=2,
    )

    offers = await fa_repo.get_offers_for_player(
        db_pool, league_id, 2025, fa_day=1, player_id=player_id
    )

    assert len(offers) == 1
    assert offers[0]["id"] == offer_id
    assert offers[0]["salary_per_year"] == 12_000_000
    assert offers[0]["years"] == 2
    assert offers[0]["status"] == "submitted"


async def test_count_offers_today(db_pool):
    """count_offers_today returns 2 after submitting 2 offers for the same team on the same day."""
    league_id, team_id, player_id = await _setup(db_pool)

    # Need a second free-agent player for the second offer.
    player2_id = await db_pool.fetchval(
        """
        INSERT INTO players (
            league_id, first_name, last_name, position,
            years_pro, roster_status,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq,
            potential, peak_age_start, peak_age_end,
            loyalty, money_drive, win_drive
        ) VALUES (
            $1, 'Second', 'Player', 'SG',
            3, 'free_agent',
            74, 70, 66, 65, 67,
            69, 72, 68, 60, 74,
            80, 25, 30,
            50, 50, 50
        )
        RETURNING id
        """,
        league_id,
    )

    await fa_repo.submit_offer(
        db_pool, league_id, 2025, fa_day=1,
        team_id=team_id, player_id=player_id,
        salary=10_000_000, years=1,
    )
    await fa_repo.submit_offer(
        db_pool, league_id, 2025, fa_day=1,
        team_id=team_id, player_id=player2_id,
        salary=8_000_000, years=2,
    )

    count = await fa_repo.count_offers_today(db_pool, league_id, 2025, fa_day=1, team_id=team_id)
    assert count == 2


async def test_count_offers_today_excludes_other_days(db_pool):
    """count_offers_today does not count offers from a different fa_day."""
    league_id, team_id, player_id = await _setup(db_pool)

    await fa_repo.submit_offer(
        db_pool, league_id, 2025, fa_day=2,  # different day
        team_id=team_id, player_id=player_id,
        salary=10_000_000, years=1,
    )

    count = await fa_repo.count_offers_today(db_pool, league_id, 2025, fa_day=1, team_id=team_id)
    assert count == 0


async def test_update_offer_status(db_pool):
    """update_offer_status persists the new status to the DB."""
    league_id, team_id, player_id = await _setup(db_pool)

    offer_id = await fa_repo.submit_offer(
        db_pool, league_id, 2025, fa_day=1,
        team_id=team_id, player_id=player_id,
        salary=9_000_000, years=2,
    )

    await fa_repo.update_offer_status(db_pool, offer_id, "accepted")

    row = await db_pool.fetchrow("SELECT status FROM fa_offers WHERE id = $1", offer_id)
    assert row["status"] == "accepted"


async def test_create_counter(db_pool):
    """create_counter inserts an fa_counters row linked to the original offer."""
    league_id, team_id, player_id = await _setup(db_pool)

    offer_id = await fa_repo.submit_offer(
        db_pool, league_id, 2025, fa_day=1,
        team_id=team_id, player_id=player_id,
        salary=10_000_000, years=2,
    )

    counter_id = await fa_repo.create_counter(db_pool, offer_id, salary=12_000_000, years=3)

    row = await db_pool.fetchrow("SELECT * FROM fa_counters WHERE id = $1", counter_id)
    assert row is not None
    assert row["original_offer_id"] == offer_id
    assert row["salary_per_year"] == 12_000_000
    assert row["years"] == 3
    assert row["team_response"] == "pending"


async def test_get_unsigned_players(db_pool):
    """get_unsigned_players returns the free-agent player inserted during setup."""
    league_id, team_id, player_id = await _setup(db_pool)

    unsigned = await fa_repo.get_unsigned_players(db_pool, league_id)

    player_ids = [r["id"] for r in unsigned]
    assert player_id in player_ids


async def test_get_unsigned_players_excludes_active(db_pool):
    """get_unsigned_players does not return a player with roster_status='active'."""
    league_id, team_id, player_id = await _setup(db_pool)

    # Insert an active player — should NOT appear in results.
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
            $1, $2, 'Active', 'Guy', 'C',
            5, 'active',
            80, 74, 70, 62, 66,
            72, 63, 76, 82, 77,
            84, 27, 32,
            55, 45, 65
        )
        RETURNING id
        """,
        league_id,
        team_id,
    )

    unsigned = await fa_repo.get_unsigned_players(db_pool, league_id)
    unsigned_ids = [r["id"] for r in unsigned]

    assert active_pid not in unsigned_ids
