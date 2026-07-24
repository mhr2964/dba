"""
Integration tests for services.progression_service.

Uses db_pool (function-scoped asyncpg pool) and relies on the global
patch_get_pool autouse fixture from conftest.py, which patches
services.progression_service.get_pool to return db_pool.
"""
from __future__ import annotations

import datetime
from unittest.mock import patch

from services import progression_service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_test_league_and_team(pool) -> tuple[int, int]:
    """Insert a minimal league and one team. Returns (league_id, team_id)."""
    league_row = await pool.fetchrow(
        """
        INSERT INTO leagues
            (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES (777001, 'Prog Test League', 2025, 2025, 99999)
        RETURNING id
        """,
    )
    league_id = league_row["id"]

    team_row = await pool.fetchrow(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'TST', 'Testers', 'Testville', 'West', 'Pacific')
        RETURNING id
        """,
        league_id,
    )
    team_id = team_row["id"]

    return league_id, team_id


async def _insert_test_player(pool, league_id: int, team_id: int, **overrides) -> int:
    """
    Insert a player row with sensible defaults; any field can be overridden.
    Returns player_id.
    """
    defaults: dict = {
        "first_name": "Test",
        "last_name": "Player",
        "position": "SF",
        "birth_date": datetime.date(1998, 6, 15),  # ~27 years old in 2025
        "years_pro": 5,
        "overall": 75,
        "speed": 75,
        "shooting_2pt": 70,
        "shooting_3pt": 68,
        "shooting_mid": 70,
        "finishing": 72,
        "playmaking": 65,
        "defense": 70,
        "rebounding": 65,
        "iq": 75,
        "potential": 78,
        "peak_age_start": 26,
        "peak_age_end": 31,
        "loyalty": 50,
        "money_drive": 50,
        "win_drive": 60,
    }
    defaults.update(overrides)

    row = await pool.fetchrow(
        """
        INSERT INTO players (
            league_id, team_id,
            first_name, last_name, position,
            birth_date, years_pro,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq,
            potential, peak_age_start, peak_age_end,
            loyalty, money_drive, win_drive
        ) VALUES (
            $1,  $2,
            $3,  $4,  $5,
            $6,  $7,
            $8,  $9,  $10, $11, $12,
            $13, $14, $15, $16, $17,
            $18, $19, $20,
            $21, $22, $23
        )
        RETURNING id
        """,
        league_id, team_id,
        defaults["first_name"], defaults["last_name"], defaults["position"],
        defaults["birth_date"], defaults["years_pro"],
        defaults["overall"], defaults["speed"], defaults["shooting_2pt"],
        defaults["shooting_3pt"], defaults["shooting_mid"],
        defaults["finishing"], defaults["playmaking"], defaults["defense"],
        defaults["rebounding"], defaults["iq"],
        defaults["potential"], defaults["peak_age_start"], defaults["peak_age_end"],
        defaults["loyalty"], defaults["money_drive"], defaults["win_drive"],
    )
    return row["id"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_young_player_grows(db_pool):
    """Pre-peak player (age 21) never declines in overall after progression."""
    league_id, team_id = await _create_test_league_and_team(db_pool)

    # birth_date gives age ~21 relative to 2025
    birth_date = datetime.date(2004, 1, 1)
    player_id = await _insert_test_player(
        db_pool,
        league_id,
        team_id,
        birth_date=birth_date,
        years_pro=2,
        overall=72,
        potential=85,
        peak_age_start=27,
        peak_age_end=32,
    )

    original_overall = 72
    await progression_service.run_progression(league_id, season=2025)

    after = await db_pool.fetchrow("SELECT overall FROM players WHERE id = $1", player_id)
    assert after["overall"] >= original_overall, (
        f"Young player overall dropped: {original_overall} -> {after['overall']}"
    )


async def test_old_player_declines(db_pool):
    """Post-peak player (age 38) never increases in overall after progression."""
    league_id, team_id = await _create_test_league_and_team(db_pool)

    birth_date = datetime.date(1987, 1, 1)  # age 38 in 2025
    player_id = await _insert_test_player(
        db_pool,
        league_id,
        team_id,
        birth_date=birth_date,
        years_pro=15,
        overall=78,
        potential=82,
        peak_age_start=26,
        peak_age_end=33,
    )

    original_overall = 78
    await progression_service.run_progression(league_id, season=2025)

    after = await db_pool.fetchrow("SELECT overall FROM players WHERE id = $1", player_id)
    assert after["overall"] <= original_overall, (
        f"Old player overall went up: {original_overall} -> {after['overall']}"
    )


async def test_progression_writes_log(db_pool):
    """run_progression inserts at least one progression_log row for the player."""
    league_id, team_id = await _create_test_league_and_team(db_pool)

    # Young player in growth phase — guaranteed delta > 0 (growth is +1 to +3)
    birth_date = datetime.date(2003, 1, 1)  # age 22
    await _insert_test_player(
        db_pool,
        league_id,
        team_id,
        birth_date=birth_date,
        years_pro=1,
        overall=65,
        potential=90,
        peak_age_start=27,
        peak_age_end=32,
    )

    # Force growth to ensure a log row (patch random to return max values)
    with patch("services.progression_service.random") as mock_rng:
        mock_rng.randint = lambda lo, hi: hi
        mock_rng.random = lambda: 0.5  # below 0.08 breakout threshold? No — 0.5 > 0.08
        mock_rng.choices = lambda seq, weights=None: [seq[0]]
        mock_rng.sample = lambda pop, k: list(pop)[:k]
        await progression_service.run_progression(league_id, season=2025)

    count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM progression_log WHERE league_id = $1 AND season_completed = $2",
        league_id,
        2025,
    )
    assert count > 0, "Expected progression_log rows but found none"


async def test_peak_player_stable(db_pool):
    """Peak player (age 28, peak 26-31) overall change is within [-1, +1]."""
    league_id, team_id = await _create_test_league_and_team(db_pool)

    birth_date = datetime.date(1997, 1, 1)  # age 28
    player_id = await _insert_test_player(
        db_pool,
        league_id,
        team_id,
        birth_date=birth_date,
        years_pro=6,
        overall=80,
        potential=82,
        peak_age_start=26,
        peak_age_end=31,
    )

    original_overall = 80
    await progression_service.run_progression(league_id, season=2025)

    after = await db_pool.fetchrow("SELECT overall FROM players WHERE id = $1", player_id)
    delta = abs(after["overall"] - original_overall)
    assert delta <= 1, (
        f"Peak player overall changed by {delta}: {original_overall} -> {after['overall']}"
    )


async def test_years_pro_incremented(db_pool):
    """After run_progression, player.years_pro equals original value + 1."""
    league_id, team_id = await _create_test_league_and_team(db_pool)

    player_id = await _insert_test_player(
        db_pool,
        league_id,
        team_id,
        years_pro=4,
    )

    await progression_service.run_progression(league_id, season=2025)

    after = await db_pool.fetchrow("SELECT years_pro FROM players WHERE id = $1", player_id)
    assert after["years_pro"] == 5


async def test_high_potential_grows_more(db_pool):
    """
    High-potential young player accumulates more OVR gain than a low-potential
    peer over many progression runs (reset between runs).

    Uses real (unseeded) randomness rather than a fixed seed, so the sample
    size has to be large enough to make the expected-mean gap (~2.0 vs ~1.5
    OVR/run from _potential_growth_weight) survive sampling noise. At 10
    iterations the two distributions' means were only ~1.65 std devs apart,
    giving a ~5% chance of a spurious flip on any given run (confirmed: this
    test failed once across many green suite runs, then passed in isolation
    and on retry — not a real regression). 50 iterations pushes that
    false-failure rate below 0.05% while keeping runtime negligible (each
    iteration is a couple of lightweight DB round-trips).
    """
    league_id, team_id = await _create_test_league_and_team(db_pool)

    birth_date = datetime.date(2003, 6, 1)  # age ~22
    base_overall = 70

    high_gains: list[int] = []
    low_gains: list[int] = []
    iterations = 50

    for _ in range(iterations):
        # Insert fresh pair of players each iteration (DB is truncated per test
        # via clean_db fixture, so we work within a single test run here by
        # tracking absolute OVR change).
        hp_id = await _insert_test_player(
            db_pool,
            league_id,
            team_id,
            birth_date=birth_date,
            years_pro=2,
            overall=base_overall,
            potential=95,
            peak_age_start=27,
            peak_age_end=32,
        )
        lp_id = await _insert_test_player(
            db_pool,
            league_id,
            team_id,
            birth_date=birth_date,
            years_pro=2,
            overall=base_overall,
            potential=60,
            peak_age_start=27,
            peak_age_end=32,
        )

        await progression_service.run_progression(league_id, season=2025)

        hp_after = await db_pool.fetchval("SELECT overall FROM players WHERE id=$1", hp_id)
        lp_after = await db_pool.fetchval("SELECT overall FROM players WHERE id=$1", lp_id)

        high_gains.append(hp_after - base_overall)
        low_gains.append(lp_after - base_overall)

        # Clean up so next iteration starts fresh (avoid cascade on leagues)
        await db_pool.execute(
            "DELETE FROM players WHERE id = ANY($1)", [hp_id, lp_id]
        )

    mean_high = sum(high_gains) / len(high_gains)
    mean_low = sum(low_gains) / len(low_gains)
    assert mean_high >= mean_low, (
        f"High potential mean gain ({mean_high:.2f}) not >= low potential ({mean_low:.2f})"
    )


async def test_overall_never_exceeds_99(db_pool):
    """Player already at OVR 99 in growth phase stays at 99 after progression."""
    league_id, team_id = await _create_test_league_and_team(db_pool)

    birth_date = datetime.date(2003, 1, 1)  # young, growth phase
    player_id = await _insert_test_player(
        db_pool,
        league_id,
        team_id,
        birth_date=birth_date,
        years_pro=1,
        overall=99,
        speed=99,
        shooting_2pt=99,
        shooting_3pt=99,
        shooting_mid=99,
        finishing=99,
        playmaking=99,
        defense=99,
        rebounding=99,
        iq=99,
        potential=99,
        peak_age_start=27,
        peak_age_end=32,
    )

    # Force maximum growth rolls so the cap is exercised
    with patch("services.progression_service.random") as mock_rng:
        mock_rng.randint = lambda lo, hi: hi
        mock_rng.random = lambda: 0.5  # no breakout (0.5 > 0.08)
        mock_rng.choices = lambda seq, weights=None: [seq[-1]]
        mock_rng.sample = lambda pop, k: list(pop)[:k]
        await progression_service.run_progression(league_id, season=2025)

    after = await db_pool.fetchrow("SELECT * FROM players WHERE id = $1", player_id)
    for attr in progression_service.RATING_ATTRIBUTES:
        assert after[attr] <= 99, f"{attr} exceeded 99: {after[attr]}"


async def test_overall_never_below_40(db_pool):
    """Player at OVR 41 in steep decline phase never drops below 40."""
    league_id, team_id = await _create_test_league_and_team(db_pool)

    birth_date = datetime.date(1983, 1, 1)  # age 42, well past peak
    player_id = await _insert_test_player(
        db_pool,
        league_id,
        team_id,
        birth_date=birth_date,
        years_pro=20,
        overall=41,
        speed=41,
        shooting_2pt=41,
        shooting_3pt=41,
        shooting_mid=41,
        finishing=41,
        playmaking=41,
        defense=41,
        rebounding=41,
        iq=41,
        potential=60,
        peak_age_start=26,
        peak_age_end=32,
    )

    # Force maximum decline rolls so the floor is exercised
    with patch("services.progression_service.random") as mock_rng:
        mock_rng.randint = lambda lo, hi: hi  # worst possible decline
        mock_rng.random = lambda: 0.5
        mock_rng.choices = lambda seq, weights=None: [seq[0]]
        mock_rng.sample = lambda pop, k: list(pop)[:k]
        await progression_service.run_progression(league_id, season=2025)

    after = await db_pool.fetchrow("SELECT * FROM players WHERE id = $1", player_id)
    for attr in progression_service.RATING_ATTRIBUTES:
        assert after[attr] >= 40, f"{attr} fell below 40: {after[attr]}"
