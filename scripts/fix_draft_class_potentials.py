"""Update potential in all draft_class_YYYY.json seeds using career peak OVR.

Priority:
  1. Peak 2K rating across all available seasons (expert-curated, most accurate)
  2. Peak stats_ratings OVR, capped at 85 (BDL-derived; inflates volume players so we cap it)
  3. Leave unchanged if no data exists

Run from the project root:
    python scripts/fix_draft_class_potentials.py
"""
from __future__ import annotations

import json
import random
import unicodedata
from pathlib import Path

ROOT = Path(__file__).parent.parent
SEEDS_DIR = ROOT / "data" / "seeds"
STATS_RATINGS_DIR = ROOT / "data" / "stats_ratings"
NBA_2K_PATH = ROOT / "data" / "seeds" / "nba_2k_ratings.json"


def _normalize(name: str) -> str:
    return (
        unicodedata.normalize("NFKD", name)
        .encode("ascii", "ignore")
        .decode()
        .lower()
        .strip()
    )


def _load_2k_ratings() -> dict[str, dict[str, int]]:
    """Load 2K ratings keyed by season → normalized_name → ovr."""
    if not NBA_2K_PATH.exists():
        return {}
    raw = json.loads(NBA_2K_PATH.read_text(encoding="utf-8"))
    return {season: {_normalize(k): v for k, v in players.items()} for season, players in raw.items()}


def _load_stats_ratings(year: int) -> dict[str, int]:
    path = STATS_RATINGS_DIR / f"{year}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _peak_2k(full_name: str, ratings_2k: dict) -> int | None:
    key = _normalize(full_name)
    peaks = [players[key] for players in ratings_2k.values() if key in players]
    return max(peaks) if peaks else None


def _peak_stats(external_id: int, draft_year: int) -> int | None:
    peaks = []
    for year in range(draft_year, 2025):
        sr = _load_stats_ratings(year)
        val = sr.get(str(external_id))
        if val is not None:
            peaks.append(val)
    return max(peaks) if peaks else None


def main() -> None:
    ratings_2k = _load_2k_ratings()
    seed_files = sorted(SEEDS_DIR.glob("draft_class_20??.json"))

    for seed_file in seed_files:
        year = int(seed_file.stem.split("_")[-1])
        data = json.loads(seed_file.read_text(encoding="utf-8"))

        changed = 0
        notable: list[tuple[str, int, int, str]] = []

        for row in data:
            if row.get("source") != "nba_real":
                continue

            old_potential = row.get("potential", 70)
            full_name = row.get("full_name", "")
            external_id = row.get("external_id")
            rng = random.Random(external_id or hash(full_name))
            jitter = rng.randint(2, 5)

            # 1. 2K peak — expert-curated, most accurate signal
            pk2k = _peak_2k(full_name, ratings_2k)
            if pk2k is not None:
                new_potential = min(99, pk2k + jitter)
                source = "2k"
            else:
                # 2. Stats peak — cap at 85 to reduce volume-stat inflation
                pk_stats = _peak_stats(external_id, year) if external_id else None
                if pk_stats is not None:
                    new_potential = min(99, min(85, pk_stats) + jitter)
                    source = "stats_capped"
                else:
                    continue

            if new_potential != old_potential:
                if len(notable) < 8:
                    notable.append((full_name, old_potential, new_potential, source))
                row["potential"] = new_potential
                changed += 1

        seed_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"{year}: {changed} potentials updated")
        for name, old, new, src in notable:
            print(f"  {name.encode('ascii','replace').decode()}: {old} -> {new} [{src}]")


if __name__ == "__main__":
    main()
