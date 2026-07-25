"""
RA2/RA4 -- real DB-backed coverage for the minimum-roster guard added to
_derive_tendency_respecter (services/role_scoring.py), driven through the
real role_service.py entry points rather than calling the pure function
directly (that pure-function coverage lives in tests/test_role_scoring.py).

Before this sweep, the smallest roster exercised anywhere in the test suite
was 12 players -- rosters of 1-5 players (plausible in practice: RA1's
free-agent/waiver bug can leave a team artificially thin in `lineups` even
when its real depth chart isn't that shallow) were completely unexercised,
and the module's own documented guarantee ("exactly one primary scorer,
exactly one defensive anchor") silently broke at that size. See
services/role_scoring.py's _derive_tendency_respecter docstring and the
_MIN_RESERVED_FOR_PRIMARY_AND_ANCHOR guard for the fix.
"""
from __future__ import annotations

import datetime

import pytest

from services import role_service

pytestmark = pytest.mark.asyncio

_PRIMARY_ROLES = {
    "iso_scorer", "primary_initiator", "post_anchor",
    "movement_shooter", "slashing_lead", "pick_and_pop",
    "rim_runner", "screen_roller",
}
_ANCHOR_ROLES = {"rim_protector", "two_way_big", "switching_big", "post_anchor"}


# ---------------------------------------------------------------------------
# Shared seeding helpers (local to this file -- deliberately not shared with
# test_franchise_plan_service.py's fixed 8-player helper, since these tests
# need variable, sub-5-player roster sizes).
# ---------------------------------------------------------------------------

async def _insert_league(db_pool, guild_id: int, name: str, season: int = 2025) -> int:
    row = await db_pool.fetchrow(
        """
        INSERT INTO leagues (
            discord_guild_id, name, start_season_year, current_season,
            current_phase, commissioner_user_id
        ) VALUES ($1, $2, $3, $3, 'REGULAR_SEASON_ACTIVE', 99999)
        RETURNING id
        """,
        guild_id, name, season,
    )
    return row["id"]


async def _insert_tiny_roster_team(
    db_pool,
    league_id: int,
    code: str,
    roster_spec: list[tuple[str, int, int]],
    # roster_spec: list of (position, overall, age_years)
) -> tuple[int, list[int]]:
    """Insert a team with an EXACTLY len(roster_spec)-player roster (no padding
    to a full 15-man roster) -- the whole point is exercising sub-5-player
    rosters end-to-end through the real derive_roles() DB read, which is
    scoped to `lineups` rows for the team (WHERE l.slot BETWEEN 1 AND 15)."""
    team_id = await db_pool.fetchval(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, $2, $3, $4, 'East', 'Atlantic')
        RETURNING id
        """,
        league_id, code, f"{code} City", code,
    )
    today = datetime.date(2025, 10, 1)
    player_ids: list[int] = []
    for i, (pos, ovr, age_years) in enumerate(roster_spec):
        birth_date = today.replace(year=today.year - age_years)
        pid = await db_pool.fetchval(
            """
            INSERT INTO players (
                league_id, team_id, first_name, last_name, position,
                birth_date, years_pro, roster_status,
                overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
                finishing, playmaking, defense, rebounding, iq,
                potential, peak_age_start, peak_age_end,
                loyalty, money_drive, win_drive
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, 6, 'active',
                $7, 70, 65, 60, 60,
                65, 60, 65, 65, 70,
                $7, 26, 31,
                50, 50, 50
            )
            RETURNING id
            """,
            league_id, team_id, code, f"Player{i}", pos, birth_date, ovr,
        )
        player_ids.append(pid)
    for slot, pid in enumerate(player_ids, start=1):
        await db_pool.execute(
            """
            INSERT INTO lineups (league_id, team_id, is_starter, slot, player_id, set_by)
            VALUES ($1, $2, $3, $4, $5, NULL)
            """,
            league_id, team_id, slot <= 5, slot, pid,
        )
    return team_id, player_ids


async def _get_roles(db_pool, league_id: int, team_id: int, season: int) -> dict[int, str]:
    rows = await db_pool.fetch(
        """
        SELECT player_id, role FROM player_roles
        WHERE league_id = $1 AND team_id = $2 AND season = $3
        """,
        league_id, team_id, season,
    )
    return {r["player_id"]: r["role"] for r in rows}


# ---------------------------------------------------------------------------
# RA2 -- MANDATORY live smoke test.
# ---------------------------------------------------------------------------

async def test_ra2_tiny_roster_gets_primary_scorer_and_anchor_via_real_entry_point(db_pool):
    """RA2 -- MANDATORY live smoke test.

    Seeds a REAL 3-player roster (1 guard, 1 shot-blocking center, 1 aging
    wing) into `players`/`lineups` for a real team in a real league, then
    calls the real public entry point `role_service.derive_and_persist_all_for_team`
    (not the pure `_derive_tendency_respecter` directly -- that pure-function
    coverage lives in tests/test_role_scoring.py) and reads back the persisted
    `player_roles` rows.

    Dispositive assertion: at least one persisted role is in the primary-scorer
    set and at least one is in the defensive-anchor set. At n=3, Step 1's
    original (pre-fix) bottom-3-OVR depth slice consumed the ENTIRE roster
    into end_of_bench/developmental/veteran_mentor roles, leaving NOTHING for
    Steps 2-3 -- both sets come back empty.

    MANDATORY pre-fix proof (performed manually via `git stash`, following the
    exact procedure documented on
    test_franchise_plan_service.py::test_fp1_pivot_fires_mid_call_against_real_sim_range):
    with the RA2 guard in services/role_scoring.py (`_MIN_RESERVED_FOR_PRIMARY_AND_ANCHOR`
    / `depth_count` capping of Step 1's bottom_3 slice) stashed out -- restoring
    the original unconditional `sorted_by_ovr[max(0, n - 3):]` -- this exact
    test FAILS: all 3 players are persisted with roles in
    {end_of_bench, developmental, veteran_mentor} and both the primary-scorer
    and defensive-anchor assertions below raise AssertionError (0 of either
    role persisted). Restoring the stash (`git stash pop`) makes it pass again.
    """
    league_id = await _insert_league(db_pool, 700200, "RA2 Smoke League")
    season = 2025

    roster_spec = [
        ("PG", 88, 26),                 # top OVR guard -- should win a primary-scorer role
        ("C", 82, 27),                  # shot-blocking, low-3pt big -- anchor candidate
        ("SF", 70, 34),                 # aging wing -- depth-role candidate
    ]
    team_id, player_ids = await _insert_tiny_roster_team(
        db_pool, league_id, "RA2T", roster_spec,
    )
    # Give the center a real rim-protector-ish tendency profile so the anchor
    # assignment isn't a coin flip -- directly patch tendencies post-insert
    # since the insert helper above doesn't expose per-tendency overrides.
    await db_pool.execute(
        "UPDATE players SET blk_tendency = 70, reb_tendency = 65, tendency_3pt = 5 "
        "WHERE id = $1",
        player_ids[1],
    )

    # derive_and_persist_all_for_team uses `conn.transaction()` internally, which
    # requires an acquired Connection (not a bare Pool) -- same pattern real call
    # sites use (e.g. trade_service.py's `async with pool.acquire() as _conn`).
    async with db_pool.acquire() as conn:
        await role_service.derive_and_persist_all_for_team(conn, league_id, team_id, season)

    roles = await _get_roles(db_pool, league_id, team_id, season)
    assert len(roles) == 3, f"expected all 3 rostered players to get a persisted role, got {roles}"

    primary_count = sum(1 for r in roles.values() if r in _PRIMARY_ROLES)
    anchor_count = sum(1 for r in roles.values() if r in _ANCHOR_ROLES)
    assert primary_count >= 1, f"expected >=1 primary-scorer role, got roles={roles}"
    assert anchor_count >= 1, f"expected >=1 defensive-anchor role, got roles={roles}"


# ---------------------------------------------------------------------------
# RA4 -- supporting tiny-roster coverage at every size 1-5, via the real
# derive_roles() DB read path (no persist needed for these -- persistence is
# already covered by the smoke test above).
# ---------------------------------------------------------------------------

async def test_ra4_one_player_roster_gets_primary_scorer_real_db(db_pool):
    league_id = await _insert_league(db_pool, 700201, "RA4 1P League")
    season = 2025
    roster_spec = [("PG", 80, 26)]
    team_id, _ = await _insert_tiny_roster_team(db_pool, league_id, "R1", roster_spec)

    assignments = await role_service.derive_roles(db_pool, league_id, team_id, season)
    assert len(assignments) == 1
    assert assignments[0]["role"] in _PRIMARY_ROLES


async def test_ra4_two_player_roster_gets_primary_and_anchor_real_db(db_pool):
    league_id = await _insert_league(db_pool, 700202, "RA4 2P League")
    season = 2025
    roster_spec = [("PG", 85, 26), ("C", 80, 27)]
    team_id, player_ids = await _insert_tiny_roster_team(db_pool, league_id, "R2", roster_spec)
    await db_pool.execute(
        "UPDATE players SET blk_tendency = 70, reb_tendency = 65, tendency_3pt = 5 WHERE id = $1",
        player_ids[1],
    )

    assignments = await role_service.derive_roles(db_pool, league_id, team_id, season)
    assert len(assignments) == 2
    roles = [a["role"] for a in assignments]
    assert sum(1 for r in roles if r in _PRIMARY_ROLES) == 1
    assert sum(1 for r in roles if r in _ANCHOR_ROLES) == 1


async def test_ra4_three_player_roster_gets_primary_and_anchor_real_db(db_pool):
    league_id = await _insert_league(db_pool, 700203, "RA4 3P League")
    season = 2025
    roster_spec = [("PG", 88, 26), ("C", 82, 27), ("SF", 70, 34)]
    team_id, player_ids = await _insert_tiny_roster_team(db_pool, league_id, "R3", roster_spec)
    await db_pool.execute(
        "UPDATE players SET blk_tendency = 70, reb_tendency = 65, tendency_3pt = 5 WHERE id = $1",
        player_ids[1],
    )

    assignments = await role_service.derive_roles(db_pool, league_id, team_id, season)
    assert len(assignments) == 3
    roles = [a["role"] for a in assignments]
    assert sum(1 for r in roles if r in _PRIMARY_ROLES) == 1
    assert sum(1 for r in roles if r in _ANCHOR_ROLES) == 1


async def test_ra4_four_player_roster_gets_primary_and_anchor_real_db(db_pool):
    # n=4: the case called out specifically in the plan -- pre-fix, Step 1 took
    # 3 for depth roles and Step 2 took the last remaining player for primary,
    # leaving zero candidates for Step 3's anchor pool.
    league_id = await _insert_league(db_pool, 700204, "RA4 4P League")
    season = 2025
    roster_spec = [("PG", 90, 26), ("C", 85, 27), ("SF", 75, 28), ("SF", 65, 34)]
    team_id, player_ids = await _insert_tiny_roster_team(db_pool, league_id, "R4", roster_spec)
    await db_pool.execute(
        "UPDATE players SET blk_tendency = 70, reb_tendency = 65, tendency_3pt = 5 WHERE id = $1",
        player_ids[1],
    )

    assignments = await role_service.derive_roles(db_pool, league_id, team_id, season)
    assert len(assignments) == 4
    roles = [a["role"] for a in assignments]
    assert sum(1 for r in roles if r in _PRIMARY_ROLES) == 1
    assert sum(1 for r in roles if r in _ANCHOR_ROLES) == 1


async def test_ra4_five_player_roster_gets_primary_and_anchor_real_db(db_pool):
    league_id = await _insert_league(db_pool, 700205, "RA4 5P League")
    season = 2025
    roster_spec = [
        ("PG", 90, 26), ("C", 85, 27), ("SF", 80, 28), ("SG", 72, 29), ("SF", 65, 34),
    ]
    team_id, player_ids = await _insert_tiny_roster_team(db_pool, league_id, "R5", roster_spec)
    await db_pool.execute(
        "UPDATE players SET blk_tendency = 70, reb_tendency = 65, tendency_3pt = 5 WHERE id = $1",
        player_ids[1],
    )

    assignments = await role_service.derive_roles(db_pool, league_id, team_id, season)
    assert len(assignments) == 5
    roles = [a["role"] for a in assignments]
    assert sum(1 for r in roles if r in _PRIMARY_ROLES) == 1
    assert sum(1 for r in roles if r in _ANCHOR_ROLES) == 1
