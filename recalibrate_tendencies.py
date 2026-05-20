"""
recalibrate_tendencies.py

Apply pre-built tendency snapshots (data/tendency_cache/season_{year}.json)
to all players in a league. Defaults to the league's current_season.

Usage:
    python recalibrate_tendencies.py             # uses league 1, current_season
    python recalibrate_tendencies.py 2022        # override season
    python recalibrate_tendencies.py 1 2022      # league_id + season

Players are matched to the cache by normalized name. If a player has no cache
entry (name mismatch or low GP), attribute-derived fallback values are used.
Run build_tendency_snapshots.py first if the cache directory is empty.
"""
import asyncio
import asyncpg
import json
import pathlib
import sys
import unicodedata

TEND_CACHE_DIR = pathlib.Path(__file__).parent / "data" / "tendency_cache"
MIN_GP = 5


# ---------------------------------------------------------------------------
# Name normalization — must stay in sync with build_tendency_snapshots.py
# ---------------------------------------------------------------------------

_NAME_ALIASES: dict[str, str] = {
    "alex sarr":       "alexandre sarr",
    "nic claxton":     "nicolas claxton",
    "bones hyland":    "nahshon hyland",
    "bub carrington":  "carlton carrington",
}


def _norm(name: str) -> str:
    normalized = " ".join(
        unicodedata.normalize("NFKD", name)
        .encode("ascii", "ignore")
        .decode()
        .lower()
        .replace(".", "")
        .replace("'", "")
        .strip()
        .split()
    )
    return _NAME_ALIASES.get(normalized, normalized)


# ---------------------------------------------------------------------------
# Attribute-derived fallback (no cache entry)
# ---------------------------------------------------------------------------

def _fallback(attrs: dict, position: str) -> dict:
    pm  = float(attrs.get("playmaking", 50) or 50)
    spd = float(attrs.get("speed", 50) or 50)
    iq  = float(attrs.get("iq", 50) or 50)
    reb = float(attrs.get("rebounding", 50) or 50)
    ovr = float(attrs.get("overall", 70) or 70)
    pos = (position or "SF").upper()

    ast_t = int(max(15, min(95, round(25 + (pm / 99) * 65))))
    stl_t = int(max(15, min(95, round(20 + ((spd + iq) / 2 / 99) * 55))))
    blk_a = reb if pos in ("C", "PF") else iq
    blk_t = int(max(15, min(95, round(15 + (blk_a / 99) * 65))))
    reb_t = int(max(15, min(95, round(20 + (reb / 99) * 70))))
    usage = int(max(15, min(90, round(30 + (ovr / 99) * 60))))

    return {
        "tendency_3pt":     50,
        "tendency_drive":   50,
        "tendency_mid":     50,
        "tendency_post":    10 if pos in ("C", "PF") else 0,
        "tendency_pass":    50,
        "usage_weight":     usage,
        "defensive_effort": 50,
        "clutch_rating":    50,
        "ast_tendency":     ast_t,
        "reb_tendency":     reb_t,
        "stl_tendency":     stl_t,
        "blk_tendency":     blk_t,
        "defense_tendency": 50,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    args = sys.argv[1:]
    league_id = 1
    season_override: int | None = None
    if len(args) == 1:
        season_override = int(args[0])
    elif len(args) >= 2:
        league_id = int(args[0])
        season_override = int(args[1])

    pool = await asyncpg.create_pool(
        "postgresql://dba:dba@localhost:5434/dba",
        min_size=2, max_size=5,
    )

    # Determine season to use
    if season_override:
        season = season_override
    else:
        row = await pool.fetchrow("SELECT current_season FROM leagues WHERE id = $1", league_id)
        season = row["current_season"] if row else 2024

    cache_path = TEND_CACHE_DIR / f"season_{season}.json"
    if not cache_path.exists():
        print(f"No tendency cache for season {season}. Run build_tendency_snapshots.py first.")
        await pool.close()
        return

    with open(cache_path, encoding="utf-8") as f:
        cache: dict[str, dict] = json.load(f)
    print(f"Tendency cache loaded: season {season}, {len(cache)} entries")

    players = await pool.fetch("""
        SELECT id, first_name, last_name, position, overall,
               playmaking, speed, iq, rebounding
        FROM players WHERE league_id = $1
        ORDER BY overall DESC
    """, league_id)
    print(f"Players in league {league_id}: {len(players)}")

    updated = 0
    cache_hit = 0
    fallback_count = 0
    errors = 0

    async with pool.acquire() as conn:
        for p in players:
            pid = p["id"]
            position = p["position"] or "SF"
            name_key = _norm(f"{p['first_name']} {p['last_name']}")

            try:
                rec = cache.get(name_key)
                if rec:
                    tends = rec
                    cache_hit += 1
                else:
                    tends = _fallback(dict(p), position)
                    fallback_count += 1

                await conn.execute("""
                    UPDATE players SET
                        tendency_3pt     = $1,
                        tendency_drive   = $2,
                        tendency_mid     = $3,
                        tendency_post    = $4,
                        tendency_pass    = $5,
                        usage_weight     = $6,
                        defensive_effort = $7,
                        clutch_rating    = $8,
                        ast_tendency     = $9,
                        reb_tendency     = $10,
                        stl_tendency     = $11,
                        blk_tendency     = $12,
                        defense_tendency = $13
                    WHERE id = $14
                """, tends["tendency_3pt"], tends["tendency_drive"], tends["tendency_mid"],
                     tends["tendency_post"], tends["tendency_pass"], tends["usage_weight"],
                     tends["defensive_effort"], tends["clutch_rating"],
                     tends["ast_tendency"], tends["reb_tendency"],
                     tends["stl_tendency"], tends["blk_tendency"],
                     tends["defense_tendency"], pid)
                updated += 1

            except Exception as exc:
                n = (p["first_name"] + " " + p["last_name"]).encode("ascii", "replace").decode()
                print(f"  ERROR {n}: {exc}")
                errors += 1

    print(f"\nDone: {updated} updated | {cache_hit} cache hits | {fallback_count} fallback | {errors} errors")

    # Spot-check
    rows = await pool.fetch("""
        SELECT first_name, last_name, position, overall,
               tendency_3pt, tendency_drive, tendency_pass,
               ast_tendency, reb_tendency, usage_weight
        FROM players WHERE league_id = $1
        ORDER BY overall DESC LIMIT 10
    """, league_id)
    print(f"\n=== Top 10 — season {season} tendencies ===")
    for r in rows:
        name = (r["first_name"] + " " + r["last_name"]).encode("ascii", "replace").decode()
        print(f"  {name} ({r['position']} OVR {r['overall']}): "
              f"3pt={r['tendency_3pt']} drive={r['tendency_drive']} pass={r['tendency_pass']} "
              f"ast={r['ast_tendency']} reb={r['reb_tendency']} usage={r['usage_weight']}")

    await pool.close()


asyncio.run(main())
