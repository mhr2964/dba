import json
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from import_players import _composite_from_row, _overall_from_raw_sum
from nba_api.stats.static import players as nba_players_static

CACHE = Path(__file__).parent.parent / "data" / "bdl_cache"

def normalize(name):
    return unicodedata.normalize("NFKD", name).encode("ascii","ignore").decode().lower().strip()

def load_cache(season, t):
    p = CACHE / f"season_{season}_{t}.json"
    if not p.exists():
        return {}
    with open(p) as f:
        records = json.load(f)
    return {rec["player"]["id"]: rec for rec in records}


def load_advanced(season):
    return load_cache(season, "advanced")

def best_ovr(bdl_id, seasons, base_by_s, usage_by_s, advanced_by_s):
    best_raw = -1.0
    best_row = None
    for s in seasons:
        base_rec = base_by_s[s].get(bdl_id)
        if not base_rec:
            continue
        gp = int(base_rec["stats"].get("gp", 0) or 0)
        if gp < 10:
            continue
        usg_rec = usage_by_s[s].get(bdl_id)
        adv_rec = advanced_by_s[s].get(bdl_id)
        usg = usg_rec["stats"] if usg_rec else {}
        adv = adv_rec["stats"] if adv_rec else {}
        bs = base_rec["stats"]
        row = {
            "gp": gp,
            "PTS":     float(bs.get("pts",    0) or 0),
            "REB":     float(bs.get("reb",    0) or 0),
            "AST":     float(bs.get("ast",    0) or 0),
            "STL":     float(bs.get("stl",    0) or 0),
            "BLK":     float(bs.get("blk",    0) or 0),
            "FG3_PCT": float(bs.get("fg3_pct",0) or 0),
            "FG3A":    float(bs.get("fg3a",   0) or 0),
            "USG_PCT": float(usg.get("usg_pct",0) or 0),
            "FGA":     float(bs.get("fga",    0) or 0),
            "FTA":     float(bs.get("fta",    0) or 0),
        }
        if adv.get("def_rating") is not None:
            row["DEF_RTG"] = float(adv["def_rating"])
        raw = _composite_from_row(row)
        if raw > best_raw:
            best_raw = raw
            best_row = row
    if best_raw >= 0:
        return _overall_from_raw_sum(best_raw), best_row
    return None, None

season = int(sys.argv[1]) if len(sys.argv) > 1 else 2015
seasons = [season, season - 1, season - 2]

base_by_s     = {s: load_cache(s, "base")    for s in seasons}
usage_by_s    = {s: load_cache(s, "usage")   for s in seasons}
advanced_by_s = {s: load_advanced(s)         for s in seasons}

name_map = {normalize(f"{p['first_name']} {p['last_name']}"): p["id"] for p in nba_players_static.get_players()}

# Build bdl_id -> nba_id
bdl_to_nba = {}
for s in seasons:
    for bdl_id, rec in base_by_s[s].items():
        if bdl_id in bdl_to_nba:
            continue
        p = rec["player"]
        key = normalize(f"{p['first_name']} {p['last_name']}")
        nba_id = name_map.get(key)
        if nba_id:
            bdl_to_nba[bdl_id] = nba_id

nba_to_bdl = {v: k for k, v in bdl_to_nba.items()}

TARGETS_BY_ERA = {
    # 2015-era
    "2015": [
        "Stephen Curry", "Kevin Durant", "LeBron James", "Russell Westbrook",
        "Kawhi Leonard", "Damian Lillard", "Kyle Lowry", "Paul George",
        "DeMar DeRozan", "Draymond Green", "Brook Lopez", "Bismack Biyombo", "Tyler Zeller",
    ],
    # 2022-era
    "2022": [
        "Nikola Jokic", "Giannis Antetokounmpo", "Stephen Curry", "Kevin Durant", "LeBron James",
        "Joel Embiid", "Jayson Tatum", "Devin Booker", "Rudy Gobert", "Mikal Bridges",
        "Chris Paul", "Kyle Lowry", "Luguentz Dort", "Matisse Thybulle", "Mo Bamba",
    ],
}
targets = TARGETS_BY_ERA.get(str(season), TARGETS_BY_ERA["2015"])

print(f"\nSeason {season}-{str(season+1)[2:]} OVR samples (3-season peak window: {seasons})\n")
print(f"{'Player':<22}  {'OVR':>3}  {'Tier':<11} {'PTS':>5} {'REB':>5} {'AST':>5} {'USG':>5} {'DEF':>5}")
print("-" * 72)

for name in targets:
    nba_id = name_map.get(normalize(name))
    if not nba_id:
        print(f"{name:<22}  ???  (not in nba_api)")
        continue
    bdl_id = nba_to_bdl.get(nba_id)
    if not bdl_id:
        print(f"{name:<22}  ---  (no BDL data for these seasons)")
        continue
    ovr, row = best_ovr(bdl_id, seasons, base_by_s, usage_by_s, advanced_by_s)
    if ovr is None:
        print(f"{name:<22}  ---  (< 10 GP in all 3 seasons)")
        continue
    tier = "Elite" if ovr >= 93 else "All-Star" if ovr >= 87 else "Starter" if ovr >= 80 else "Role Plyr" if ovr >= 72 else "Bench"
    def_rtg = row.get("DEF_RTG")
    def_str = f"{def_rtg:>5.1f}" if def_rtg else "  N/A"
    print(f"{name:<22}  {ovr:>3}  {tier:<11} {row['PTS']:>5.1f} {row['REB']:>5.1f} {row['AST']:>5.1f} {row['USG_PCT']:>5.2f} {def_str}")
