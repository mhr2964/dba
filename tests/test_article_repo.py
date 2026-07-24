"""
Integration tests for data.repositories.article_repo, focused on the
structured_data JSONB column added for Finding #5 (power_list's rank-delta
tracking regex-parsed the LLM's own rendered markdown instead of storing
structured data).

Uses db_pool directly -- article_repo takes pool as an explicit argument
(no module-level get_pool binding to patch).

NOTE on migration bootstrap: conftest.py's session-scoped setup_test_db fixture
runs `alembic upgrade head` from a hardcoded main-checkout path, so a worktree
agent's brand-new migration isn't visible to it (git worktrees each have their
own working-tree files). This module upgrades the test DB to this worktree's
head (which includes migration 046) before its tests run, and downgrades back
to 045 afterward in a try/finally so the shared conftest bootstrap — which
only knows revisions up to 045 from the main checkout — keeps working for
every other test file in the same session, pass or fail.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from data.repositories import article_repo

_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_ALEMBIC_EXE = r"C:\Users\Owner\Desktop\AI\Projects\dba\.venv\Scripts\alembic.exe"
_TEST_DB_URL = "postgresql://dba:dba@localhost:5434/dba_test"


def _db_available() -> bool:
    try:
        import psycopg2
        conn = psycopg2.connect("postgresql://dba:dba@localhost:5434/postgres", connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="Postgres not available at localhost:5434")


@pytest.fixture(scope="module", autouse=True)
def _migrate_to_worktree_head():
    env = os.environ.copy()
    env["DATABASE_URL"] = _TEST_DB_URL
    subprocess.run([_ALEMBIC_EXE, "upgrade", "head"], cwd=_REPO_ROOT, env=env, check=True, capture_output=True)
    try:
        yield
    finally:
        subprocess.run([_ALEMBIC_EXE, "downgrade", "045"], cwd=_REPO_ROOT, env=env, check=True, capture_output=True)


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
