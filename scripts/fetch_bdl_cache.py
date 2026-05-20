"""Download and cache BDL season averages for seasons 2010-2024.

Requires GOAT tier API key in BDL_API_KEY env var.
Run once while the GOAT trial is active; output is saved locally and
read by build_stats_ratings.py forever after (no live API needed).

Usage:
    python scripts/fetch_bdl_cache.py
    python scripts/fetch_bdl_cache.py --seasons 2022 2023 2024
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

CACHE_DIR = Path(__file__).parent.parent / "data" / "bdl_cache"
BASE_URL = "https://api.balldontlie.io/v1"
DELAY = 0.12  # GOAT tier allows 600 req/min; 0.12s ≈ 500 req/min
DEFAULT_SEASONS = list(range(2010, 2025))  # 2010-11 through 2024-25
STAT_TYPES = ("base", "usage", "advanced")


def _headers() -> dict:
    key = os.getenv("BDL_API_KEY")
    if not key:
        raise SystemExit("BDL_API_KEY not set")
    return {"Authorization": key}


def fetch_all_pages(url: str, params: dict) -> list:
    headers = _headers()
    results = []
    cursor = None
    while True:
        p = {**params}
        if cursor:
            p["cursor"] = cursor
        r = requests.get(url, headers=headers, params=p, timeout=30)
        if r.status_code == 429:
            print("    Rate limited — sleeping 5s...")
            time.sleep(5)
            continue
        r.raise_for_status()
        data = r.json()
        results.extend(data.get("data", []))
        cursor = data.get("meta", {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(DELAY)
    return results


def fetch_season(season: int) -> None:
    for stat_type in STAT_TYPES:
        out_path = CACHE_DIR / f"season_{season}_{stat_type}.json"
        if out_path.exists():
            print(f"  [skip] season_{season}_{stat_type}.json already cached")
            continue
        print(f"  Fetching {season} {stat_type}...", end=" ", flush=True)
        try:
            records = fetch_all_pages(
                f"{BASE_URL}/season_averages/general",
                {
                    "season": season,
                    "season_type": "regular",
                    "type": stat_type,
                    "per_page": 100,
                },
            )
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(records, f)
            print(f"{len(records)} players saved.")
        except Exception as exc:
            print(f"FAILED: {exc}")
        time.sleep(DELAY)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--seasons",
        type=int,
        nargs="+",
        default=DEFAULT_SEASONS,
        metavar="YEAR",
        help="Season start years to fetch (default: 2010-2024)",
    )
    args = parser.parse_args()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Caching BDL season averages for {len(args.seasons)} seasons...")
    for season in args.seasons:
        print(f"Season {season}:")
        fetch_season(season)

    print("\nDone.")


if __name__ == "__main__":
    main()
