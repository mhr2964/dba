"""
Integration tests for data.repositories.draft_repo.

D4 (docs/design/draft-logic-rules.md): draft had zero test coverage before
this pass. Covers create/get/update_draft_status, available-prospects
filtering (excludes already-drafted players), pick recording, and
on-the-clock team resolution.
"""
from __future__ import annotations

from data.repositories import draft_repo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_league_and_teams(pool, n_teams: int = 2) -> tuple[int, list[int]]:
    league_row = await pool.fetchrow(
        """
        INSERT INTO leagues
            (discord_guild_id, name, start_season_year, current_season,
             commissioner_user_id, salary_cap)
        VALUES (900001, 'Draft Repo Test League', 2025, 2025, 99999, 140000000)
        RETURNING id
        """
    )
    league_id: int = league_row["id"]

    team_ids: list[int] = []
    for i in range(n_teams):
        team_row = await pool.fetchrow(
            """
            INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
            VALUES ($1, $2, $3, $4, 'East', 'Atlantic')
            RETURNING id
            """,
            league_id,
            f"T{i}",
            f"Team{i}",
            f"City{i}",
        )
        team_ids.append(team_row["id"])

    return league_id, team_ids


async def _insert_prospect(pool, league_id: int, overall: int = 70, mock_rank: int | None = None) -> int:
    player_row = await pool.fetchrow(
        """
        INSERT INTO players (
            league_id, first_name, last_name, position,
            team_id, roster_status,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq,
            potential, peak_age_start, peak_age_end,
            loyalty, money_drive, win_drive, years_pro, is_rookie
        ) VALUES (
            $1, 'Prospect', 'Player', 'SF',
            NULL, 'prospect',
            $2, 70, 70, 70, 70,
            70, 70, 70, 70, 70,
            75, 26, 31,
            50, 50, 50, 0, TRUE
        )
        RETURNING id
        """,
        league_id,
        overall,
    )
    player_id: int = player_row["id"]

    await pool.execute(
        """
        INSERT INTO draft_classes (league_id, class_year, player_id, mock_rank, is_generated, source)
        VALUES ($1, $2, $3, $4, TRUE, 'generated')
        """,
        league_id,
        2025,
        player_id,
        mock_rank,
    )
    return player_id


# ---------------------------------------------------------------------------
# create_draft / get_draft / update_draft_status
# ---------------------------------------------------------------------------


async def test_create_draft_creates_pending_lottery_draft(db_pool):
    league_id, _ = await _create_league_and_teams(db_pool)

    draft = await draft_repo.create_draft(db_pool, league_id, 2025)

    assert draft.league_id == league_id
    assert draft.season == 2025
    assert draft.status == "pending_lottery"
    assert draft.current_pick_number == 1


async def test_create_draft_is_idempotent_on_conflict(db_pool):
    """A second create_draft call for the same league/season should not
    error and should not reset an already-advanced draft's status."""
    league_id, _ = await _create_league_and_teams(db_pool)

    first = await draft_repo.create_draft(db_pool, league_id, 2025)
    await draft_repo.update_draft_status(db_pool, first.id, "in_progress")

    second = await draft_repo.create_draft(db_pool, league_id, 2025)

    assert second.id == first.id
    assert second.status == "in_progress"


async def test_get_draft_returns_none_when_missing(db_pool):
    league_id, _ = await _create_league_and_teams(db_pool)
    draft = await draft_repo.get_draft(db_pool, league_id, 2099)
    assert draft is None


async def test_update_draft_status_sets_current_pick(db_pool):
    league_id, _ = await _create_league_and_teams(db_pool)
    draft = await draft_repo.create_draft(db_pool, league_id, 2025)

    await draft_repo.update_draft_status(db_pool, draft.id, "in_progress", current_pick=5)

    updated = await draft_repo.get_draft(db_pool, league_id, 2025)
    assert updated.status == "in_progress"
    assert updated.current_pick_number == 5


# ---------------------------------------------------------------------------
# get_available_prospects
# ---------------------------------------------------------------------------


async def test_get_available_prospects_returns_undrafted(db_pool):
    league_id, _ = await _create_league_and_teams(db_pool)
    player_id = await _insert_prospect(db_pool, league_id, overall=80, mock_rank=1)

    prospects = await draft_repo.get_available_prospects(db_pool, league_id, 2025)

    assert len(prospects) == 1
    assert prospects[0]["id"] == player_id
    assert prospects[0]["overall"] == 80


async def test_get_available_prospects_excludes_already_drafted(db_pool):
    """A prospect with a draft_selections row for this league/season should
    not appear in get_available_prospects — this is the filter D1's
    _cpu_select and human make_pick both rely on to avoid double-drafting."""
    league_id, team_ids = await _create_league_and_teams(db_pool)
    drafted_id = await _insert_prospect(db_pool, league_id, overall=85, mock_rank=1)
    available_id = await _insert_prospect(db_pool, league_id, overall=70, mock_rank=2)

    draft = await draft_repo.create_draft(db_pool, league_id, 2025)
    await draft_repo.record_selection(
        db_pool,
        draft_id=draft.id,
        pick_number=1,
        round=1,
        team_id=team_ids[0],
        player_id=drafted_id,
    )

    prospects = await draft_repo.get_available_prospects(db_pool, league_id, 2025)

    prospect_ids = {p["id"] for p in prospects}
    assert drafted_id not in prospect_ids
    assert available_id in prospect_ids


async def test_get_available_prospects_ordered_by_overall_desc(db_pool):
    league_id, _ = await _create_league_and_teams(db_pool)
    low_id = await _insert_prospect(db_pool, league_id, overall=60, mock_rank=30)
    high_id = await _insert_prospect(db_pool, league_id, overall=88, mock_rank=1)

    prospects = await draft_repo.get_available_prospects(db_pool, league_id, 2025)

    assert [p["id"] for p in prospects] == [high_id, low_id]


# ---------------------------------------------------------------------------
# record_selection / get_selections
# ---------------------------------------------------------------------------


async def test_record_selection_persists_pick(db_pool):
    league_id, team_ids = await _create_league_and_teams(db_pool)
    player_id = await _insert_prospect(db_pool, league_id, overall=75, mock_rank=1)
    draft = await draft_repo.create_draft(db_pool, league_id, 2025)

    selection = await draft_repo.record_selection(
        db_pool,
        draft_id=draft.id,
        pick_number=1,
        round=1,
        team_id=team_ids[0],
        player_id=player_id,
    )

    assert selection.pick_number == 1
    assert selection.round == 1
    assert selection.team_id == team_ids[0]
    assert selection.player_id == player_id


async def test_get_selections_returns_all_ordered_by_pick(db_pool):
    league_id, team_ids = await _create_league_and_teams(db_pool)
    p1 = await _insert_prospect(db_pool, league_id, overall=90, mock_rank=1)
    p2 = await _insert_prospect(db_pool, league_id, overall=85, mock_rank=2)
    draft = await draft_repo.create_draft(db_pool, league_id, 2025)

    # Insert out of pick order to confirm the query re-sorts.
    await draft_repo.record_selection(
        db_pool, draft_id=draft.id, pick_number=2, round=1, team_id=team_ids[1], player_id=p2
    )
    await draft_repo.record_selection(
        db_pool, draft_id=draft.id, pick_number=1, round=1, team_id=team_ids[0], player_id=p1
    )

    selections = await draft_repo.get_selections(db_pool, draft.id)

    assert [s.pick_number for s in selections] == [1, 2]
    assert [s.player_id for s in selections] == [p1, p2]


# ---------------------------------------------------------------------------
# get_on_the_clock_team
# ---------------------------------------------------------------------------


async def test_get_on_the_clock_team_resolves_via_draft_picks(db_pool):
    league_id, team_ids = await _create_league_and_teams(db_pool)
    draft = await draft_repo.create_draft(db_pool, league_id, 2025)

    await db_pool.execute(
        """
        INSERT INTO draft_picks (league_id, season, round, original_team_id, current_team_id, pick_number)
        VALUES ($1, 2025, 1, $2, $2, 1)
        """,
        league_id,
        team_ids[0],
    )

    on_clock = await draft_repo.get_on_the_clock_team(db_pool, draft.id, 1)

    assert on_clock == team_ids[0]


async def test_get_on_the_clock_team_returns_none_when_no_pick_mapped(db_pool):
    league_id, _ = await _create_league_and_teams(db_pool)
    draft = await draft_repo.create_draft(db_pool, league_id, 2025)

    on_clock = await draft_repo.get_on_the_clock_team(db_pool, draft.id, 99)

    assert on_clock is None


# ---------------------------------------------------------------------------
# seed_draft_class
# ---------------------------------------------------------------------------


async def test_seed_draft_class_inserts_players_and_draft_classes(db_pool):
    league_id, _ = await _create_league_and_teams(db_pool)

    from services.draft_class_generator import generate_draft_class
    prospects = generate_draft_class(2026, num_players=10)

    inserted = await draft_repo.seed_draft_class(db_pool, league_id, 2026, prospects)

    assert inserted == 10
    available = await draft_repo.get_available_prospects(db_pool, league_id, 2026)
    assert len(available) == 10
