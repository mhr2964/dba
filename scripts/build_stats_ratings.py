"""Pre-compute player OVR ratings from cached BDL season stats.

Reads from data/bdl_cache/season_{season}_{type}.json (built by fetch_bdl_cache.py).
Produces data/stats_ratings/{season}.json keyed by nba_api player ID.

Usage:
    python scripts/build_stats_ratings.py --season 2024
    python scripts/build_stats_ratings.py --season 2024 --dry-run
"""
from __future__ import annotations

import argparse
import json
import unicodedata
from pathlib import Path

from nba_api.stats.static import players as nba_players_static

from import_players import (
    _composite_from_row,
    _overall_from_raw_sum,
    _overall_for_draft_pick,
    _overall_for_exp,
)

CACHE_DIR = Path(__file__).parent.parent / "data" / "bdl_cache"
OUTPUT_DIR = Path(__file__).parent.parent / "data" / "stats_ratings"


def _normalize(name: str) -> str:
    """Lowercase + strip accents so 'Luka Dončić' == 'Luka Doncic'."""
    return (
        unicodedata.normalize("NFKD", name)
        .encode("ascii", "ignore")
        .decode()
        .lower()
        .strip()
    )


def _load_season_cache(season: int, stat_type: str) -> dict[int, dict]:
    """Load one cached BDL season file. Returns {bdl_player_id: record}."""
    path = CACHE_DIR / f"season_{season}_{stat_type}.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        records = json.load(f)
    return {rec["player"]["id"]: rec for rec in records}


def _build_name_map() -> dict[str, int]:
    """Normalized full name -> nba_api player ID (all historical players)."""
    mapping: dict[str, int] = {}
    for p in nba_players_static.get_players():
        key = _normalize(f"{p['first_name']} {p['last_name']}")
        mapping[key] = p["id"]
    return mapping


def _bdl_to_stat_row(base_stats: dict, usage_stats: dict, gp: int, adv_stats: dict | None = None) -> dict:
    row: dict = {
        "gp":      gp,
        "PTS":     float(base_stats.get("pts",    0) or 0),
        "REB":     float(base_stats.get("reb",    0) or 0),
        "AST":     float(base_stats.get("ast",    0) or 0),
        "STL":     float(base_stats.get("stl",    0) or 0),
        "BLK":     float(base_stats.get("blk",    0) or 0),
        "FG3_PCT": float(base_stats.get("fg3_pct",0) or 0),
        "FG3A":    float(base_stats.get("fg3a",   0) or 0),
        "USG_PCT": float(usage_stats.get("usg_pct",0) or 0),  # already decimal (0–1)
        "FGA":     float(base_stats.get("fga",    0) or 0),
        "FTA":     float(base_stats.get("fta",    0) or 0),
    }
    if adv_stats:
        def_rtg = adv_stats.get("def_rating")
        if def_rtg is not None:
            row["DEF_RTG"] = float(def_rtg)
    return row


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"{args.season}.json"

    target_seasons = [args.season, args.season - 1, args.season - 2]

    base_cache     = {s: _load_season_cache(s, "base")     for s in target_seasons}
    usage_cache    = {s: _load_season_cache(s, "usage")    for s in target_seasons}
    advanced_cache = {s: _load_season_cache(s, "advanced") for s in target_seasons}

    if not any(base_cache.values()):
        raise SystemExit(
            f"No BDL cache found for seasons {target_seasons}. "
            "Run fetch_bdl_cache.py first."
        )

    # Collect every BDL player who appeared in any of the 3 target seasons.
    all_bdl_players: dict[int, dict] = {}
    for s in target_seasons:
        for bdl_id, rec in base_cache[s].items():
            if bdl_id not in all_bdl_players:
                all_bdl_players[bdl_id] = rec["player"]

    print(f"BDL players across {target_seasons}: {len(all_bdl_players)}")

    name_map = _build_name_map()
    result: dict[str, int] = {}
    unmatched: list[str] = []

    for bdl_id, pinfo in all_bdl_players.items():
        full_name = f"{pinfo['first_name']} {pinfo['last_name']}"
        nba_id = name_map.get(_normalize(full_name))
        if nba_id is None:
            unmatched.append(full_name)
            continue

        best_raw = -1.0
        for s in target_seasons:
            base_rec = base_cache[s].get(bdl_id)
            if base_rec is None:
                continue
            gp = int(base_rec["stats"].get("gp", 0) or 0)
            if gp < 10:
                continue
            usage_rec    = usage_cache[s].get(bdl_id)
            advanced_rec = advanced_cache[s].get(bdl_id)
            usg_stats = usage_rec["stats"]     if usage_rec    else {}
            adv_stats = advanced_rec["stats"]  if advanced_rec else {}
            row = _bdl_to_stat_row(base_rec["stats"], usg_stats, gp, adv_stats)
            raw = _composite_from_row(row)
            if raw > best_raw:
                best_raw = raw

        if best_raw >= 0:
            ovr = _overall_from_raw_sum(best_raw)
        else:
            # Fallback for players with <10 GP in all 3 seasons (rookies, injuries).
            draft_year   = pinfo.get("draft_year")
            draft_round  = pinfo.get("draft_round")
            draft_number = pinfo.get("draft_number")
            if draft_year == args.season and draft_round and draft_number:
                ovr = _overall_for_draft_pick(draft_round, draft_number)
            else:
                exp = max(0, args.season - (draft_year or args.season))
                ovr = _overall_for_exp(exp)

        result[str(nba_id)] = ovr

    print(f"Matched: {len(result)}  |  Unmatched: {len(unmatched)}")
    if unmatched:
        print(f"  Sample unmatched: {unmatched[:10]}")

    if args.dry_run:
        print("[DRY RUN] Not writing file.")
        return

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(f"Saved -> {out_path}")


if __name__ == "__main__":
    main()
