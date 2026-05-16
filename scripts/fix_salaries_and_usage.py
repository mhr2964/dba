"""One-shot migration: recalculate contract salaries and usage_weight for all players
in an existing league using the updated piecewise salary formula and BDL pts-based
usage derivation.

Usage:
    python scripts/fix_salaries_and_usage.py --league-id 29 --season 2024
    python scripts/fix_salaries_and_usage.py --league-id 29 --season 2024 --dry-run

What it does:
    1. For every active player in the league, recalculate contract salary using
       the piecewise formula (replaces old linear (ovr-60)*800k formula).
    2. Sets usage_weight from BDL pts: clamp(round(pts / 35.0 * 100), 10, 90).
       Lookup is by normalized player name since external_id in the DB is from
       nba_api (different ID space than BDL).  Players not found in BDL cache
       keep their existing usage_weight.
    3. Updates both the contracts.salary column and the players.usage_weight column.
    4. Prints a summary of old vs new salaries.
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

_BDL_CACHE_DIR = pathlib.Path(__file__).parent.parent / "data" / "bdl_cache"


def _norm_name(name: str) -> str:
    """Lowercase, strip accents, strip whitespace."""
    return (
        unicodedata.normalize("NFKD", name)
        .encode("ascii", "ignore")
        .decode()
        .lower()
        .strip()
    )


def _load_bdl_pts_by_name(season: int) -> dict[str, float]:
    """Return {normalized_full_name: pts_per_game} from season_{season}_base.json."""
    path = _BDL_CACHE_DIR / f"season_{season}_base.json"
    if not path.exists():
        print(f"[WARN] BDL cache not found: {path} — usage_weight will not be updated")
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            records = json.load(f)
        lookup: dict[str, float] = {}
        for rec in records:
            p = rec.get("player", {})
            full = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            norm = _norm_name(full)
            pts = float(rec.get("stats", {}).get("pts") or 0.0)
            lookup[norm] = pts
        return lookup
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

    bdl_pts = _load_bdl_pts_by_name(args.season)
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
                   p.first_name,
                   p.last_name,
                   p.overall,
                   p.usage_weight AS old_usage,
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
        salary_changes: list[tuple[int, int, int, str]] = []  # (ovr, old, new, name)

        for row in rows:
            ovr = row["overall"]
            pid = row["player_id"]
            cid = row["contract_id"]
            old_salary = row["old_salary"] or 0
            fname = row["first_name"] or ""
            lname = row["last_name"] or ""
            full_name = f"{fname} {lname}".strip()

            # --- Salary update ---
            if cid is None:
                skipped_no_contract += 1
                continue

            new_sal = _new_salary(ovr, salary_cap)
            new_type = _contract_type(new_sal, salary_cap)
            salary_changes.append((ovr, old_salary, new_sal, full_name))

            if not args.dry_run:
                await conn.execute(
                    "UPDATE contracts SET salary = $1, contract_type = $2 WHERE id = $3",
                    new_sal, new_type, cid,
                )
            updated_contracts += 1

            # --- Usage weight update (name-based BDL lookup) ---
            norm = _norm_name(full_name)
            pts = bdl_pts.get(norm)
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
        print(f"Usage weights updated from BDL: {updated_usage}")
        print(f"  (Remaining {updated_contracts - updated_usage} players keep existing usage_weight)")
        print(f"Skipped (no active contract): {skipped_no_contract}")
        print()

        # Show sample of biggest changes
        salary_changes.sort(key=lambda x: x[0], reverse=True)
        print("Sample salary changes (top 25 by OVR):")
        print(f"  {'OVR':>4}  {'Name':<28}  {'Old Salary':>12}  {'New Salary':>12}  {'Delta':>12}")
        print(f"  {'-'*4}  {'-'*28}  {'-'*12}  {'-'*12}  {'-'*12}")
        for ovr, old_s, new_s, name in salary_changes[:25]:
            delta = new_s - old_s
            sign = "+" if delta >= 0 else ""
            # Strip non-ASCII for safe terminal output on Windows
            safe_name = name.encode("ascii", "replace").decode("ascii")[:27]
            print(f"  {ovr:>4}  {safe_name:<28}  ${old_s:>11,}  ${new_s:>11,}  {sign}${delta:>11,}")

        if salary_changes:
            all_new = [s for _, _, s, _ in salary_changes]
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
