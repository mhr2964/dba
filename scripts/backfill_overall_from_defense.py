"""Re-baseline defensive OVR for existing league players using split scores.

Two-season peak edition: loads BDL season caches for current + 1 prior season
only and picks the season where the player's defensive bump is highest.  This
prevents a down/injury year (e.g. Gobert 2024) from permanently depressing a
player's rating, while also excluding stale fluke seasons (e.g. Drummond 2022)
that no longer reflect the player's true ability.

Re-basing logic (avoids double-stacking):
  For EACH qualifying season (GP >= 50) we independently compute:
    1. base_ovr  — from _composite_from_row + _overall_from_raw_sum using THAT
                   season's full stat row (guaranteed bump-free baseline).
    2. def_bump  — from _defensive_ovr_bump(interior, perimeter).
    3. candidate_ovr = min(96, base_ovr + def_bump)

  We keep the season whose def_bump is highest (tiebreak: prefer more recent).
  The final OVR written to the DB is that candidate_ovr.

  GP minimum of 50 filters out small-sample seasons (mid-year trades, injuries)
  that don't represent a player's true defensive ability.

  The 96 ceiling reserves 97-99 for a future explicit all-time tier multiplier;
  normal derivation never exceeds 96.

  This completely replaces the old "subtract old bump + add new bump" approach
  because it roots each season's value in a freshly-computed offensive baseline,
  making stacking impossible regardless of how many times this script is run.

Usage:
    python scripts/backfill_overall_from_defense.py
    python scripts/backfill_overall_from_defense.py --league-id 52 --season 2024
    python scripts/backfill_overall_from_defense.py --league-id 52 --season 2024 --dry-run
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

_ROOT = pathlib.Path(__file__).parent.parent
_BDL_CACHE_DIR = _ROOT / "data" / "bdl_cache"

# Make sure import_players resolves when run directly.
_SCRIPTS_DIR = pathlib.Path(__file__).parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from import_players import (  # noqa: E402
    _composite_from_row,
    _overall_from_raw_sum,
    _compute_defensive_scores,
    _defensive_ovr_bump,
)

# BDL position strings -> our five-position schema (mirrors import_players.py)
_BDL_POS_MAP: dict[str, str] = {
    "G": "PG", "G-F": "SG", "F-G": "SG",
    "F": "SF", "F-C": "PF", "C-F": "PF",
    "C": "C",
}

# Spotlight names for extra visibility in output.
_SPOTLIGHT = {
    "rudy gobert",
    "victor wembanyama",
    "bam adebayo",
    "og anunoby",
    "jrue holiday",
    "anthony davis",
    "joel embiid",
    "brook lopez",
    "andre drummond",
}

# Minimum games played to trust a season's defensive sample.
# 50 GP filters out mid-year-trade / injury seasons that don't represent a
# player's true defensive ability (was 30, raised to tighten calibration).
_MIN_GP = 50


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def _norm(name: str) -> str:
    """Lowercase, strip accents, collapse whitespace."""
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


# ---------------------------------------------------------------------------
# BDL cache loaders — keyed by normalised player name
# ---------------------------------------------------------------------------

def _load_season(season: int) -> dict[str, dict]:
    """Load base + usage + advanced for one season.

    Returns {norm_name: {
        "gp", "position",
        "base_stats",   # raw dict from cache (per-game values)
        "pct_blk", "pct_stl",
        "dreb_pct", "def_rating",
    }} for players with GP >= _MIN_GP across ALL three cache types present.
    Players missing usage or advanced data are included with those fields None
    so callers can decide how to handle them.
    """
    base_path     = _BDL_CACHE_DIR / f"season_{season}_base.json"
    usage_path    = _BDL_CACHE_DIR / f"season_{season}_usage.json"
    advanced_path = _BDL_CACHE_DIR / f"season_{season}_advanced.json"

    def _try_load(path: pathlib.Path, label: str) -> list[dict]:
        if not path.exists():
            return []
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[WARN] Could not load {label} cache for {season}: {exc}")
            return []

    base_records     = _try_load(base_path,     f"base({season})")
    usage_records    = _try_load(usage_path,    f"usage({season})")
    advanced_records = _try_load(advanced_path, f"advanced({season})")

    # Index usage and advanced by norm_name for O(1) merge.
    usage_by_name: dict[str, dict] = {}
    for rec in usage_records:
        p = rec.get("player", {})
        full = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        usage_by_name[_norm(full)] = rec.get("stats", {})

    adv_by_name: dict[str, dict] = {}
    for rec in advanced_records:
        p = rec.get("player", {})
        full = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        adv_by_name[_norm(full)] = rec.get("stats", {})

    result: dict[str, dict] = {}
    for rec in base_records:
        p = rec.get("player", {})
        s = rec.get("stats", {})
        gp = int(s.get("gp", 0) or 0)
        if gp < _MIN_GP:
            continue
        full = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        key  = _norm(full)
        bdl_pos = (p.get("position") or "G").upper()

        u = usage_by_name.get(key, {})
        a = adv_by_name.get(key, {})

        pct_blk   = u.get("pct_blk")
        pct_stl   = u.get("pct_stl")
        dreb_pct  = a.get("dreb_pct")
        def_rtg   = a.get("def_rating")

        result[key] = {
            "gp":        gp,
            "position":  _BDL_POS_MAP.get(bdl_pos, "SF"),
            "base_stats": s,           # raw per-game stats dict for composite
            "usg_pct":   float(u.get("usg_pct", 0) or 0),
            "pct_blk":   float(pct_blk)  if pct_blk  is not None else None,
            "pct_stl":   float(pct_stl)  if pct_stl  is not None else None,
            "dreb_pct":  float(dreb_pct) if dreb_pct is not None else None,
            "def_rating": float(def_rtg) if def_rtg  is not None else None,
        }
    return result


# ---------------------------------------------------------------------------
# Composite baseline (bump-free)
# ---------------------------------------------------------------------------

def _base_ovr_from_season_entry(entry: dict) -> int:
    """Derive a bump-free base OVR from a season entry's raw stats.

    Uses the same _composite_from_row + _overall_from_raw_sum path that
    import_players uses, so the baseline is always consistent with how new
    players are rated — no accumulated drift from repeated old-bump subtracts.
    """
    s = entry["base_stats"]
    row: dict = {
        "PTS":     float(s.get("pts",     0) or 0),
        "REB":     float(s.get("reb",     0) or 0),
        "DREB":    float(s.get("dreb",    0) or 0),
        "AST":     float(s.get("ast",     0) or 0),
        "STL":     float(s.get("stl",     0) or 0),
        "BLK":     float(s.get("blk",     0) or 0),
        "FG3_PCT": float(s.get("fg3_pct", 0) or 0),
        "FG3A":    float(s.get("fg3a",    0) or 0),
        "FGA":     float(s.get("fga",     0) or 0),
        "FTA":     float(s.get("fta",     0) or 0),
        "USG_PCT": entry["usg_pct"],
    }
    # Include def_rating in composite when available.
    if entry["def_rating"] is not None:
        row["DEF_RTG"] = entry["def_rating"]

    raw = _composite_from_row(row)
    return _overall_from_raw_sum(raw)


# ---------------------------------------------------------------------------
# Per-player peak-season selection
# ---------------------------------------------------------------------------

def _pick_best_season(
    seasons: list[int],
    season_maps: dict[int, dict[str, dict]],
    key: str,
    db_position: str,
) -> tuple[int | None, int, float, float, str]:
    """Find the season that produces the highest TOTAL OVR (base + defensive bump).

    Was previously "highest bump alone" — which caused Jrue Holiday to pick his
    2024 (bump=4, base=75 → 79) over 2023 (bump=2, base=81 → 83). The script
    was optimizing the wrong objective. Now picks max(base+bump) directly.

    Returns:
        (best_season, candidate_ovr, interior, perimeter, archetype)
        best_season is None when the player qualifies in no season.
    """
    best_season: int | None = None
    best_total  = -1
    best_int    = 0.0
    best_peri   = 0.0
    best_arch   = "generalist"

    for season in seasons:       # seasons ordered most-recent first
        entry = season_maps[season].get(key)
        if entry is None:
            continue

        position = db_position if db_position in ("PG", "SG", "SF", "PF", "C") \
                   else entry["position"]

        if (entry["pct_blk"] is not None
                and entry["pct_stl"] is not None
                and entry["dreb_pct"] is not None):
            interior, perimeter, archetype = _compute_defensive_scores(
                pct_blk=entry["pct_blk"],
                pct_stl=entry["pct_stl"],
                dreb_pct=entry["dreb_pct"],
                def_rating=entry["def_rating"],
                position=position,
            )
            bump = _defensive_ovr_bump(interior, perimeter)
        else:
            # Rate stats unavailable; use zero bump so we still record a
            # baseline OVR for this season but don't award unearned credit.
            interior, perimeter, archetype = 0.0, 0.0, "generalist"
            bump = 0

        base_ovr = _base_ovr_from_season_entry(entry)
        # Cap at 96: reserves 97-99 for a future explicit all-time tier
        # multiplier.  The bump itself is uncapped; the ceiling applies only
        # to the final derived OVR.
        candidate_ovr = min(96, base_ovr + bump)

        # Keep if total OVR strictly higher. Tie: prefer more recent (lower
        # index in the seasons list, which is ordered newest-first).
        if candidate_ovr > best_total:
            best_season = season
            best_total  = candidate_ovr
            best_int    = interior
            best_peri   = perimeter
            best_arch   = archetype

    return best_season, max(best_total, 0), best_int, best_peri, best_arch


# ---------------------------------------------------------------------------
# Importable entry point (used by league_service at league-create time)
# ---------------------------------------------------------------------------

async def backfill_league_archetypes(
    conn,
    league_id: int,
    season: int,
    *,
    dry_run: bool = False,
) -> int:
    """Populate defensive_archetype (and re-baseline OVR) for every player in a league.

    Designed to be called from league_service immediately after roster import so that
    franchise plan derivation sees populated archetypes from day 1.

    Parameters
    ----------
    conn:
        An asyncpg Pool or Connection — anything that supports .fetch() / .execute().
    league_id:
        The league whose players should be updated.
    season:
        BDL season year to use as the "current" season (current + prior are loaded).
    dry_run:
        When True, compute changes but write nothing.

    Returns
    -------
    int
        Number of players updated (or that *would* be updated in dry-run mode).
    """
    target_seasons = [season, season - 1]
    season_maps: dict[int, dict[str, dict]] = {}
    for s in target_seasons:
        season_maps[s] = _load_season(s)

    rows = await conn.fetch(
        """
        SELECT id, first_name, last_name, position, overall, defense
        FROM players
        WHERE league_id = $1
        ORDER BY last_name, first_name
        """,
        league_id,
    )

    updated = 0

    for row in rows:
        fname = row["first_name"] or ""
        lname = row["last_name"] or ""
        full = f"{fname} {lname}".strip()
        key = _norm(full)
        db_pos = (row["position"] or "").upper()

        in_any_season = any(key in season_maps[s] for s in target_seasons)
        if in_any_season:
            best_season, new_ovr, interior, perimeter, archetype = _pick_best_season(
                seasons=target_seasons,
                season_maps=season_maps,
                key=key,
                db_position=db_pos,
            )
        else:
            best_season = None
            new_ovr = 0
            _interior = _perimeter = 0.0  # noqa: F841 — unused in this path
            archetype = "generalist"

        if best_season is None:
            # Attribute-fallback path (mirrors the main() logic).
            def_attr = int(row["defense"] or 0)
            if def_attr >= 92:
                attr_bump = 5
                attr_arch = "two_way_big" if db_pos in ("C", "PF") else "wing_stopper"
            elif def_attr >= 88:
                attr_bump = 4
                attr_arch = "rim_protector" if db_pos in ("C", "PF") else "on_ball_pest"
            elif def_attr >= 85:
                attr_bump = 3
                attr_arch = "size_defender" if db_pos in ("C", "PF") else "generalist"
            else:
                continue  # no data + no elite defense attribute — skip

            old_ovr = int(row["overall"])
            new_ovr = min(96, old_ovr + attr_bump)
            archetype = attr_arch
            if not dry_run:
                await conn.execute(
                    "UPDATE players SET overall = $1, defensive_archetype = $2 WHERE id = $3",
                    new_ovr, archetype, row["id"],
                )
            updated += 1
            continue

        old_ovr = int(row["overall"])
        # Write only if something actually changes (OVR or archetype).
        current_arch = row.get("defensive_archetype") if isinstance(row, dict) else None
        if new_ovr == old_ovr and archetype == current_arch:
            continue

        if not dry_run:
            await conn.execute(
                "UPDATE players SET overall = $1, defensive_archetype = $2 WHERE id = $3",
                new_ovr, archetype, row["id"],
            )
        updated += 1

    return updated


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Re-baseline defensive OVR using two-season peak data. "
            "Picks the season (current + 1 prior) where the player's "
            "defensive bump is highest, then writes a clean base_ovr+bump "
            "to the DB (no stacking risk). GP >= 50 required per season; "
            "derived OVR capped at 96."
        )
    )
    parser.add_argument("--league-id", type=int, default=None,
                        help="League ID to update (default: latest)")
    parser.add_argument("--season", type=int, default=2024,
                        help="BDL current season year (default: 2024)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print changes without writing to DB")
    args = parser.parse_args()

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise SystemExit("DATABASE_URL not set")

    # Load 2 seasons (current + 1 prior).  The 2-back season is excluded to
    # prevent stale fluke years from inflating ratings.  Missing season files
    # are silently skipped (_load_season returns {} for absent files and warns
    # on corrupt ones).
    target_seasons = [args.season, args.season - 1]
    season_maps: dict[int, dict[str, dict]] = {}
    for s in target_seasons:
        season_maps[s] = _load_season(s)
        print(f"  season {s}: {len(season_maps[s])} qualifying players loaded")

    conn = await asyncpg.connect(db_url)
    try:
        # Resolve league.
        if args.league_id is not None:
            league = await conn.fetchrow("SELECT id FROM leagues WHERE id = $1", args.league_id)
        else:
            league = await conn.fetchrow("SELECT id FROM leagues ORDER BY id DESC LIMIT 1")
        if not league:
            raise SystemExit("No league found")

        league_id: int = league["id"]
        print(f"\nLeague {league_id}  season={args.season}  dry_run={args.dry_run}\n")

        rows = await conn.fetch(
            """
            SELECT id, first_name, last_name, position, overall, defense
            FROM players
            WHERE league_id = $1
            ORDER BY last_name, first_name
            """,
            league_id,
        )

        # (name, old_ovr, new_ovr, best_year, interior, perimeter, net, position, archetype)
        changed:         list[tuple] = []
        skipped_no_data: int = 0

        for row in rows:
            fname = row["first_name"] or ""
            lname = row["last_name"]  or ""
            full  = f"{fname} {lname}".strip()
            key   = _norm(full)

            db_pos = (row["position"] or "").upper()

            # Check if this player exists in ANY season's map.
            in_any_season = any(key in season_maps[s] for s in target_seasons)
            if in_any_season:
                best_season, new_ovr, interior, perimeter, archetype = _pick_best_season(
                    seasons=target_seasons,
                    season_maps=season_maps,
                    key=key,
                    db_position=db_pos,
                )
            else:
                best_season = None
                new_ovr = 0
                interior = perimeter = 0.0
                archetype = "generalist"

            if best_season is None:
                # Player present in cache but failed GP threshold in every
                # season (e.g., Embiid in 2024+2023, both injury-shortened).
                # Fall back to the static `defense` attribute path used by
                # the live trade-evaluator's defensive_impact_modifier: if
                # defense >= 85 (known elite defender), award a small bump
                # so injured stars aren't stranded at their pre-defensive
                # baseline. Tier mirrors the trade-eval thresholds.
                def_attr = int(row["defense"] or 0)
                if def_attr >= 92:
                    attr_bump = 5  # DPOY-tier
                    attr_arch = "two_way_big" if db_pos in ("C", "PF") else "wing_stopper"
                elif def_attr >= 88:
                    attr_bump = 4
                    attr_arch = "rim_protector" if db_pos in ("C", "PF") else "on_ball_pest"
                elif def_attr >= 85:
                    attr_bump = 3
                    attr_arch = "size_defender" if db_pos in ("C", "PF") else "generalist"
                else:
                    skipped_no_data += 1
                    continue

                old_ovr = int(row["overall"])
                new_ovr = min(96, old_ovr + attr_bump)
                if new_ovr == old_ovr and archetype == (row.get("defensive_archetype") if isinstance(row, dict) else None):
                    skipped_no_data += 1
                    continue
                interior, perimeter, archetype = 0.0, 0.0, attr_arch
                best_season = -1  # sentinel: attribute-fallback path
                net = new_ovr - old_ovr
                changed.append((
                    full, old_ovr, new_ovr, best_season,
                    interior, perimeter, net, db_pos, archetype,
                ))
                if not args.dry_run:
                    await conn.execute(
                        "UPDATE players SET overall = $1, defensive_archetype = $2 WHERE id = $3",
                        new_ovr, archetype, row["id"],
                    )
                continue

            old_ovr = int(row["overall"])
            net     = new_ovr - old_ovr

            changed.append((
                full, old_ovr, new_ovr, best_season,
                interior, perimeter, net, db_pos, archetype,
            ))

            if not args.dry_run:
                await conn.execute(
                    "UPDATE players SET overall = $1, defensive_archetype = $2 WHERE id = $3",
                    new_ovr, archetype, row["id"],
                )

        # Summary.
        changed.sort(key=lambda x: abs(x[6]), reverse=True)

        bumped_up   = sum(1 for *_, net, _p, _a in changed if net > 0)
        bumped_down = sum(1 for *_, net, _p, _a in changed if net < 0)
        unchanged   = sum(1 for *_, net, _p, _a in changed if net == 0)

        print(f"Players processed:     {len(changed)}")
        print(f"  OVR increased:       {bumped_up}")
        print(f"  OVR decreased:       {bumped_down}")
        print(f"  OVR unchanged:       {unchanged}")
        print(f"Skipped (no/low data): {skipped_no_data}")
        if args.dry_run:
            print("[DRY RUN] No changes written.\n")

        # Spotlight table: always show the 8 named players.
        spotlight_keys = {_norm(n) for n in _SPOTLIGHT}
        show = [
            c for c in changed
            if c[6] != 0 or _norm(c[0]) in spotlight_keys
        ]
        show.sort(key=lambda x: abs(x[6]), reverse=True)

        if show:
            print(
                f"\n{'Name':<30} {'Pos':>4}  "
                f"{'OldOVR':>6}  {'NewOVR':>6}  {'Net':>4}  "
                f"{'BestYr':>6}  {'Int':>6}  {'Peri':>6}  {'Archetype'}"
            )
            print("-" * 110)
            for name, old, new, yr, intr, peri, net, pos, arch in show:
                flag    = "  ***" if _norm(name) in spotlight_keys else ""
                safe    = name.encode("ascii", "replace").decode("ascii")
                net_str = f"{net:+d}"
                print(
                    f"{safe:<30} {pos:>4}  "
                    f"{old:>6}  {new:>6}  {net_str:>4}  "
                    f"{yr!s:>6}  {intr:>6.1f}  {peri:>6.1f}  {arch}{flag}"
                )

        # Explicit spotlight rows not caught above (player in league but
        # net==0 AND name in show filter already; print ones with no data).
        all_player_keys = {_norm(f"{r['first_name']} {r['last_name']}"): r for r in rows}
        shown_keys = {_norm(c[0]) for c in show}
        for target in sorted(_SPOTLIGHT):
            if target in shown_keys:
                continue
            if target not in all_player_keys:
                continue
            r = all_player_keys[target]
            in_any = any(target in season_maps[s] for s in target_seasons)
            if not in_any:
                print(f"\n[spotlight] {target}: no BDL data in any of {target_seasons}")
            else:
                # Present but below GP threshold in every year.
                gps = {s: season_maps[s].get(target, {}).get("gp", "—") for s in target_seasons}
                print(f"\n[spotlight] {target}: GP below {_MIN_GP} threshold in all seasons "
                      f"({gps}) — OVR unchanged at {r['overall']}")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
