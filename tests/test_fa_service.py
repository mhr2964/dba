"""
Integration tests for services.fa_service.

Tests run against dba_test with a real asyncpg pool.
patch_get_pool (autouse) routes all service get_pool() calls to db_pool.
advance_to_responses requires a discord.Guild mock; mock_guild from conftest
returns None for all guild.get_channel() calls, so Discord sends are skipped.
"""
from __future__ import annotations

import datetime

import pytest

from core.errors import DBAError
from data.repositories import fa_repo
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


# ---------------------------------------------------------------------------
# FA6 — respond_to_counter must re-validate cap space before accepting
# ---------------------------------------------------------------------------


async def test_respond_to_counter_rejects_when_over_cap(db_pool):
    """
    FA6 regression: respond_to_counter used to sign a countered offer without
    re-checking cap space at all, unlike submit_offer. A counter that would
    push the team over the cap must raise the same DBAError shape and leave
    both the counter and the player untouched.
    """
    league_id, team_id, player_id = await _setup_fa_open(db_pool)

    # Fill the team's cap almost entirely with a blocker contract so the
    # counter (well under a normal offer) still overflows the remaining room.
    blocker_id = await db_pool.fetchval(
        """
        INSERT INTO players (
            league_id, team_id, first_name, last_name, position,
            years_pro, roster_status,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq,
            potential, peak_age_start, peak_age_end,
            loyalty, money_drive, win_drive
        ) VALUES (
            $1, $2, 'Cap', 'Blocker', 'C',
            5, 'active',
            75, 70, 65, 60, 65,
            70, 65, 70, 70, 70,
            80, 27, 32,
            50, 50, 50
        )
        RETURNING id
        """,
        league_id,
        team_id,
    )
    blocker_salary = 139_000_000  # league salary_cap in _setup is 140,000,000
    await db_pool.execute(
        """
        INSERT INTO contracts
            (league_id, player_id, team_id, salary, years_remaining, total_years,
             contract_type, signed_in_season, is_active)
        VALUES ($1, $2, $3, $4, 2, 2, 'standard', 2024, TRUE)
        """,
        league_id,
        blocker_id,
        team_id,
        blocker_salary,
    )

    offer_id = await fa_service.submit_offer(
        league_id, 2025, team_id, player_id, salary=500_000, years=1
    )
    # Manually create a counter well above the ~$1M of remaining cap room.
    counter_id = await fa_repo.create_counter(db_pool, offer_id, salary=10_000_000, years=2)

    with pytest.raises(DBAError, match="salary cap"):
        await fa_service.respond_to_counter(league_id, counter_id, team_id, accept=True)

    counter_row = await db_pool.fetchrow(
        "SELECT team_response FROM fa_counters WHERE id = $1", counter_id
    )
    assert counter_row["team_response"] == "pending"

    status = await db_pool.fetchval(
        "SELECT roster_status FROM players WHERE id = $1", player_id
    )
    assert status == "free_agent"


# ---------------------------------------------------------------------------
# FA7 — CPU-team counters must get auto-resolved by advance_day
# ---------------------------------------------------------------------------


async def test_cpu_counters_get_resolved_by_advance_day(db_pool):
    """
    FA7 regression: a countered offer to a CPU team (no manager_user_id, so
    nobody can invoke /fa counter-accept|counter-decline) used to sit
    status='countered' forever — advance_day only ever expired 'waiting'
    offers. advance_day must now resolve it one way or the other.
    """
    league_id, team_id, player_id = await _setup_fa_open(db_pool)
    # team_id has no manager_user_id set (NULL by default) — a CPU team.

    offer_id = await fa_service.submit_offer(
        league_id, 2025, team_id, player_id, salary=10_000_000, years=2
    )
    await fa_repo.update_offer_status(db_pool, offer_id, "countered")
    # 10.5M is under 115% of the 10M original offer — should auto-accept.
    counter_id = await fa_repo.create_counter(db_pool, offer_id, salary=10_500_000, years=2)

    await fa_service.advance_day(league_id, 2025)

    counter_row = await db_pool.fetchrow(
        "SELECT team_response FROM fa_counters WHERE id = $1", counter_id
    )
    assert counter_row["team_response"] == "accepted"

    signed_player = await db_pool.fetchrow(
        "SELECT roster_status, team_id FROM players WHERE id = $1", player_id
    )
    assert signed_player["roster_status"] == "active"
    assert signed_player["team_id"] == team_id


async def test_cpu_counters_declined_when_too_far_above_original(db_pool):
    """FA7: a CPU counter demanding more than 115% of the original offer gets
    declined rather than auto-accepted."""
    league_id, team_id, player_id = await _setup_fa_open(db_pool)

    offer_id = await fa_service.submit_offer(
        league_id, 2025, team_id, player_id, salary=10_000_000, years=2
    )
    await fa_repo.update_offer_status(db_pool, offer_id, "countered")
    # 13M is well above 115% of the 10M original — should be declined.
    counter_id = await fa_repo.create_counter(db_pool, offer_id, salary=13_000_000, years=2)

    await fa_service.advance_day(league_id, 2025)

    counter_row = await db_pool.fetchrow(
        "SELECT team_response FROM fa_counters WHERE id = $1", counter_id
    )
    assert counter_row["team_response"] == "declined"

    status = await db_pool.fetchval(
        "SELECT roster_status FROM players WHERE id = $1", player_id
    )
    assert status == "free_agent"


# ---------------------------------------------------------------------------
# FA1 — CPU offer terms follow live team_intel posture, not the static
# team.cpu_mode column
# ---------------------------------------------------------------------------


async def test_submit_cpu_offers_respects_posture_mode(db_pool):
    """
    FA1 regression: both teams share the same static cpu_mode='contending'
    column value, but their live records put them in very different postures
    (contending vs. rebuilding). A sub-floor free agent (OVR 55, below the
    OVR-70 contender floor) should draw a bid from the rebuilding-posture team
    and NOT from the contending-posture team — proving mode is read from live
    posture, not the static column.
    """
    league_row = await db_pool.fetchrow(
        """
        INSERT INTO leagues
            (discord_guild_id, name, start_season_year, current_season,
             commissioner_user_id, salary_cap, current_phase)
        VALUES (888100, 'Posture Test League', 2025, 2025, 99999, 140000000, 'FA_OPEN')
        RETURNING id
        """
    )
    league_id = league_row["id"]

    contender_id = await db_pool.fetchval(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division, cpu_mode)
        VALUES ($1, 'CTD', 'Contenders', 'Winville', 'East', 'Atlantic', 'contending')
        RETURNING id
        """,
        league_id,
    )
    rebuild_id = await db_pool.fetchval(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division, cpu_mode)
        VALUES ($1, 'RBD', 'Rebuilders', 'Loseville', 'East', 'Atlantic', 'contending')
        RETURNING id
        """,
        league_id,
    )

    await db_pool.execute(
        """
        INSERT INTO standings_cache (league_id, season, team_id, wins, losses, conference, division)
        VALUES ($1, 2025, $2, 45, 15, 'East', 'Atlantic')
        """,
        league_id,
        contender_id,
    )
    await db_pool.execute(
        """
        INSERT INTO standings_cache (league_id, season, team_id, wins, losses, conference, division)
        VALUES ($1, 2025, $2, 5, 55, 'East', 'Atlantic')
        """,
        league_id,
        rebuild_id,
    )

    # A single scrub free agent, OVR well below the contender floor (70).
    fa_id = await db_pool.fetchval(
        """
        INSERT INTO players
            (league_id, first_name, last_name, position, years_pro, is_rookie,
             roster_status, overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
             finishing, playmaking, defense, rebounding, iq, potential,
             peak_age_start, peak_age_end, loyalty, money_drive, win_drive,
             market_pref, star_leverage)
        VALUES ($1, 'Scrub', 'Player', 'SG', 3, FALSE,
                'free_agent', 55, 60, 60, 60, 60, 60, 60, 60, 60, 60, 65, 24, 29,
                50, 50, 50, 'neutral', 50)
        RETURNING id
        """,
        league_id,
    )

    await db_pool.execute(
        """
        INSERT INTO fa_state (league_id, season, current_day, phase, total_days)
        VALUES ($1, 2025, 1, 'offers_open', 8)
        """,
        league_id,
    )

    await fa_service.submit_cpu_offers(league_id, 2025)

    offer_rows = await db_pool.fetch(
        "SELECT team_id FROM fa_offers WHERE league_id = $1 AND player_id = $2",
        league_id,
        fa_id,
    )
    offering_team_ids = {r["team_id"] for r in offer_rows}

    assert rebuild_id in offering_team_ids, "Rebuilding-posture team should bid under the OVR-70 floor"
    assert contender_id not in offering_team_ids, "Contending-posture team should NOT bid on a sub-floor scrub"


# ---------------------------------------------------------------------------
# FA2 — positional need re-sorts each CPU team's free-agent target list
# ---------------------------------------------------------------------------


async def test_submit_cpu_offers_prioritizes_positional_need(db_pool):
    """
    FA2 regression: a team already 3-deep at center (surplus) and with zero
    point guards (a hole) should prioritize a lower-OVR point guard over a
    higher-OVR center free agent — the raw global OVR-sorted list would have
    tried the center first and never reached the point guard once cap space
    ran out.
    """
    league_row = await db_pool.fetchrow(
        """
        INSERT INTO leagues
            (discord_guild_id, name, start_season_year, current_season,
             commissioner_user_id, salary_cap, current_phase)
        VALUES (888200, 'Need Test League', 2025, 2025, 99999, 140000000, 'FA_OPEN')
        RETURNING id
        """
    )
    league_id = league_row["id"]

    team_id = await db_pool.fetchval(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division, cpu_mode)
        VALUES ($1, 'NED', 'Needers', 'Thinville', 'West', 'Pacific', 'rebuilding')
        RETURNING id
        """,
        league_id,
    )

    # 3 active centers already rostered — surplus at C. Zero point guards — a hole.
    filler_salary = 40_000_000
    for i in range(3):
        pid = await db_pool.fetchval(
            """
            INSERT INTO players
                (league_id, first_name, last_name, position, years_pro, is_rookie,
                 roster_status, team_id, overall, speed, shooting_2pt, shooting_3pt,
                 shooting_mid, finishing, playmaking, defense, rebounding, iq, potential,
                 peak_age_start, peak_age_end, loyalty, money_drive, win_drive,
                 market_pref, star_leverage)
            VALUES ($1, $2, 'Filler', 'C', 6, FALSE, 'active', $3,
                    72, 70, 65, 60, 65, 70, 65, 78, 78, 70, 75, 26, 31,
                    50, 50, 50, 'neutral', 50)
            RETURNING id
            """,
            league_id,
            f"Center{i}",
            team_id,
        )
        await db_pool.execute(
            """
            INSERT INTO contracts
                (league_id, player_id, team_id, salary, years_remaining, total_years,
                 contract_type, signed_in_season, is_active)
            VALUES ($1, $2, $3, $4, 2, 2, 'fa', 2024, TRUE)
            """,
            league_id,
            pid,
            team_id,
            filler_salary,
        )
    # cap_used = 120,000,000; remaining = 20,000,000.

    # Free agent A: a PG (position of need) at a modest OVR.
    pg_id = await db_pool.fetchval(
        """
        INSERT INTO players
            (league_id, first_name, last_name, position, years_pro, is_rookie,
             roster_status, overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
             finishing, playmaking, defense, rebounding, iq, potential,
             peak_age_start, peak_age_end, loyalty, money_drive, win_drive,
             market_pref, star_leverage)
        VALUES ($1, 'Need', 'Guard', 'PG', 4, FALSE, 'free_agent',
                70, 78, 70, 68, 65, 65, 78, 68, 60, 72, 78, 25, 30,
                50, 50, 50, 'neutral', 50)
        RETURNING id
        """,
        league_id,
    )
    # Free agent B: a C (already-surplus position) at a HIGHER raw OVR.
    c_id = await db_pool.fetchval(
        """
        INSERT INTO players
            (league_id, first_name, last_name, position, years_pro, is_rookie,
             roster_status, overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
             finishing, playmaking, defense, rebounding, iq, potential,
             peak_age_start, peak_age_end, loyalty, money_drive, win_drive,
             market_pref, star_leverage)
        VALUES ($1, 'Surplus', 'Big', 'C', 5, FALSE, 'free_agent',
                82, 65, 68, 55, 60, 75, 55, 72, 82, 70, 84, 26, 31,
                50, 50, 50, 'neutral', 50)
        RETURNING id
        """,
        league_id,
    )

    await db_pool.execute(
        """
        INSERT INTO fa_state (league_id, season, current_day, phase, total_days)
        VALUES ($1, 2025, 1, 'offers_open', 8)
        """,
        league_id,
    )

    await fa_service.submit_cpu_offers(league_id, 2025)

    offer_rows = await db_pool.fetch(
        "SELECT player_id FROM fa_offers WHERE league_id = $1 AND team_id = $2",
        league_id,
        team_id,
    )
    offered_player_ids = {r["player_id"] for r in offer_rows}

    # Need-weighted scoring (PG: 70*1.4=98) outranks the raw-higher-OVR
    # surplus center (C: 82*0.6=49.2), so the PG is tried — and signed —
    # first; by the time the higher-OVR center is tried, cap room is gone.
    assert pg_id in offered_player_ids
    assert c_id not in offered_player_ids


async def test_get_team_wins_counts_simmed_games_not_scheduled(db_pool):
    """
    Regression: _get_team_wins filtered on status='final', a value never
    actually written by the sim (real completed games go scheduled->simmed,
    see game_repo.py) -- every team's win count silently came back 0. A
    scheduled-but-unplayed game must not count even though it carries scores
    left over from a prior day's matchup generation.
    """
    league_id, team_id, _player_id = await _setup_fa_open(db_pool)
    opponent_id = await db_pool.fetchval(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'OPP', 'Opponents', 'Rivalburg', 'East', 'Atlantic')
        RETURNING id
        """,
        league_id,
    )

    await db_pool.execute(
        """
        INSERT INTO games
            (league_id, season, game_index, home_team_id, away_team_id,
             scheduled_date, status, home_score, away_score, is_user_matchup, rng_seed)
        VALUES
            ($1, 2025, 1, $2, $3, '2025-04-01', 'simmed', 100, 90, FALSE, 1),
            ($1, 2025, 2, $2, $3, '2025-04-02', 'scheduled', 100, 90, FALSE, 1)
        """,
        league_id,
        team_id,
        opponent_id,
    )

    wins = await fa_service._get_team_wins(db_pool, league_id, team_id, 2025)
    assert wins == 1


# ---------------------------------------------------------------------------
# RO4 — close_fa's retirement branch must be age-gated (consistent with
# progression_service's P5 _RETIREMENT_MIN_AGE), not tenure-gated.
#
# close_fa previously force-retired any unsigned free agent purely on
# years_pro > 8 with NO age/birth_date check. That let a young player who
# simply sat unsigned for 8+ seasons get force-retired at ~26-27, while a
# genuinely washed veteran who entered the league late (short tenure, high
# age) could never be retired via this path at all. Both tests below are
# structured to fail against the old years_pro > 8 condition.
# ---------------------------------------------------------------------------


async def _setup_unsigned_player(
    pool, birth_date: datetime.date, years_pro: int, overall: int = 55
) -> tuple[int, int]:
    """
    Insert a league (with an fa_state row so close_fa's update_fa_state UPDATE
    has a row to touch) and a single low-OVR, already-unsigned free agent.
    Returns (league_id, player_id).
    """
    league_row = await pool.fetchrow(
        """
        INSERT INTO leagues
            (discord_guild_id, name, start_season_year, current_season,
             commissioner_user_id, salary_cap, current_phase)
        VALUES (888300, 'RO4 Test League', 2025, 2025, 99999, 140000000, 'FA_OPEN')
        RETURNING id
        """
    )
    league_id: int = league_row["id"]

    player_row = await pool.fetchrow(
        """
        INSERT INTO players (
            league_id, first_name, last_name, position,
            birth_date, years_pro,
            roster_status,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq,
            potential, peak_age_start, peak_age_end,
            loyalty, money_drive, win_drive
        ) VALUES (
            $1, 'Unsigned', 'Player', 'SF',
            $2, $3,
            'free_agent',
            $4, 60, 55, 50, 55,
            55, 55, 55, 55, 60,
            60, 24, 29,
            50, 50, 50
        )
        RETURNING id
        """,
        league_id,
        birth_date,
        years_pro,
        overall,
    )
    player_id: int = player_row["id"]

    await pool.execute(
        """
        INSERT INTO fa_state (league_id, season, current_day, phase, total_days)
        VALUES ($1, 2025, 8, 'offers_open', 8)
        """,
        league_id,
    )

    return league_id, player_id


async def test_close_fa_does_not_retire_young_long_tenure_player(db_pool):
    """
    A young player (age ~26) with years_pro > 8 (10) and low OVR must NOT be
    retired by close_fa. Pre-fix, the bare years_pro > 8 condition would have
    force-retired this player purely on tenure despite being nowhere near
    retirement age.
    """
    league_id, player_id = await _setup_unsigned_player(
        db_pool, birth_date=datetime.date(2000, 1, 1), years_pro=10, overall=55
    )

    await fa_service.close_fa(league_id, 2025)

    status = await db_pool.fetchval(
        "SELECT roster_status FROM players WHERE id = $1", player_id
    )
    assert status == "free_agent", (
        "Young player with long tenure but low retirement age should remain "
        f"a free agent, got roster_status={status!r}"
    )


async def test_close_fa_retires_old_short_tenure_player(db_pool):
    """
    An old player (age ~40, past progression's _RETIREMENT_MIN_AGE=36) with
    years_pro <= 8 (5) and low OVR IS retired by close_fa. Pre-fix, the bare
    years_pro > 8 condition required 8+ years of tenure and would NEVER have
    retired this player via close_fa despite being well past retirement age.
    """
    league_id, player_id = await _setup_unsigned_player(
        db_pool, birth_date=datetime.date(1986, 1, 1), years_pro=5, overall=55
    )

    await fa_service.close_fa(league_id, 2025)

    status = await db_pool.fetchval(
        "SELECT roster_status FROM players WHERE id = $1", player_id
    )
    assert status == "retired", (
        f"Old, low-tenure, low-OVR player should be retired, got roster_status={status!r}"
    )
