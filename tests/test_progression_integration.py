"""
Integration tests for services.progression_service.run_progression.

Unlike test_progression.py (which patches random), these tests use the real
stochastic algorithm and only assert DB side-effects — not specific attribute
values — so they remain deterministic.

patch_get_pool (autouse) routes progression_service.get_pool to db_pool.
"""
from __future__ import annotations

import datetime

from services import progression_service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A birth_date that makes the player ~22 years old regardless of the current date.
_BIRTH_DATE_AGE_22 = datetime.date(2004, 1, 1)


async def _setup(pool) -> tuple[int, int, int]:
    """
    Insert league + team + one active player (all ratings 75, age ~22, growth phase).
    Returns (league_id, team_id, player_id).
    """
    league_row = await pool.fetchrow(
        """
        INSERT INTO leagues
            (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES (888003, 'Progression Int Test League', 2025, 2025, 99999)
        RETURNING id
        """
    )
    league_id: int = league_row["id"]

    team_row = await pool.fetchrow(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'PRI', 'Prog Int Testers', 'Intville', 'West', 'Pacific')
        RETURNING id
        """,
        league_id,
    )
    team_id: int = team_row["id"]

    player_id = await _insert_active_player(pool, league_id, team_id)
    return league_id, team_id, player_id


async def _insert_active_player(pool, league_id: int, team_id: int, **overrides) -> int:
    """
    Insert a single active player with growth-phase defaults.
    Any field can be overridden via keyword arguments.
    Returns player_id.
    """
    defaults = dict(
        first_name="Int",
        last_name="Player",
        position="SF",
        birth_date=_BIRTH_DATE_AGE_22,
        years_pro=3,
        roster_status="active",
        overall=75,
        speed=75,
        shooting_2pt=75,
        shooting_3pt=75,
        shooting_mid=75,
        finishing=75,
        playmaking=75,
        defense=75,
        rebounding=75,
        iq=75,
        potential=85,
        peak_age_start=26,
        peak_age_end=31,
        loyalty=50,
        money_drive=50,
        win_drive=60,
    )
    defaults.update(overrides)

    pid = await pool.fetchval(
        """
        INSERT INTO players (
            league_id, team_id,
            first_name, last_name, position,
            birth_date, years_pro, roster_status,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq,
            potential, peak_age_start, peak_age_end,
            loyalty, money_drive, win_drive
        ) VALUES (
            $1,  $2,
            $3,  $4,  $5,
            $6,  $7,  $8,
            $9,  $10, $11, $12, $13,
            $14, $15, $16, $17, $18,
            $19, $20, $21,
            $22, $23, $24
        )
        RETURNING id
        """,
        league_id, team_id,
        defaults["first_name"], defaults["last_name"], defaults["position"],
        defaults["birth_date"], defaults["years_pro"], defaults["roster_status"],
        defaults["overall"], defaults["speed"], defaults["shooting_2pt"],
        defaults["shooting_3pt"], defaults["shooting_mid"],
        defaults["finishing"], defaults["playmaking"], defaults["defense"],
        defaults["rebounding"], defaults["iq"],
        defaults["potential"], defaults["peak_age_start"], defaults["peak_age_end"],
        defaults["loyalty"], defaults["money_drive"], defaults["win_drive"],
    )
    return pid


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_run_progression_returns_count(db_pool):
    """run_progression returns the count of active players processed."""
    league_id, team_id, _ = await _setup(db_pool)

    # Insert 2 more active players for a total of 3.
    await _insert_active_player(db_pool, league_id, team_id, first_name="Second")
    await _insert_active_player(db_pool, league_id, team_id, first_name="Third")

    count = await progression_service.run_progression(league_id, season=2025)
    assert count == 3


async def test_run_progression_skips_inactive(db_pool):
    """
    run_progression processes only active players.
    A free_agent player's years_pro must not increment.
    """
    league_id, team_id, active_pid = await _setup(db_pool)

    # Insert a free_agent player — progression must skip it.
    fa_pid = await _insert_active_player(
        db_pool, league_id, team_id,
        first_name="Free",
        roster_status="free_agent",
        years_pro=5,
    )

    fa_years_before = await db_pool.fetchval(
        "SELECT years_pro FROM players WHERE id = $1", fa_pid
    )

    await progression_service.run_progression(league_id, season=2025)

    fa_years_after = await db_pool.fetchval(
        "SELECT years_pro FROM players WHERE id = $1", fa_pid
    )
    assert fa_years_after == fa_years_before, (
        f"free_agent player years_pro changed: {fa_years_before} -> {fa_years_after}"
    )

    # Active player's years_pro must have incremented.
    active_years_after = await db_pool.fetchval(
        "SELECT years_pro FROM players WHERE id = $1", active_pid
    )
    assert active_years_after == 4  # started at 3


async def test_progression_writes_log_rows(db_pool):
    """
    run_progression inserts at least one progression_log row for a player in
    growth phase (overall delta is always >= +1 in growth, so a log row is guaranteed).
    """
    league_id, team_id, player_id = await _setup(db_pool)

    # Use a very young player with high potential to maximise the chance of a non-zero
    # delta. Growth phase guarantees overall +1 to +3, so at least one log entry always
    # appears.
    await db_pool.execute(
        """
        UPDATE players
        SET birth_date = '2006-01-01', years_pro = 0, potential = 95,
            peak_age_start = 27, peak_age_end = 33
        WHERE id = $1
        """,
        player_id,
    )

    await progression_service.run_progression(league_id, season=2025)

    count = await db_pool.fetchval(
        """
        SELECT COUNT(*) FROM progression_log
        WHERE league_id = $1 AND season_completed = 2025 AND player_id = $2
        """,
        league_id,
        player_id,
    )
    assert count > 0, "Expected at least one progression_log row for a growth-phase player"


async def test_years_pro_incremented_for_all(db_pool):
    """
    After run_progression, every active player's years_pro equals its original value + 1.
    """
    league_id, team_id, p1 = await _setup(db_pool)
    p2 = await _insert_active_player(db_pool, league_id, team_id, first_name="Two", years_pro=6)
    p3 = await _insert_active_player(db_pool, league_id, team_id, first_name="Three", years_pro=10)

    # Capture years_pro before progression.
    before = {}
    for pid in (p1, p2, p3):
        before[pid] = await db_pool.fetchval(
            "SELECT years_pro FROM players WHERE id = $1", pid
        )

    await progression_service.run_progression(league_id, season=2025)

    for pid, original in before.items():
        after = await db_pool.fetchval(
            "SELECT years_pro FROM players WHERE id = $1", pid
        )
        assert after == original + 1, (
            f"Player {pid}: expected years_pro {original + 1}, got {after}"
        )
