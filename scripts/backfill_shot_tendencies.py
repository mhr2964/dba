"""One-shot backfill: populate tendency_3pt, tendency_drive, tendency_pass
for all players in the latest league (or a specified league) using the same
formulas now in roster_seed_service._compute_stat_tendencies.

These three columns were always written as the DB default (50) because the
seeding pipeline never computed them.  This script fills them in from:
  1. Pre-built tendency snapshot (data/tendency_cache/season_N.json) — preferred.
  2. BDL base stats cache (data/bdl_cache/season_N_base.json) — fallback.

Players for whom neither source has data keep their existing values.

Usage:
    python scripts/backfill_shot_tendencies.py
    python scripts/backfill_shot_tendencies.py --league-id 52 --season 2024
    python scripts/backfill_shot_tendencies.py --league-id 52 --season 2024 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import unicodedata

import asyncpg
from dotenv import load_dotenv

load_dotenv()

_ROOT = pathlib.Path(__file__).parent.parent
_BDL_CACHE_DIR = _ROOT / "data" / "bdl_cache"
_TEND_CACHE_DIR = _ROOT / "data" / "tendency_cache"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _norm_name(name: str) -> str:
    """Lowercase, strip accents, collapse whitespace."""
    return (
        unicodedata.normalize("NFKD", name)
        .encode("ascii", "ignore")
        .decode()
        .lower()
        .strip()
    )


def _load_snap(season: int) -> dict[str, dict]:
    """Return {normalized_name: tendency_dict} from pre-built snapshot, or {}."""
    path = _TEND_CACHE_DIR / f"season_{season}.json"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        # Keys may already be normalized; ensure they are.
        return {_norm_name(k): v for k, v in raw.items()}
    except (json.JSONDecodeError, OSError):
        return {}


def _load_bdl_base(season: int) -> dict[str, dict]:
    """Return {normalized_full_name: stats_dict} from BDL base cache, or {}."""
    path = _BDL_CACHE_DIR / f"season_{season}_base.json"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            records = json.load(f)
        lookup: dict[str, dict] = {}
        for rec in records:
            p = rec.get("player", {})
            full = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            norm = _norm_name(full)
            lookup[norm] = rec.get("stats", {})
        return lookup
    except (json.JSONDecodeError, KeyError, OSError):
        return {}


def _compute_from_snap(snap_entry: dict) -> tuple[int, int, int]:
    """Extract the three shot tendencies from a snapshot entry."""
    t3   = int(snap_entry.get("tendency_3pt",   50) or 50)
    tdrv = int(snap_entry.get("tendency_drive",  50) or 50)
    tps  = int(snap_entry.get("tendency_pass",   50) or 50)
    return t3, tdrv, tps


def _compute_from_stats(stats: dict) -> tuple[int, int, int]:
    """Compute the three shot tendencies from BDL per-game stats."""
    fga = float(stats.get("fga") or 0.0)
    tpa = float(stats.get("tpa") or 0.0)
    fta = float(stats.get("fta") or 0.0)
    ast = float(stats.get("ast") or 0.0)
    pts = float(stats.get("pts") or 0.0)

    three_rate = min(tpa / max(fga, 1.0), 1.0)
    tendency_3pt = max(5, min(95, round(three_rate * 100)))

    drive_rate = min(fta / max(fga, 1.0), 1.0)
    tendency_drive = max(5, min(95, round(drive_rate * 80)))

    tendency_pass = max(5, min(95, round(min(ast / max(pts / 10, 0.5), 1.0) * 80)))

    return tendency_3pt, tendency_drive, tendency_pass


# ── Main ─────────────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill tendency_3pt / tendency_drive / tendency_pass for an existing league"
    )
    parser.add_argument("--league-id", type=int, default=None,
                        help="League ID to update (default: latest league)")
    parser.add_argument("--season", type=int, default=2024,
                        help="BDL season year (default: 2024)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print changes without writing to DB")
    args = parser.parse_args()

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise SystemExit("DATABASE_URL not set")

    snap = _load_snap(args.season)
    bdl  = _load_bdl_base(args.season)
    print(f"Snapshot entries: {len(snap)}  |  BDL base entries: {len(bdl)}")

    conn = await asyncpg.connect(db_url)
    try:
        # Resolve league ID
        if args.league_id is not None:
            league = await conn.fetchrow(
                "SELECT id, current_season FROM leagues WHERE id = $1",
                args.league_id,
            )
        else:
            league = await conn.fetchrow(
                "SELECT id, current_season FROM leagues ORDER BY id DESC LIMIT 1"
            )
        if not league:
            raise SystemExit("No league found")

        league_id: int = league["id"]
        print(f"League {league_id} (season={args.season})  dry_run={args.dry_run}\n")

        rows = await conn.fetch(
            """
            SELECT id,
                   first_name,
                   last_name,
                   tendency_3pt,
                   tendency_drive,
                   tendency_pass
            FROM players
            WHERE league_id = $1
            ORDER BY last_name, first_name
            """,
            league_id,
        )

        updated = 0
        skipped_no_data = 0
        spot_check_names = {"stephen curry", "joel embiid", "nikola jokic", "giannis antetokounmpo"}
        spot_rows: list[tuple[str, int, int, int]] = []

        for row in rows:
            fname = row["first_name"] or ""
            lname = row["last_name"] or ""
            full  = f"{fname} {lname}".strip()
            norm  = _norm_name(full)

            t3: int | None = None
            tdrv: int | None = None
            tps: int | None = None

            if norm in snap:
                t3, tdrv, tps = _compute_from_snap(snap[norm])
            elif norm in bdl:
                t3, tdrv, tps = _compute_from_stats(bdl[norm])

            if t3 is None:
                skipped_no_data += 1
                continue

            if not args.dry_run:
                await conn.execute(
                    """
                    UPDATE players
                    SET tendency_3pt   = $1,
                        tendency_drive = $2,
                        tendency_pass  = $3
                    WHERE id = $4
                    """,
                    t3, tdrv, tps, row["id"],
                )

            updated += 1

            if norm in spot_check_names:
                safe = full.encode("ascii", "replace").decode("ascii")
                spot_rows.append((safe, t3, tdrv, tps))

        # Summary
        print(f"Players updated:        {updated}")
        print(f"Skipped (no BDL data):  {skipped_no_data}")
        if args.dry_run:
            print("\n[DRY RUN] No changes written.")

        if spot_rows:
            print("\nSpot-check (stars):")
            print(f"  {'Name':<30}  {'3pt':>4}  {'drive':>5}  {'pass':>4}")
            print(f"  {'-'*30}  {'-'*4}  {'-'*5}  {'-'*4}")
            for name, t3, tdrv, tps in sorted(spot_rows):
                print(f"  {name:<30}  {t3:>4}  {tdrv:>5}  {tps:>4}")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
