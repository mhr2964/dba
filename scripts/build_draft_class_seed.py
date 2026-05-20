"""Build a year-accurate draft class seed file for the DBA sim bot.

Two modes:
  historical  — pulls real draft history + birth dates from nba_api, derives OVR
                from stats_ratings/{year}.json or pick-number tier.
  projected   — reads a hand-curated curator file and generates OVR from tier bands.

The output is consumed by services/draft_service.ensure_draft_class() which
checks for the seed file before falling back to the synthetic generator.

Usage:
    python scripts/build_draft_class_seed.py --year 2024 --mode historical
    python scripts/build_draft_class_seed.py --year 2025 --mode projected
    python scripts/build_draft_class_seed.py --year 2024 --mode historical --dry-run
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
import unicodedata
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent
SEEDS_DIR = REPO_ROOT / "data" / "seeds"
STATS_RATINGS_DIR = REPO_ROOT / "data" / "stats_ratings"
PLAYER_INFO_CACHE_DIR = REPO_ROOT / "data" / "bdl_cache" / "player_info"

# Courtesy delay between nba_api per-player calls.
_API_DELAY = 0.7  # seconds

# ---------------------------------------------------------------------------
# Shared helpers (mirror build_stats_ratings._normalize)
# ---------------------------------------------------------------------------

def _normalize(name: str) -> str:
    """Lowercase + strip accents: 'Luka Dončić' == 'luka doncic'."""
    return (
        unicodedata.normalize("NFKD", name)
        .encode("ascii", "ignore")
        .decode()
        .lower()
        .strip()
    )


def _clamp(val: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, val))


# ---------------------------------------------------------------------------
# Attribute generation — imported directly from import_service
# (uses ±5 jitter, consistent with real-player import flow)
# ---------------------------------------------------------------------------

# Inline here so the script has no runtime dependency on the bot's service layer.
_POSITION_PROFILE: dict[str, dict[str, int]] = {
    "PG": {
        "speed": 8, "playmaking": 10, "shooting_3pt": 5,
        "shooting_2pt": 2, "shooting_mid": 2,
        "finishing": -5, "defense": -2, "rebounding": -12, "iq": 5,
    },
    "SG": {
        "speed": 4, "shooting_3pt": 8, "shooting_2pt": 8, "shooting_mid": 6,
        "playmaking": 2, "finishing": 0, "defense": 0, "rebounding": -8, "iq": 2,
    },
    "SF": {
        "speed": 0, "shooting_3pt": 2, "shooting_2pt": 2, "shooting_mid": 2,
        "finishing": 2, "playmaking": 0, "defense": 2, "rebounding": 0, "iq": 0,
    },
    "PF": {
        "speed": -4, "rebounding": 8, "finishing": 6, "shooting_mid": 4,
        "shooting_3pt": -8, "shooting_2pt": 0, "defense": 4, "playmaking": -4, "iq": 0,
    },
    "C": {
        "speed": -10, "rebounding": 14, "finishing": 10, "defense": 8,
        "shooting_3pt": -18, "shooting_2pt": -4, "shooting_mid": -6,
        "playmaking": -10, "iq": 0,
    },
}

_ALL_ATTRS = [
    "speed", "shooting_2pt", "shooting_3pt", "shooting_mid",
    "finishing", "playmaking", "defense", "rebounding", "iq",
]

_POSITION_MAP = {
    "G": "PG", "G-F": "SG", "F-G": "SG",
    "F": "SF", "F-C": "PF", "C-F": "PF",
    "C": "C",
    # Draft history uses these raw position strings.
    "PG": "PG", "SG": "SG", "SF": "SF", "PF": "PF",
    "Center": "C", "Forward": "SF", "Guard": "PG",
    "Point Guard": "PG", "Shooting Guard": "SG",
    "Small Forward": "SF", "Power Forward": "PF",
}


def _normalize_position(raw: str) -> str:
    return _POSITION_MAP.get((raw or "").strip(), "SF")


def _generate_attributes(overall: int, position: str) -> dict[str, int]:
    """Matches import_service._generate_attributes (±5 jitter)."""
    profile = _POSITION_PROFILE.get(position, _POSITION_PROFILE["SF"])
    return {
        attr: _clamp(overall + profile.get(attr, 0) + random.randint(-5, 5), 40, 99)
        for attr in _ALL_ATTRS
    }


def _hidden_fields(overall: int) -> dict:
    """Personality + contract hidden fields for a rookie."""
    peak_start = random.randint(24, 28)
    peak_end = peak_start + random.randint(4, 6)
    if overall >= 80:
        star_leverage = random.randint(80, 95)
    elif overall >= 72:
        star_leverage = random.randint(50, 70)
    else:
        star_leverage = random.randint(10, 40)
    return {
        "years_pro": 0,
        "is_rookie": True,
        "peak_age_start": peak_start,
        "peak_age_end": peak_end,
        "loyalty": random.randint(0, 100),
        "money_drive": random.randint(0, 100),
        "win_drive": random.randint(0, 100),
        "market_pref": random.choice(["big_market", "neutral", "indifferent", "neutral", "neutral"]),
        "star_leverage": star_leverage,
    }


# ---------------------------------------------------------------------------
# OVR from pick-number tier (fallback when stats are unavailable)
# ---------------------------------------------------------------------------

def _ovr_from_pick_tier(overall_pick: int) -> int:
    """Tier-based OVR fallback.  Spec: 1-5→75, 6-14→70, 15-30→66, 31-60→61."""
    if overall_pick <= 5:
        return 75
    if overall_pick <= 14:
        return 70
    if overall_pick <= 30:
        return 66
    return 61


def _potential_from_pick(ovr: int, overall_pick: int) -> int:
    """Pick-tier heuristic — used only for projected seeds and as a fallback."""
    if overall_pick <= 5:
        return _clamp(ovr + random.randint(10, 15), 55, 99)
    if overall_pick <= 14:
        return _clamp(ovr + random.randint(6, 12), 55, 99)
    if overall_pick <= 30:
        return _clamp(ovr + random.randint(3, 8), 55, 99)
    return _clamp(ovr + random.randint(0, 5), 55, 99)


def _potential_from_career_peak(
    person_id: int,
    draft_year: int,
    fallback_ovr: int,
    fallback_pick: int,
) -> int:
    """Compute potential from career peak OVR across stats_ratings files.

    Scans stats_ratings/{year}.json for year in [draft_year, 2024].
    Uses a fixed RNG seed per player so re-runs produce the same jitter.
    Falls back to pick-tier heuristic when no data is found.
    """
    key = str(person_id)
    found: list[int] = []
    for year in range(draft_year, 2025):
        ratings = _load_stats_ratings(year)
        if key in ratings:
            found.append(ratings[key])

    if not found:
        return _potential_from_pick(fallback_ovr, fallback_pick)

    peak = max(found)
    rng = random.Random(person_id)
    return min(99, peak + rng.randint(2, 5))


# ---------------------------------------------------------------------------
# OVR from tier band (projected mode)
# ---------------------------------------------------------------------------

_TIER_OVR: dict[int, tuple[int, int]] = {
    1: (78, 81),
    2: (74, 78),
    3: (70, 74),
    4: (65, 70),
    5: (60, 65),
}


def _ovr_from_tier(tier: int) -> int:
    lo, hi = _TIER_OVR.get(tier, (60, 65))
    return random.randint(lo, hi)


# ---------------------------------------------------------------------------
# Stats-ratings lookup (keyed by nba player_id → OVR int)
# ---------------------------------------------------------------------------

def _load_stats_ratings(year: int) -> dict[int, int]:
    """Returns {player_id_int: ovr} for the given draft year, or empty dict."""
    path = STATS_RATINGS_DIR / f"{year}.json"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        # Keys are stored as strings in the JSON.
        return {int(k): int(v) for k, v in raw.items()}
    except Exception as exc:
        print(f"[WARN] Could not load stats_ratings/{year}.json: {exc}")
        return {}


# ---------------------------------------------------------------------------
# Player-info cache (birth date)
# ---------------------------------------------------------------------------

def _player_info_cache_path(person_id: int) -> Path:
    return PLAYER_INFO_CACHE_DIR / f"{person_id}.json"


def _load_cached_player_info(person_id: int) -> dict | None:
    path = _player_info_cache_path(person_id)
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def _save_player_info_cache(person_id: int, data: dict) -> None:
    PLAYER_INFO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _player_info_cache_path(person_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _fetch_birth_date(person_id: int) -> str | None:
    """
    Fetches birth date from cache or nba_api CommonPlayerInfo.
    Returns 'YYYY-MM-DD' string or None.
    Caches the full API response to avoid re-fetching.
    """
    cached = _load_cached_player_info(person_id)
    if cached is not None:
        return cached.get("birth_date")

    try:
        from nba_api.stats.endpoints.commonplayerinfo import CommonPlayerInfo
        info = CommonPlayerInfo(player_id=person_id)
        df = info.get_data_frames()[0]
        if df.empty:
            _save_player_info_cache(person_id, {"birth_date": None})
            return None
        raw_bd = str(df.iloc[0].get("BIRTHDATE", "") or "")
        birth_date = raw_bd[:10] if len(raw_bd) >= 10 else None
        _save_player_info_cache(person_id, {"birth_date": birth_date})
        return birth_date
    except Exception as exc:
        print(f"  [WARN] Could not fetch birth date for player {person_id}: {exc}")
        _save_player_info_cache(person_id, {"birth_date": None})
        return None


# ---------------------------------------------------------------------------
# Physical dimensions lookup from draft history
# (DraftHistory provides height/weight; we extract if available)
# ---------------------------------------------------------------------------

_TYPICAL_HEIGHT: dict[str, int] = {
    "PG": 74, "SG": 76, "SF": 78, "PF": 81, "C": 84,
}
_TYPICAL_WEIGHT: dict[str, int] = {
    "PG": 185, "SG": 195, "SF": 210, "PF": 225, "C": 245,
}


def _height_from_position(position: str) -> int:
    return _TYPICAL_HEIGHT.get(position, 78) + random.randint(-2, 2)


def _weight_from_position(position: str) -> int:
    return _TYPICAL_WEIGHT.get(position, 210) + random.randint(-10, 10)


# ---------------------------------------------------------------------------
# Historical mode
# ---------------------------------------------------------------------------

def build_historical(year: int, dry_run: bool) -> list[dict]:
    """
    Pull real draft picks from nba_api DraftHistory for `year`,
    fetch birth dates via CommonPlayerInfo (cached), derive OVR from
    stats_ratings/{year}.json or pick-number tier.
    """
    try:
        from nba_api.stats.endpoints.drafthistory import DraftHistory
    except ImportError:
        print("[ERROR] nba_api not installed. Run: pip install nba_api")
        sys.exit(1)

    print(f"Fetching DraftHistory for season_year_nullable={year} ...")
    draft = DraftHistory(season_year_nullable=str(year))
    df = draft.get_data_frames()[0]

    if df.empty:
        print(f"[ERROR] No draft data returned for year {year}.")
        sys.exit(1)

    print(f"  Got {len(df)} rows from DraftHistory.")

    # Load stats ratings keyed by player_id → OVR
    stats_ratings = _load_stats_ratings(year)
    print(f"  Loaded {len(stats_ratings)} entries from stats_ratings/{year}.json")

    prospects: list[dict] = []

    for _, row in df.iterrows():
        person_id = int(row.get("PERSON_ID", 0) or 0)
        player_name = str(row.get("PLAYER_NAME", "") or "").strip()
        organization = str(row.get("ORGANIZATION", "") or "").strip()
        overall_pick = int(row.get("OVERALL_PICK", 0) or 0)

        if person_id == 0 or not player_name:
            continue

        # Cap at 60 picks (2 rounds × 30).
        if overall_pick > 60:
            continue

        # Parse name — split on last space to handle "De'Aaron Fox" etc.
        parts = player_name.rsplit(" ", 1)
        first_name = parts[0] if len(parts) > 1 else player_name
        last_name = parts[1] if len(parts) > 1 else ""

        # DraftHistory does not include a POSITION column — default all to SF.
        # Positions can be corrected post-build by hand-editing the seed file.
        position = "SF"

        # OVR: prefer stats_ratings lookup (real performance), else tier.
        if person_id in stats_ratings:
            ovr_raw = stats_ratings[person_id]
            ovr = _clamp(ovr_raw, 55, 80)
            ovr_source = "stats_ratings"
        else:
            ovr = _ovr_from_pick_tier(overall_pick)
            ovr_source = "pick_tier"

        # Potential: derive from career peak OVR across stats_ratings files;
        # falls back to pick-tier heuristic if no post-draft data exists.
        potential = _potential_from_career_peak(person_id, year, ovr, overall_pick)
        attrs = _generate_attributes(ovr, position)
        hidden = _hidden_fields(ovr)

        # Fetch birth date — rate-limited, cached.
        # ASCII-encode name for console safety on Windows (non-ASCII chars become ?).
        display_name = player_name.encode("ascii", "replace").decode("ascii")
        print(f"  [{overall_pick:2d}] {display_name} (id={person_id}) OVR={ovr} [{ovr_source}] POT={potential} -- fetching birth date ...", end=" ", flush=True)
        birth_date = _fetch_birth_date(person_id)
        print(birth_date or "N/A")
        time.sleep(_API_DELAY)

        height_in = _height_from_position(position)
        weight_lb = _weight_from_position(position)

        prospect = {
            "external_id": person_id,
            "first_name": first_name,
            "last_name": last_name,
            "full_name": player_name,
            "position": position,
            "height_in": height_in,
            "weight_lb": weight_lb,
            "birth_date": birth_date,
            "college_or_country": organization,
            "mock_rank": overall_pick,
            "overall": ovr,
            "potential": potential,
            "source": "nba_real",
            "season": year,
            # Flat attributes for seed_draft_class()
            **attrs,
            **hidden,
            # Internal key used by seed_draft_class for mock_rank column.
            "_mock_rank": overall_pick,
        }
        prospects.append(prospect)

    return prospects


# ---------------------------------------------------------------------------
# Projected mode
# ---------------------------------------------------------------------------

_PROJECTED_OVERWRITE_MARKER = "_generated"


def build_projected(year: int, dry_run: bool) -> list[dict]:
    """
    Read curator input file data/seeds/draft_class_{year}_inputs.json,
    generate OVR from tier bands, fill out remaining picks with tier-5.
    """
    inputs_path = SEEDS_DIR / f"draft_class_{year}_inputs.json"
    if not inputs_path.exists():
        print(f"[ERROR] Curator input file not found: {inputs_path}")
        sys.exit(1)

    with open(inputs_path, encoding="utf-8") as f:
        inputs = json.load(f)

    # Check overwrite guard on output file.
    if not dry_run:
        out_path = SEEDS_DIR / f"draft_class_{year}.json"
        if out_path.exists():
            with open(out_path, encoding="utf-8") as f:
                existing_text = f.read()
            if _PROJECTED_OVERWRITE_MARKER in existing_text:
                print(
                    f"[ERROR] Output file {out_path} already contains a generated projected class. "
                    "Delete it manually to regenerate (prevents overwriting hand-edits)."
                )
                sys.exit(1)

    prospects: list[dict] = []

    for idx, entry in enumerate(inputs):
        overall_pick = idx + 1
        if overall_pick > 60:
            break

        full_name = str(entry.get("full_name", f"Prospect {overall_pick}")).strip()
        parts = full_name.rsplit(" ", 1)
        first_name = parts[0] if len(parts) > 1 else full_name
        last_name = parts[1] if len(parts) > 1 else ""

        position = _normalize_position(str(entry.get("position", "SF")).strip())
        college_or_country = str(entry.get("college_or_country", "")).strip()
        tier = int(entry.get("tier", 5))

        ovr = _ovr_from_tier(tier)
        ovr = _clamp(ovr, 55, 80)
        potential = _potential_from_pick(ovr, overall_pick)
        attrs = _generate_attributes(ovr, position)
        hidden = _hidden_fields(ovr)

        height_in = _height_from_position(position)
        weight_lb = _weight_from_position(position)

        prospect = {
            "external_id": None,
            "first_name": first_name,
            "last_name": last_name,
            "full_name": full_name,
            "position": position,
            "height_in": height_in,
            "weight_lb": weight_lb,
            "birth_date": None,
            "college_or_country": college_or_country,
            "mock_rank": overall_pick,
            "overall": ovr,
            "potential": potential,
            "source": "projected_consensus",
            "season": year,
            **attrs,
            **hidden,
            "_mock_rank": overall_pick,
            # Overwrite guard marker — present in all projected outputs.
            _PROJECTED_OVERWRITE_MARKER: True,
        }
        prospects.append(prospect)

    # Pad to 60 picks with tier-5 generics if the input has fewer than 60.
    while len(prospects) < 60:
        overall_pick = len(prospects) + 1
        position = random.choice(["PG", "SG", "SF", "PF", "C"])
        ovr = _clamp(_ovr_from_tier(5), 55, 80)
        potential = _potential_from_pick(ovr, overall_pick)
        attrs = _generate_attributes(ovr, position)
        hidden = _hidden_fields(ovr)
        prospects.append({
            "external_id": None,
            "first_name": "Prospect",
            "last_name": f"{overall_pick}",
            "full_name": f"Prospect {overall_pick}",
            "position": position,
            "height_in": _height_from_position(position),
            "weight_lb": _weight_from_position(position),
            "birth_date": None,
            "college_or_country": "",
            "mock_rank": overall_pick,
            "overall": ovr,
            "potential": potential,
            "source": "projected_consensus",
            "season": year,
            **attrs,
            **hidden,
            "_mock_rank": overall_pick,
            _PROJECTED_OVERWRITE_MARKER: True,
        })

    return prospects


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_output(prospects: list[dict], year: int, dry_run: bool) -> None:
    out_path = SEEDS_DIR / f"draft_class_{year}.json"

    if dry_run:
        print(f"\n[DRY RUN] Would write {len(prospects)} rows to {out_path}")
        print("\nTop 5 picks:")
        for p in prospects[:5]:
            print(
                f"  #{p['mock_rank']:2d} {p['full_name']:<22} "
                f"{p['position']} OVR={p['overall']} POT={p['potential']} "
                f"src={p['source']}"
            )
        return

    SEEDS_DIR.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(prospects, f, indent=2, default=str)

    print(f"\nWrote {len(prospects)} rows to {out_path}")
    print("\nTop 5 picks:")
    for p in prospects[:5]:
        print(
            f"  #{p['mock_rank']:2d} {p['full_name']:<22} "
            f"{p['position']} OVR={p['overall']} POT={p['potential']} "
            f"src={p['source']}"
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a draft class seed file for the DBA sim bot."
    )
    parser.add_argument("--year", type=int, required=True, help="Draft class year (e.g. 2024)")
    parser.add_argument(
        "--mode",
        choices=["historical", "projected"],
        required=True,
        help="historical: pull from nba_api; projected: use curator inputs file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print results without writing the seed file",
    )
    args = parser.parse_args()

    print(f"\nBuilding {args.mode} draft class for year {args.year} ...")

    if args.mode == "historical":
        prospects = build_historical(args.year, args.dry_run)
    else:
        prospects = build_projected(args.year, args.dry_run)

    if not prospects:
        print("[ERROR] No prospects generated — aborting.")
        sys.exit(1)

    print(f"\nGenerated {len(prospects)} prospects.")
    write_output(prospects, args.year, args.dry_run)


if __name__ == "__main__":
    main()
