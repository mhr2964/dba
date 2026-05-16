"""One-shot calibration fix for league 30 (and any league ID you specify).

Applies three stat calibration corrections to existing player rows:

1. Rebounding cap by position — guards were showing 7 RPG (Curry IRL = 4.5).
   Caps reb_tendency to position max: PG=45, SG=50, SF=65, PF=80, C=90.

2. Defense tendency from BDL stl+blk data — players like Luka with low stl/blk
   per game get a low defense_tendency (35-50) rather than inheriting their high
   OVR-based defense attribute. The sim engine now uses defense_tendency for
   stl/blk weight instead of the raw defense attribute.

3. Updates stl_tendency and blk_tendency position caps so guards can't block at
   center rates: PG blk_tendency max=40, SG=45, SF=60, PF=80, C=90.

Usage:
    python scripts/fix_stat_calibration.py --league-id 30 --season 2024
    python scripts/fix_stat_calibration.py --league-id 30 --season 2024 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg
from dotenv import load_dotenv

load_dotenv()

# Add project root to path so services/ imports resolve.
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))

from services.roster_seed_service import (
    _REB_TENDENCY_CAP,
    _STL_TENDENCY_CAP,
    _BLK_TENDENCY_CAP,
    _compute_stat_tendencies,
    _load_bdl_base,
)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Fix stat calibration for existing league")
    parser.add_argument("--league-id", type=int, required=True)
    parser.add_argument("--season", type=int, required=True,
                        help="BDL season year (e.g. 2024 for 2024-25 season)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print changes without writing to DB")
    args = parser.parse_args()

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise SystemExit("DATABASE_URL not set")

    conn = await asyncpg.connect(db_url)
    try:
        league = await conn.fetchrow(
            "SELECT id FROM leagues WHERE id = $1", args.league_id
        )
        if not league:
            raise SystemExit(f"League {args.league_id} not found")

        print(f"League {args.league_id}  season={args.season}  dry_run={args.dry_run}")

        # Ensure defense_tendency column exists (migration 029 may not have run yet
        # if this script is run before deploy; ADD COLUMN IF NOT EXISTS is safe).
        if not args.dry_run:
            await conn.execute(
                "ALTER TABLE players ADD COLUMN IF NOT EXISTS "
                "defense_tendency SMALLINT NOT NULL DEFAULT 50"
            )

        rows = await conn.fetch(
            """
            SELECT id, first_name, last_name, position,
                   external_id,
                   reb_tendency, stl_tendency, blk_tendency,
                   COALESCE(defense_tendency, 50) AS defense_tendency
            FROM players
            WHERE league_id = $1
            ORDER BY id
            """,
            args.league_id,
        )
        print(f"Players fetched: {len(rows)}")

        # Pre-load BDL data for the season.
        by_id = _load_bdl_base(args.season)
        print(f"BDL records loaded: {len(by_id)}")

        updated = 0
        skipped_no_bdl = 0
        capped_reb = 0
        capped_stl = 0
        capped_blk = 0

        for row in rows:
            pid = row["id"]
            full_pos = (row["position"] or "SF").upper()
            ext_id = row["external_id"]

            # Compute fresh tendencies from BDL (includes all caps + defense_tendency).
            new_tend = _compute_stat_tendencies(ext_id, args.season, full_pos)

            old_reb = row["reb_tendency"]
            old_stl = row["stl_tendency"]
            old_blk = row["blk_tendency"]
            old_def = row["defense_tendency"]

            new_reb = new_tend["reb_tendency"]
            new_stl = new_tend["stl_tendency"]
            new_blk = new_tend["blk_tendency"]
            new_def = new_tend.get("defense_tendency", 50)

            name = f"{row['first_name']} {row['last_name']}".strip()

            if ext_id is None or not str(ext_id).isdigit():
                skipped_no_bdl += 1
                # Still apply position hard-caps to whatever the DB has.
                cap_reb = min(old_reb, _REB_TENDENCY_CAP.get(full_pos, 65))
                cap_stl = min(old_stl, _STL_TENDENCY_CAP.get(full_pos, 65))
                cap_blk = min(old_blk, _BLK_TENDENCY_CAP.get(full_pos, 60))
                if not args.dry_run:
                    await conn.execute(
                        """UPDATE players
                           SET reb_tendency = $1,
                               stl_tendency = $2,
                               blk_tendency = $3
                           WHERE id = $4""",
                        cap_reb, cap_stl, cap_blk, pid,
                    )
                continue

            changed = (
                new_reb != old_reb or new_stl != old_stl
                or new_blk != old_blk or new_def != old_def
            )

            if old_reb != new_reb:
                capped_reb += 1
            if old_stl != new_stl:
                capped_stl += 1
            if old_blk != new_blk:
                capped_blk += 1

            if args.dry_run and changed:
                safe_name = name.encode("ascii", "replace").decode("ascii")
                print(
                    f"  {safe_name:<30} [{full_pos}]  "
                    f"reb {old_reb}->{new_reb}  "
                    f"stl {old_stl}->{new_stl}  "
                    f"blk {old_blk}->{new_blk}  "
                    f"def {old_def}->{new_def}"
                )
                continue

            if not args.dry_run and changed:
                await conn.execute(
                    """UPDATE players
                       SET reb_tendency      = $1,
                           stl_tendency      = $2,
                           blk_tendency      = $3,
                           defense_tendency  = $4
                       WHERE id = $5""",
                    new_reb, new_stl, new_blk, new_def, pid,
                )
                updated += 1

        print()
        print(f"Players updated:        {updated}")
        print(f"Skipped (no BDL ext_id): {skipped_no_bdl} (position caps applied)")
        print(f"reb_tendency changed:   {capped_reb}")
        print(f"stl_tendency changed:   {capped_stl}")
        print(f"blk_tendency changed:   {capped_blk}")

        if args.dry_run:
            print()
            print("[DRY RUN] No changes written.")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
