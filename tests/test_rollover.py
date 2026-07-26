"""
Integration tests for services.rollover_service and data.repositories.history_repo.

No external service patching required — progression_service.run_progression was
removed from rollover_service in the 2026-05 refactor. run_rollover is now
self-contained: it ages contracts and archives history. HOF induction
(hof_service.check_and_induct) is NOT called from here (RO3) -- it runs from
the /offseason progression command handler, after progression has updated
years_pro/retirement state. See test_hof_induction_runs_after_progression_not_rollover
below for the mandatory cross-service sequencing proof.
"""
from __future__ import annotations

from unittest.mock import patch

from data.repositories import extension_repo, history_repo, league_repo
from services import hof_service, progression_service, rollover_service


# ---------------------------------------------------------------------------
# Helper — minimal league + 2 teams + 1 player with a contract
# ---------------------------------------------------------------------------


async def _create_minimal_league(
    pool,
) -> tuple[int, int, int, int]:
    """
    Insert league (current_season=2025, phase=OFFSEASON_AWARDS_CLOSED -- a
    real precondition /offseason rollover allows calling run_rollover from,
    per phase/graph.py PT3), two teams, one active player on team 1 with a
    3-year contract.
    Returns (league_id, team_id, player_id, contract_id).
    """
    league_row = await pool.fetchrow(
        """
        INSERT INTO leagues (
            discord_guild_id, name, start_season_year, current_season,
            current_phase, commissioner_user_id
        ) VALUES (777001, 'Rollover Test League', 2025, 2025, 'OFFSEASON_AWARDS_CLOSED', 99999)
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
    """
    RO2/RO8: run_rollover clears the current league's standings_cache scoped
    to the new season only (prior seasons' rows survive for /offseason
    history) and clears trade_block entirely (pure current-state table, like
    ready_status -- its read paths never filter by season).
    """
    league_id, team_id, player_id, _ = await _create_minimal_league(db_pool)

    # Prior-season row -- must survive rollover.
    await db_pool.execute(
        """
        INSERT INTO standings_cache
            (league_id, season, team_id, wins, losses, conference, division)
        VALUES ($1, 2024, $2, 38, 44, 'East', 'Atlantic')
        """,
        league_id,
        team_id,
    )
    # Current-season row -- would previously be wiped by the unscoped DELETE.
    await db_pool.execute(
        """
        INSERT INTO standings_cache
            (league_id, season, team_id, wins, losses, conference, division)
        VALUES ($1, 2025, $2, 40, 42, 'East', 'Atlantic')
        """,
        league_id,
        team_id,
    )
    await db_pool.execute(
        """
        INSERT INTO trade_block (league_id, team_id, player_id, note)
        VALUES ($1, $2, $3, 'shopping this player')
        """,
        league_id,
        team_id,
        player_id,
    )

    await rollover_service.run_rollover(league_id)

    prior_season_count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM standings_cache WHERE league_id = $1 AND season = 2024",
        league_id,
    )
    assert prior_season_count == 1, "Prior season's standings_cache row must survive rollover"

    trade_block_count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM trade_block WHERE league_id = $1", league_id
    )
    assert trade_block_count == 0, "trade_block listings must be cleared at rollover"


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
    #      longer includes 'players_progressed'. RO3 further removed
    #      'hof_inducted' -- HOF induction now runs from the /offseason
    #      progression handler, not rollover. RO6 added 'new_salary_cap'.
    league_id, _, _, _ = await _create_minimal_league(db_pool)

    summary = await rollover_service.run_rollover(league_id)

    assert summary["season_archived"] == 2025
    assert summary["next_season"] == 2026
    assert isinstance(summary["contracts_expired"], int)
    assert isinstance(summary["extensions_activated"], int)
    assert isinstance(summary["picks_seeded"], int)
    assert isinstance(summary["new_salary_cap"], int)
    assert "hof_inducted" not in summary


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


# ---------------------------------------------------------------------------
# P1 — season off-by-one in the rollover -> progression handoff
# ---------------------------------------------------------------------------


async def test_rollover_sets_pending_progression_season(db_pool):
    """
    run_rollover stores the PRE-increment season on pending_progression_season
    in the same statement that sets current_phase=PROGRESSION_PENDING, while
    current_season itself moves forward to next_season.
    """
    league_id, _, _, _ = await _create_minimal_league(db_pool)

    await rollover_service.run_rollover(league_id)

    league = await league_repo.get_by_guild(db_pool, 777001)
    assert league.pending_progression_season == 2025
    assert league.current_season == 2026


async def test_rollover_then_progression_uses_correct_season(db_pool):
    """
    Regression test for the season off-by-one in the rollover -> progression
    handoff (P1). run_rollover increments current_season BEFORE progression
    ever runs; progression must filter that season's games/injuries by
    league.pending_progression_season (the season that just finished), not
    league.current_season directly.

    Both the low-minutes game log and the season-ending injury below are
    recorded under season 2025 -- the season that is about to be rolled over.
    Reading league.current_season (2026, post-rollover) after run_rollover
    would find zero matching games/injuries and neither penalty would ever
    log, which is exactly the bug this test must catch.
    """
    league_id, team_id, player_id, _ = await _create_minimal_league(db_pool)

    # Force decline stage (age >= peak_age_end) so both penalties land on the
    # same, deterministic delta path regardless of growth-phase randomness.
    await db_pool.execute(
        """
        UPDATE players
        SET birth_date = '1987-01-01', peak_age_start = 26, peak_age_end = 31
        WHERE id = $1
        """,
        player_id,
    )

    # Known low-minutes game log under season 2025 (>10 games, avg < 15 min).
    for i in range(12):
        game_row = await db_pool.fetchrow(
            """
            INSERT INTO games (
                league_id, season, season_type, game_index,
                home_team_id, away_team_id, scheduled_date,
                status, home_score, away_score, winner_team_id
            ) VALUES ($1, 2025, 'regular', $2, $3, $3, '2025-01-01', 'simmed', 100, 95, $3)
            RETURNING id
            """,
            league_id,
            i + 1,
            team_id,
        )
        await db_pool.execute(
            """
            INSERT INTO game_box_scores (game_id, player_id, team_id, minutes, points)
            VALUES ($1, $2, $3, 8, 4)
            """,
            game_row["id"],
            player_id,
            team_id,
        )

    # Season-ending injury recorded under season 2025.
    await db_pool.execute(
        """
        INSERT INTO injuries (league_id, player_id, season, severity, games_missed)
        VALUES ($1, $2, 2025, 'season_ending', 40)
        """,
        league_id,
        player_id,
    )

    summary = await rollover_service.run_rollover(league_id)
    assert summary["season_archived"] == 2025

    league = await league_repo.get_by_guild(db_pool, 777001)
    assert league.pending_progression_season == 2025
    assert league.current_season == 2026

    # This is the exact call the fixed offseason_cog now makes -- via the
    # handoff column, NOT league.current_season.
    await progression_service.run_progression(league_id, league.pending_progression_season)

    reasons = await db_pool.fetch(
        "SELECT reason FROM progression_log WHERE league_id = $1 AND player_id = $2",
        league_id,
        player_id,
    )
    reason_set = {r["reason"] for r in reasons}
    assert "low_minutes" in reason_set, f"Expected low_minutes penalty, got {reason_set}"
    assert "injury_setback" in reason_set, f"Expected injury_setback penalty, got {reason_set}"


# ---------------------------------------------------------------------------
# RO6 — salary cap grows season-over-season
# ---------------------------------------------------------------------------


async def test_rollover_grows_salary_cap(db_pool):
    """
    run_rollover grows leagues.salary_cap by _SALARY_CAP_GROWTH_RATE (3%),
    rounded to the nearest $100k, in the same statement as the current_season
    advance, and returns the new value as summary['new_salary_cap'].
    """
    league_id, _, _, _ = await _create_minimal_league(db_pool)

    cap_before = await db_pool.fetchval(
        "SELECT salary_cap FROM leagues WHERE id = $1", league_id
    )

    summary = await rollover_service.run_rollover(league_id)

    cap_after = await db_pool.fetchval(
        "SELECT salary_cap FROM leagues WHERE id = $1", league_id
    )
    expected = round(cap_before * 1.03 / 100_000) * 100_000
    assert cap_after == expected
    assert summary["new_salary_cap"] == expected


# ---------------------------------------------------------------------------
# RO1 — MANDATORY live smoke test: extension activation ordering
# ---------------------------------------------------------------------------


async def test_rollover_extension_activates_with_full_new_years_term(db_pool):
    """
    RO1 -- MANDATORY live smoke test. Seeds a real league with a contract at
    years_remaining=1 plus a pending extension (new_years=4), calls the REAL
    rollover_service.run_rollover, and asserts the resulting contract has
    years_remaining == 4 (not 3) and the correct signed_in_season.

    Proven to fail against the pre-fix call order: with
    extension_repo.process_extensions_for_season running BEFORE
    _age_contracts (the old order in run_rollover), _age_contracts then
    unconditionally decrements every active contract in the league --
    including the just-inserted 4-year extension contract -- producing
    years_remaining == 3, not 4. Temporarily reverting run_rollover's call
    order back to extensions-then-aging (keeping everything else, including
    the new signed_in_season param) and rerunning this test reproduces that
    failure; restoring the fixed order passes it. tests/test_strategy.py's
    test_process_extensions_activates calls the repo function in isolation
    and structurally cannot catch this ordering bug.
    """
    league_row = await db_pool.fetchrow(
        """
        INSERT INTO leagues (
            discord_guild_id, name, start_season_year, current_season,
            current_phase, commissioner_user_id
        ) VALUES (777010, 'RO1 Smoke League', 2025, 2025, 'OFFSEASON_AWARDS_CLOSED', 99999)
        RETURNING id
        """
    )
    league_id: int = league_row["id"]

    team_id = await db_pool.fetchval(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'TST', 'Testers', 'Testville', 'East', 'Atlantic')
        RETURNING id
        """,
        league_id,
    )
    await db_pool.execute(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'OPP', 'Opponents', 'Othertown', 'West', 'Pacific')
        """,
        league_id,
    )

    player_id = await db_pool.fetchval(
        """
        INSERT INTO players (
            league_id, first_name, last_name, position, team_id,
            roster_status,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq,
            potential, peak_age_start, peak_age_end,
            loyalty, money_drive, win_drive
        ) VALUES (
            $1, 'Extension', 'Guy', 'PG', $2,
            'active',
            82, 78, 74, 68, 70,
            75, 80, 72, 62, 78,
            85, 25, 30,
            60, 40, 70
        )
        RETURNING id
        """,
        league_id,
        team_id,
    )

    # Active contract at years_remaining=1 -- ends this season.
    await db_pool.execute(
        """
        INSERT INTO contracts (
            league_id, player_id, team_id,
            salary, years_remaining, total_years,
            contract_type, signed_in_season, is_active
        ) VALUES ($1, $2, $3, 9000000, 1, 3, 'standard', 2023, TRUE)
        """,
        league_id,
        player_id,
        team_id,
    )

    # Pending extension activating after the season that's about to roll over.
    await extension_repo.create_extension(
        db_pool,
        league_id=league_id,
        player_id=player_id,
        team_id=team_id,
        salary=15_000_000,
        years=4,
        season=2025,
        activates_after=2025,
    )

    await rollover_service.run_rollover(league_id)

    new_contract = await db_pool.fetchrow(
        """
        SELECT years_remaining, signed_in_season, is_active
        FROM contracts
        WHERE player_id = $1 AND contract_type = 'extension'
        """,
        player_id,
    )
    assert new_contract is not None
    assert new_contract["is_active"] is True
    assert new_contract["years_remaining"] == 4, (
        f"Expected the extension to activate with its full 4-year term, got "
        f"{new_contract['years_remaining']} -- this is RO1's bug: "
        f"_age_contracts decrementing a contract that was just inserted."
    )
    assert new_contract["signed_in_season"] == 2026

    player_row = await db_pool.fetchrow(
        "SELECT roster_status, team_id FROM players WHERE id = $1", player_id
    )
    assert player_row["roster_status"] == "active"
    assert player_row["team_id"] == team_id


# ---------------------------------------------------------------------------
# RO3 — MANDATORY live smoke test: HOF induction sequencing
# ---------------------------------------------------------------------------


async def test_hof_induction_runs_after_progression_not_rollover(db_pool):
    """
    RO3 -- MANDATORY live smoke test, cross-service sequencing proof.

    Seeds a player one cycle away from crossing the veteran-longevity HOF
    threshold (years_pro >= 15, overall >= 80), runs the REAL
    rollover_service.run_rollover, and proves it never calls
    hof_service.check_and_induct at all -- patched to raise if called, which
    is exactly what the pre-fix code did (check_and_induct ran inside
    run_rollover, evaluating every candidate against progression-stale
    years_pro). It then runs the REAL progression_service.run_progression for
    that season (bumping years_pro 14 -> 15) and calls the REAL relocated
    induction check exactly as offseason_cog's progression handler now does,
    proving the player is inducted only once progression has actually
    updated their state -- a genuine cross-service sequencing proof that
    tests/test_hof.py's isolated check_and_induct tests cannot provide.
    """
    league_row = await db_pool.fetchrow(
        """
        INSERT INTO leagues (
            discord_guild_id, name, start_season_year, current_season,
            current_phase, commissioner_user_id
        ) VALUES (777011, 'RO3 Smoke League', 2025, 2025, 'OFFSEASON_AWARDS_CLOSED', 99999)
        RETURNING id
        """
    )
    league_id: int = league_row["id"]

    team_id = await db_pool.fetchval(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'TST', 'Testers', 'Testville', 'East', 'Atlantic')
        RETURNING id
        """,
        league_id,
    )
    await db_pool.execute(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'OPP', 'Opponents', 'Othertown', 'West', 'Pacific')
        """,
        league_id,
    )

    # years_pro=14 with no birth_date -- _compute_age falls back to 20+years_pro
    # (34), which is decline-stage (>peak_age_end=31) but well below the
    # retirement-consideration floor (_RETIREMENT_MIN_AGE=36), so this player
    # cannot randomly retire mid-test. overall/potential=90 leaves enough
    # margin above the veteran-longevity path's overall>=80 floor to survive a
    # worst-case single-season decline delta.
    player_id = await db_pool.fetchval(
        """
        INSERT INTO players (
            league_id, first_name, last_name, position, team_id,
            roster_status, years_pro,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq,
            potential, peak_age_start, peak_age_end,
            loyalty, money_drive, win_drive
        ) VALUES (
            $1, 'Almost', 'Legend', 'SF', $2,
            'active', 14,
            90, 75, 78, 70, 72,
            80, 75, 78, 74, 80,
            90, 26, 31,
            55, 45, 65
        )
        RETURNING id
        """,
        league_id,
        team_id,
    )

    # RO3's dispositive assertion: run_rollover must not touch hof_service at
    # all. Pre-fix, hof_service.check_and_induct ran inside run_rollover --
    # patching it to raise reproduces that pre-fix call if it's still there.
    with patch(
        "services.hof_service.check_and_induct",
        side_effect=AssertionError("check_and_induct must not run inside run_rollover (RO3)"),
    ):
        await rollover_service.run_rollover(league_id)

    hof_row = await db_pool.fetchrow(
        "SELECT * FROM hall_of_fame WHERE league_id = $1 AND player_id = $2",
        league_id,
        player_id,
    )
    assert hof_row is None, "Player must not be inducted before progression runs"

    league = await league_repo.get_by_guild(db_pool, 777011)
    assert league.pending_progression_season == 2025

    # Real progression run -- bumps years_pro 14 -> 15, crossing the
    # veteran-longevity threshold (years_pro >= 15, overall >= 80).
    await progression_service.run_progression(league_id, league.pending_progression_season)

    years_pro_after = await db_pool.fetchval(
        "SELECT years_pro FROM players WHERE id = $1", player_id
    )
    assert years_pro_after == 15

    # The exact call offseason_cog's progression handler now makes, AFTER
    # progression completes and before advancing to FA_OPEN.
    inducted = await hof_service.check_and_induct(db_pool, league_id, league.current_season)

    assert any(r["player_id"] == player_id for r in inducted), (
        "Player should be inducted now that progression has updated years_pro"
    )
    hof_row = await db_pool.fetchrow(
        "SELECT * FROM hall_of_fame WHERE league_id = $1 AND player_id = $2",
        league_id,
        player_id,
    )
    assert hof_row is not None
