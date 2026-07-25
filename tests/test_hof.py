from __future__ import annotations

import pytest

from services.hof_service import check_and_induct

pytestmark = pytest.mark.asyncio

_SALARY_CAP = 136_021_000


async def _insert_league_team_player(
    pool,
    *,
    years_pro: int,
    overall: int,
    roster_status: str = "retired",
) -> tuple[int, int, int]:
    """Returns (league_id, team_id, player_id)."""
    league_id: int = await pool.fetchval(
        """
        INSERT INTO leagues
            (name, discord_guild_id, commissioner_user_id, salary_cap,
             current_season, current_phase, start_season_year)
        VALUES ('HOF Test League', 999001, 12345, $1, 2025, 'PRESEASON_READY', 2025)
        RETURNING id
        """,
        _SALARY_CAP,
    )
    team_id: int = await pool.fetchval(
        """
        INSERT INTO teams
            (league_id, nba_team_code, name, city, conference, division, cpu_mode)
        VALUES ($1, 'HOF', 'Legends', 'Legend City', 'East', 'Atlantic', 'contending')
        RETURNING id
        """,
        league_id,
    )
    player_id: int = await pool.fetchval(
        """
        INSERT INTO players
            (league_id, first_name, last_name, position, years_pro, is_rookie,
             roster_status, team_id, overall, speed, shooting_2pt, shooting_3pt,
             shooting_mid, finishing, playmaking, defense, rebounding, iq, potential,
             peak_age_start, peak_age_end, loyalty, money_drive, win_drive,
             market_pref, star_leverage)
        VALUES ($1, 'Hall', 'Famer', 'SF', $2, FALSE, $3, $4,
                $5, 80, 80, 80, 80, 80, 80, 80, 80, 80, 85, 27, 33,
                50, 50, 50, 'any', 50)
        RETURNING id
        """,
        league_id, years_pro, roster_status, team_id, overall,
    )
    return league_id, team_id, player_id


async def test_check_and_induct_career_veteran(db_pool) -> None:
    """Player with years_pro=15 and OVR=82 should be inducted as a career veteran."""
    league_id, _team_id, player_id = await _insert_league_team_player(
        db_pool, years_pro=15, overall=82
    )

    inducted = await check_and_induct(db_pool, league_id, 2025)

    assert any(r["player_id"] == player_id for r in inducted), (
        "Expected the veteran player to be inducted"
    )

    # Verify the row landed in hall_of_fame.
    row = await db_pool.fetchrow(
        "SELECT * FROM hall_of_fame WHERE league_id = $1 AND player_id = $2",
        league_id, player_id,
    )
    assert row is not None, "hall_of_fame row was not inserted"
    assert row["career_seasons"] == 15
    assert row["inducted_season"] == 2025
    assert "veteran" in row["induction_reason"].lower() or "15" in row["induction_reason"]


async def test_player_not_inducted_twice(db_pool) -> None:
    """Calling check_and_induct twice must not create a duplicate HOF row."""
    league_id, _team_id, player_id = await _insert_league_team_player(
        db_pool, years_pro=15, overall=82
    )

    first = await check_and_induct(db_pool, league_id, 2025)
    second = await check_and_induct(db_pool, league_id, 2025)

    assert any(r["player_id"] == player_id for r in first), "First call should induct"
    assert not any(r["player_id"] == player_id for r in second), (
        "Second call should not re-induct the same player"
    )

    row_count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM hall_of_fame WHERE league_id = $1 AND player_id = $2",
        league_id, player_id,
    )
    assert row_count == 1, f"Expected 1 HOF row, found {row_count}"


async def test_young_player_not_inducted(db_pool) -> None:
    """A player with only 3 years_pro and OVR=90 does not meet any threshold."""
    league_id, _team_id, player_id = await _insert_league_team_player(
        db_pool, years_pro=3, overall=90
    )

    inducted = await check_and_induct(db_pool, league_id, 2025)

    assert not any(r["player_id"] == player_id for r in inducted), (
        "A 3-year player should not be inducted (no championship/MVP/elite-season criteria met)"
    )
    row = await db_pool.fetchrow(
        "SELECT * FROM hall_of_fame WHERE league_id = $1 AND player_id = $2",
        league_id, player_id,
    )
    assert row is None, "No hall_of_fame row should exist for an ineligible player"


# ---------------------------------------------------------------------------
# PA10 -- All-NBA / All-Star selection-count induction path
# PA11 -- retirement/age gate on every non-longevity path
# ---------------------------------------------------------------------------


async def _insert_award_result(pool, league_id: int, season: int, award_type: str, player_id: int) -> None:
    """Insert one award_votings + award_results row, same shape _count_mvp_votes
    already reads for the mvp path -- used here to seed All-NBA/All-Star/MVP
    selection counts across several seasons."""
    voting_id = await pool.fetchval(
        """
        INSERT INTO award_votings (league_id, season, award_type, status)
        VALUES ($1, $2, $3, 'closed')
        RETURNING id
        """,
        league_id, season, award_type,
    )
    await pool.execute(
        """
        INSERT INTO award_results (voting_id, player_id, place, vote_count, vote_share)
        VALUES ($1, $2, 1, 10, 1.0)
        """,
        voting_id, player_id,
    )


async def test_all_nba_selection_path_inducts_retired_player(db_pool) -> None:
    """PA10: 5+ All-NBA selections is sufficient for induction on its own."""
    league_id, _team_id, player_id = await _insert_league_team_player(
        db_pool, years_pro=10, overall=78, roster_status="retired"
    )
    for season in range(2015, 2020):  # 5 seasons
        await _insert_award_result(db_pool, league_id, season, "all_nba_2", player_id)

    inducted = await check_and_induct(db_pool, league_id, 2025)

    assert any(r["player_id"] == player_id for r in inducted)
    row = await db_pool.fetchrow(
        "SELECT induction_reason FROM hall_of_fame WHERE league_id=$1 AND player_id=$2",
        league_id, player_id,
    )
    assert "all-nba" in row["induction_reason"].lower()


async def test_all_star_selection_path_inducts_retired_player(db_pool) -> None:
    """PA10: 8+ All-Star selections is sufficient on its own."""
    league_id, _team_id, player_id = await _insert_league_team_player(
        db_pool, years_pro=10, overall=76, roster_status="retired"
    )
    for season in range(2012, 2020):  # 8 seasons
        await _insert_award_result(db_pool, league_id, season, "all_star_east", player_id)

    inducted = await check_and_induct(db_pool, league_id, 2025)

    assert any(r["player_id"] == player_id for r in inducted)


async def test_pa11_gate_blocks_early_career_all_nba_stack(db_pool) -> None:
    """PA11: a still-active player with years_pro < 8 must NOT be inducted
    off All-NBA selections alone, even with 5+ of them -- pre-fix, no
    retirement/age gate existed on this (or any non-longevity) path."""
    league_id, _team_id, player_id = await _insert_league_team_player(
        db_pool, years_pro=4, overall=88, roster_status="active"
    )
    for season in range(2021, 2026):
        await _insert_award_result(db_pool, league_id, season, "all_nba_1", player_id)

    inducted = await check_and_induct(db_pool, league_id, 2025)

    assert not any(r["player_id"] == player_id for r in inducted), (
        "An active player with years_pro < 8 should not be inducted off "
        "All-NBA selections alone (PA11 gate)"
    )


async def test_pa11_gate_blocks_early_career_mvp_votes(db_pool) -> None:
    """PA11 also gates the pre-existing MVP-votes path, not just the new PA10 path."""
    league_id, _team_id, player_id = await _insert_league_team_player(
        db_pool, years_pro=3, overall=90, roster_status="active"
    )
    for season in range(2022, 2031):  # 9 seasons of mvp votes -- exceeds the 8-vote threshold
        await _insert_award_result(db_pool, league_id, season, "mvp", player_id)

    inducted = await check_and_induct(db_pool, league_id, 2025)

    assert not any(r["player_id"] == player_id for r in inducted), (
        "An active player with years_pro < 8 should not be inducted off "
        "career MVP votes alone (PA11 gate)"
    )


async def test_pa11_gate_satisfied_by_years_pro_even_if_still_active(db_pool) -> None:
    """PA11's gate is roster_status='retired' OR years_pro >= 8 -- years_pro
    alone should satisfy it for a still-active player."""
    league_id, _team_id, player_id = await _insert_league_team_player(
        db_pool, years_pro=9, overall=80, roster_status="active"
    )
    for season in range(2015, 2020):
        await _insert_award_result(db_pool, league_id, season, "all_nba_3", player_id)

    inducted = await check_and_induct(db_pool, league_id, 2025)

    assert any(r["player_id"] == player_id for r in inducted)
