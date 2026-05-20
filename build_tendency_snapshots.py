"""
build_tendency_snapshots.py

For each BDL cache season (2010–2024), pull from base + advanced + usage stats
to compute all tendency fields for every player in that season, then write:
    data/tendency_cache/season_{year}.json

Data sources per field:
  tendency_3pt     → base: fg3a/fga ratio
  tendency_drive   → base: fta/fga ratio
  tendency_mid     → base: derived (1 - 3rate - drate*0.5)
  tendency_post    → position constant
  tendency_pass    → usage: pct_ast / pct_fga (pass-vs-score split)
  usage_weight     → advanced: usg_pct (actual usage rate)
  defensive_effort → usage: pct_stl + pct_blk*0.5 (position-relative)
  clutch_rating    → advanced: PIE (Player Impact Estimate)
  ast_tendency     → advanced: ast_pct position-relative sqrt
  reb_tendency     → advanced: reb_pct position-relative sqrt
  stl_tendency     → usage: pct_stl position-relative sqrt
  blk_tendency     → usage: pct_blk position-relative sqrt
  defense_tendency → usage: 60% pct_stl + 40% pct_blk position-relative sqrt

Usage:
    python build_tendency_snapshots.py              # all seasons 2010-2024
    python build_tendency_snapshots.py 2024         # single season
    python build_tendency_snapshots.py 2020 2024    # inclusive range
"""
import json
import math
import pathlib
import sys
import unicodedata

BDL_CACHE_DIR  = pathlib.Path(__file__).parent / "data" / "bdl_cache"
TEND_CACHE_DIR = pathlib.Path(__file__).parent / "data" / "tendency_cache"
MIN_GP = 5
SEASONS = list(range(2010, 2025))


# ---------------------------------------------------------------------------
# Name normalization
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


# ---------------------------------------------------------------------------
# Cache loaders — all three files per season
# ---------------------------------------------------------------------------

def _load_season(season: int) -> tuple[list[dict], list[dict], list[dict]]:
    """Return (base_records, advanced_records, usage_records) for a season."""
    def _read(suffix: str) -> list[dict]:
        path = BDL_CACHE_DIR / f"season_{season}_{suffix}.json"
        if not path.exists():
            return []
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return _read("base"), _read("advanced"), _read("usage")


def _by_name(records: list[dict]) -> dict[str, dict]:
    """Build {normalized_full_name: stats_dict} from a record list."""
    out: dict[str, dict] = {}
    for rec in records:
        p = rec["player"]
        key = _norm(f"{p['first_name']} {p['last_name']}")
        out[key] = rec
    return out


# ---------------------------------------------------------------------------
# Position-average computation for pct-based fields
# ---------------------------------------------------------------------------

# Fields we need position-relative scaling for
_PCT_FIELDS = ("ast_pct", "reb_pct", "pct_stl", "pct_blk")


def _pos_bucket(position: str) -> str:
    pos = (position or "G").upper()
    return pos[-1] if pos[-1] in ("G", "F", "C") else "G"


def _position_avgs(
    adv_by_name: dict[str, dict],
    usg_by_name: dict[str, dict],
) -> dict[str, dict[str, float]]:
    """Compute per-position-bucket averages for ast_pct, reb_pct, pct_stl, pct_blk."""
    buckets: dict[str, dict[str, list[float]]] = {}

    for key, adv_rec in adv_by_name.items():
        a_stats = adv_rec.get("stats", {})
        gp = int(a_stats.get("gp", 0) or 0)
        if gp < 10:
            continue
        pos = _pos_bucket(adv_rec["player"].get("position") or "G")
        bucket = buckets.setdefault(pos, {f: [] for f in _PCT_FIELDS})

        # ast_pct, reb_pct from advanced
        for field in ("ast_pct", "reb_pct"):
            v = a_stats.get(field)
            if v is not None:
                bucket[field].append(float(v))

        # pct_stl, pct_blk from usage (same player, same normalized name)
        usg_rec = usg_by_name.get(key)
        if usg_rec:
            u_stats = usg_rec.get("stats", {})
            for field in ("pct_stl", "pct_blk"):
                v = u_stats.get(field)
                if v is not None:
                    bucket[field].append(float(v))

    # Flatten to averages
    all_vals: dict[str, list[float]] = {f: [] for f in _PCT_FIELDS}
    avgs: dict[str, dict[str, float]] = {}
    for pos, stat_lists in buckets.items():
        avgs[pos] = {}
        for field, vals in stat_lists.items():
            avg = sum(vals) / len(vals) if vals else 0.01
            avgs[pos][field] = avg
            all_vals[field].extend(vals)

    global_avgs = {
        f: (sum(v) / len(v) if v else 0.01)
        for f, v in all_vals.items()
    }
    for pos in ("G", "F", "C"):
        if pos not in avgs:
            avgs[pos] = dict(global_avgs)
        else:
            for f in _PCT_FIELDS:
                if avgs[pos].get(f, 0.0) == 0.0:
                    avgs[pos][f] = global_avgs.get(f, 0.01)

    return avgs


# ---------------------------------------------------------------------------
# Tendency computation
# ---------------------------------------------------------------------------

def _sqrt_tend(val: float, avg: float, lo: int = 5, hi: int = 95) -> int:
    """Position-relative sqrt-scaled tendency.  avg → 50, 4×avg → 95."""
    ratio = max(0.0, val / avg) if avg > 0 else 0.0
    return max(lo, min(hi, int(round(math.sqrt(ratio) * 50))))


def _clutch_from_pie(pie: float) -> int:
    """Map PIE → clutch_rating.  League avg ~0.08-0.10 → 50; MVP ~0.20 → 90+."""
    # Linear: (pie - 0.04) / 0.16 maps [0.04, 0.20] → [0, 1]
    scaled = (float(pie) - 0.04) / 0.16
    return max(10, min(95, int(round(scaled * 85 + 10))))


def _compute(
    base_stats: dict,
    adv_stats:  dict,
    usg_stats:  dict,
    position:   str,
    pos_avgs:   dict[str, dict[str, float]],
) -> dict:
    # ---- base fields ----
    gp    = int(base_stats.get("gp", 0) or 0)
    fga   = float(base_stats.get("fga", 0) or 0) or 1.0
    fg3a  = float(base_stats.get("fg3a", 0) or 0)
    fta   = float(base_stats.get("fta", 0) or 0)
    pts   = float(base_stats.get("pts", 0) or 0)
    ast_pg = float(base_stats.get("ast", 0) or 0)   # per-game AST (raw reference)
    reb_pg = float(base_stats.get("reb", 0) or 0)
    stl_pg = float(base_stats.get("stl", 0) or 0)
    blk_pg = float(base_stats.get("blk", 0) or 0)

    # ---- advanced fields ----
    usg_pct  = float(adv_stats.get("usg_pct", 0.20) or 0.20)
    ast_pct  = float(adv_stats.get("ast_pct", 0.0)  or 0.0)   # % teammate FGMs assisted
    reb_pct  = float(adv_stats.get("reb_pct", 0.0)  or 0.0)   # total rebound %
    pie      = float(adv_stats.get("pie",     0.08)  or 0.08)  # Player Impact Estimate
    ts_pct   = float(adv_stats.get("ts_pct",  0.50)  or 0.50)  # true shooting %

    # ---- usage fields ----
    pct_ast  = float(usg_stats.get("pct_ast",  0.0) or 0.0)   # % of team assists
    pct_blk  = float(usg_stats.get("pct_blk",  0.0) or 0.0)   # % of opp 2pt FGA blocked
    pct_stl  = float(usg_stats.get("pct_stl",  0.0) or 0.0)   # % of opp poss stolen
    pct_fga  = float(usg_stats.get("pct_fga",  0.0) or 0.0)   # % of team FGA

    pos = _pos_bucket(position)
    pa  = pos_avgs.get(pos, {f: 0.01 for f in _PCT_FIELDS})

    # ── Shot selection (from base) ──────────────────────────────────────────
    three_rate = min(fg3a / fga, 1.0)
    drive_rate = min(fta / fga, 1.0)
    mid_rate   = max(0.0, 1.0 - three_rate - drive_rate * 0.5)
    post_rate  = 0.1 if position.upper() in ("C", "PF") else 0.0

    tendency_3pt   = max(0, min(100, int(round(three_rate * 100))))
    tendency_drive = max(0, min(100, int(round(drive_rate * 80))))
    tendency_mid   = max(0, min(100, int(round(mid_rate * 70))))
    tendency_post  = max(0, min(100, int(round(post_rate * 100))))

    # ── Tendency pass (pct_ast / pct_fga ratio, 1.0 = balanced) ───────────
    # Measures whether a player prioritizes setting up teammates vs shooting.
    # Jokic (~1.39) → ~59; Kawhi (~0.57) → ~38; pure scorer → ~24.
    if pct_fga > 0 and pct_ast > 0:
        pass_ratio = pct_ast / pct_fga
    elif pct_ast > 0:
        pass_ratio = pct_ast / 0.25  # fallback denominator
    else:
        pass_ratio = 1.0
    tendency_pass = max(5, min(95, int(round(math.sqrt(pass_ratio) * 50))))

    # ── Usage weight (direct USG%) ──────────────────────────────────────────
    # usg_pct=0.35 (superstar) → 95, usg_pct=0.20 (average) → 54, 0.10 → 27
    usage_weight = max(10, min(95, int(round((usg_pct / 0.35) * 95))))

    # ── Clutch rating (PIE) ─────────────────────────────────────────────────
    # PIE ~0.12 league avg → ~60; MVP-level ~0.20 → 90+; scrub ~0.04 → 10
    clutch_rating = _clutch_from_pie(pie)

    # ── Position-relative tendencies (pct_ metrics, sqrt-scaled) ───────────
    ast_tendency = _sqrt_tend(ast_pct,  pa.get("ast_pct",  0.01))
    reb_tendency = _sqrt_tend(reb_pct,  pa.get("reb_pct",  0.01))
    stl_tendency = _sqrt_tend(pct_stl,  pa.get("pct_stl",  0.01))
    blk_tendency = _sqrt_tend(pct_blk,  pa.get("pct_blk",  0.01))

    # ── Defense composite ───────────────────────────────────────────────────
    # Position-aware weights: blocks matter far more for bigs (rim protection),
    # steals dominate for guards (perimeter disruption).
    # C/PF: 30% stl + 70% blk — Gobert/Wemby profile
    # SF/SG (F bucket): 50%/50% — two-way wings like Anunoby, OG
    # PG/SG (G bucket): 70% stl + 30% blk — Holiday/Marcus Smart profile
    stl_rel = max(0.0, pct_stl / (pa.get("pct_stl") or 0.01))
    blk_rel = max(0.0, pct_blk / (pa.get("pct_blk") or 0.01))
    if pos == "C":
        def_weights = (0.30, 0.70)
    elif pos == "F":
        def_weights = (0.50, 0.50)
    else:  # G
        def_weights = (0.70, 0.30)
    def_raw = int(round(math.sqrt(stl_rel * def_weights[0] + blk_rel * def_weights[1]) * 50))
    defense_tendency = max(5, min(85, def_raw))

    # ── Defensive effort (per-floor-time defensive activity rate) ───────────
    # stl + blk*0.5 per possession proxy; position-relative; capped 10-100
    stl_blk_rate = stl_rel * 0.65 + blk_rel * 0.35
    defensive_effort = max(10, min(100, int(round(math.sqrt(stl_blk_rate) * 50))))

    return {
        # Shot selection
        "tendency_3pt":     tendency_3pt,
        "tendency_drive":   tendency_drive,
        "tendency_mid":     tendency_mid,
        "tendency_post":    tendency_post,
        # Passing & scoring role
        "tendency_pass":    tendency_pass,
        "usage_weight":     usage_weight,
        # Broad impact
        "clutch_rating":    clutch_rating,
        # Position-relative defensive/rebounding
        "defensive_effort": defensive_effort,
        "ast_tendency":     ast_tendency,
        "reb_tendency":     reb_tendency,
        "stl_tendency":     stl_tendency,
        "blk_tendency":     blk_tendency,
        "defense_tendency": defense_tendency,
        # Raw stats for inspection / future use
        "_raw": {
            "gp":     gp,
            "pts":    round(pts, 2),
            "ast":    round(ast_pg, 2),
            "reb":    round(reb_pg, 2),
            "stl":    round(stl_pg, 2),
            "blk":    round(blk_pg, 2),
            "fg3a":   round(fg3a, 2),
            "fga":    round(fga, 2),
            "fta":    round(fta, 2),
            "usg_pct":  round(usg_pct, 3),
            "ast_pct":  round(ast_pct, 3),
            "reb_pct":  round(reb_pct, 3),
            "pct_ast":  round(pct_ast, 3),
            "pct_stl":  round(pct_stl, 3),
            "pct_blk":  round(pct_blk, 3),
            "pie":      round(pie, 3),
            "ts_pct":   round(ts_pct, 3),
        },
    }


# ---------------------------------------------------------------------------
# Per-season builder
# ---------------------------------------------------------------------------

def _build_season(season: int) -> int:
    base_recs, adv_recs, usg_recs = _load_season(season)
    if not base_recs:
        print(f"  {season}: base cache missing, skipped")
        return 0

    adv_by_name = _by_name(adv_recs)
    usg_by_name = _by_name(usg_recs)
    pos_avgs    = _position_avgs(adv_by_name, usg_by_name)

    out: dict[str, dict] = {}
    for rec in base_recs:
        stats = rec.get("stats", {})
        gp    = int(stats.get("gp", 0) or 0)
        if gp < MIN_GP:
            continue

        p    = rec["player"]
        pos  = p.get("position") or "G"
        full = f"{p['first_name']} {p['last_name']}"
        key  = _norm(full)

        adv_rec = adv_by_name.get(key)
        usg_rec = usg_by_name.get(key)
        adv_stats = adv_rec["stats"] if adv_rec else {}
        usg_stats = usg_rec["stats"] if usg_rec else {}

        tends = _compute(stats, adv_stats, usg_stats, pos, pos_avgs)
        tends["season"]   = season
        tends["position"] = pos
        tends["name"]     = full
        out[key] = tends

    TEND_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dst = TEND_CACHE_DIR / f"season_{season}.json"
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    return len(out)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]
    if len(args) == 0:
        seasons = SEASONS
    elif len(args) == 1:
        seasons = [int(args[0])]
    else:
        lo, hi = int(args[0]), int(args[1])
        seasons = list(range(lo, hi + 1))

    print(f"Building tendency snapshots (base+advanced+usage) for seasons {seasons[0]}–{seasons[-1]}")
    total = 0
    for season in seasons:
        count = _build_season(season)
        if count:
            print(f"  {season}: {count} players")
        total += count

    print(f"\nDone. {total} player-season records across {len(seasons)} seasons.")
    print(f"Output: {TEND_CACHE_DIR}")


if __name__ == "__main__":
    main()
