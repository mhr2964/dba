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

NOTE: DB external_ids use nba_api IDs (different space from BDL), so lookups
are done by normalized player name — the same approach used by fix_salaries_and_usage.py.

Usage:
    python scripts/fix_stat_calibration.py --league-id 30 --season 2024
    python scripts/fix_stat_calibration.py --league-id 30 --season 2024 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
import unicodedata

import asyncpg
from dotenv import load_dotenv

load_dotenv()

_BDL_CACHE_DIR = pathlib.Path(__file__).parent.parent / "data" / "bdl_cache"

# Position-based hard caps (must match roster_seed_service.py).
_REB_CAP = {"PG": 45, "SG": 50, "SF": 65, "PF": 80, "C": 90}
_STL_CAP = {"PG": 70, "SG": 70, "SF": 65, "PF": 60, "C": 55}
_BLK_CAP = {"PG": 40, "SG": 45, "SF": 60, "PF": 80, "C": 90}


def _norm(name: str) -> str:
    return (
        unicodedata.normalize("NFKD", name)
        .encode("ascii", "ignore")
        .decode()
        .lower()
        .strip()
    )


def _load_bdl_by_name(season: int) -> dict[str, dict]:
    """Return {normalized_name: stats_dict} from season_{season}_base.json."""
    path = _BDL_CACHE_DIR / f"season_{season}_base.json"
    if not path.exists():
        print(f"[WARN] BDL cache not found: {path}")
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            records = json.load(f)
        lookup: dict[str, dict] = {}
        for rec in records:
            p = rec.get("player", {})
            full = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            stats = rec.get("stats", {})
            gp = int(stats.get("gp", 0) or 0)
            if gp < 5:
                continue  # skip injury-shortened seasons
            lookup[_norm(full)] = stats
        return lookup
    except (json.JSONDecodeError, KeyError, OSError) as exc:
        print(f"[WARN] Failed to load BDL cache: {exc}")
        return {}


def _compute_position_avgs(bdl_by_name: dict[str, dict]) -> dict[str, dict[str, float]]:
    """Compute per position-group (G/F/C) averages for reb/stl/blk."""
    # We don't have position in the name-keyed lookup so use overall averages
    # as denominator — good enough for the relative scaling.
    totals: dict[str, list[float]] = {"reb": [], "stl": [], "blk": []}
    for stats in bdl_by_name.values():
        for stat in totals:
            v = stats.get(stat)
            if v is not None:
                totals[stat].append(float(v))
    avgs: dict[str, float] = {
        stat: sum(vs) / len(vs) if vs else 1.0
        for stat, vs in totals.items()
    }
    # Return same avg for all position groups since we can't segment by position
    # with a name-keyed lookup (no position in BDL stats record).
    return {"G": avgs, "F": avgs, "C": avgs}


def _tend(player_val: float, pos_avg: float) -> int:
    raw = round((player_val / max(pos_avg, 0.01)) * 50)
    return max(5, min(95, raw))


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

    bdl = _load_bdl_by_name(args.season)
    print(f"BDL records loaded: {len(bdl)}")
    pos_avgs = _compute_position_avgs(bdl)
    avgs = pos_avgs["G"]  # same for all groups in our name-keyed approach

    conn = await asyncpg.connect(db_url)
    try:
        league = await conn.fetchrow(
            "SELECT id FROM leagues WHERE id = $1", args.league_id
        )
        if not league:
            raise SystemExit(f"League {args.league_id} not found")

        print(f"League {args.league_id}  season={args.season}  dry_run={args.dry_run}")

        # Ensure defense_tendency column exists.
        if not args.dry_run:
            await conn.execute(
                "ALTER TABLE players ADD COLUMN IF NOT EXISTS "
                "defense_tendency SMALLINT NOT NULL DEFAULT 50"
            )

        rows = await conn.fetch(
            """
            SELECT id, first_name, last_name, position,
                   reb_tendency, stl_tendency, blk_tendency,
                   COALESCE(defense_tendency, 50) AS defense_tendency
            FROM players
            WHERE league_id = $1
            ORDER BY id
            """,
            args.league_id,
        )
        print(f"Players fetched: {len(rows)}")

        updated = 0
        skipped_no_bdl = 0
        capped_reb = 0
        capped_stl = 0
        capped_blk = 0
        def_computed = 0

        for row in rows:
            pid = row["id"]
            full_pos = (row["position"] or "SF").upper()
            if full_pos not in _REB_CAP:
                full_pos = "SF"

            name = f"{row['first_name']} {row['last_name']}".strip()
            norm_name = _norm(name)
            stats = bdl.get(norm_name)

            old_reb = row["reb_tendency"]
            old_stl = row["stl_tendency"]
            old_blk = row["blk_tendency"]
            old_def = row["defense_tendency"]

            if stats is None:
                # No BDL data — apply position hard-caps to current values only.
                skipped_no_bdl += 1
                new_reb = min(old_reb, _REB_CAP.get(full_pos, 65))
                new_stl = min(old_stl, _STL_CAP.get(full_pos, 65))
                new_blk = min(old_blk, _BLK_CAP.get(full_pos, 60))
                new_def = old_def  # can't improve without data
                cap_only = True
            else:
                reb_val = float(stats.get("reb") or 0.0)
                stl_val = float(stats.get("stl") or 0.0)
                blk_val = float(stats.get("blk") or 0.0)

                raw_reb = _tend(reb_val, avgs["reb"])
                raw_stl = _tend(stl_val, avgs["stl"])
                raw_blk = _tend(blk_val, avgs["blk"])

                new_reb = min(raw_reb, _REB_CAP.get(full_pos, 65))
                new_stl = min(raw_stl, _STL_CAP.get(full_pos, 65))
                new_blk = min(raw_blk, _BLK_CAP.get(full_pos, 60))

                # defense_tendency: stl 60% weight, blk 40%.
                defense_raw = round(
                    (stl_val / max(avgs["stl"], 0.01)) * 0.60 * 50
                    + (blk_val / max(avgs["blk"], 0.01)) * 0.40 * 50
                )
                new_def = max(5, min(85, defense_raw))
                cap_only = False
                def_computed += 1

            changed = (
                new_reb != old_reb or new_stl != old_stl
                or new_blk != old_blk or new_def != old_def
            )

            if not changed:
                continue

            if old_reb != new_reb:
                capped_reb += 1
            if old_stl != new_stl:
                capped_stl += 1
            if old_blk != new_blk:
                capped_blk += 1

            safe_name = name.encode("ascii", "replace").decode("ascii")

            if args.dry_run:
                tag = "[cap-only]" if cap_only else ""
                print(
                    f"  {safe_name:<32} [{full_pos}] {tag}"
                    f"  reb {old_reb}->{new_reb}"
                    f"  stl {old_stl}->{new_stl}"
                    f"  blk {old_blk}->{new_blk}"
                    f"  def {old_def}->{new_def}"
                )
            else:
                await conn.execute(
                    """UPDATE players
                       SET reb_tendency     = $1,
                           stl_tendency     = $2,
                           blk_tendency     = $3,
                           defense_tendency = $4
                       WHERE id = $5""",
                    new_reb, new_stl, new_blk, new_def, pid,
                )
                updated += 1

        print()
        print(f"Players updated:           {updated}")
        print(f"Skipped (no BDL name match): {skipped_no_bdl}")
        print(f"defense_tendency computed:  {def_computed}")
        print(f"reb_tendency changed:       {capped_reb}")
        print(f"stl_tendency changed:       {capped_stl}")
        print(f"blk_tendency changed:       {capped_blk}")

        if args.dry_run:
            print()
            print("[DRY RUN] No changes written.")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
