"""One-off utility: update existing player overall ratings from a 2K ratings file.

Usage:
    python scripts/update_player_ratings.py --league-id 1 --edition 2k26
    python scripts/update_player_ratings.py --league-id 1 --edition 2k26 --dry-run

Run after fetch_2k_ratings.py has produced a ratings file, or when re-seeding an
existing league with better ratings.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Optional

import asyncpg
from dotenv import load_dotenv

load_dotenv()

_RATINGS_DIR = Path(__file__).parent.parent / "data" / "2k_ratings"


def _normalize(name: str) -> str:
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", ascii_name.strip().lower())


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--league-id", type=int, required=True)
    parser.add_argument("--edition", default="2k26")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    path = _RATINGS_DIR / f"{args.edition}.json"
    if not path.exists():
        raise SystemExit(f"Ratings file not found: {path}")

    with open(path, encoding="utf-8") as f:
        ratings: dict[str, int] = json.load(f)

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise SystemExit("DATABASE_URL not set")

    conn = await asyncpg.connect(db_url)
    try:
        players = await conn.fetch(
            "SELECT id, first_name, last_name, overall FROM players WHERE league_id = $1",
            args.league_id,
        )
        updated = skipped = not_found = 0
        for p in players:
            full = _normalize(f"{p['first_name']} {p['last_name']}")
            new_ovr = ratings.get(full)
            if new_ovr is None:
                not_found += 1
                continue
            if new_ovr == p["overall"]:
                skipped += 1
                continue
            print(f"  {p['first_name']} {p['last_name']}: {p['overall']} → {new_ovr}")
            if not args.dry_run:
                await conn.execute(
                    "UPDATE players SET overall = $1 WHERE id = $2",
                    new_ovr, p["id"],
                )
            updated += 1

        print(f"\nUpdated: {updated}  |  Unchanged: {skipped}  |  Not in file: {not_found}")
        if args.dry_run:
            print("[DRY RUN] No changes written.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
