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
import sys
import unicodedata
from pathlib import Path

# Ensure import_players resolves whether this script is run directly from the
# repo root (python scripts/build_stats_ratings.py) or imported as a package
# module (from scripts import build_stats_ratings).
_SCRIPTS_DIR = Path(__file__).parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from nba_api.stats.static import players as nba_players_static

from import_players import (
    _composite_from_row,
    _overall_from_raw_sum,
    _overall_for_draft_pick,
    _overall_for_exp,
    _compute_defensive_scores,
    _defensive_ovr_bump,
    _defensive_ovr_bump_fallback,
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
    """Build a unified stat row from BDL base, usage, and advanced caches.

    Keys follow the ALL_CAPS convention used by _composite_from_row and
    _compute_defensive_scores in import_players. Rate-based defensive fields
    (PCT_BLK, PCT_STL, DREB_PCT) are only populated when their source caches
    exist — callers must handle None gracefully.
    """
    row: dict = {
        "gp":      gp,
        "PTS":     float(base_stats.get("pts",    0) or 0),
        "REB":     float(base_stats.get("reb",    0) or 0),
        "DREB":    float(base_stats.get("dreb",   0) or 0),
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
        dreb_pct = adv_stats.get("dreb_pct")
        if dreb_pct is not None:
            row["DREB_PCT"] = float(dreb_pct)
    if usage_stats:
        pct_blk = usage_stats.get("pct_blk")
        if pct_blk is not None:
            row["PCT_BLK"] = float(pct_blk)
        pct_stl = usage_stats.get("pct_stl")
        if pct_stl is not None:
            row["PCT_STL"] = float(pct_stl)
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
    # OVR output keyed by nba_api player ID (str).
    result: dict[str, int] = {}
    # Archetype output — written to a companion file used by the backfill.
    archetypes: dict[str, str] = {}
    unmatched: list[str] = []

    _POS_MAP = {"G": "PG", "G-F": "SG", "F-G": "SG", "F": "SF", "F-C": "PF", "C-F": "PF", "C": "C"}

    for bdl_id, pinfo in all_bdl_players.items():
        full_name = f"{pinfo['first_name']} {pinfo['last_name']}"
        nba_id = name_map.get(_normalize(full_name))
        if nba_id is None:
            unmatched.append(full_name)
            continue

        best_raw = -1.0
        best_row: dict | None = None
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
                best_row = row

        if best_raw >= 0 and best_row is not None:
            bdl_pos = (pinfo.get("position") or "G").upper()
            our_pos = _POS_MAP.get(bdl_pos, "SF")
            base_ovr = _overall_from_raw_sum(best_raw)

            pct_blk  = best_row.get("PCT_BLK")
            pct_stl  = best_row.get("PCT_STL")
            dreb_pct = best_row.get("DREB_PCT")
            def_rtg  = best_row.get("DEF_RTG")

            if pct_blk is not None and pct_stl is not None and dreb_pct is not None:
                interior, perimeter, arch = _compute_defensive_scores(
                    pct_blk=pct_blk,
                    pct_stl=pct_stl,
                    dreb_pct=dreb_pct,
                    def_rating=def_rtg,
                    position=our_pos,
                )
                bump = _defensive_ovr_bump(interior, perimeter)
            else:
                # Missing usage/advanced cache for this player-season.
                bump = _defensive_ovr_bump_fallback(base_ovr)
                arch = "generalist"

            ovr = min(99, base_ovr + bump)
            archetypes[str(nba_id)] = arch
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

    # Write companion archetype file alongside the OVR file.
    arch_path = OUTPUT_DIR / f"{args.season}_archetypes.json"
    with open(arch_path, "w", encoding="utf-8") as f:
        json.dump(archetypes, f, indent=2)
    print(f"Saved archetypes -> {arch_path}  ({len(archetypes)} entries)")


if __name__ == "__main__":
    main()
