"""
FP3 -- real DB-backed integration tests for franchise_plan_service.py itself
(derive_plan, persist_plan, get_or_derive, derive_and_persist_all).

Before this sweep, only the pure functions in franchise_plan_math.py had test
coverage (test_franchise_plan_math.py, test_franchise_plan_early_season.py).
Mock-based tests structurally can't catch sequencing bugs (FP1) or bulk-loop
behavior (FP2) -- this file follows the same "real seeded DB, real entry
point" discipline as tests/test_rollover.py.

Also includes:
- FP1's MANDATORY live smoke test: drives the REAL sim_orchestrator.sim_range
  entry point across enough games in ONE call to span a mid-call reassessment
  checkpoint, and asserts the persisted plan reflects the updated in-call
  standings rather than the pre-call snapshot. Proven to fail against the
  pre-fix cadence (see docstring on that test for the git-stash proof
  procedure) and pass post-fix.
- FP2's observability test: proves a per-team derive failure (a) logs with
  full league/team/game context at log.error, and (b) does not permanently
  strand the team -- the very next derive_and_persist_all call for the same
  league/season naturally retries it, since a failed persist never advances
  last_derived_game_index in the DB.
"""
from __future__ import annotations

import datetime
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services import franchise_plan_service, sim_orchestrator

pytestmark = pytest.mark.asyncio

_POSITIONS = ["PG", "SG", "SF", "PF", "C", "PG", "SF", "C"]


# ---------------------------------------------------------------------------
# Shared seeding helpers
# ---------------------------------------------------------------------------


async def _insert_league(
    db_pool, guild_id: int, name: str, season: int = 2025, phase: str = "REGULAR_SEASON_ACTIVE"
) -> int:
    row = await db_pool.fetchrow(
        """
        INSERT INTO leagues (
            discord_guild_id, name, start_season_year, current_season,
            current_phase, commissioner_user_id
        ) VALUES ($1, $2, $3, $3, $4, 99999)
        RETURNING id
        """,
        guild_id, name, season, phase,
    )
    return row["id"]


async def _insert_team_with_roster(
    db_pool,
    league_id: int,
    code: str,
    overalls: list[int],
    birth_date: datetime.date,
    manager_user_id: int | None = None,
) -> tuple[int, list[int]]:
    """Insert a CPU (or human, if manager_user_id given) team with an 8-player
    roster and a full lineup. All players share the same birth_date so avg_age
    is directly controllable by the caller."""
    team_id = await db_pool.fetchval(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division, manager_user_id)
        VALUES ($1, $2, $3, $4, 'East', 'Atlantic', $5)
        RETURNING id
        """,
        league_id, code, f"{code} City", code, manager_user_id,
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


async def _schedule_games(
    db_pool, league_id: int, season: int, home_team_id: int, away_team_id: int, count: int
) -> None:
    base_date = datetime.date(season, 11, 1)
    for i in range(1, count + 1):
        await db_pool.execute(
            """
            INSERT INTO games (
                league_id, season, season_type, game_index,
                home_team_id, away_team_id, scheduled_date,
                status, is_user_matchup, rng_seed
            ) VALUES ($1, $2, 'regular', $3, $4, $5, $6, 'scheduled', FALSE, $7)
            """,
            league_id, season, i, home_team_id, away_team_id,
            base_date + datetime.timedelta(days=i * 2), 9000 + i,
        )


# ---------------------------------------------------------------------------
# derive_plan / persist_plan / get_plan / get_or_derive -- basic real-DB coverage
# ---------------------------------------------------------------------------


async def test_derive_plan_returns_expected_shape(db_pool):
    """derive_plan against a real (gameless) league returns every documented key."""
    league_id = await _insert_league(db_pool, 700001, "FP3 Shape League")
    team_id, _ = await _insert_team_with_roster(
        db_pool, league_id, "TST", [90, 82, 78, 74, 70, 66, 62, 58],
        datetime.date(1997, 1, 1),
    )

    plan = await franchise_plan_service.derive_plan(db_pool, league_id, team_id, 2025)

    for key in (
        "league_id", "team_id", "season", "goal", "horizon_seasons",
        "core_player_ids", "flex_player_ids", "surplus_player_ids",
        "asset_targets", "rationale", "derived_from_record",
    ):
        assert key in plan, f"derive_plan output missing key {key!r}"
    assert plan["league_id"] == league_id
    assert plan["team_id"] == team_id
    assert plan["season"] == 2025
    assert plan["goal"] in ("win_now", "transition", "rebuild", "tank")


async def test_persist_plan_then_get_plan_roundtrip(db_pool):
    """persist_plan upserts a row that get_plan can read back unchanged."""
    league_id = await _insert_league(db_pool, 700002, "FP3 Roundtrip League")
    team_id, _ = await _insert_team_with_roster(
        db_pool, league_id, "TST", [90, 82, 78, 74, 70, 66, 62, 58],
        datetime.date(1997, 1, 1),
    )

    plan = await franchise_plan_service.derive_plan(db_pool, league_id, team_id, 2025)
    plan_id = await franchise_plan_service.persist_plan(db_pool, plan)
    assert isinstance(plan_id, int)

    stored = await franchise_plan_service.get_plan(db_pool, league_id, team_id, 2025)
    assert stored is not None
    assert stored["goal"] == plan["goal"]
    assert stored["core_player_ids"] == plan["core_player_ids"]
    assert stored["asset_targets"] == plan["asset_targets"]


async def test_get_plan_returns_none_when_never_derived(db_pool):
    league_id = await _insert_league(db_pool, 700003, "FP3 Absent League")
    team_id, _ = await _insert_team_with_roster(
        db_pool, league_id, "TST", [90, 82, 78, 74, 70, 66, 62, 58],
        datetime.date(1997, 1, 1),
    )
    stored = await franchise_plan_service.get_plan(db_pool, league_id, team_id, 2025)
    assert stored is None


async def test_get_or_derive_creates_plan_when_absent(db_pool):
    league_id = await _insert_league(db_pool, 700004, "FP3 GetOrDerive League")
    team_id, _ = await _insert_team_with_roster(
        db_pool, league_id, "TST", [90, 82, 78, 74, 70, 66, 62, 58],
        datetime.date(1997, 1, 1),
    )

    plan = await franchise_plan_service.get_or_derive(db_pool, league_id, team_id, 2025)
    assert plan is not None

    stored = await franchise_plan_service.get_plan(db_pool, league_id, team_id, 2025)
    assert stored is not None
    assert stored["goal"] == plan["goal"]


async def test_get_or_derive_never_rederives_an_existing_row(db_pool):
    """get_or_derive's own documented contract: re-derives ONLY when the row is
    absent, never when stale. Manually corrupt a persisted plan's goal, then
    confirm get_or_derive returns the (stale) stored value untouched."""
    league_id = await _insert_league(db_pool, 700005, "FP3 Staleness League")
    team_id, _ = await _insert_team_with_roster(
        db_pool, league_id, "TST", [90, 82, 78, 74, 70, 66, 62, 58],
        datetime.date(1997, 1, 1),
    )
    await franchise_plan_service.get_or_derive(db_pool, league_id, team_id, 2025)

    await db_pool.execute(
        "UPDATE franchise_plans SET goal = 'tank' WHERE league_id = $1 AND team_id = $2 AND season = $3",
        league_id, team_id, 2025,
    )

    plan = await franchise_plan_service.get_or_derive(db_pool, league_id, team_id, 2025)
    assert plan["goal"] == "tank", (
        "get_or_derive must not silently re-derive an existing (stale) row -- "
        "that is franchise_plan_math's stickiness gate's job, invoked only via "
        "derive_and_persist_all, not get_or_derive."
    )


# ---------------------------------------------------------------------------
# derive_and_persist_all -- bulk behavior
# ---------------------------------------------------------------------------


async def test_derive_and_persist_all_skips_human_managed_teams(db_pool):
    league_id = await _insert_league(db_pool, 700006, "FP3 Human Skip League")
    cpu_team_id, _ = await _insert_team_with_roster(
        db_pool, league_id, "CPU", [90, 82, 78, 74, 70, 66, 62, 58],
        datetime.date(1997, 1, 1),
    )
    human_team_id, _ = await _insert_team_with_roster(
        db_pool, league_id, "HUM", [90, 82, 78, 74, 70, 66, 62, 58],
        datetime.date(1997, 1, 1), manager_user_id=555,
    )

    count = await franchise_plan_service.derive_and_persist_all(db_pool, league_id, 2025)
    assert count == 1

    cpu_plan = await franchise_plan_service.get_plan(db_pool, league_id, cpu_team_id, 2025)
    human_plan = await franchise_plan_service.get_plan(db_pool, league_id, human_team_id, 2025)
    assert cpu_plan is not None
    assert human_plan is None, "Human-managed teams must never get a franchise_plans row"


async def test_derive_and_persist_all_persists_last_derived_game_index(db_pool):
    league_id = await _insert_league(db_pool, 700007, "FP3 GameIndex League")
    team_id, _ = await _insert_team_with_roster(
        db_pool, league_id, "TST", [90, 82, 78, 74, 70, 66, 62, 58],
        datetime.date(1997, 1, 1),
    )

    await franchise_plan_service.derive_and_persist_all(
        db_pool, league_id, 2025, current_game_index=17
    )

    plan = await franchise_plan_service.get_plan(db_pool, league_id, team_id, 2025)
    assert plan["last_derived_game_index"] == 17


# ---------------------------------------------------------------------------
# FP2 -- observability on the swallowed-exception path + natural-retry proof
# ---------------------------------------------------------------------------


async def test_derive_failure_logs_with_league_team_game_context_and_naturally_retries(
    db_pool, caplog
):
    """FP2: a per-team derive_plan failure must (a) log at error level with
    league/team/season/game-index context (not just a bare warning with no
    traceback), and (b) leave the team naturally retriable on the very next
    derive_and_persist_all call -- because the failed attempt never persists,
    last_derived_game_index in the DB never advances past None, so the
    checkpoint gate (_is_reassessment_checkpoint) fires 'initial' again next
    time rather than treating the team as permanently stale."""
    league_id = await _insert_league(db_pool, 700008, "FP2 Retry League")
    team_id, _ = await _insert_team_with_roster(
        db_pool, league_id, "FAIL", [90, 82, 78, 74, 70, 66, 62, 58],
        datetime.date(1997, 1, 1),
    )

    real_derive_plan = franchise_plan_service.derive_plan
    call_count = {"n": 0}

    async def _flaky_derive_plan(pool, league_id_, team_id_, season_):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated transient DB error")
        return await real_derive_plan(pool, league_id_, team_id_, season_)

    with patch.object(franchise_plan_service, "derive_plan", side_effect=_flaky_derive_plan):
        with caplog.at_level(logging.ERROR, logger="services.franchise_plan_service"):
            count_first = await franchise_plan_service.derive_and_persist_all(
                db_pool, league_id, 2025, current_game_index=12
            )

    assert count_first == 0, "The single CPU team's derive failed -- nothing should be persisted"
    plan_after_failure = await franchise_plan_service.get_plan(db_pool, league_id, team_id, 2025)
    assert plan_after_failure is None, "A failed derive must not leave a partial/corrupt row"

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "Expected at least one error-level log record from the failed derive"
    msg = error_records[0].getMessage()
    assert str(league_id) in msg, "Log must include league_id for correlation"
    assert "FAIL" in msg or str(team_id) in msg, "Log must include team context (code or id)"
    assert "12" in msg, "Log must include the game index the failure occurred at"

    # Natural retry: next top-level call (no code change) re-attempts and succeeds,
    # since last_derived_game_index was never advanced in the DB by the failed attempt.
    with patch.object(franchise_plan_service, "derive_plan", side_effect=_flaky_derive_plan):
        count_second = await franchise_plan_service.derive_and_persist_all(
            db_pool, league_id, 2025, current_game_index=13
        )
    assert count_second == 1, "The next call must naturally retry and succeed for the same team"
    plan_after_retry = await franchise_plan_service.get_plan(db_pool, league_id, team_id, 2025)
    assert plan_after_retry is not None
    assert plan_after_retry["last_derived_game_index"] == 13


# ---------------------------------------------------------------------------
# FP1 -- MANDATORY live smoke test: checkpoint/pivot cadence
# ---------------------------------------------------------------------------


async def test_fp1_pivot_fires_mid_call_against_real_sim_range(db_pool, monkeypatch):
    """FP1 -- MANDATORY live smoke test.

    Seeds a real two-team league: FADE (age ~32, one OVR-90 "star" but a
    mostly-weak OVR~58 supporting cast, avg_age >= 30) versus a genuinely
    strong opponent (avg OVR ~86.5). At games_played=0 (the pre-call snapshot
    taken at the very top of sim_range), FADE's early-season heuristic
    (avg_age>=30 AND has_any_star) derives goal='win_now' -- a real,
    reproducible CPU decision, not a hand-seeded plan.

    82 regular-season games are scheduled up front (so total_games=82 matches
    franchise_plan_math's _TOTAL_SEASON_GAMES scaling and the season doesn't
    look "complete" mid-test), but only 45 are actually simmed in ONE
    sim_range call -- comfortably spanning three internal ~10-game sub-batches
    past _PIVOT_MIN_GAMES=30. Given the ~24-point team-OVR gap, FADE should
    lose the vast majority of its real, unmocked simulated games, driving its
    live projected-wins pace below _WIN_NOW_COLLAPSE_CEIL=40 well before game 45.

    Dispositive assertions: the persisted plan's last_derived_game_index must
    have advanced past the pre-call snapshot (proving a mid-call re-derive
    happened at all), its derived_from_record must reflect real accumulated
    standings (not the 0-0 pre-call snapshot), and its goal must have pivoted
    away from win_now (a full re-derive against the live 0-30 record lands on
    "rebuild", not the "transition" label _should_pivot uses for its own log
    line -- see the assertion below for why that is even stronger proof).

    MANDATORY pre-fix proof (performed manually, not asserted here since it
    requires reverting real production code): with the FP1 sub-batch re-derive
    calls in sim_orchestrator.py (`_maybe_refresh_franchise_plans` invoked
    from inside the game loop) removed/reverted -- leaving only the original
    single pre-call derive at the top of sim_range -- this exact test fails:
    the persisted plan's last_derived_game_index stays at the pre-call value
    (0) and goal stays 'win_now' forever, because derive_and_persist_all is
    never called again until the NEXT top-level sim_range/sim_until_rival
    invocation. Verified via `git stash` against the sim_orchestrator.py
    change before this test was written to pass.
    """
    league_id = await _insert_league(db_pool, 700100, "FP1 Smoke League", phase="REGULAR_SEASON_ACTIVE")
    season = 2025

    fade_overalls = [90, 58, 58, 58, 58, 58, 58, 58]
    fade_id, _ = await _insert_team_with_roster(
        db_pool, league_id, "FADE", fade_overalls, datetime.date(1993, 1, 1),
    )
    strong_overalls = [90, 89, 88, 87, 86, 85, 84, 83]
    strong_id, _ = await _insert_team_with_roster(
        db_pool, league_id, "STRG", strong_overalls, datetime.date(1997, 1, 1),
    )

    await _schedule_games(db_pool, league_id, season, fade_id, strong_id, count=82)

    mock_guild = MagicMock()
    mock_guild.get_channel = MagicMock(return_value=None)

    with patch("services.sim_orchestrator.get_pool", AsyncMock(return_value=db_pool)):
        summary = await sim_orchestrator.sim_range(
            league_id, mock_guild, season, to_game_index=45, bot=None, force=False,
        )

    assert summary["games_simmed"] == 45

    plan = await franchise_plan_service.get_plan(db_pool, league_id, fade_id, season)
    assert plan is not None

    assert plan["last_derived_game_index"] is not None
    assert plan["last_derived_game_index"] >= 30, (
        f"Expected a mid-call re-derive at/after the games_played>=30 reassessment "
        f"checkpoint, got last_derived_game_index={plan['last_derived_game_index']!r} -- "
        f"this is exactly the pre-fix symptom (stuck at the pre-call snapshot)."
    )

    dfr = plan["derived_from_record"]
    assert dfr["games_played"] >= 30, (
        f"Persisted plan's derived_from_record must reflect the standings at the "
        f"mid-call reassessment, not the 0-0 pre-call snapshot -- got "
        f"games_played={dfr['games_played']!r}"
    )
    assert dfr["wins"] + dfr["losses"] == dfr["games_played"]

    # _should_pivot's returned "new_goal" (transition, for a win_now collapse) is only
    # used to label the FRANCHISE-PIVOT log line -- the actual persisted goal comes from
    # a full derive_plan() re-run against the LIVE 0-30 record, which correctly lands on
    # "rebuild" (0 wins + avg_age 32.75 satisfies the rebuild gate) rather than
    # "transition". That is even stronger proof this reflects live mid-call standings,
    # not a hardcoded pivot target.
    assert plan["goal"] == "rebuild", (
        f"Expected FADE's collapsed 0-30 real record to pivot away from win_now via the "
        f"games_played>=30 mid_season_checkpoint, got goal={plan['goal']!r} "
        f"(derived_from_record={dfr!r}). This is the FP1 dispositive assertion: it can "
        f"only pass if a reassessment fired against LIVE mid-call standings, since the "
        f"pre-call snapshot (games_played=0) always derives win_now for this roster."
    )
    assert plan["goal"] != "win_now"
    assert dfr.get("pivot_from_goal") == "win_now"
    assert "championship window collapsed" in (dfr.get("pivot_reason") or "")
