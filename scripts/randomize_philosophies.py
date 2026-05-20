"""One-shot script: randomize coach_philosophy for all teams in an existing league,
then re-derive roles so the DB reflects the new philosophies immediately.

Run against the current dev league to see variance without recreating the league:

    .venv/Scripts/python.exe scripts/randomize_philosophies.py

Dry-run (print planned assignments without writing):

    .venv/Scripts/python.exe scripts/randomize_philosophies.py --dry-run

Use --seed N for reproducible assignment (useful in tests):

    .venv/Scripts/python.exe scripts/randomize_philosophies.py --seed 42
"""
from __future__ import annotations

import argparse
import asyncio
import io
import os
import random
import sys

import asyncpg
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

load_dotenv()

from services.role_service import (  # noqa: E402
    COACH_PHILOSOPHIES,
    derive_and_persist_all_for_team,
)


async def go(dry_run: bool = False, seed: int | None = None) -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])

    lg = await conn.fetchval("SELECT id FROM leagues ORDER BY id DESC LIMIT 1")
    if lg is None:
        print("No leagues found.")
        await conn.close()
        return

    season = await conn.fetchval(
        "SELECT COALESCE(MAX(season), 2024) FROM games WHERE league_id = $1", lg
    )

    team_rows = await conn.fetch(
        "SELECT id, nba_team_code FROM teams WHERE league_id = $1 ORDER BY nba_team_code",
        lg,
    )

    rng = random.Random(seed) if seed is not None else random.Random()

    assignments: list[tuple[int, str, str]] = []  # (team_id, code, philosophy)
    for row in team_rows:
        philosophy = rng.choice(COACH_PHILOSOPHIES)
        assignments.append((row["id"], row["nba_team_code"], philosophy))

    print(f"League {lg}  season {season}  dry_run={dry_run}  seed={seed}")
    print(f"{'team':<6}{'philosophy'}")
    print("-" * 30)
    for _tid, code, phil in assignments:
        print(f"{code:<6}{phil}")

    if dry_run:
        print("\n[dry-run] no changes written")
        await conn.close()
        return

    print("\nWriting philosophies…")
    for team_id, code, philosophy in assignments:
        await conn.execute(
            "UPDATE teams SET coach_philosophy = $1 WHERE id = $2",
            philosophy,
            team_id,
        )

    print("Re-deriving roles for all teams…")
    errors = 0
    for team_id, code, philosophy in assignments:
        try:
            await derive_and_persist_all_for_team(conn, lg, team_id, season)
            print(f"  {code}: OK ({philosophy})")
        except Exception as exc:
            print(f"  {code}: ERROR — {exc}")
            errors += 1

    await conn.close()
    print(f"\nDone. {len(assignments)} teams updated, {errors} errors.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Randomize coach philosophies for current league")
    parser.add_argument("--dry-run", action="store_true", help="Print planned assignments without writing")
    parser.add_argument("--seed", type=int, default=None, help="RNG seed for reproducible assignment")
    args = parser.parse_args()
    asyncio.run(go(dry_run=args.dry_run, seed=args.seed))
