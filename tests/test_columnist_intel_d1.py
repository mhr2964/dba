"""
Integration tests for the D1 columnist_intel providers (season_history,
hall_of_fame, all_time_records) and the all_time_records_repo.get_all helper
they depend on.

Uses db_pool directly, matching test_trade_repo.py's convention. clean_db
(autouse) truncates leagues CASCADE before each test.
"""
from __future__ import annotations

from types import SimpleNamespace

from data.repositories import all_time_records_repo, hof_repo, history_repo
from services import columnist_intel
from services.personas.base import Persona


async def _make_league_and_team(pool) -> tuple[int, int]:
    league_row = await pool.fetchrow(
        """
        INSERT INTO leagues (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES (777001, 'D1 Intel Test League', 2025, 2025, 22222)
        RETURNING id
        """,
    )
    league_id = league_row["id"]
    team_row = await pool.fetchrow(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'LAL', 'Lakers', 'Los Angeles', 'West', 'Pacific')
        RETURNING id
        """,
        league_id,
    )
    return league_id, team_row["id"]


async def _make_player(pool, league_id: int, team_id: int) -> int:
    row = await pool.fetchrow(
        """
        INSERT INTO players (
            league_id, first_name, last_name, position, team_id, overall,
            speed, shooting_2pt, shooting_3pt, shooting_mid, finishing,
            playmaking, defense, rebounding, iq, potential,
            peak_age_start, peak_age_end, loyalty, money_drive, win_drive
        ) VALUES (
            $1, 'Test', 'Player', 'PG', $2, 85,
            80, 80, 80, 80, 80,
            80, 80, 80, 80, 80,
            24, 30, 50, 50, 50
        )
        RETURNING id
        """,
        league_id, team_id,
    )
    return row["id"]


# ---------------------------------------------------------------------------
# all_time_records_repo.get_all
# ---------------------------------------------------------------------------

async def test_get_all_empty_for_new_league(db_pool):
    if db_pool is None:
        return
    league_id, _ = await _make_league_and_team(db_pool)
    result = await all_time_records_repo.get_all(db_pool, league_id)
    assert result == []


async def test_get_all_returns_every_set_record(db_pool):
    if db_pool is None:
        return
    league_id, team_id = await _make_league_and_team(db_pool)
    await all_time_records_repo.set_record(db_pool, league_id, "most_pts_game_player", 55.0, team_id=team_id)
    await all_time_records_repo.set_record(db_pool, league_id, "highest_team_score", 140.0, team_id=team_id)
    result = await all_time_records_repo.get_all(db_pool, league_id)
    record_types = {r["record_type"] for r in result}
    assert record_types == {"most_pts_game_player", "highest_team_score"}


# ---------------------------------------------------------------------------
# columnist_intel providers — empty-safe path
# ---------------------------------------------------------------------------

async def test_season_history_provider_empty_safe(db_pool):
    if db_pool is None:
        return
    league_id, team_id = await _make_league_and_team(db_pool)
    league = SimpleNamespace(id=league_id)
    result = await columnist_intel._provide_season_history(db_pool, league, 2025, [team_id])
    assert result == {team_id: []}


async def test_hall_of_fame_provider_empty_safe(db_pool):
    if db_pool is None:
        return
    league_id, team_id = await _make_league_and_team(db_pool)
    league = SimpleNamespace(id=league_id)
    result = await columnist_intel._provide_hall_of_fame(db_pool, league, 2025, [team_id])
    assert result == {team_id: []}


async def test_all_time_records_provider_empty_safe(db_pool):
    if db_pool is None:
        return
    league_id, team_id = await _make_league_and_team(db_pool)
    league = SimpleNamespace(id=league_id)
    result = await columnist_intel._provide_all_time_records(db_pool, league, 2025, [team_id])
    assert result == {team_id: []}


# ---------------------------------------------------------------------------
# columnist_intel providers — populated path, same league-wide list per team
# ---------------------------------------------------------------------------

async def test_season_history_provider_returns_completed_seasons(db_pool):
    if db_pool is None:
        return
    league_id, team_id = await _make_league_and_team(db_pool)
    player_id = await _make_player(db_pool, league_id, team_id)
    await history_repo.record_season(
        db_pool, league_id, season=2025,
        champion_team_id=team_id, mvp_player_id=player_id,
    )
    league = SimpleNamespace(id=league_id)
    result = await columnist_intel._provide_season_history(db_pool, league, 2026, [team_id])
    assert len(result[team_id]) == 1
    assert result[team_id][0]["champion_team_id"] == team_id


async def test_hall_of_fame_provider_returns_inductees(db_pool):
    if db_pool is None:
        return
    league_id, team_id = await _make_league_and_team(db_pool)
    player_id = await _make_player(db_pool, league_id, team_id)
    await hof_repo.induct(
        db_pool, league_id, player_id,
        inducted_season=2030, career_seasons=15, career_championships=3,
        career_mvp_votes=2, career_overall_peak=95, induction_reason="career greatness",
    )
    league = SimpleNamespace(id=league_id)
    result = await columnist_intel._provide_hall_of_fame(db_pool, league, 2031, [team_id])
    assert len(result[team_id]) == 1
    assert result[team_id][0]["player_id"] == player_id


async def test_all_time_records_provider_returns_records_duplicated_per_team(db_pool):
    """League-wide data (not team-scoped) — the same list is attached under
    every requested team_id, matching build_columnist_intel's per-team shape."""
    if db_pool is None:
        return
    league_id, team_id = await _make_league_and_team(db_pool)
    other_team_row = await db_pool.fetchrow(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'BOS', 'Celtics', 'Boston', 'East', 'Atlantic')
        RETURNING id
        """,
        league_id,
    )
    other_team_id = other_team_row["id"]
    await all_time_records_repo.set_record(db_pool, league_id, "biggest_blowout", 45.0, team_id=team_id)
    league = SimpleNamespace(id=league_id)
    result = await columnist_intel._provide_all_time_records(db_pool, league, 2025, [team_id, other_team_id])
    assert result[team_id] == result[other_team_id]
    assert len(result[team_id]) == 1


# ---------------------------------------------------------------------------
# build_columnist_intel end-to-end — new keys registered and mergeable
# ---------------------------------------------------------------------------

async def test_build_columnist_intel_merges_new_provider_keys(db_pool):
    if db_pool is None:
        return
    league_id, team_id = await _make_league_and_team(db_pool)
    player_id = await _make_player(db_pool, league_id, team_id)
    await hof_repo.induct(
        db_pool, league_id, player_id,
        inducted_season=2030, career_seasons=15, career_championships=3,
        career_mvp_votes=2, career_overall_peak=95, induction_reason="career greatness",
    )
    league = SimpleNamespace(id=league_id)
    persona = Persona(
        id="test_persona", display_name="Test", byline="Test Desk", avatar_emoji="🧪",
        voice_notes="test", categories=("game_recap",),
        context_keys=("hall_of_fame", "season_history", "all_time_records"),
    )
    result = await columnist_intel.build_columnist_intel(db_pool, league, 2031, persona, [team_id])
    assert "team_intel" in result
    team_data = result["team_intel"][team_id]
    assert len(team_data["hall_of_fame"]) == 1
    assert team_data["season_history"] == []
    assert team_data["all_time_records"] == []
