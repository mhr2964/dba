"""
Integration tests for services.trade_magnitude (Phase 1 fix D2 — the
"biggest trade" comparator).

Uses db_pool directly, matching test_trade_repo.py's convention. clean_db
(autouse) truncates leagues CASCADE before each test.
"""
from __future__ import annotations

import datetime
import itertools

from data.repositories import trade_repo
from services import trade_magnitude

_SALARY_CAP = 140_000_000
_CURRENT_SEASON = 2026
_GUILD_ID_COUNTER = itertools.count(888001)


async def _setup(pool) -> tuple[int, int, int]:
    """Insert a league and two teams. Returns (league_id, team_a_id, team_b_id)."""
    guild_id = next(_GUILD_ID_COUNTER)
    league_row = await pool.fetchrow(
        """
        INSERT INTO leagues (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES ($1, 'Trade Magnitude Test League', 2025, 2026, 33333)
        RETURNING id
        """,
        guild_id,
    )
    league_id = league_row["id"]
    team_a = await pool.fetchrow(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'LAL', 'Lakers', 'Los Angeles', 'West', 'Pacific')
        RETURNING id
        """,
        league_id,
    )
    team_b = await pool.fetchrow(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'BOS', 'Celtics', 'Boston', 'East', 'Atlantic')
        RETURNING id
        """,
        league_id,
    )
    return league_id, team_a["id"], team_b["id"]


async def _insert_player(pool, league_id: int, team_id: int, overall: int = 80, age: int = 27) -> int:
    birth_year = 2026 - age
    birth_date = datetime.date(birth_year, 1, 1)
    row = await pool.fetchrow(
        """
        INSERT INTO players (
            league_id, first_name, last_name, position, team_id, birth_date,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq, potential,
            peak_age_start, peak_age_end, loyalty, money_drive, win_drive
        ) VALUES (
            $1, 'Trade', 'Guy', 'SF', $2, $3,
            $4, 75, 75, 70, 70,
            75, 70, 75, 70, 75, 85,
            24, 30, 50, 50, 60
        )
        RETURNING id
        """,
        league_id, team_id, birth_date, overall,
    )
    return row["id"]


async def _insert_contract(pool, league_id: int, player_id: int, team_id: int, salary: int, years_remaining: int) -> None:
    await pool.execute(
        """
        INSERT INTO contracts (
            league_id, player_id, team_id, salary, years_remaining,
            total_years, contract_type, signed_in_season, is_active
        ) VALUES ($1, $2, $3, $4, $5, $6, 'standard', 2025, true)
        """,
        league_id, player_id, team_id, salary, years_remaining, years_remaining,
    )


async def _insert_pick(pool, league_id: int, team_id: int, season: int, round_num: int) -> int:
    row = await pool.fetchrow(
        """
        INSERT INTO draft_picks (league_id, season, round, original_team_id, current_team_id)
        VALUES ($1, $2, $3, $4, $4)
        RETURNING id
        """,
        league_id, season, round_num, team_id,
    )
    return row["id"]


async def _approved_trade_with_player(pool, league_id, from_team, to_team, player_id) -> int:
    trade = await trade_repo.create_trade(pool, league_id=league_id, season=_CURRENT_SEASON,
                                           proposer_id=from_team, counterparty_id=to_team)
    await trade_repo.add_asset(pool, trade_id=trade.id, from_team_id=from_team, to_team_id=to_team,
                                asset_type="player", player_id=player_id)
    await trade_repo.update_status(pool, trade.id, "approved")
    return trade.id


# ---------------------------------------------------------------------------
# compute_trade_magnitude
# ---------------------------------------------------------------------------

async def test_compute_trade_magnitude_sums_player_and_pick_assets(db_pool):
    if db_pool is None:
        return
    league_id, team_a, team_b = await _setup(db_pool)
    player_id = await _insert_player(db_pool, league_id, team_a, overall=85, age=26)
    await _insert_contract(db_pool, league_id, player_id, team_a, salary=20_000_000, years_remaining=2)
    pick_id = await _insert_pick(db_pool, league_id, team_a, season=_CURRENT_SEASON, round_num=1)

    trade = await trade_repo.create_trade(db_pool, league_id=league_id, season=_CURRENT_SEASON,
                                           proposer_id=team_a, counterparty_id=team_b)
    await trade_repo.add_asset(db_pool, trade_id=trade.id, from_team_id=team_a, to_team_id=team_b,
                                asset_type="player", player_id=player_id)
    await trade_repo.add_asset(db_pool, trade_id=trade.id, from_team_id=team_a, to_team_id=team_b,
                                asset_type="pick", pick_id=pick_id)

    magnitude = await trade_magnitude.compute_trade_magnitude(db_pool, trade.id, _SALARY_CAP, _CURRENT_SEASON)
    assert magnitude > 0
    # Sanity: a real player + a current-season R1 pick should clear a modest floor.
    assert magnitude > 30


async def test_compute_trade_magnitude_handles_player_with_no_active_contract(db_pool):
    """A player asset with no resolvable active contract falls back to a
    neutral contract instead of erroring."""
    if db_pool is None:
        return
    league_id, team_a, team_b = await _setup(db_pool)
    player_id = await _insert_player(db_pool, league_id, team_a, overall=78, age=25)
    # Deliberately no contract row inserted.

    trade = await trade_repo.create_trade(db_pool, league_id=league_id, season=_CURRENT_SEASON,
                                           proposer_id=team_a, counterparty_id=team_b)
    await trade_repo.add_asset(db_pool, trade_id=trade.id, from_team_id=team_a, to_team_id=team_b,
                                asset_type="player", player_id=player_id)

    magnitude = await trade_magnitude.compute_trade_magnitude(db_pool, trade.id, _SALARY_CAP, _CURRENT_SEASON)
    assert magnitude > 0


async def test_compute_trade_magnitude_bigger_star_scores_higher(db_pool):
    if db_pool is None:
        return
    league_id, team_a, team_b = await _setup(db_pool)
    star_id = await _insert_player(db_pool, league_id, team_a, overall=95, age=25)
    await _insert_contract(db_pool, league_id, star_id, team_a, salary=45_000_000, years_remaining=3)
    role_player_id = await _insert_player(db_pool, league_id, team_b, overall=72, age=30)
    await _insert_contract(db_pool, league_id, role_player_id, team_b, salary=6_000_000, years_remaining=1)

    star_trade_id = await _approved_trade_with_player(db_pool, league_id, team_a, team_b, star_id)
    role_trade_id = await _approved_trade_with_player(db_pool, league_id, team_b, team_a, role_player_id)

    star_magnitude = await trade_magnitude.compute_trade_magnitude(db_pool, star_trade_id, _SALARY_CAP, _CURRENT_SEASON)
    role_magnitude = await trade_magnitude.compute_trade_magnitude(db_pool, role_trade_id, _SALARY_CAP, _CURRENT_SEASON)
    assert star_magnitude > role_magnitude


# ---------------------------------------------------------------------------
# rank_trade_in_team_history / rank_trade_in_league_history
# ---------------------------------------------------------------------------

async def test_rank_trade_in_team_history_identifies_the_biggest(db_pool):
    if db_pool is None:
        return
    league_id, team_a, team_b = await _setup(db_pool)
    star_id = await _insert_player(db_pool, league_id, team_a, overall=96, age=24)
    await _insert_contract(db_pool, league_id, star_id, team_a, salary=48_000_000, years_remaining=4)
    scrub_id = await _insert_player(db_pool, league_id, team_a, overall=70, age=32)
    await _insert_contract(db_pool, league_id, scrub_id, team_a, salary=3_000_000, years_remaining=1)

    big_trade_id = await _approved_trade_with_player(db_pool, league_id, team_a, team_b, star_id)
    small_trade_id = await _approved_trade_with_player(db_pool, league_id, team_a, team_b, scrub_id)

    result = await trade_magnitude.rank_trade_in_team_history(
        db_pool, league_id, team_a, big_trade_id, _SALARY_CAP, _CURRENT_SEASON,
    )
    assert result is not None
    assert result["rank"] == 1
    assert result["is_biggest"] is True
    assert result["total_trades"] == 2

    small_result = await trade_magnitude.rank_trade_in_team_history(
        db_pool, league_id, team_a, small_trade_id, _SALARY_CAP, _CURRENT_SEASON,
    )
    assert small_result["rank"] == 2
    assert small_result["is_biggest"] is False


async def test_rank_trade_in_team_history_returns_none_for_unrelated_trade(db_pool):
    if db_pool is None:
        return
    league_id, team_a, team_b = await _setup(db_pool)
    player_id = await _insert_player(db_pool, league_id, team_a)
    await _insert_contract(db_pool, league_id, player_id, team_a, salary=10_000_000, years_remaining=1)
    trade_id = await _approved_trade_with_player(db_pool, league_id, team_a, team_b, player_id)

    # Different, unrelated team has no trade history at all.
    _, other_league_id_teams_a, other_league_id_teams_b = await _setup(db_pool)
    result = await trade_magnitude.rank_trade_in_team_history(
        db_pool, league_id, other_league_id_teams_a, trade_id, _SALARY_CAP, _CURRENT_SEASON,
    )
    assert result is None


async def test_rank_trade_in_league_history_identifies_the_biggest(db_pool):
    if db_pool is None:
        return
    league_id, team_a, team_b = await _setup(db_pool)
    star_id = await _insert_player(db_pool, league_id, team_a, overall=96, age=24)
    await _insert_contract(db_pool, league_id, star_id, team_a, salary=48_000_000, years_remaining=4)
    scrub_id = await _insert_player(db_pool, league_id, team_b, overall=70, age=32)
    await _insert_contract(db_pool, league_id, scrub_id, team_b, salary=3_000_000, years_remaining=1)

    big_trade_id = await _approved_trade_with_player(db_pool, league_id, team_a, team_b, star_id)
    small_trade_id = await _approved_trade_with_player(db_pool, league_id, team_b, team_a, scrub_id)

    result = await trade_magnitude.rank_trade_in_league_history(
        db_pool, league_id, big_trade_id, _SALARY_CAP, _CURRENT_SEASON,
    )
    assert result["rank"] == 1
    assert result["is_biggest"] is True

    small_result = await trade_magnitude.rank_trade_in_league_history(
        db_pool, league_id, small_trade_id, _SALARY_CAP, _CURRENT_SEASON,
    )
    assert small_result["rank"] == 2
