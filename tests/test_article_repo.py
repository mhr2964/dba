"""
Integration tests for data.repositories.article_repo, focused on the
structured_data JSONB column added for Finding #5 (power_list's rank-delta
tracking regex-parsed the LLM's own rendered markdown instead of storing
structured data).

Uses db_pool directly -- article_repo takes pool as an explicit argument
(no module-level get_pool binding to patch). Migration 046 (structured_data)
is part of master now, so conftest.py's session-scoped `alembic upgrade head`
bootstrap already covers it -- no per-file migrate/downgrade dance needed.
"""
from __future__ import annotations

import pytest

from data.repositories import article_repo


def _db_available() -> bool:
    try:
        import psycopg2
        conn = psycopg2.connect("postgresql://dba:dba@localhost:5434/postgres", connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="Postgres not available at localhost:5434")


async def _insert_test_league(pool) -> int:
    row = await pool.fetchrow(
        """
        INSERT INTO leagues (discord_guild_id, name, start_season_year, current_season, commissioner_user_id)
        VALUES (666001, 'Article Repo Test League', 2025, 2025, 11111)
        RETURNING id
        """,
    )
    return row["id"]


async def test_insert_and_read_back_structured_data(db_pool):
    league_id = await _insert_test_league(db_pool)

    await article_repo.insert(
        db_pool, league_id=league_id, season=2025,
        persona_id="power_list", category="power_rankings",
        headline="Thunder Move to No. 1", body="rendered body text",
        structured_data={"OKC": 1, "BOS": 2, "DEN": 3},
    )

    rows = await article_repo.recent_by_persona(db_pool, league_id, "power_list", limit=1, season=2025)
    assert len(rows) == 1
    assert rows[0]["structured_data"] == {"OKC": 1, "BOS": 2, "DEN": 3}


async def test_insert_without_structured_data_reads_back_none(db_pool):
    league_id = await _insert_test_league(db_pool)

    await article_repo.insert(
        db_pool, league_id=league_id, season=2025,
        persona_id="jordan_rivera", category="hot_take",
        headline="Some Take", body="body text",
    )

    rows = await article_repo.recent_by_persona(db_pool, league_id, "jordan_rivera", limit=1, season=2025)
    assert len(rows) == 1
    assert rows[0]["structured_data"] is None


async def test_recent_about_team_also_decodes_structured_data(db_pool):
    league_id = await _insert_test_league(db_pool)
    team_row = await db_pool.fetchrow(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'OKC', 'Thunder', 'Oklahoma City', 'West', 'Northwest')
        RETURNING id
        """,
        league_id,
    )
    team_id = team_row["id"]

    await article_repo.insert(
        db_pool, league_id=league_id, season=2025,
        persona_id="power_list", category="power_rankings",
        headline="Thunder Move to No. 1", body="rendered body text",
        subject_team_ids=[team_id],
        structured_data={"OKC": 1},
    )

    rows = await article_repo.recent_about_team(db_pool, league_id, team_id, limit=1)
    assert len(rows) == 1
    assert rows[0]["structured_data"] == {"OKC": 1}
