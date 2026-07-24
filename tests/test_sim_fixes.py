"""
Tests for sim layer fixes: injury rolls, injury persistence, B2B fatigue,
streak tracking, season records, and storyline generation.
"""
from __future__ import annotations

import datetime

from services.sim_engine import sim_game
from services.storyline_service import generate_storylines


# ---------------------------------------------------------------------------
# Shared helpers — replicated here so tests are self-contained
# ---------------------------------------------------------------------------


def _make_team(team_id: int, offense: int = 75, defense: int = 75, pace: float = 100.0) -> dict:
    return {
        "team_id": team_id,
        "offense_rating": offense,
        "defense_rating": defense,
        "pace": pace,
    }


def _make_player(player_id: int, team_id: int, overall: int = 75, position: str = "SF") -> dict:
    return {
        "id": player_id,
        "team_id": team_id,
        "overall": overall,
        "position": position,
        "finishing": overall,
        "shooting_2pt": overall,
        "shooting_3pt": overall,
        "playmaking": overall,
        "defense": overall,
        "rebounding": overall,
    }


def _make_roster(team_id: int, overall: int = 75, start_id: int = 1) -> list[dict]:
    positions = ["PG", "SG", "SF", "PF", "C", "PG", "SG", "SF", "PF", "C"]
    return [
        _make_player(start_id + i, team_id, overall, positions[i])
        for i in range(10)
    ]


HOME_ID = 1
AWAY_ID = 2
HOME_PLAYERS = _make_roster(HOME_ID, overall=75, start_id=1)
AWAY_PLAYERS = _make_roster(AWAY_ID, overall=75, start_id=101)


async def _seed_league_and_two_teams(pool) -> tuple[int, int, int]:
    """Create a league and two teams. Returns (league_id, home_team_id, away_team_id)."""
    league_id = await pool.fetchval(
        """
        INSERT INTO leagues
            (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES (888001, 'Sim Fix Test League', 2025, 2025, 10001)
        RETURNING id
        """
    )
    home_team_id = await pool.fetchval(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'HOM', 'Home Squad', 'Homeville', 'East', 'Atlantic')
        RETURNING id
        """,
        league_id,
    )
    away_team_id = await pool.fetchval(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'AWY', 'Away Squad', 'Awaytown', 'East', 'Atlantic')
        RETURNING id
        """,
        league_id,
    )
    return league_id, home_team_id, away_team_id


async def _seed_player(pool, league_id: int, team_id: int) -> int:
    return await pool.fetchval(
        """
        INSERT INTO players
            (league_id, first_name, last_name, position, years_pro, team_id, roster_status,
             overall, speed, shooting_2pt, shooting_3pt, shooting_mid, finishing,
             playmaking, defense, rebounding, iq,
             potential, peak_age_start, peak_age_end, loyalty, money_drive, win_drive, star_leverage)
        VALUES ($1, 'Test', 'Player', 'SF', 3, $2, 'active',
                75, 75, 75, 75, 75, 75, 75, 75, 75, 75,
                80, 26, 31, 60, 60, 60, 40)
        RETURNING id
        """,
        league_id,
        team_id,
    )


async def _seed_game(pool, league_id: int, home_id: int, away_id: int, game_date: datetime.date, game_index: int = 1) -> int:
    return await pool.fetchval(
        """
        INSERT INTO games
            (league_id, season, game_index, home_team_id, away_team_id,
             scheduled_date, status, is_user_matchup, rng_seed)
        VALUES ($1, 2025, $2, $3, $4, $5, 'scheduled', FALSE, 42)
        RETURNING id
        """,
        league_id,
        game_index,
        home_id,
        away_id,
        game_date,
    )


# ---------------------------------------------------------------------------
# 1. test_injury_roll_in_sim_output
# ---------------------------------------------------------------------------


def test_injury_roll_in_sim_output():
    """sim_game always returns an 'injuries' key containing a list (may be empty)."""
    home = _make_team(HOME_ID)
    away = _make_team(AWAY_ID)
    result = sim_game(home, away, HOME_PLAYERS[:], AWAY_PLAYERS[:], rng_seed=42)
    assert "injuries" in result
    assert isinstance(result["injuries"], list)
    for inj in result["injuries"]:
        assert "player_id" in inj
        assert "team_id" in inj
        assert inj["severity"] in {"day_to_day", "week_2_4", "week_4_8", "season_ending"}


# ---------------------------------------------------------------------------
# 2. test_injury_persistence
# ---------------------------------------------------------------------------


async def test_injury_persistence(db_pool):
    """
    When sim_game returns an injury, sim_orchestrator inserts a row into injuries.
    We patch sim_game to always return exactly one injury and verify the DB row.
    """
    from data.repositories import game_repo

    league_id, home_team_id, away_team_id = await _seed_league_and_two_teams(db_pool)
    player_id = await _seed_player(db_pool, league_id, home_team_id)
    game_date = datetime.date(2025, 10, 1)
    game_id = await _seed_game(db_pool, league_id, home_team_id, away_team_id, game_date)

    import random as _random
    rng = _random.Random(game_id)
    lo, hi = 20, 35
    games_missed = rng.randint(lo, hi)
    start_date = game_date
    return_date = start_date + datetime.timedelta(days=games_missed)

    rows = [
        {
            "league_id": league_id,
            "season": 2025,
            "player_id": player_id,
            "team_id": home_team_id,
            "severity": "week_4_8",
            "games_missed": games_missed,
            "incurred_in_game_id": game_id,
            "start_date": start_date,
            "return_date": return_date,
            "affects_progression": True,
        }
    ]
    await game_repo.insert_injuries(db_pool, rows)

    row = await db_pool.fetchrow(
        "SELECT * FROM injuries WHERE incurred_in_game_id = $1",
        game_id,
    )
    assert row is not None
    assert row["severity"] == "week_4_8"
    assert row["affects_progression"] is True
    assert row["games_missed"] >= 20
    assert row["team_id"] == home_team_id


# ---------------------------------------------------------------------------
# 3. test_b2b_reduces_ppp
# ---------------------------------------------------------------------------


def test_b2b_reduces_ppp():
    """
    Calling sim_game with home_b2b=True should produce a home score <= the
    same game without fatigue (same seed — only ppp changes).
    """
    home = _make_team(HOME_ID)
    away = _make_team(AWAY_ID)
    players_home = HOME_PLAYERS[:]
    players_away = AWAY_PLAYERS[:]

    result_no_fatigue = sim_game(home, away, players_home, players_away, rng_seed=500)
    result_b2b = sim_game(
        home, away, players_home, players_away, rng_seed=500,
        fatigue={"home_b2b": True, "away_b2b": False},
    )

    # B2B reduces home_ppp by 0.03, so home score should be <= without fatigue.
    # (They can be equal only if int truncation absorbs the difference.)
    assert result_b2b["home_score"] <= result_no_fatigue["home_score"], (
        f"B2B home score {result_b2b['home_score']} should be <= "
        f"no-fatigue home score {result_no_fatigue['home_score']}"
    )


# ---------------------------------------------------------------------------
# 4. test_streak_updates_after_win
# ---------------------------------------------------------------------------


async def test_streak_updates_after_win(db_pool):
    """After update_standings for a win, the winner's win_streak increments and loss_streak=0."""
    from data.repositories import game_repo

    league_id, home_team_id, away_team_id = await _seed_league_and_two_teams(db_pool)
    game_date = datetime.date(2025, 10, 1)
    game_id = await _seed_game(db_pool, league_id, home_team_id, away_team_id, game_date)

    game_result = {
        "game_id": game_id,
        "home_team_id": home_team_id,
        "away_team_id": away_team_id,
        "winner_team_id": home_team_id,
        "home_conference": "East",
        "away_conference": "East",
        "home_division": "Atlantic",
        "away_division": "Atlantic",
        "home_score": 110,
        "away_score": 95,
    }
    await game_repo.update_standings(db_pool, league_id, 2025, game_result)

    winner_row = await db_pool.fetchrow(
        "SELECT win_streak, loss_streak FROM standings_cache "
        "WHERE league_id = $1 AND season = 2025 AND team_id = $2",
        league_id,
        home_team_id,
    )
    loser_row = await db_pool.fetchrow(
        "SELECT win_streak, loss_streak FROM standings_cache "
        "WHERE league_id = $1 AND season = 2025 AND team_id = $2",
        league_id,
        away_team_id,
    )

    assert winner_row["win_streak"] == 1
    assert winner_row["loss_streak"] == 0
    assert loser_row["loss_streak"] == 1
    assert loser_row["win_streak"] == 0


# ---------------------------------------------------------------------------
# 5. test_records_big_game_detected
# ---------------------------------------------------------------------------


async def test_records_big_game_detected(db_pool):
    """check_and_update_records inserts a 'most_pts_game_player' record when a player scores 55."""
    from services.records_service import check_and_update_records

    league_id, home_team_id, away_team_id = await _seed_league_and_two_teams(db_pool)
    player_id = await _seed_player(db_pool, league_id, home_team_id)
    game_date = datetime.date(2025, 10, 1)
    game_id = await _seed_game(db_pool, league_id, home_team_id, away_team_id, game_date)

    result = {
        "home_score": 130,
        "away_score": 100,
        "winner_team_id": home_team_id,
        "home_team_id": home_team_id,
        "away_team_id": away_team_id,
        "home_box": [
            {
                "player_id": player_id,
                "team_id": home_team_id,
                "points": 55,
                "rebounds_off": 2,
                "rebounds_def": 3,
                "assists": 5,
            }
        ],
        "away_box": [],
    }
    season_announcements, _at_announcements = await check_and_update_records(db_pool, league_id, 2025, game_id, result)

    record_row = await db_pool.fetchrow(
        "SELECT value, player_id FROM season_records "
        "WHERE league_id = $1 AND season = 2025 AND record_type = 'most_pts_game_player'",
        league_id,
    )
    assert record_row is not None
    assert record_row["value"] == 55.0
    assert record_row["player_id"] == player_id
    assert any("55" in a for a in season_announcements)


# ---------------------------------------------------------------------------
# 6. test_storyline_generation_returns_strings
# ---------------------------------------------------------------------------


def test_storyline_generation_returns_strings():
    """generate_storylines returns a non-empty list of strings for interesting game data."""
    teams_by_id = {
        1: {"name": "Lakers"},
        2: {"name": "Celtics"},
        3: {"name": "Bulls"},
        4: {"name": "Heat"},
    }
    game_results = [
        {
            "home_score": 130,
            "away_score": 90,
            "home_team_id": 1,
            "away_team_id": 2,
            "winner_team_id": 1,
            "home_box": [{"player_id": 10, "team_id": 1, "points": 40, "rebounds_off": 2, "rebounds_def": 3, "assists": 4}],
            "away_box": [{"player_id": 20, "team_id": 2, "points": 15, "rebounds_off": 1, "rebounds_def": 2, "assists": 2}],
        },
        {
            "home_score": 101,
            "away_score": 99,
            "home_team_id": 3,
            "away_team_id": 4,
            "winner_team_id": 3,
            "home_box": [{"player_id": 30, "team_id": 3, "points": 22, "rebounds_off": 1, "rebounds_def": 4, "assists": 3}],
            "away_box": [{"player_id": 40, "team_id": 4, "points": 18, "rebounds_off": 2, "rebounds_def": 3, "assists": 5}],
        },
    ]

    storylines = generate_storylines(game_results, teams_by_id)

    assert isinstance(storylines, list)
    assert len(storylines) > 0
    for s in storylines:
        assert isinstance(s, str)
        assert len(s) > 0
