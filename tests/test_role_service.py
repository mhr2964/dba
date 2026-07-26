"""
RA4 -- real DB-backed integration tests for role_service.py's DB-orchestration
layer (derive_and_persist_all_for_team, persist_roles). Before this sweep,
only the pure scoring functions in role_scoring.py had test coverage
(tests/test_role_scoring.py) -- this file follows the same "real seeded DB,
real entry point" discipline as tests/test_franchise_plan_service.py.

Also includes RA1's MANDATORY live smoke test: proves the FA-signing/
waiver-claim "roster ghost" bug (a player signed via fa_service never got a
`lineups` row, role-cache invalidation, or a fresh role derivation -- unlike
the draft path, which already did this) is fixed. Drives the REAL
fa_service.claim_waiver entry point against a seeded test-DB league, then
drives the REAL sim_persistence layer (_load_lineup_for_team +
_stamp_role_data, the exact two functions draft_service.py's docstring names
as what makes an un-lineup'd player invisible) to prove the signed player now
gets a real, nonzero touch_share. Proven to fail pre-fix and pass post-fix
via `git stash` on services/fa_service.py -- see that test's docstring for
the exact procedure.

Also includes RA2/RA4's coverage for the minimum-roster guard added to
_derive_tendency_respecter (services/role_scoring.py), driven through the
real role_service.py entry points rather than calling the pure function
directly (that pure-function coverage lives in tests/test_role_scoring.py).
Before this sweep, the smallest roster exercised anywhere in the test suite
was 12 players -- rosters of 1-5 players (plausible in practice: RA1's
free-agent/waiver bug above can leave a team artificially thin in `lineups`
even when its real depth chart isn't that shallow) were completely
unexercised, and the module's own documented guarantee ("exactly one primary
scorer, exactly one defensive anchor") silently broke at that size. See
services/role_scoring.py's _derive_tendency_respecter docstring and the
_MIN_RESERVED_FOR_PRIMARY_AND_ANCHOR guard for the fix.

RA1 and RA2 sections were authored independently by sibling builder agents in
separate worktrees and merged into one file; each keeps its own local seeding
helpers rather than sharing them, since they need different roster shapes
(fixed 7-8 player rosters for RA1 vs. variable sub-5-player rosters for RA2).
"""
from __future__ import annotations

import datetime

import pytest

from services import fa_service, role_service, sim_persistence
from services.role_scoring import ROLE_REGISTRY

pytestmark = pytest.mark.asyncio

_POSITIONS = ["PG", "SG", "SF", "PF", "C", "PG", "SF", "C"]

_PRIMARY_ROLES = {
    "iso_scorer", "primary_initiator", "post_anchor",
    "movement_shooter", "slashing_lead", "pick_and_pop",
    "rim_runner", "screen_roller",
}
_ANCHOR_ROLES = {"rim_protector", "two_way_big", "switching_big", "post_anchor"}


# ---------------------------------------------------------------------------
# RA1 -- shared seeding helpers
# ---------------------------------------------------------------------------


async def _insert_league(
    db_pool, guild_id: int, name: str, season: int = 2025, salary_cap: int = 140_000_000
) -> int:
    row = await db_pool.fetchrow(
        """
        INSERT INTO leagues (
            discord_guild_id, name, start_season_year, current_season,
            commissioner_user_id, salary_cap
        ) VALUES ($1, $2, $3, $3, 99999, $4)
        RETURNING id
        """,
        guild_id, name, season, salary_cap,
    )
    return row["id"]


async def _insert_team_with_roster(
    db_pool,
    league_id: int,
    code: str,
    overalls: list[int],
    birth_date: datetime.date,
) -> tuple[int, list[int]]:
    """Insert a team with a full roster + matching lineup rows (slots 1..N).

    Mirrors test_franchise_plan_service.py's helper of the same name -- kept
    as its own local copy rather than a cross-file import since these two
    test files may be independently authored/merged by sibling agents.
    """
    team_id = await db_pool.fetchval(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, $2, $3, $4, 'East', 'Atlantic')
        RETURNING id
        """,
        league_id, code, f"{code} City", code,
    )
    player_ids: list[int] = []
    for i, (pos, ovr) in enumerate(zip(_POSITIONS, overalls)):
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


async def _insert_waived_player(
    db_pool, league_id: int, code: str, overall: int, birth_date: datetime.date
) -> int:
    """Insert a player with no team and roster_status='waived' (waiver-claim target)."""
    pid = await db_pool.fetchval(
        """
        INSERT INTO players (
            league_id, first_name, last_name, position,
            birth_date, years_pro, roster_status,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq,
            potential, peak_age_start, peak_age_end,
            loyalty, money_drive, win_drive
        ) VALUES (
            $1, $2, 'Waived', 'SF',
            $3, 6, 'waived',
            $4, 70, 65, 60, 60,
            65, 60, 65, 65, 70,
            $4, 26, 31,
            50, 50, 50
        )
        RETURNING id
        """,
        league_id, code, birth_date, overall,
    )
    return pid


# ---------------------------------------------------------------------------
# derive_and_persist_all_for_team / persist_roles -- basic real-DB coverage
# ---------------------------------------------------------------------------


async def test_derive_and_persist_all_for_team_persists_a_role_for_every_lineup_slot(db_pool):
    """A freshly-seeded 8-man roster (all in `lineups`) should get exactly 8
    player_roles rows after derive_and_persist_all_for_team, each with a
    valid ROLE_REGISTRY role name and a positive touch_share."""
    league_id = await _insert_league(db_pool, 900001, "RS Basic League")
    team_id, player_ids = await _insert_team_with_roster(
        db_pool, league_id, "RSB", [90, 82, 78, 74, 70, 66, 62, 58],
        datetime.date(1997, 1, 1),
    )

    async with db_pool.acquire() as conn:
        await role_service.derive_and_persist_all_for_team(conn, league_id, team_id, 2025)

    rows = await db_pool.fetch(
        "SELECT player_id, role, touch_share FROM player_roles WHERE league_id = $1 AND team_id = $2",
        league_id, team_id,
    )
    assert {r["player_id"] for r in rows} == set(player_ids)
    for r in rows:
        assert r["role"] in ROLE_REGISTRY
        assert r["touch_share"] > 0


async def test_persist_roles_rejects_invalid_role_name(db_pool):
    """persist_roles must raise before hitting the DB CHECK constraint when an
    assignment carries a role name absent from ROLE_REGISTRY."""
    league_id = await _insert_league(db_pool, 900002, "RS Invalid Role League")
    team_id, player_ids = await _insert_team_with_roster(
        db_pool, league_id, "RSI", [80, 75, 70, 65, 60, 55, 50, 45],
        datetime.date(1997, 1, 1),
    )

    bad_assignment = [{
        "player_id": player_ids[0], "role": "not_a_real_role",
        "touch_share": 0.2, "rationale": "test",
    }]

    with pytest.raises(ValueError, match="Invalid role"):
        async with db_pool.acquire() as conn:
            await role_service.persist_roles(conn, league_id, team_id, 2025, bad_assignment)


async def test_persist_roles_upsert_updates_touch_share_on_second_call(db_pool):
    """persist_roles' ON CONFLICT DO UPDATE path: calling it twice with a
    different touch_share for the same (league, team, season, player) updates
    the row in place rather than erroring or duplicating it."""
    league_id = await _insert_league(db_pool, 900003, "RS Upsert League")
    team_id, player_ids = await _insert_team_with_roster(
        db_pool, league_id, "RSU", [80, 75, 70, 65, 60, 55, 50, 45],
        datetime.date(1997, 1, 1),
    )
    pid = player_ids[0]

    async with db_pool.acquire() as conn:
        await role_service.persist_roles(
            conn, league_id, team_id, 2025,
            [{"player_id": pid, "role": "iso_scorer", "touch_share": 0.30, "rationale": "first"}],
        )
        await role_service.persist_roles(
            conn, league_id, team_id, 2025,
            [{"player_id": pid, "role": "glue_guy", "touch_share": 0.10, "rationale": "second"}],
        )

    rows = await db_pool.fetch(
        "SELECT role, touch_share FROM player_roles WHERE league_id = $1 AND team_id = $2 AND player_id = $3",
        league_id, team_id, pid,
    )
    assert len(rows) == 1
    assert rows[0]["role"] == "glue_guy"
    assert float(rows[0]["touch_share"]) == pytest.approx(0.10)


async def test_persist_roles_does_not_overwrite_locked_row(db_pool):
    """A row with locked=TRUE (human override) must survive persist_roles'
    UPSERT -- the WHERE player_roles.locked = FALSE guard on the DO UPDATE
    clause."""
    league_id = await _insert_league(db_pool, 900004, "RS Locked League")
    team_id, player_ids = await _insert_team_with_roster(
        db_pool, league_id, "RSL", [80, 75, 70, 65, 60, 55, 50, 45],
        datetime.date(1997, 1, 1),
    )
    pid = player_ids[0]

    await db_pool.execute(
        """
        INSERT INTO player_roles
            (league_id, team_id, season, player_id, role, touch_share, rationale,
             assigned_by, locked, assigned_at)
        VALUES ($1, $2, 2025, $3, 'primary_initiator', 0.40, 'human override', 'human', TRUE, NOW())
        """,
        league_id, team_id, pid,
    )

    async with db_pool.acquire() as conn:
        await role_service.persist_roles(
            conn, league_id, team_id, 2025,
            [{"player_id": pid, "role": "glue_guy", "touch_share": 0.08, "rationale": "cpu re-derive"}],
        )

    row = await db_pool.fetchrow(
        "SELECT role, touch_share, locked FROM player_roles WHERE league_id = $1 AND team_id = $2 AND player_id = $3",
        league_id, team_id, pid,
    )
    assert row["locked"] is True
    assert row["role"] == "primary_initiator"
    assert float(row["touch_share"]) == pytest.approx(0.40)


async def test_derive_and_persist_all_for_team_prunes_rows_for_players_no_longer_in_lineup(db_pool):
    """The DELETE-then-persist step inside derive_and_persist_all_for_team
    must remove player_roles rows for any player_id no longer present in
    `lineups` for this team (e.g. traded/waived away since the last derive)."""
    league_id = await _insert_league(db_pool, 900005, "RS Prune League")
    team_id, player_ids = await _insert_team_with_roster(
        db_pool, league_id, "RSP", [80, 75, 70, 65, 60, 55, 50, 45],
        datetime.date(1997, 1, 1),
    )

    async with db_pool.acquire() as conn:
        await role_service.derive_and_persist_all_for_team(conn, league_id, team_id, 2025)

    removed_pid = player_ids[-1]
    await db_pool.execute(
        "DELETE FROM lineups WHERE league_id = $1 AND team_id = $2 AND player_id = $3",
        league_id, team_id, removed_pid,
    )

    async with db_pool.acquire() as conn:
        await role_service.derive_and_persist_all_for_team(conn, league_id, team_id, 2025)

    stale_row = await db_pool.fetchrow(
        "SELECT 1 FROM player_roles WHERE league_id = $1 AND team_id = $2 AND player_id = $3",
        league_id, team_id, removed_pid,
    )
    assert stale_row is None, "player_roles row for a player no longer in lineups must be pruned"


# ---------------------------------------------------------------------------
# RA1 -- MANDATORY live smoke test: FA/waiver "roster ghost" fix
# ---------------------------------------------------------------------------


async def test_ra1_waiver_claim_wires_up_lineup_role_and_sim_touch_share(db_pool):
    """RA1 -- MANDATORY live smoke test.

    Seeds a real 8-man roster (7 filler players already active + rostered in
    `lineups`) for one team, plus a separate waived free agent (no team,
    roster_status='waived'). Drives the REAL fa_service.claim_waiver entry
    point (no mocking of fa_service/role_service/sim_persistence logic --
    only infra get_pool patching via conftest.py's autouse patch_get_pool
    fixture, same pattern as tests/test_fa_service.py and
    tests/test_franchise_plan_service.py).

    Dispositive assertions, all against real DB state / real function calls:
      1. A `lineups` row now exists for the claimed player on the claiming
         team (pre-fix: never inserted).
      2. `player_roles` has a real ROLE_REGISTRY role + positive touch_share
         for the claimed player (pre-fix: no row at all, since
         derive_and_persist_all_for_team was never invoked for this team
         after the claim).
      3. Driving the REAL sim entry point --
         sim_persistence._load_lineup_for_team (the exact `JOIN lineups`
         query draft_service.py's docstring names as what makes an
         un-lineup'd player invisible) followed by the REAL (unmocked)
         sim_persistence._stamp_role_data (the exact function whose docstring
         on this bug says "invisible to sim_persistence._stamp_role_data and
         thus invisible to sim_engine.py") -- returns the claimed player WITH
         a positive `_role_touch_share`, proving the player is not a "roster
         ghost": zero touch_share / zero minutes in a real sim.

    MANDATORY pre-fix proof (performed manually via `git stash push --
    services/fa_service.py` against this exact test, since the fix is real
    production code and can't be reverted via a fixture): with fa_service.py
    reverted to its pre-fix state (claim_waiver -> _sign_player only touches
    contracts/players, never lineups/role-cache/role-derivation), this exact
    test fails at assertion 1 (`lineup_row is None`) -- and, more tellingly,
    the claimed player is entirely ABSENT from
    sim_persistence._load_lineup_for_team's result list (assertion 3 never
    even gets a player dict to stamp), which is the literal "roster ghost"
    symptom this fix addresses. Restoring the stash (`git stash pop`) makes
    all three assertions pass again with no other change.
    """
    league_id = await _insert_league(db_pool, 900100, "RA1 Smoke League", season=2025)
    team_id, _filler_ids = await _insert_team_with_roster(
        db_pool, league_id, "RA1", [88, 84, 80, 76, 72, 68, 64],
        datetime.date(1996, 1, 1),
    )
    waived_id = await _insert_waived_player(
        db_pool, league_id, "RA1W", overall=79, birth_date=datetime.date(1998, 6, 15),
    )

    await fa_service.claim_waiver(league_id, team_id, waived_id)

    # -- 1. lineups row --------------------------------------------------
    lineup_row = await db_pool.fetchrow(
        "SELECT * FROM lineups WHERE league_id = $1 AND team_id = $2 AND player_id = $3",
        league_id, team_id, waived_id,
    )
    assert lineup_row is not None, (
        "Expected a lineups row for the waiver-claimed player immediately "
        "after claim_waiver, not just after some later unrelated rebuild -- "
        "this is exactly the pre-fix 'roster ghost' symptom."
    )
    assert lineup_row["is_starter"] is False

    # -- 2. player_roles row ----------------------------------------------
    role_row = await db_pool.fetchrow(
        "SELECT role, touch_share FROM player_roles WHERE league_id = $1 AND team_id = $2 AND player_id = $3 AND season = 2025",
        league_id, team_id, waived_id,
    )
    assert role_row is not None, (
        "Expected a player_roles row for the waiver-claimed player right "
        "after claim_waiver -- pre-fix, derive_and_persist_all_for_team was "
        "never called for this team after the claim."
    )
    assert role_row["role"] in ROLE_REGISTRY
    assert float(role_row["touch_share"]) > 0

    # -- 3. real sim entry point: _load_lineup_for_team + _stamp_role_data --
    team_players = await sim_persistence._load_lineup_for_team(db_pool, league_id, team_id)
    claimed_dict = next((p for p in team_players if p["id"] == waived_id), None)
    assert claimed_dict is not None, (
        "The waiver-claimed player must appear in _load_lineup_for_team's "
        "result -- pre-fix, the missing lineups row means this INNER JOIN "
        "query silently omits the player entirely, and sim_engine never "
        "sees them at all (not even a zero-touch_share fallback)."
    )

    await sim_persistence._stamp_role_data(
        db_pool, league_id, team_id, 2025, team_players, offensive_scheme="balanced",
    )
    assert claimed_dict.get("_role_touch_share") is not None
    assert claimed_dict["_role_touch_share"] > 0, (
        "Waiver-claimed player must get a real, positive touch_share from "
        "the real (unmocked) sim persistence layer, proving they are not a "
        "roster ghost with zero minutes/touches in the sim."
    )


# ---------------------------------------------------------------------------
# RA2 -- shared seeding helpers (kept local/separate from RA1's helpers above
# -- these need variable, sub-5-player roster sizes rather than a fixed
# 7-8 player roster).
# ---------------------------------------------------------------------------


async def _insert_league_ra2(db_pool, guild_id: int, name: str, season: int = 2025) -> int:
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
    league_id = await _insert_league_ra2(db_pool, 700200, "RA2 Smoke League")
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
    league_id = await _insert_league_ra2(db_pool, 700201, "RA4 1P League")
    season = 2025
    roster_spec = [("PG", 80, 26)]
    team_id, _ = await _insert_tiny_roster_team(db_pool, league_id, "R1", roster_spec)

    assignments = await role_service.derive_roles(db_pool, league_id, team_id, season)
    assert len(assignments) == 1
    assert assignments[0]["role"] in _PRIMARY_ROLES


async def test_ra4_two_player_roster_gets_primary_and_anchor_real_db(db_pool):
    league_id = await _insert_league_ra2(db_pool, 700202, "RA4 2P League")
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
    league_id = await _insert_league_ra2(db_pool, 700203, "RA4 3P League")
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
    league_id = await _insert_league_ra2(db_pool, 700204, "RA4 4P League")
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
    league_id = await _insert_league_ra2(db_pool, 700205, "RA4 5P League")
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
