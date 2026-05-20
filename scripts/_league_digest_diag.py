"""Diagnostic for services/league_digest — Phase 6 verification.

Connects to the current active league, calls build_league_digest with
since_batches=7 (default), and prints the resulting dict.

Usage:
    python scripts/_league_digest_diag.py [since_batches]
    python scripts/_league_digest_diag.py        # defaults to 7 batches
    python scripts/_league_digest_diag.py 14     # look back 14 batches
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys

import asyncpg
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
load_dotenv()

from data.repositories import league_repo  # noqa: E402
from services import league_digest  # noqa: E402


async def main(since_batches: int) -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)

    pool = await asyncpg.create_pool(db_url)

    # Resolve the current active league.
    raw = await pool.fetchrow(
        "SELECT * FROM leagues WHERE archived_at IS NULL ORDER BY id DESC LIMIT 1"
    )
    if not raw:
        print("No active league found.")
        await pool.close()
        return

    league = league_repo._league_from_record(raw)
    season = league.current_season
    print(f"League: {league.name}  (id={league.id}, season={season})")

    # Resolve current max batch index.
    max_batch_row = await pool.fetchrow(
        """
        SELECT MAX(sim_batch_index) AS max_idx
        FROM team_state_snapshots
        WHERE league_id = $1 AND season = $2
        """,
        league.id, season,
    )
    max_idx = max_batch_row["max_idx"] if max_batch_row and max_batch_row["max_idx"] is not None else 0
    since_batch_index = max(0, max_idx - since_batches)

    print(f"Looking back {since_batches} batches: max_idx={max_idx}  since_batch_index={since_batch_index}")
    print()

    result = await league_digest.build_league_digest(pool, league, season, since_batch_index)
    print(json.dumps(result, indent=2, default=str))
    await pool.close()


if __name__ == "__main__":
    batches = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    asyncio.run(main(batches))
