"""
Tests for services.draft_service.

D4 (docs/design/draft-logic-rules.md): draft had zero test coverage before
this pass. Covers the weighted lottery (seeded — see test_progression.py's
test_high_potential_grows_more for the "don't repeat the unseeded flaky
sampling test" lesson this domain follows from the start), D1's positional-
need/philosophy CPU-select regressions (pure, no DB), and the pick/contract
flow (advance_pick, make_pick).
"""
from __future__ import annotations

import datetime
import random

import pytest

from core.errors import DBAError
from data.repositories import draft_repo
from services import draft_service
from services.draft_service import _cpu_select, _PHILOSOPHY_DRAFT_BIAS


# ---------------------------------------------------------------------------
# _cpu_select — D1 regressions (pure, no DB)
# ---------------------------------------------------------------------------


def _prospect(pid: int, position: str = "SF", overall: int = 70, defense: int = 50) -> dict:
    return {"id": pid, "position": position, "overall": overall, "defense": defense}


def test_cpu_select_prefers_position_of_need():
    """A prospect at a thin position (<2 rostered) should win over an
    equal-OVR prospect at a position the team already has 2-3 of — the
    1.25x need multiplier outweighs the +/-2 noise band."""
    thin_position_prospect = _prospect(1, position="C", overall=70)
    normal_position_prospect = _prospect(2, position="PG", overall=70)
    position_counts = {"C": 0, "PG": 2}

    best = _cpu_select(
        [thin_position_prospect, normal_position_prospect],
        team=object(),
        position_counts=position_counts,
    )

    assert best["id"] == 1


def test_cpu_select_downweights_surplus_position():
    """A prospect at a surplus position (>=4 rostered) should lose to an
    equal-OVR prospect at a position with normal depth."""
    surplus_position_prospect = _prospect(1, position="C", overall=70)
    normal_position_prospect = _prospect(2, position="PG", overall=70)
    position_counts = {"C": 5, "PG": 2}

    best = _cpu_select(
        [surplus_position_prospect, normal_position_prospect],
        team=object(),
        position_counts=position_counts,
    )

    assert best["id"] == 2


def test_cpu_select_with_no_position_counts_is_pure_bpa():
    """Existing callers that don't pass position_counts/philosophy keep the
    pre-D1 pure best-player-available behavior (modulo noise) — signature
    change must be additive."""
    high = _prospect(1, overall=80)
    low = _prospect(2, overall=60)
    # Gap of 20 overwhelms the +/-2 noise band regardless of seed.
    best = _cpu_select([high, low], team=object())
    assert best["id"] == 1


def test_cpu_select_philosophy_bias_defense_first_favors_defense_floor():
    """defense_first biases toward prospects who clear the defense floor,
    even at equal OVR."""
    assert "defense_first" in _PHILOSOPHY_DRAFT_BIAS
    high_defense = _prospect(1, overall=75, defense=70)  # clears defense_first's floor (65)
    low_defense = _prospect(2, overall=75, defense=50)   # below floor

    best = _cpu_select(
        [high_defense, low_defense],
        team=object(),
        philosophy="defense_first",
    )

    assert best["id"] == 1


def test_cpu_select_philosophy_bias_vet_overrater_favors_defense_floor():
    high_defense = _prospect(1, overall=75, defense=65)  # clears vet_overrater's floor (60)
    low_defense = _prospect(2, overall=75, defense=40)
    best = _cpu_select(
        [high_defense, low_defense],
        team=object(),
        philosophy="vet_overrater",
    )
    assert best["id"] == 1


def test_cpu_select_star_maxer_dampens_noise_toward_pure_bpa():
    """star_maxer's narrower noise band means a small OVR gap should
    deterministically go to the higher-OVR prospect, unlike the default
    +/-2 band which could occasionally flip a 2-point gap."""
    higher = _prospect(1, overall=72)
    lower = _prospect(2, overall=70)
    for _ in range(20):
        best = _cpu_select([higher, lower], team=object(), philosophy="star_maxer")
        assert best["id"] == 1


def test_cpu_select_chaos_widens_noise_enough_to_flip_outcomes():
    """chaos widens the noise band enough that, across many trials, the
    lower-OVR prospect sometimes wins — unlike the default/star_maxer bands."""
    random.seed(42)
    higher = _prospect(1, overall=73)
    lower = _prospect(2, overall=70)

    winners = {
        _cpu_select([higher, lower], team=object(), philosophy="chaos")["id"]
        for _ in range(50)
    }
    assert winners == {1, 2}, f"Expected both prospects to win at least once under chaos noise, got {winners}"


def test_cpu_select_unknown_philosophy_falls_back_to_default():
    """A philosophy string with no _PHILOSOPHY_DRAFT_BIAS entry (e.g.
    youth_developer, tendency_respecter, egalitarian) should behave exactly
    like passing no philosophy at all — intentional, per D1's doc."""
    high = _prospect(1, overall=80)
    low = _prospect(2, overall=60)
    best = _cpu_select([high, low], team=object(), philosophy="youth_developer")
    assert best["id"] == 1


# ---------------------------------------------------------------------------
# run_lottery — seeded statistical tests
# ---------------------------------------------------------------------------


async def _create_league(pool, guild_id: int) -> int:
    league_row = await pool.fetchrow(
        """
        INSERT INTO leagues
            (discord_guild_id, name, start_season_year, current_season,
             commissioner_user_id, salary_cap)
        VALUES ($1, 'Draft Service Test League', 2025, 2025, 99999, 140000000)
        RETURNING id
        """,
        guild_id,
    )
    return league_row["id"]


async def _create_team(pool, league_id: int, code: str, manager_user_id: int | None = None) -> int:
    team_row = await pool.fetchrow(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division, manager_user_id)
        VALUES ($1, $2, $2, $2, 'East', 'Atlantic', $3)
        RETURNING id
        """,
        league_id,
        code,
        manager_user_id,
    )
    return team_row["id"]


async def _record_win(pool, league_id: int, winner_id: int, loser_id: int, game_index: int) -> None:
    await pool.execute(
        """
        INSERT INTO games
            (league_id, season, game_index, home_team_id, away_team_id,
             scheduled_date, status, home_score, away_score, is_user_matchup, rng_seed)
        VALUES ($1, 2025, $2, $3, $4, $5, 'simmed', 100, 80, FALSE, 1)
        """,
        league_id,
        game_index,
        winner_id,
        loser_id,
        datetime.date(2025, 4, 1) + datetime.timedelta(days=game_index),
    )


async def _setup_lottery_league(pool) -> tuple[int, list[int]]:
    """
    14 teams. team[i] beats team[0] exactly `i` times (i=1..13) and team[0]
    never wins — gives team[0] 0 wins (worst) and team[i] exactly `i` wins,
    so every team has a distinct win total with no tiebreak ambiguity.
    Bottom 7 (teams[0..6], wins 0-6) land in the lottery; top 7
    (teams[7..13], wins 7-13) land in the playoff group.
    """
    league_id = await _create_league(pool, guild_id=850001)
    team_ids = [await _create_team(pool, league_id, f"T{i}") for i in range(14)]

    game_index = 1
    for i in range(1, 14):
        for _ in range(i):
            await _record_win(pool, league_id, team_ids[i], team_ids[0], game_index)
            game_index += 1

    return league_id, team_ids


async def _reset_lottery(pool, draft_id: int, league_id: int, season: int) -> None:
    await pool.execute(
        "UPDATE drafts SET status = 'pending_lottery', lottery_completed_at = NULL WHERE id = $1",
        draft_id,
    )
    await pool.execute(
        "UPDATE draft_picks SET pick_number = NULL WHERE league_id = $1 AND season = $2",
        league_id,
        season,
    )


async def _seed_draft_picks_round1(pool, league_id: int, season: int, team_ids: list[int]) -> None:
    for team_id in team_ids:
        await pool.execute(
            """
            INSERT INTO draft_picks (league_id, season, round, original_team_id, current_team_id)
            VALUES ($1, $2, 1, $3, $3)
            """,
            league_id,
            season,
            team_id,
        )


async def test_run_lottery_weighted_odds(db_pool):
    """Across many seeded trials, the worst-record team (rank 1, 14.0 odds)
    should win pick #1 more often than the weakest lottery team (rank 7,
    7.5 odds)."""
    league_id, team_ids = await _setup_lottery_league(db_pool)
    await _seed_draft_picks_round1(db_pool, league_id, 2025, team_ids)
    draft = await draft_repo.create_draft(db_pool, league_id, 2025)

    worst_team_id = team_ids[0]     # 0 wins -> rank 1 -> odds 14.0
    weakest_lottery_team_id = team_ids[6]  # 6 wins -> rank 7 -> odds 7.5

    random.seed(20260724)
    n_trials = 300
    worst_pick1_count = 0
    weakest_pick1_count = 0

    for _ in range(n_trials):
        await _reset_lottery(db_pool, draft.id, league_id, 2025)
        ordered = await draft_service.run_lottery(league_id, 2025)
        pick1_team = ordered[0]["team_id"]
        if pick1_team == worst_team_id:
            worst_pick1_count += 1
        elif pick1_team == weakest_lottery_team_id:
            weakest_pick1_count += 1

    assert worst_pick1_count > weakest_pick1_count, (
        f"Expected worst-record team ({worst_pick1_count}/{n_trials}) to win pick #1 "
        f"more often than the weakest lottery team ({weakest_pick1_count}/{n_trials})"
    )


async def test_run_lottery_playoff_teams_get_picks_after_lottery_group(db_pool):
    """Non-lottery (playoff-group) teams get picks assigned after all lottery
    picks, worst-record-among-playoff-teams first (mirrors 'worst eliminated
    earliest = pick 15, champion = last' for a real 30-team/14-lottery league;
    generalized here to this fixture's 7-lottery/7-playoff split)."""
    league_id, team_ids = await _setup_lottery_league(db_pool)
    await _seed_draft_picks_round1(db_pool, league_id, 2025, team_ids)
    await draft_repo.create_draft(db_pool, league_id, 2025)

    random.seed(1)
    ordered = await draft_service.run_lottery(league_id, 2025)

    assert len(ordered) == 14
    lottery_team_ids = {team_ids[i] for i in range(7)}
    playoff_team_ids_in_order = [pick["team_id"] for pick in ordered[7:]]

    # Picks 1-7 are the lottery group (some order via weighted draw);
    # picks 8-14 are the playoff group in worst-to-best record order.
    assert {pick["team_id"] for pick in ordered[:7]} == lottery_team_ids
    assert playoff_team_ids_in_order == team_ids[7:14]
    assert [pick["pick_number"] for pick in ordered] == list(range(1, 15))


async def test_run_lottery_rejects_when_already_run(db_pool):
    league_id, team_ids = await _setup_lottery_league(db_pool)
    await _seed_draft_picks_round1(db_pool, league_id, 2025, team_ids)
    await draft_repo.create_draft(db_pool, league_id, 2025)

    await draft_service.run_lottery(league_id, 2025)

    with pytest.raises(DBAError, match="Lottery already run"):
        await draft_service.run_lottery(league_id, 2025)


# ---------------------------------------------------------------------------
# advance_pick / make_pick — pick + contract flow
# ---------------------------------------------------------------------------


async def _setup_two_team_draft(
    pool, cpu_team_manager: int | None = None, second_team_manager: int | None = None
) -> tuple[int, int, int]:
    """League + 2 teams (team_a, team_b) + a draft already past the lottery
    (status='lottery_done') with 2 round-1 picks: pick 1 -> team_a, pick 2 -> team_b.
    Returns (league_id, team_a_id, team_b_id).
    """
    league_id = await _create_league(pool, guild_id=860001 + random.randint(0, 999999))
    team_a = await _create_team(pool, league_id, "TMA", manager_user_id=cpu_team_manager)
    team_b = await _create_team(pool, league_id, "TMB", manager_user_id=second_team_manager)

    draft = await draft_repo.create_draft(pool, league_id, 2025)
    await draft_repo.update_draft_status(pool, draft.id, "in_progress", current_pick=1)

    for pick_number, team_id in [(1, team_a), (2, team_b)]:
        await pool.execute(
            """
            INSERT INTO draft_picks (league_id, season, round, original_team_id, current_team_id, pick_number)
            VALUES ($1, 2025, 1, $2, $2, $3)
            """,
            league_id,
            team_id,
            pick_number,
        )

    return league_id, team_a, team_b


async def _seed_prospects(pool, league_id: int, n: int, class_year: int = 2025) -> list[int]:
    from services.draft_class_generator import generate_draft_class
    prospects = generate_draft_class(class_year, num_players=n)
    await draft_repo.seed_draft_class(pool, league_id, class_year, prospects)
    available = await draft_repo.get_available_prospects(pool, league_id, class_year)
    return [p["id"] for p in available]


async def test_advance_pick_resolves_all_cpu_picks_and_assigns_contract(db_pool, mock_guild):
    """Both teams in this fixture default to CPU-managed (no manager_user_id),
    so advance_pick should auto-resolve every pick, marking the draft
    complete and assigning a rookie-scale contract to each drafted player."""
    league_id, team_a, team_b = await _setup_two_team_draft(db_pool)
    await _seed_prospects(db_pool, league_id, n=5)

    state = await draft_service.advance_pick(league_id, 2025, mock_guild, bot=None)

    assert state["status"] == "complete"

    draft = await draft_repo.get_draft(db_pool, league_id, 2025)
    selections = await draft_repo.get_selections(db_pool, draft.id)
    assert len(selections) == 2
    assert {s.team_id for s in selections} == {team_a, team_b}

    for selection in selections:
        contract = await db_pool.fetchrow(
            "SELECT * FROM contracts WHERE player_id = $1 AND is_active = TRUE",
            selection.player_id,
        )
        assert contract is not None
        assert contract["contract_type"] == "rookie_scale"
        assert contract["total_years"] == 4
        assert contract["years_remaining"] == 4

        player = await db_pool.fetchrow("SELECT * FROM players WHERE id = $1", selection.player_id)
        assert player["team_id"] == selection.team_id
        assert player["roster_status"] == "active"


async def test_advance_pick_human_on_clock_waits_for_manual_pick(db_pool, mock_guild):
    """When the on-the-clock team has a manager_user_id, advance_pick should
    stop and return human_on_clock without recording a selection."""
    league_id, team_a, team_b = await _setup_two_team_draft(db_pool, cpu_team_manager=12345)
    await _seed_prospects(db_pool, league_id, n=5)

    state = await draft_service.advance_pick(league_id, 2025, mock_guild, bot=None)

    assert state["status"] == "human_on_clock"
    assert state["team"].id == team_a
    assert state["pick_number"] == 1

    draft = await draft_repo.get_draft(db_pool, league_id, 2025)
    assert draft.current_pick_number == 1
    selections = await draft_repo.get_selections(db_pool, draft.id)
    assert selections == []


async def test_make_pick_rejects_wrong_team(db_pool):
    league_id, team_a, team_b = await _setup_two_team_draft(db_pool, cpu_team_manager=1, second_team_manager=2)
    prospect_ids = await _seed_prospects(db_pool, league_id, n=5)

    with pytest.raises(DBAError, match="not your team's pick"):
        await draft_service.make_pick(league_id, 2025, team_b, prospect_ids[0])


async def test_make_pick_rejects_already_drafted_player(db_pool):
    league_id, team_a, team_b = await _setup_two_team_draft(db_pool, cpu_team_manager=1, second_team_manager=2)
    prospect_ids = await _seed_prospects(db_pool, league_id, n=5)

    await draft_service.make_pick(league_id, 2025, team_a, prospect_ids[0])

    with pytest.raises(DBAError, match="no longer available"):
        await draft_service.make_pick(league_id, 2025, team_b, prospect_ids[0])


async def test_make_pick_assigns_rookie_contract(db_pool):
    league_id, team_a, team_b = await _setup_two_team_draft(db_pool, cpu_team_manager=1, second_team_manager=2)
    prospect_ids = await _seed_prospects(db_pool, league_id, n=5)

    result = await draft_service.make_pick(league_id, 2025, team_a, prospect_ids[0])

    assert result["pick_number"] == 1
    assert result["team_id"] == team_a

    contract = await db_pool.fetchrow(
        "SELECT * FROM contracts WHERE player_id = $1 AND is_active = TRUE",
        prospect_ids[0],
    )
    assert contract is not None
    assert contract["contract_type"] == "rookie_scale"

    player = await db_pool.fetchrow("SELECT * FROM players WHERE id = $1", prospect_ids[0])
    assert player["team_id"] == team_a
    assert player["roster_status"] == "active"
