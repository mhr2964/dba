"""
Integration tests for services.awards_service.

Uses db_pool + a local patch of services.awards_service.get_pool,
because the module binds the name at import time via
`from data.db import get_pool`.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from core.errors import DBAError
from services import awards_service


# ---------------------------------------------------------------------------
# Helper — insert league, multiple teams, one player
# ---------------------------------------------------------------------------


async def _insert_test_league_team_player(
    pool, *, num_teams: int = 1
) -> tuple[int, list[int], int]:
    """
    Insert a league, `num_teams` teams, and one player on the first team.
    Returns (league_id, [team_id, ...], player_id).
    """
    league_row = await pool.fetchrow(
        """
        INSERT INTO leagues (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES (555001, 'Awards Test League', 2025, 2025, 11111)
        RETURNING id
        """,
    )
    league_id = league_row["id"]

    codes = ["LAL", "BOS", "MIA", "CHI", "GSW", "SAS", "NYK", "PHX"]
    cities = ["Los Angeles", "Boston", "Miami", "Chicago", "Golden State", "San Antonio", "New York", "Phoenix"]
    names = ["Lakers", "Celtics", "Heat", "Bulls", "Warriors", "Spurs", "Knicks", "Suns"]
    conferences = ["West", "East", "East", "East", "West", "West", "East", "West"]
    divisions = ["Pacific", "Atlantic", "Southeast", "Central", "Pacific", "Southwest", "Atlantic", "Pacific"]

    team_ids: list[int] = []
    for i in range(num_teams):
        row = await pool.fetchrow(
            """
            INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            league_id, codes[i], names[i], cities[i], conferences[i], divisions[i],
        )
        team_ids.append(row["id"])

    player_row = await pool.fetchrow(
        """
        INSERT INTO players (
            league_id, first_name, last_name, position, team_id,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq, potential,
            peak_age_start, peak_age_end, loyalty, money_drive, win_drive
        ) VALUES (
            $1, 'Test', 'Player', 'PG', $2,
            85, 80, 80, 75, 75,
            80, 85, 75, 65, 80, 90,
            25, 30, 50, 50, 70
        )
        RETURNING id
        """,
        league_id, team_ids[0],
    )
    player_id = player_row["id"]

    return league_id, team_ids, player_id


async def _insert_extra_players(pool, league_id: int, team_ids: list[int], count: int) -> list[int]:
    """Insert `count` additional low-OVR players, one per team_ids[1:1+count]."""
    player_ids: list[int] = []
    for i in range(count):
        row = await pool.fetchrow(
            """
            INSERT INTO players (
                league_id, first_name, last_name, position, team_id,
                overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
                finishing, playmaking, defense, rebounding, iq, potential,
                peak_age_start, peak_age_end, loyalty, money_drive, win_drive
            ) VALUES (
                $1, $2, 'Stray', 'PG', $3,
                70, 70, 70, 70, 70, 70, 70, 70, 70, 70, 70, 25, 30, 50, 50, 50
            )
            RETURNING id
            """,
            league_id, f"Player{i}", team_ids[i + 1],
        )
        player_ids.append(row["id"])
    return player_ids


def _patch_pool(pool):
    """Returns a context manager that injects pool into awards_service.get_pool."""
    return patch(
        "services.awards_service.get_pool",
        new=AsyncMock(return_value=pool),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_open_voting_creates_session(db_pool):
    """open_voting inserts a row in award_votings and returns an id."""
    league_id, team_ids, player_id = await _insert_test_league_team_player(db_pool)

    with _patch_pool(db_pool):
        voting_id = await awards_service.open_voting(league_id, season=2025, award_type="mvp")

    assert isinstance(voting_id, int)
    row = await db_pool.fetchrow("SELECT * FROM award_votings WHERE id = $1", voting_id)
    assert row is not None
    assert row["award_type"] == "mvp"
    assert row["status"] == "open"


async def test_open_voting_is_idempotent(db_pool):
    """Calling open_voting twice for the same award returns the same id; only one row exists."""
    league_id, team_ids, player_id = await _insert_test_league_team_player(db_pool)

    with _patch_pool(db_pool):
        vid1 = await awards_service.open_voting(league_id, season=2025, award_type="mvp")
        vid2 = await awards_service.open_voting(league_id, season=2025, award_type="mvp")

    assert vid1 == vid2
    count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM award_votings WHERE league_id=$1 AND season=2025 AND award_type='mvp'",
        league_id,
    )
    assert count == 1


async def test_cast_vote_records_vote(db_pool):
    """cast_vote inserts an award_votes row with the correct player_id."""
    league_id, team_ids, player_id = await _insert_test_league_team_player(db_pool)
    team_id = team_ids[0]

    with _patch_pool(db_pool):
        voting_id = await awards_service.open_voting(league_id, season=2025, award_type="mvp")
        await awards_service.cast_vote(voting_id, team_id=team_id, player_id=player_id)

    row = await db_pool.fetchrow(
        "SELECT * FROM award_votes WHERE voting_id=$1 AND voter_team_id=$2",
        voting_id, team_id,
    )
    assert row is not None
    assert row["player_id"] == player_id


async def test_cast_vote_rejects_duplicate(db_pool):
    """A team casting a second vote in the same session raises DBAError."""
    league_id, team_ids, player_id = await _insert_test_league_team_player(db_pool)
    team_id = team_ids[0]

    with _patch_pool(db_pool):
        voting_id = await awards_service.open_voting(league_id, season=2025, award_type="mvp")
        await awards_service.cast_vote(voting_id, team_id=team_id, player_id=player_id)

        with pytest.raises(DBAError, match="already cast"):
            await awards_service.cast_vote(voting_id, team_id=team_id, player_id=player_id)


async def test_cast_vote_rejects_closed_session(db_pool):
    """Voting in a closed session raises DBAError."""
    league_id, team_ids, player_id = await _insert_test_league_team_player(db_pool)
    team_id = team_ids[0]

    with _patch_pool(db_pool):
        voting_id = await awards_service.open_voting(league_id, season=2025, award_type="mvp")
        # Manually close it
        await db_pool.execute(
            "UPDATE award_votings SET status='closed' WHERE id=$1", voting_id
        )

        with pytest.raises(DBAError, match="closed"):
            await awards_service.cast_vote(voting_id, team_id=team_id, player_id=player_id)


async def test_close_voting_single_winner(db_pool):
    """3 votes for player A vs 1 vote for player B — player A is place=1 in award_results."""
    league_id, team_ids, player_a = await _insert_test_league_team_player(
        db_pool, num_teams=4
    )

    # Insert a second player on team 2 to use as the minority candidate
    player_b_row = await db_pool.fetchrow(
        """
        INSERT INTO players (
            league_id, first_name, last_name, position, team_id,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq, potential,
            peak_age_start, peak_age_end, loyalty, money_drive, win_drive
        ) VALUES (
            $1, 'Other', 'Guy', 'C', $2,
            70, 60, 65, 50, 55,
            70, 50, 75, 85, 65, 75,
            27, 33, 60, 40, 60
        )
        RETURNING id
        """,
        league_id, team_ids[1],
    )
    player_b = player_b_row["id"]

    with _patch_pool(db_pool):
        voting_id = await awards_service.open_voting(league_id, season=2025, award_type="mvp")

        # 3 teams vote for player A
        for i in range(3):
            await awards_service.cast_vote(voting_id, team_id=team_ids[i], player_id=player_a)
        # 1 team votes for player B
        await awards_service.cast_vote(voting_id, team_id=team_ids[3], player_id=player_b)

        results = await awards_service.close_voting(voting_id)

    assert results[0]["player_id"] == player_a
    assert results[0]["place"] == 1
    assert results[0]["vote_count"] == 3


async def test_close_voting_sets_status_closed(db_pool):
    """After close_voting, the award_votings row status is 'closed'."""
    league_id, team_ids, player_id = await _insert_test_league_team_player(db_pool)

    with _patch_pool(db_pool):
        voting_id = await awards_service.open_voting(league_id, season=2025, award_type="dpoy")
        # Cast at least one vote so close_voting has something to tally
        await awards_service.cast_vote(voting_id, team_id=team_ids[0], player_id=player_id)
        await awards_service.close_voting(voting_id)

    row = await db_pool.fetchrow("SELECT status FROM award_votings WHERE id=$1", voting_id)
    assert row["status"] == "closed"


# ---------------------------------------------------------------------------
# PA8 -- close_voting caps single-winner awards to top 5
# ---------------------------------------------------------------------------


async def test_close_voting_single_winner_caps_at_top_five(db_pool):
    """PA8: mvp/dpoy/roy/6moy must cap persisted award_results rows to the
    top 5, mirroring the All-NBA branch's existing [:5] -- pre-fix, every
    player with even one stray vote got a permanent award_results row."""
    league_id, team_ids, first_player_id = await _insert_test_league_team_player(db_pool, num_teams=6)
    player_ids = [first_player_id] + await _insert_extra_players(db_pool, league_id, team_ids, count=5)

    with _patch_pool(db_pool):
        voting_id = await awards_service.open_voting(league_id, season=2025, award_type="mvp")
        # 6 teams, 6 distinct players, one vote each -- a stray-vote scenario.
        for team_id, player_id in zip(team_ids, player_ids):
            await awards_service.cast_vote(voting_id, team_id=team_id, player_id=player_id)

        results = await awards_service.close_voting(voting_id)

    assert len(results) == 5, "Single-winner awards must cap to top 5 results"

    row_count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM award_results WHERE voting_id = $1", voting_id
    )
    assert row_count == 5


async def test_close_voting_all_star_remains_uncapped(db_pool):
    """PA8 scope check: All-Star voting is NOT capped (multiple players
    legitimately make each conference team) -- only mvp/dpoy/roy/6moy are."""
    league_id, team_ids, first_player_id = await _insert_test_league_team_player(db_pool, num_teams=6)
    player_ids = [first_player_id] + await _insert_extra_players(db_pool, league_id, team_ids, count=5)

    with _patch_pool(db_pool):
        voting_id = await awards_service.open_voting(league_id, season=2025, award_type="all_star_east")
        for team_id, player_id in zip(team_ids, player_ids):
            await awards_service.cast_vote(voting_id, team_id=team_id, player_id=player_id)

        results = await awards_service.close_voting(voting_id)

    assert len(results) == 6


# ---------------------------------------------------------------------------
# PA6 -- _mvp_team_adjustments (unified team-component formula)
# ---------------------------------------------------------------------------


def test_mvp_team_adjustments_tank_floor_applies_under_25_wins():
    assert awards_service._mvp_team_adjustments(0.30, 15) == -15.0


def test_mvp_team_adjustments_no_floor_at_25_wins_or_more():
    result = awards_service._mvp_team_adjustments(0.45, 25)
    assert result > -15.0


def test_mvp_team_adjustments_sub_500_penalty():
    at_500 = awards_service._mvp_team_adjustments(0.500, 41)
    below_500 = awards_service._mvp_team_adjustments(0.400, 33)
    assert below_500 < at_500


# ---------------------------------------------------------------------------
# PA7 -- AI awards-odds prompt no longer claims a false team_win_pct term
# ---------------------------------------------------------------------------


def test_pa7_ai_prompt_matches_real_mvp_score_formula():
    """The prompt text must describe _mvp_score's ACTUAL formula (no
    team_win_pct term) instead of the stale, wrong-multiplier description
    that used to claim ppg*1.0 + apg*0.6 + rpg*0.4 + team_win_pct*20 + ts_pct*10."""
    import inspect

    source = inspect.getsource(awards_service.generate_awards_race_odds)
    assert "team_win_pct*20" not in source
    assert "ppg*1.0 + apg*0.6 + rpg*0.4 + ts_pct*10" in source
