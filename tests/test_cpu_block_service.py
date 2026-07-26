"""
Integration + unit tests for services.cpu_block_service.

TB1 test is a live smoke test against a real seeded test-DB league, driving the
real refresh_team_block entry point (per this sweep's discipline -- no mocked
DB layer). TB2 test is a standard pure-ish unit test proving the asking_price
scaling fix.

Tests run against dba_test with a real asyncpg pool (see tests/conftest.py).
cpu_block_service takes `pool` as an explicit parameter (no module-level
get_pool()), so no patch_get_pool target is needed for it.
"""
from __future__ import annotations

from services import cpu_block_service, trade_grading

_SALARY_CAP = 136_000_000


# ---------------------------------------------------------------------------
# TB1 -- refresh_team_block must read live posture, not the stale cpu_mode column
# ---------------------------------------------------------------------------


async def test_refresh_team_block_uses_live_posture_not_stale_cpu_mode(db_pool) -> None:
    """
    TB1 regression: team row is created with cpu_mode='developing' (the static,
    creation-time column), but its actual season record (5-55) reads as a clear
    tanking/rebuilding team under team_intel's live posture.

    Roster is a single veteran (age 31, OVR 75, non-expiring 2yr contract):
      - _candidates_rebuilding lists him (age >= 30 and OVR >= 72).
      - _candidates_developing does NOT list him (age 31 < 32 veteran floor,
        and he's the only player at his position so no redundant-depth hit).

    Pre-fix (reads team.cpu_mode='developing'): 0 players listed.
    Post-fix (reads live posture -> 'rebuilding'): 1 player listed, with the
    rebuilding-branch note.
    """
    league_id: int = await db_pool.fetchval(
        """
        INSERT INTO leagues
            (discord_guild_id, name, start_season_year, current_season,
             commissioner_user_id, salary_cap, current_phase)
        VALUES (888300, 'TB1 Test League', 2025, 2025, 99999, $1, 'REGULAR_SEASON_ACTIVE')
        RETURNING id
        """,
        _SALARY_CAP,
    )

    team_id: int = await db_pool.fetchval(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division, cpu_mode)
        VALUES ($1, 'TNK', 'Tankers', 'Loseville', 'East', 'Atlantic', 'developing')
        RETURNING id
        """,
        league_id,
    )

    # Bad record -> team_intel.build_team_intel's posture.mode should read
    # 'rebuilding' (projected_wins = round(5/60*82) = 7, well under the <=25
    # hard-rebuild cutoff in trade_context_builder.compute_team_mode).
    await db_pool.execute(
        """
        INSERT INTO standings_cache (league_id, season, team_id, wins, losses, conference, division)
        VALUES ($1, 2025, $2, 5, 55, 'East', 'Atlantic')
        """,
        league_id,
        team_id,
    )

    # Single veteran: age 31 (years_pro=11, no birth_date -> _compute_age falls
    # back to 20 + years_pro), OVR 75.
    player_id: int = await db_pool.fetchval(
        """
        INSERT INTO players
            (league_id, first_name, last_name, position, years_pro, is_rookie,
             roster_status, team_id, overall, speed, shooting_2pt, shooting_3pt,
             shooting_mid, finishing, playmaking, defense, rebounding, iq, potential,
             peak_age_start, peak_age_end, loyalty, money_drive, win_drive,
             market_pref, star_leverage)
        VALUES ($1, 'Vet', 'Anchor', 'PG', 11, FALSE, 'active', $2,
                75, 70, 70, 70, 70, 70, 70, 70, 70, 70, 70, 24, 29,
                50, 50, 50, 'neutral', 50)
        RETURNING id
        """,
        league_id,
        team_id,
    )

    await db_pool.execute(
        """
        INSERT INTO contracts
            (league_id, player_id, team_id, salary, years_remaining, total_years,
             contract_type, signed_in_season, is_active)
        VALUES ($1, $2, $3, 5000000, 2, 3, 'standard', 2023, TRUE)
        """,
        league_id,
        player_id,
        team_id,
    )

    listed_count = await cpu_block_service.refresh_team_block(db_pool, league_id, team_id)

    block_rows = await db_pool.fetch(
        "SELECT player_id, note FROM trade_block WHERE league_id = $1 AND team_id = $2",
        league_id,
        team_id,
    )
    listed_player_ids = {r["player_id"] for r in block_rows}

    assert listed_count == 1, (
        f"Expected 1 player listed under live 'rebuilding' posture, got {listed_count} "
        "(this fails against pre-fix code, which reads the stale cpu_mode='developing' "
        "column and lists 0 players for this roster)"
    )
    assert player_id in listed_player_ids, "The veteran should be listed as a rebuilding-branch candidate"
    listed_note = next(r["note"] for r in block_rows if r["player_id"] == player_id)
    assert listed_note == "Rebuilding — veteran asset available"


# ---------------------------------------------------------------------------
# TB2 -- asking_price must match a real trade proposal's valuation, not a
# fabricated dollar figure
# ---------------------------------------------------------------------------


async def test_refresh_team_block_asking_price_matches_real_trade_valuation(db_pool) -> None:
    """
    TB2 [real bug, fixed]: asking_price used to be
    `player_trade_value(...) * 1_000_000`, the only dollar-scaled use of
    player_trade_value's raw output anywhere in the codebase -- every other
    call site (trade_grading, cpu_trade_evaluation, cpu_trade_acceptance,
    trade_gates, trade_magnitude, trade_proposal_scoring, trade_return_builder,
    cpu_trade_proposal_runner) treats it as an unscaled comparison score.

    Proves the fixed asking_price for a listed player is numerically identical
    to what a real trade proposal (trade_grading.evaluate_trade's
    market_score_a, the same "abstract market value" a live CPU trade
    evaluation computes) assigns that same player -- not an inflated,
    unrelated dollar figure.

    Uses age 28 (>= 26) so asset_upside_modifier is exactly 1.0 -- this keeps
    evaluate_trade's market_score_a numerically identical to the raw
    player_trade_value() call cpu_block_service makes, avoiding a second,
    separate layer (the trade-proposal-only upside premium) that
    cpu_block_service's per-player valuation was never asked to reproduce.
    """
    league_id: int = await db_pool.fetchval(
        """
        INSERT INTO leagues
            (discord_guild_id, name, start_season_year, current_season,
             commissioner_user_id, salary_cap, current_phase)
        VALUES (888301, 'TB2 Test League', 2025, 2025, 99999, $1, 'REGULAR_SEASON_ACTIVE')
        RETURNING id
        """,
        _SALARY_CAP,
    )

    team_id: int = await db_pool.fetchval(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division, cpu_mode)
        VALUES ($1, 'VAL', 'Valuers', 'Priceton', 'East', 'Atlantic', 'rebuilding')
        RETURNING id
        """,
        league_id,
    )

    # Bad record so live posture resolves to 'rebuilding' -- matches the static
    # cpu_mode column too, so this test is purely about asking_price scaling,
    # not about TB1's branch-selection fix.
    await db_pool.execute(
        """
        INSERT INTO standings_cache (league_id, season, team_id, wins, losses, conference, division)
        VALUES ($1, 2025, $2, 5, 55, 'East', 'Atlantic')
        """,
        league_id,
        team_id,
    )

    # Expiring-contract candidate: OVR 70, age 28 (years_pro=8, no birth_date),
    # years_remaining=1 -- hits _candidates_rebuilding's "expiring contract" rule.
    salary = 8_000_000
    player_id: int = await db_pool.fetchval(
        """
        INSERT INTO players
            (league_id, first_name, last_name, position, years_pro, is_rookie,
             roster_status, team_id, overall, speed, shooting_2pt, shooting_3pt,
             shooting_mid, finishing, playmaking, defense, rebounding, iq, potential,
             peak_age_start, peak_age_end, loyalty, money_drive, win_drive,
             market_pref, star_leverage)
        VALUES ($1, 'Expiring', 'Player', 'SF', 8, FALSE, 'active', $2,
                70, 70, 70, 70, 70, 70, 70, 70, 70, 70, 70, 24, 29,
                50, 50, 50, 'neutral', 50)
        RETURNING id
        """,
        league_id,
        team_id,
    )
    await db_pool.execute(
        """
        INSERT INTO contracts
            (league_id, player_id, team_id, salary, years_remaining, total_years,
             contract_type, signed_in_season, is_active)
        VALUES ($1, $2, $3, $4, 1, 1, 'standard', 2024, TRUE)
        """,
        league_id,
        player_id,
        team_id,
        salary,
    )

    listed_count = await cpu_block_service.refresh_team_block(db_pool, league_id, team_id)
    assert listed_count == 1

    row = await db_pool.fetchrow(
        "SELECT asking_price FROM trade_block WHERE league_id = $1 AND team_id = $2 AND player_id = $3",
        league_id,
        team_id,
        player_id,
    )
    asking_price = row["asking_price"]

    # What a real trade proposal would value this exact player at: the same
    # "abstract market value" trade_grading.evaluate_trade computes for a
    # trade offering this player, with age 28 keeping asset_upside_modifier at 1.0.
    evaluation = trade_grading.evaluate_trade(
        side_a_players=[{
            "player": {"overall": 70, "age": 28},
            "contract": {"salary": salary, "years_remaining": 1},
        }],
        side_a_picks=[],
        side_b_players=[],
        side_b_picks=[],
        salary_cap=_SALARY_CAP,
        current_season=2025,
    )
    real_trade_valuation = int(evaluation["market_score_a"])

    assert asking_price == real_trade_valuation, (
        f"asking_price ({asking_price}) should match what a real trade proposal "
        f"values this player at ({real_trade_valuation}) -- pre-fix code inflated "
        f"this by 1,000,000x (would have stored {real_trade_valuation * 1_000_000})"
    )
    # Sanity: the old bug's output would have been in the tens of millions;
    # confirm we're nowhere near that scale.
    assert asking_price < 1_000, (
        f"asking_price ({asking_price}) looks like it's still dollar-scaled, "
        "not a raw trade-value score"
    )
