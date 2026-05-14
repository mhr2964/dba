"""Recompute player OVR ratings for an existing league.

Reads from data/stats_ratings/{season}.json if present (built by build_stats_ratings.py).
Falls back to live multi-season API fetch if the file is absent.

Usage:
    python scripts/recompute_ratings.py --league-id 9 --season 2024
    python scripts/recompute_ratings.py --league-id 9 --season 2024 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import os
import time

import asyncpg
from dotenv import load_dotenv

from import_players import (
    _load_stats_ratings,
    _fetch_multi_season_peak,
    _fetch_draft_class_sync,
    _overall_from_stats,
)

load_dotenv()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--league-id", type=int, required=True)
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise SystemExit("DATABASE_URL not set")

    # Try pre-built file first; fall back to live API if absent.
    prebuilt: dict[int, int] = _load_stats_ratings(args.season)
    peak_lookup: dict[int, dict] = {}
    draft_info: dict[int, dict] = {}

    if not prebuilt:
        print(f"No pre-built file found — fetching live stats...")
        try:
            peak_lookup = _fetch_multi_season_peak(args.season)
            print(f"  Peak stats resolved for {len(peak_lookup)} players.")
        except Exception as exc:
            print(f"[WARN] Stats fetch failed: {exc}")
        time.sleep(0.5)
        try:
            draft_info = _fetch_draft_class_sync(args.season)
            print(f"  Draft info loaded for {len(draft_info)} players.")
        except Exception as exc:
            print(f"[WARN] DraftHistory failed: {exc}")

    conn = await asyncpg.connect(db_url)
    try:
        players = await conn.fetch(
            "SELECT id, first_name, last_name, external_id, years_pro, overall, is_rookie "
            "FROM players WHERE league_id = $1",
            args.league_id,
        )

        updated = skipped = 0
        for p in players:
            ext_id = int(p["external_id"]) if p["external_id"] else None
            if ext_id is None:
                skipped += 1
                continue

            if ext_id in prebuilt:
                new_ovr = prebuilt[ext_id]
            else:
                new_ovr = _overall_from_stats(
                    ext_id, peak_lookup, p["years_pro"],
                    draft_info=draft_info if p["is_rookie"] else None,
                )

            if new_ovr == p["overall"]:
                skipped += 1
                continue

            print(f"  {p['first_name']} {p['last_name']}: {p['overall']} -> {new_ovr}")
            if not args.dry_run:
                await conn.execute(
                    "UPDATE players SET overall = $1 WHERE id = $2",
                    new_ovr, p["id"],
                )
            updated += 1

        print(f"\nUpdated: {updated}  |  Unchanged/skipped: {skipped}")
        if args.dry_run:
            print("[DRY RUN] No changes written.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
