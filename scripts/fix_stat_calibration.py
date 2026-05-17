"""One-shot calibration fix for any league.

Applies stat calibration corrections to existing player rows using pure
BDL position-relative formulas — no hard position caps.

For each of reb/stl/blk: tendency = clamp(round((player_avg / position_avg) * 50), 5, 95)
where position_avg is computed from all BDL players at the same position group (G/F/C).
50 = position-average player; the position-relative denominator naturally produces lower
values for guards (e.g. Curry ~4.5 RPG vs G avg ~3.5 RPG → reb_tendency ≈ 64) without
needing an arbitrary cap.

1. reb_tendency — from BDL reb per game, position-relative.
2. stl_tendency — from BDL stl per game, position-relative.
3. blk_tendency — from BDL blk per game, position-relative.
4. ast_tendency — from BDL ast per game, position-relative. Without this fix, players
   default to 50 from migration 026, capping every player's APG at ~7 regardless of
   their real passing ability (Trae Young 11.6 APG should reach 95).
5. defense_tendency — stl 60% + blk 40% weighted composite, position-relative. Capped at 85.
6. usage_weight — from BDL pts per game: clamp(round((pts/25)*100), 10, 95).

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


def _norm(name: str) -> str:
    return (
        unicodedata.normalize("NFKD", name)
        .encode("ascii", "ignore")
        .decode()
        .lower()
        .strip()
    )


def _load_bdl_by_name(season: int) -> dict[str, dict]:
    """Return {normalized_name: {stats, pos_group}} from season_{season}_base.json.

    Each value has keys:
        stats     — raw BDL stats dict
        pos_group — 'G', 'F', or 'C' derived from BDL position string
    """
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
            raw_pos = (p.get("position") or "G").upper()
            pos_group = raw_pos[-1] if raw_pos[-1] in ("G", "F", "C") else "G"
            lookup[_norm(full)] = {"stats": stats, "pos_group": pos_group}
        return lookup
    except (json.JSONDecodeError, KeyError, OSError) as exc:
        print(f"[WARN] Failed to load BDL cache: {exc}")
        return {}


def _compute_position_avgs(bdl_by_name: dict[str, dict]) -> dict[str, dict[str, float]]:
    """Compute per position-group (G/F/C) averages for reb/stl/blk/ast."""
    _STATS = ("reb", "stl", "blk", "ast")
    buckets: dict[str, dict[str, list[float]]] = {
        "G": {s: [] for s in _STATS},
        "F": {s: [] for s in _STATS},
        "C": {s: [] for s in _STATS},
    }
    all_vals: dict[str, list[float]] = {s: [] for s in _STATS}
    for entry in bdl_by_name.values():
        stats = entry["stats"]
        pg = entry["pos_group"]
        bucket = buckets.get(pg, buckets["G"])
        for stat in _STATS:
            v = stats.get(stat)
            if v is not None:
                fv = float(v)
                bucket[stat].append(fv)
                all_vals[stat].append(fv)
    overall_avgs = {
        stat: sum(vs) / len(vs) if vs else 1.0
        for stat, vs in all_vals.items()
    }
    avgs: dict[str, dict[str, float]] = {}
    for pos, stat_lists in buckets.items():
        avgs[pos] = {}
        for stat, vs in stat_lists.items():
            avgs[pos][stat] = sum(vs) / len(vs) if vs else overall_avgs[stat]
    return avgs


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
                   COALESCE(ast_tendency, 50)     AS ast_tendency,
                   COALESCE(defense_tendency, 50) AS defense_tendency,
                   usage_weight
            FROM players
            WHERE league_id = $1
            ORDER BY id
            """,
            args.league_id,
        )
        print(f"Players fetched: {len(rows)}")

        updated = 0
        skipped_no_bdl = 0
        reb_changed = 0
        stl_changed = 0
        blk_changed = 0
        ast_changed = 0
        def_computed = 0
        usage_updated = 0

        for row in rows:
            pid = row["id"]
            db_pos = (row["position"] or "SF").upper()

            name = f"{row['first_name']} {row['last_name']}".strip()
            norm_name = _norm(name)
            entry = bdl.get(norm_name)

            old_reb = row["reb_tendency"]
            old_stl = row["stl_tendency"]
            old_blk = row["blk_tendency"]
            old_ast = row["ast_tendency"]
            old_def = row["defense_tendency"]
            old_usage = row["usage_weight"]

            if entry is None:
                # No BDL data — leave existing values unchanged.
                skipped_no_bdl += 1
                continue

            stats = entry["stats"]
            # Use BDL position group for denominator; fall back to last char of DB position.
            pos_group = entry["pos_group"]

            avgs = pos_avgs.get(pos_group, pos_avgs.get("G", {"reb": 1.0, "stl": 1.0, "blk": 1.0, "ast": 1.0}))

            reb_val = float(stats.get("reb") or 0.0)
            stl_val = float(stats.get("stl") or 0.0)
            blk_val = float(stats.get("blk") or 0.0)
            ast_val = float(stats.get("ast") or 0.0)
            pts_val = float(stats.get("pts") or 0.0)

            new_reb = _tend(reb_val, avgs["reb"])
            new_stl = _tend(stl_val, avgs["stl"])
            new_blk = _tend(blk_val, avgs["blk"])
            # ast_tendency: position-relative, same formula as reb/stl/blk.
            # Trae Young (11.6 APG, G avg ~2.7 APG) → (11.6/2.7)*50 = 215 → capped to 95.
            # Without this fix, all players default to 50 from migration 026, which caps
            # every player's APG at ~7 regardless of their real passing ability.
            new_ast = _tend(ast_val, avgs["ast"])

            # defense_tendency: stl 60% weight, blk 40%. Capped at 85.
            defense_raw = round(
                (stl_val / max(avgs["stl"], 0.01)) * 0.60 * 50
                + (blk_val / max(avgs["blk"], 0.01)) * 0.40 * 50
            )
            new_def = max(5, min(85, defense_raw))

            # usage_weight: pts/25 * 100, clamped 10–95.
            new_usage = max(10, min(95, round(pts_val / 25.0 * 100)))

            def_computed += 1
            if new_usage != old_usage:
                usage_updated += 1

            changed = (
                new_reb != old_reb or new_stl != old_stl
                or new_blk != old_blk or new_ast != old_ast
                or new_def != old_def or new_usage != old_usage
            )

            if not changed:
                continue

            if old_reb != new_reb:
                reb_changed += 1
            if old_stl != new_stl:
                stl_changed += 1
            if old_blk != new_blk:
                blk_changed += 1
            if old_ast != new_ast:
                ast_changed += 1

            safe_name = name.encode("ascii", "replace").decode("ascii")

            if args.dry_run:
                print(
                    f"  {safe_name:<32} [{db_pos}/{pos_group}]"
                    f"  reb {old_reb}->{new_reb}"
                    f"  stl {old_stl}->{new_stl}"
                    f"  blk {old_blk}->{new_blk}"
                    f"  ast {old_ast}->{new_ast}"
                    f"  def {old_def}->{new_def}"
                    f"  usage {old_usage}->{new_usage}"
                )
            else:
                await conn.execute(
                    """UPDATE players
                       SET reb_tendency     = $1,
                           stl_tendency     = $2,
                           blk_tendency     = $3,
                           ast_tendency     = $4,
                           defense_tendency = $5,
                           usage_weight     = $6
                       WHERE id = $7""",
                    new_reb, new_stl, new_blk, new_ast, new_def, new_usage, pid,
                )
                updated += 1

        print()
        print(f"Players updated:             {updated}")
        print(f"Skipped (no BDL name match): {skipped_no_bdl}")
        print(f"defense_tendency computed:   {def_computed}")
        print(f"usage_weight updated:        {usage_updated}")
        print(f"reb_tendency changed:        {reb_changed}")
        print(f"stl_tendency changed:        {stl_changed}")
        print(f"blk_tendency changed:        {blk_changed}")
        print(f"ast_tendency changed:        {ast_changed}")

        if args.dry_run:
            print()
            print("[DRY RUN] No changes written.")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
