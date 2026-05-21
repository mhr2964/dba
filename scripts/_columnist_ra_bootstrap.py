"""Columnist ride-along bootstrap — create a fresh league ready for sim.

Creates a headless league, assigns one user (the configured Eyeleg user ID or
a synthetic fallback) to the LAL, advances to REGULAR_SEASON_ACTIVE, and
generates the schedule.  Prints the league_id on stdout so the launcher can
capture it.  Exits 0 on success, 1 on failure.

Usage:
    python scripts/_columnist_ra_bootstrap.py --new [--season 2024]
    python scripts/_columnist_ra_bootstrap.py --reuse-league <id>

The launcher calls `--new` on every fresh session.  `--reuse-league` skips
creation and just confirms the league is in REGULAR_SEASON_ACTIVE; useful when
iterating on the same league across multiple run.py restarts.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys
import time
from pathlib import Path

# ── stdout/stderr encoding (Windows safe) ───────────────────────────────────
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

# Synthetic IDs — no real Discord users needed for this bootstrap.
_COMMISSIONER_ID = 2_000_000_001   # distinct from headless_test's ids
_MANAGER_LAL_ID  = 2_000_000_002
# Use a distinct guild_id so this league doesn't conflict with headless_test.
_RA_GUILD_ID     = 2_000_000_000

SEASON_YEAR = 2024
LEAGUE_NAME = "ColumnistRALeague"

_t0 = time.monotonic()


def _progress(msg: str) -> None:
    elapsed = time.monotonic() - _t0
    print(f"[T+{elapsed:.0f}s] {msg}", flush=True)


async def _bootstrap_new(season: int) -> int:
    """Create a fresh league and advance to REGULAR_SEASON_ACTIVE.

    Returns the new league_id.
    """
    import asyncpg
    from data import db as _db

    DATABASE_URL = os.environ.get("DATABASE_URL")
    if not DATABASE_URL:
        print("FATAL: DATABASE_URL not set.", file=sys.stderr)
        sys.exit(1)

    # Wipe any existing RA league so we always start clean.
    _progress(f"Wiping existing guild_id={_RA_GUILD_ID} leagues from DB...")
    raw_conn = await asyncpg.connect(DATABASE_URL)
    try:
        deleted = await raw_conn.execute(
            "DELETE FROM leagues WHERE discord_guild_id = $1", _RA_GUILD_ID
        )
    finally:
        await raw_conn.close()
    _progress(f"Wiped: {deleted}")

    pool = await _db.get_pool()

    # ── Create league ─────────────────────────────────────────────────────────
    _progress(f"Creating league '{LEAGUE_NAME}' season={season} (headless)...")
    from services import league_service
    league = await league_service.create_headless(
        name=LEAGUE_NAME,
        season_year=season,
        commissioner_id=_COMMISSIONER_ID,
        guild_id=_RA_GUILD_ID,
    )
    _progress(f"League created: id={league.id}  phase={league.current_phase}")

    # ── Assign one human manager (LAL → Eyeleg) ───────────────────────────────
    from data.repositories import team_repo, game_repo
    lal = await team_repo.get_by_code(pool, league.id, "LAL")
    if not lal:
        print("FATAL: LAL team not found after league creation.", file=sys.stderr)
        sys.exit(1)
    await team_repo.set_manager(pool, lal.id, _MANAGER_LAL_ID)
    await team_repo.log_manager_change(pool, lal.id, _MANAGER_LAL_ID, assigned_by=_COMMISSIONER_ID)
    _progress(f"LAL assigned to manager id={_MANAGER_LAL_ID}")

    # ── Advance phase ─────────────────────────────────────────────────────────
    _progress("Advancing phase: SETUP → PRESEASON_READY...")
    await league_service.advance_phase(league.id, "PRESEASON_READY")
    _progress("Advancing phase: PRESEASON_READY → REGULAR_SEASON_ACTIVE...")
    await league_service.advance_phase(league.id, "REGULAR_SEASON_ACTIVE")

    # Confirm.
    row = await pool.fetchrow("SELECT current_phase FROM leagues WHERE id = $1", league.id)
    _progress(f"Phase confirmed: {row['current_phase']}")

    # ── Generate schedule ─────────────────────────────────────────────────────
    _progress("Generating schedule...")
    from services import schedule_service
    game_count = await schedule_service.generate_season(league.id, season)
    _progress(f"Schedule: {game_count} games")
    if game_count != 1230:
        print(f"WARNING: expected 1230 games, got {game_count}", file=sys.stderr)

    # ── Mark the managed team ready ───────────────────────────────────────────
    _progress("Marking LAL ready...")
    await game_repo.set_ready(pool, league.id, lal.id, _MANAGER_LAL_ID, True)
    _progress("LAL marked ready.")

    return league.id


async def _reuse_league(league_id: int) -> int:
    """Confirm an existing league is in a sim-able state.  Returns league_id."""
    from data import db as _db

    pool = await _db.get_pool()
    row = await pool.fetchrow(
        "SELECT id, current_phase, name FROM leagues WHERE id = $1", league_id
    )
    if not row:
        print(f"FATAL: league id={league_id} not found.", file=sys.stderr)
        sys.exit(1)

    phase = row["current_phase"]
    active_phases = {
        "REGULAR_SEASON_ACTIVE",
        "TRADE_DEADLINE_OPEN",
        "REGULAR_SEASON_POSTDEADLINE",
    }
    if phase not in active_phases:
        print(
            f"FATAL: league {league_id} is in phase {phase!r}, "
            f"expected one of {active_phases}.",
            file=sys.stderr,
        )
        sys.exit(1)

    _progress(f"Reusing league id={league_id}  name={row['name']!r}  phase={phase}")
    return league_id


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Columnist ride-along bootstrap — create or validate a league."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--new",
        action="store_true",
        help="Create a fresh league (wipes any existing RA league).",
    )
    group.add_argument(
        "--reuse-league",
        type=int,
        metavar="ID",
        help="Reuse an existing league id (must be in REGULAR_SEASON_ACTIVE).",
    )
    parser.add_argument(
        "--season",
        type=int,
        default=SEASON_YEAR,
        help=f"Season year (default: {SEASON_YEAR}).",
    )
    args = parser.parse_args()

    if args.new:
        league_id = await _bootstrap_new(args.season)
    else:
        league_id = await _reuse_league(args.reuse_league)

    # Print the league id on stdout so the launcher can capture it.
    print(f"LEAGUE_ID={league_id}", flush=True)
    _progress("Bootstrap complete.")


if __name__ == "__main__":
    asyncio.run(main())
