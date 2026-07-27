from __future__ import annotations

import itertools
import os
import subprocess
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import discord
import psycopg2
import pytest

TEST_DB_URL = "postgresql://dba:dba@localhost:5434/dba_test"
DBA_ROOT = r"C:\Users\Owner\Desktop\AI\Projects\dba"


def _check_db_available() -> bool:
    """Probe Postgres at collection time so skip decisions can be made early."""
    try:
        conn = psycopg2.connect(
            "postgresql://dba:dba@localhost:5434/postgres",
            connect_timeout=2,
        )
        conn.close()
        return True
    except Exception:
        return False


_DB_AVAILABLE = _check_db_available()


def pytest_collection_modifyitems(items):
    """Skip tests that explicitly request db_pool when Postgres is unavailable."""
    if _DB_AVAILABLE:
        return
    skip_marker = pytest.mark.skip(reason="Postgres not available at localhost:5434")
    for item in items:
        if "db_pool" in getattr(item, "fixturenames", []):
            item.add_marker(skip_marker, append=False)


# ---------------------------------------------------------------------------
# DB bootstrap — sync, session-scoped so migrations run exactly once.
# Skips gracefully when Postgres is not reachable (e.g. CI without DB).
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    if not _DB_AVAILABLE:
        return

    conn = psycopg2.connect("postgresql://dba:dba@localhost:5434/postgres")
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = 'dba_test'")
    if not cur.fetchone():
        cur.execute("CREATE DATABASE dba_test")
    cur.close()
    conn.close()

    env = os.environ.copy()
    env["DATABASE_URL"] = TEST_DB_URL
    subprocess.run(
        [os.path.join(DBA_ROOT, r".venv\Scripts\alembic.exe"), "upgrade", "head"],
        cwd=DBA_ROOT,
        env=env,
        check=True,
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# Per-test pool — function-scoped so pool and event loop always match.
# Yields None when DB is unavailable; callers must handle None or skip.
# ---------------------------------------------------------------------------

@pytest.fixture
async def db_pool():
    if not _DB_AVAILABLE:
        yield None
        return
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=3)
    yield pool
    await pool.close()


@pytest.fixture(autouse=True)
async def clean_db(db_pool):
    if db_pool is None:
        yield
        return
    await db_pool.execute(
        "TRUNCATE leagues RESTART IDENTITY CASCADE"
    )
    yield


# Every module below binds its own module-level `get_pool` at import time
# (no shared registry), so each one needs its own patch target here. Kept as
# a plain list + ExitStack loop rather than one long parenthesized `with (...)`
# -- a 19th entry previously tipped that form into a CPython 3.12 codegen
# crash (segfault) once combined with pytest's assertion-rewrite recompile of
# this file; a loop has no such ceiling.
_GET_POOL_PATCH_TARGETS = [
    "data.db.get_pool",
    "services.league_service.get_pool",
    "services.roster_service.get_pool",
    "services.awards_service.get_pool",
    "services.trade_service.get_pool",
    "services.fa_service.get_pool",
    "services.progression_service.get_pool",
    "services.rollover_service.get_pool",
    "services.draft_service.get_pool",
    "services.playoff_service.get_pool",
    "services.schedule_service.get_pool",
    "services.notifier_service.get_pool",
    "bot.cogs.setup_cog.get_pool",
    "bot.cogs.team_cog.get_pool",
    "bot.cogs.roster_cog.get_pool",
    "bot.cogs.awards_cog.get_pool",
    "bot.cogs.draft_cog.get_pool",
    "bot.cogs.offseason_cog.get_pool",
    "bot.cogs.strategy_cog.get_pool",
    "bot.cogs.feedback_cog.get_pool",
    "bot.cogs.admin_data_ops.get_pool",
    "bot.cogs.extension_cog.get_pool",
    "bot.cogs.trade_cog.get_pool",
    "bot.cogs.season_cog.get_pool",
    "bot.cogs.stats_cog.get_pool",
    "bot.cogs.fa_cog.get_pool",
    "bot.ui.stats_views.get_pool",
    "phase.helpers.get_pool",
]


@pytest.fixture(autouse=True)
async def patch_get_pool(db_pool):
    if db_pool is None:
        yield
        return
    pool_mock = AsyncMock(return_value=db_pool)
    with ExitStack() as stack:
        for target in _GET_POOL_PATCH_TARGETS:
            stack.enter_context(patch(target, pool_mock))
        yield


# ---------------------------------------------------------------------------
# Discord mock fixtures
# ---------------------------------------------------------------------------

_CHANNEL_COUNTER = itertools.count(200)
_ROLE_COUNTER = itertools.count(300)


@pytest.fixture
def mock_guild():
    guild = MagicMock(spec=discord.Guild)
    guild.id = 999001
    guild.name = "Test Server"
    # league_service._create_locked compares bot_top_role.position > 1;
    # MagicMock doesn't support __gt__ with int, so stub it as an int.
    # Added 2026-05-20 after _create_locked role-reorder logic was added.
    guild.me.top_role.position = 99

    _created_roles: dict[int, MagicMock] = {}

    async def _create_role(**kwargs):
        role = MagicMock(spec=discord.Role)
        role.id = next(_ROLE_COUNTER)
        role.name = kwargs.get("name", "role")
        _created_roles[role.id] = role
        return role

    async def _create_category(name, **kwargs):
        category = MagicMock(spec=discord.CategoryChannel)
        category.id = 111
        category.name = name

        async def _create_text_channel(ch_name, **kw):
            ch = MagicMock(spec=discord.TextChannel)
            ch.id = next(_CHANNEL_COUNTER)
            ch.name = ch_name
            return ch

        category.create_text_channel = _create_text_channel
        return category

    guild.create_category = _create_category
    guild.create_role = _create_role
    guild.get_role = lambda role_id: _created_roles.get(role_id)
    guild.get_member = MagicMock(return_value=None)
    guild.get_channel = MagicMock(return_value=None)
    return guild


@pytest.fixture
def mock_commissioner():
    member = MagicMock(spec=discord.Member)
    member.id = 12345
    member.mention = "<@12345>"
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    return member


@pytest.fixture
def mock_manager():
    member = MagicMock(spec=discord.Member)
    member.id = 67890
    member.mention = "<@67890>"
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    return member


@pytest.fixture
def mock_interaction(mock_guild, mock_commissioner):
    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = mock_guild
    interaction.guild_id = mock_guild.id
    interaction.user = mock_commissioner
    interaction.response = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction
