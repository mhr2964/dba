from __future__ import annotations

import pytest

from services.fa_service import submit_cpu_offers

pytestmark = pytest.mark.asyncio

_SALARY_CAP = 136_021_000


async def _setup(pool) -> tuple[int, int, int]:
    """
    Insert league, 2 CPU teams, 3 free agent players.
    Returns (league_id, team1_id, team2_id).
    """
    league_id: int = await pool.fetchval(
        """
        INSERT INTO leagues
            (name, discord_guild_id, commissioner_user_id, salary_cap,
             current_season, current_phase, start_season_year)
        VALUES ('Test League', 999001, 12345, $1, 2025, 'FA_OPEN', 2025)
        RETURNING id
        """,
        _SALARY_CAP,
    )

    team1_id: int = await pool.fetchval(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division, cpu_mode)
        VALUES ($1, 'TST', 'Testers', 'Testville', 'East', 'Atlantic', 'contending')
        RETURNING id
        """,
        league_id,
    )
    team2_id: int = await pool.fetchval(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division, cpu_mode)
        VALUES ($1, 'RBL', 'Rebuilders', 'Rebuild City', 'West', 'Pacific', 'rebuilding')
        RETURNING id
        """,
        league_id,
    )

    # 3 free agent players with varied OVR
    for ovr, first, last in [(78, "Alpha", "One"), (65, "Beta", "Two"), (90, "Gamma", "Three")]:
        await pool.execute(
            """
            INSERT INTO players
                (league_id, first_name, last_name, position, years_pro, is_rookie,
                 roster_status, overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
                 finishing, playmaking, defense, rebounding, iq, potential,
                 peak_age_start, peak_age_end, loyalty, money_drive, win_drive,
                 market_pref, star_leverage)
            VALUES ($1, $2, $3, 'SG', 5, FALSE, 'free_agent',
                    $4, 75, 75, 75, 75, 75, 75, 75, 75, 75, 80, 27, 32,
                    50, 50, 50, 'any', 50)
            """,
            league_id, first, last, ovr,
        )

    # FA state
    await pool.execute(
        """
        INSERT INTO fa_state (league_id, season, current_day, phase, total_days)
        VALUES ($1, 2025, 1, 'offers_open', 8)
        """,
        league_id,
    )

    return league_id, team1_id, team2_id


async def test_cpu_offers_submitted(db_pool) -> None:
    league_id, team1_id, team2_id = await _setup(db_pool)

    count = await submit_cpu_offers(league_id, 2025)

    assert count > 0, "Expected CPU teams to submit at least one offer"

    cpu_team_ids = {team1_id, team2_id}
    offer_rows = await db_pool.fetch(
        "SELECT team_id FROM fa_offers WHERE league_id = $1 AND season = 2025",
        league_id,
    )
    offering_team_ids = {r["team_id"] for r in offer_rows}
    # At least one CPU team placed an offer
    assert offering_team_ids & cpu_team_ids, "No fa_offers rows found for CPU team IDs"


async def test_cpu_respects_cap(db_pool) -> None:
    league_id, team1_id, _team2_id = await _setup(db_pool)

    # Add a placeholder player on team1 to nearly fill the cap (leave only $500k space).
    player_id: int = await db_pool.fetchval(
        """
        INSERT INTO players
            (league_id, first_name, last_name, position, years_pro, is_rookie,
             roster_status, team_id, overall, speed, shooting_2pt, shooting_3pt,
             shooting_mid, finishing, playmaking, defense, rebounding, iq, potential,
             peak_age_start, peak_age_end, loyalty, money_drive, win_drive,
             market_pref, star_leverage)
        VALUES ($1, 'Cap', 'Filler', 'C', 3, FALSE, 'active', $2,
                75, 75, 75, 75, 75, 75, 75, 75, 75, 75, 80, 27, 32,
                50, 50, 50, 'any', 50)
        RETURNING id
        """,
        league_id, team1_id,
    )
    blocker_salary = _SALARY_CAP - 500_000
    await db_pool.execute(
        """
        INSERT INTO contracts
            (league_id, player_id, team_id, salary, years_remaining, total_years,
             contract_type, signed_in_season, is_active)
        VALUES ($1, $2, $3, $4, 3, 3, 'fa', 2023, TRUE)
        """,
        league_id, player_id, team1_id, blocker_salary,
    )

    await submit_cpu_offers(league_id, 2025)

    # team1 should have submitted 0 offers because no offer can fit in $500k
    offer_count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM fa_offers WHERE league_id = $1 AND team_id = $2 AND season = 2025",
        league_id, team1_id,
    )
    assert offer_count == 0, f"team1 should not have submitted any offers over cap, got {offer_count}"


async def test_cpu_skips_full_roster(db_pool) -> None:
    league_id, team1_id, _team2_id = await _setup(db_pool)

    # Insert 15 active players on team1.
    for i in range(15):
        pid: int = await db_pool.fetchval(
            """
            INSERT INTO players
                (league_id, first_name, last_name, position, years_pro, is_rookie,
                 roster_status, team_id, overall, speed, shooting_2pt, shooting_3pt,
                 shooting_mid, finishing, playmaking, defense, rebounding, iq, potential,
                 peak_age_start, peak_age_end, loyalty, money_drive, win_drive,
                 market_pref, star_leverage)
            VALUES ($1, $2, 'Bench', 'PF', 2, FALSE, 'active', $3,
                    70, 70, 70, 70, 70, 70, 70, 70, 70, 70, 75, 26, 31,
                    50, 50, 50, 'any', 50)
            RETURNING id
            """,
            league_id, f"Player{i}", team1_id,
        )
        await db_pool.execute(
            """
            INSERT INTO contracts
                (league_id, player_id, team_id, salary, years_remaining, total_years,
                 contract_type, signed_in_season, is_active)
            VALUES ($1, $2, $3, 2000000, 2, 2, 'fa', 2024, TRUE)
            """,
            league_id, pid, team1_id,
        )

    await submit_cpu_offers(league_id, 2025)

    offer_count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM fa_offers WHERE league_id = $1 AND team_id = $2 AND season = 2025",
        league_id, team1_id,
    )
    assert offer_count == 0, f"Full-roster team should submit 0 offers, got {offer_count}"
