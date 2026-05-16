"""One-shot migration: recalculate contract salaries and usage_weight for all players
in an existing league using the updated piecewise salary formula and BDL pts-based
usage derivation.

Usage:
    python scripts/fix_salaries_and_usage.py --league-id 1 --season 2024
    python scripts/fix_salaries_and_usage.py --league-id 1 --season 2024 --dry-run

What it does:
    1. For every active player in the league, recalculate contract salary using
       the piecewise formula (replaces old linear (ovr-60)*800k formula).
    2. Sets usage_weight from BDL pts: clamp(round(pts / 35.0 * 100), 10, 90).
       Players not found in the BDL cache keep their existing usage_weight.
    3. Updates both the contracts.salary column and the players.usage_weight column.
    4. Prints a summary of old vs new salaries.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys

import asyncpg
from dotenv import load_dotenv

load_dotenv()

_BDL_CACHE_DIR = pathlib.Path(__file__).parent.parent / "data" / "bdl_cache"


def _load_bdl_pts(season: int) -> dict[int, float]:
    """Return {bdl_player_id: pts_per_game} from season_{season}_base.json."""
    path = _BDL_CACHE_DIR / f"season_{season}_base.json"
    if not path.exists():
        print(f"[WARN] BDL cache not found: {path} — usage_weight will not be updated")
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            records = json.load(f)
        return {
            rec["player"]["id"]: float(rec["stats"].get("pts") or 0.0)
            for rec in records
            if rec.get("stats")
        }
    except (json.JSONDecodeError, KeyError, OSError) as exc:
        print(f"[WARN] Failed to load BDL cache: {exc}")
        return {}


def _new_salary(overall: int, salary_cap: int) -> int:
    """Piecewise salary formula — must match import_service._contract_salary."""
    min_salary = 1_100_000
    if overall >= 90:
        raw = salary_cap * (0.25 + (overall - 90) * 0.015)
    elif overall >= 80:
        raw = salary_cap * (0.08 + (overall - 80) * 0.017)
    elif overall >= 68:
        raw = salary_cap * (0.02 + (overall - 68) * 0.005)
    else:
        raw = min_salary

    return max(min_salary, min(int(raw), int(salary_cap * 0.40)))


def _contract_type(salary: int, salary_cap: int) -> str:
    if salary >= int(salary_cap * 0.25):
        return "max"
    if salary <= 1_100_000:
        return "minimum"
    return "standard"


async def main() -> None:
    parser = argparse.ArgumentParser(description="Fix salaries + usage for existing league")
    parser.add_argument("--league-id", type=int, required=True)
    parser.add_argument("--season", type=int, required=True,
                        help="BDL season year (e.g. 2024 for 2024-25 season)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print changes without writing to DB")
    args = parser.parse_args()

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise SystemExit("DATABASE_URL not set")

    bdl_pts = _load_bdl_pts(args.season)
    print(f"BDL pts loaded for {len(bdl_pts)} players (season {args.season})")

    conn = await asyncpg.connect(db_url)
    try:
        league = await conn.fetchrow(
            "SELECT id, salary_cap, current_season FROM leagues WHERE id = $1",
            args.league_id,
        )
        if not league:
            raise SystemExit(f"League {args.league_id} not found")

        salary_cap: int = league["salary_cap"]
        current_season: int = league["current_season"]
        print(f"League {args.league_id}: salary_cap=${salary_cap:,}  season={current_season}")
        print(f"Dry run: {args.dry_run}")
        print()

        # Fetch all players + their active contract
        rows = await conn.fetch(
            """
            SELECT p.id AS player_id,
                   p.overall,
                   p.external_id,
                   p.usage_weight,
                   c.id AS contract_id,
                   c.salary AS old_salary,
                   c.contract_type AS old_type
            FROM players p
            LEFT JOIN contracts c ON c.player_id = p.id
                AND c.league_id = $1
                AND c.is_active = TRUE
            WHERE p.league_id = $1
            ORDER BY p.overall DESC
            """,
            args.league_id,
        )

        updated_contracts = 0
        updated_usage = 0
        skipped_no_contract = 0
        salary_changes: list[tuple[int, int, int]] = []  # (ovr, old, new)

        for row in rows:
            ovr = row["overall"]
            pid = row["player_id"]
            cid = row["contract_id"]
            old_salary = row["old_salary"]
            ext_id = row["external_id"]

            # --- Salary update ---
            if cid is None:
                skipped_no_contract += 1
                continue

            new_sal = _new_salary(ovr, salary_cap)
            new_type = _contract_type(new_sal, salary_cap)
            salary_changes.append((ovr, old_salary or 0, new_sal))

            if not args.dry_run:
                await conn.execute(
                    "UPDATE contracts SET salary = $1, contract_type = $2 WHERE id = $3",
                    new_sal, new_type, cid,
                )
            updated_contracts += 1

            # --- Usage weight update ---
            if ext_id and str(ext_id).isdigit():
                bdl_id = int(ext_id)
                pts = bdl_pts.get(bdl_id)
                if pts is not None:
                    new_usage = max(10, min(90, round(pts / 35.0 * 100)))
                    if not args.dry_run:
                        await conn.execute(
                            "UPDATE players SET usage_weight = $1 WHERE id = $2",
                            new_usage, pid,
                        )
                    updated_usage += 1

        # Summary
        print(f"Contracts updated: {updated_contracts}")
        print(f"Usage weights updated: {updated_usage}")
        print(f"Skipped (no active contract): {skipped_no_contract}")
        print()

        # Show sample of biggest changes
        salary_changes.sort(key=lambda x: x[0], reverse=True)
        print("Sample salary changes (top 20 by OVR):")
        print(f"  {'OVR':>4}  {'Old Salary':>12}  {'New Salary':>12}  {'Delta':>12}")
        print(f"  {'-'*4}  {'-'*12}  {'-'*12}  {'-'*12}")
        for ovr, old_s, new_s in salary_changes[:20]:
            delta = new_s - old_s
            sign = "+" if delta >= 0 else ""
            print(f"  {ovr:>4}  ${old_s:>11,}  ${new_s:>11,}  {sign}${delta:>11,}")

        if salary_changes:
            all_new = [s for _, _, s in salary_changes]
            print()
            print(f"  Average new salary: ${sum(all_new)//len(all_new):,}")
            print(f"  Min new salary:     ${min(all_new):,}")
            print(f"  Max new salary:     ${max(all_new):,}")

        if args.dry_run:
            print()
            print("[DRY RUN] No changes written.")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
