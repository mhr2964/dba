"""Backfill defense_tendency, blk_tendency, stl_tendency for the current league.

Uses the same logic as the primary tendency pipeline:
  1. Pre-built tendency snapshot (data/tendency_cache/season_N.json) — preferred.
  2. BDL base stats cache + position-aware formula (Fix 1) — fallback.

The snapshot was rebuilt with position-aware defense weights before this script
runs, so Gobert/Wemby/Bam will now receive bloc-weighted C values rather than
the old flat stl*0.60+blk*0.40 composite.

Usage:
    python scripts/backfill_defensive_tendencies.py
    python scripts/backfill_defensive_tendencies.py --league-id 52 --season 2024
    python scripts/backfill_defensive_tendencies.py --league-id 52 --season 2024 --dry-run
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
_BDL_CACHE_DIR  = _ROOT / "data" / "bdl_cache"
_TEND_CACHE_DIR = _ROOT / "data" / "tendency_cache"

# Spot-check targets — confirm these look correct after backfill.
_SPOT_CHECK_NAMES = {
    "rudy gobert",
    "victor wembanyama",
    "bam adebayo",
    "anthony davis",
    "jrue holiday",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm(name: str) -> str:
    return " ".join(
        unicodedata.normalize("NFKD", name)
        .encode("ascii", "ignore")
        .decode()
        .lower()
        .replace(".", "")
        .replace("'", "")
        .strip()
        .split()
    )


def _load_snapshot(season: int) -> dict[str, dict]:
    """Return {normalized_name: tendency_dict} from pre-built snapshot, or {}."""
    path = _TEND_CACHE_DIR / f"season_{season}.json"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        return {_norm(k): v for k, v in raw.items()}
    except (json.JSONDecodeError, OSError):
        return {}


def _load_bdl_base(season: int) -> tuple[dict[str, dict], dict[str, float], dict[str, float]]:
    """Return (by_name_stats, C_stl_avg, C_blk_avg) from BDL base cache.

    Computes per-position-bucket averages for stl/blk so the fallback formula
    mirrors what _compute_stat_tendencies does in roster_seed_service.
    """
    path = _BDL_CACHE_DIR / f"season_{season}_base.json"
    if not path.exists():
        return {}, {}, {}
    try:
        with open(path, encoding="utf-8") as f:
            records = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}, {}, {}

    by_name: dict[str, dict] = {}
    pos_stl: dict[str, list[float]] = {"G": [], "F": [], "C": []}
    pos_blk: dict[str, list[float]] = {"G": [], "F": [], "C": []}

    for rec in records:
        p = rec["player"]
        full = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        key = _norm(full)
        stats = rec.get("stats", {})
        by_name[key] = {"stats": stats, "position": p.get("position") or "G"}

        gp = int(stats.get("gp", 0) or 0)
        if gp < 10:
            continue
        raw_pos = (p.get("position") or "G").upper()
        pos = raw_pos[-1] if raw_pos[-1] in ("G", "F", "C") else "G"
        stl = stats.get("stl")
        blk = stats.get("blk")
        if stl is not None:
            pos_stl[pos].append(float(stl))
        if blk is not None:
            pos_blk[pos].append(float(blk))

    stl_avgs = {
        p: (sum(vals) / len(vals) if vals else 1.0)
        for p, vals in pos_stl.items()
    }
    blk_avgs = {
        p: (sum(vals) / len(vals) if vals else 1.0)
        for p, vals in pos_blk.items()
    }

    return by_name, stl_avgs, blk_avgs


def _defense_tendency_from_stats(
    stl: float,
    blk: float,
    stl_avg: float,
    blk_avg: float,
    pos_bucket: str,
) -> int:
    """Position-aware defense_tendency formula (mirrors Fix 1 in roster_seed_service).

    C:  30% stl + 70% blk
    F:  50% / 50%
    G:  70% stl + 30% blk
    """
    if pos_bucket == "C":
        w_stl, w_blk = 0.30, 0.70
    elif pos_bucket == "F":
        w_stl, w_blk = 0.50, 0.50
    else:  # G
        w_stl, w_blk = 0.70, 0.30
    stl_ratio = stl / max(stl_avg, 0.01)
    blk_ratio = blk / max(blk_avg, 0.01)
    raw = round((stl_ratio * w_stl + blk_ratio * w_blk) * 50)
    return max(5, min(85, raw))


def _blk_stl_tend(val: float, avg: float) -> int:
    raw = round((val / max(avg, 0.01)) * 50)
    return max(5, min(95, raw))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill defense_tendency / blk_tendency / stl_tendency for an existing league"
    )
    parser.add_argument("--league-id", type=int, default=None,
                        help="League ID to update (default: latest)")
    parser.add_argument("--season", type=int, default=2024,
                        help="BDL season year (default: 2024)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print changes without writing to DB")
    args = parser.parse_args()

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise SystemExit("DATABASE_URL not set")

    snap = _load_snapshot(args.season)
    bdl_by_name, stl_avgs, blk_avgs = _load_bdl_base(args.season)
    print(f"Snapshot entries: {len(snap)}  |  BDL base entries: {len(bdl_by_name)}")

    conn = await asyncpg.connect(db_url)
    try:
        # Resolve league
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
        print(f"League {league_id}  season={args.season}  dry_run={args.dry_run}\n")

        rows = await conn.fetch(
            """
            SELECT id, first_name, last_name, position,
                   defense_tendency, blk_tendency, stl_tendency
            FROM players
            WHERE league_id = $1
            ORDER BY last_name, first_name
            """,
            league_id,
        )

        updated = 0
        skipped_no_data = 0
        spot_rows: list[tuple[str, int, int, int, int, int, int]] = []

        for row in rows:
            fname = row["first_name"] or ""
            lname = row["last_name"] or ""
            full  = f"{fname} {lname}".strip()
            norm_key = _norm(full)

            new_def: int | None = None
            new_blk: int | None = None
            new_stl: int | None = None

            if norm_key in snap:
                entry = snap[norm_key]
                new_def = int(entry.get("defense_tendency") or 50)
                new_blk = int(entry.get("blk_tendency")     or 50)
                new_stl = int(entry.get("stl_tendency")     or 50)

            elif norm_key in bdl_by_name:
                rec = bdl_by_name[norm_key]
                stats = rec["stats"]
                raw_pos = (row["position"] or rec["position"] or "G").upper()
                pos = raw_pos[-1] if raw_pos[-1] in ("G", "F", "C") else "G"

                stl_v = float(stats.get("stl") or 0.0)
                blk_v = float(stats.get("blk") or 0.0)
                stl_a = stl_avgs.get(pos, 1.0)
                blk_a = blk_avgs.get(pos, 1.0)

                new_def = _defense_tendency_from_stats(stl_v, blk_v, stl_a, blk_a, pos)
                new_blk = _blk_stl_tend(blk_v, blk_a)
                new_stl = _blk_stl_tend(stl_v, stl_a)

            if new_def is None:
                skipped_no_data += 1
                continue

            if not args.dry_run:
                await conn.execute(
                    """
                    UPDATE players
                    SET defense_tendency = $1,
                        blk_tendency     = $2,
                        stl_tendency     = $3
                    WHERE id = $4
                    """,
                    new_def, new_blk, new_stl, row["id"],
                )

            updated += 1

            if norm_key in _SPOT_CHECK_NAMES:
                old_def = row["defense_tendency"]
                old_blk = row["blk_tendency"]
                old_stl = row["stl_tendency"]
                safe = full.encode("ascii", "replace").decode("ascii")
                spot_rows.append((safe, old_def, old_blk, old_stl, new_def, new_blk, new_stl))

        print(f"Players updated:        {updated}")
        print(f"Skipped (no BDL data):  {skipped_no_data}")
        if args.dry_run:
            print("\n[DRY RUN] No changes written.")

        if spot_rows:
            print("\nSpot-check (defensive stars) — BEFORE vs AFTER:")
            print(f"  {'Name':<25}  {'def_t':>5}  {'blk_t':>5}  {'stl_t':>5}    {'def_t':>5}  {'blk_t':>5}  {'stl_t':>5}")
            print(f"  {'':25}  {'--- BEFORE ---':>17}    {'--- AFTER  ---':>17}")
            for name, od, ob, os_, nd, nb, ns in sorted(spot_rows):
                print(f"  {name:<25}  {od:>5}  {ob:>5}  {os_:>5}    {nd:>5}  {nb:>5}  {ns:>5}")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
