"""Build a static season-start roster snapshot for the DBA reconciler.

Calls CommonTeamRoster via nba_api for every NBA team and writes a JSON
array to data/seeds/rosters_{season}.json.  The file is consumed by the
roster-year consistency reconciler (later slice) — the row shape is stable.

Usage:
    python scripts/build_roster_seed.py
    python scripts/build_roster_seed.py --season 2024
    python scripts/build_roster_seed.py --season 2024 --dry-run
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from nba_api.stats.endpoints.commonteamroster import CommonTeamRoster
from nba_api.stats.static import teams as nba_teams_static

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# All 30 NBA team codes — matches teams.nba_team_code in the DBA database.
NBA_TEAM_CODES = [
    "ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW",
    "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NOP", "NYK",
    "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS",
]

# Courtesy delay between API calls to avoid rate-limiting on stats.nba.com.
_INTER_TEAM_DELAY = 0.8  # seconds
_RETRY_DELAY = 5.0       # seconds before retrying a failed team

SEEDS_DIR = Path(__file__).parent.parent / "data" / "seeds"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_birthdate(raw: str) -> str | None:
    """Normalize nba_api birth date strings to 'YYYY-MM-DD'.

    CommonTeamRoster returns dates as 'MMM DD, YYYY' (e.g. 'APR 03, 1999').
    Some other endpoints use ISO 'YYYY-MM-DDT00:00:00'.  Both are handled.
    Returns None when the value is missing or unparseable.
    """
    if not raw:
        return None
    raw = str(raw).strip()
    if not raw:
        return None

    # ISO format: 'YYYY-MM-DDT00:00:00' or 'YYYY-MM-DD'
    if len(raw) >= 10 and raw[4] == "-":
        return raw[:10]

    # Month-name format: 'APR 03, 1999'
    from datetime import datetime
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    return None


def _fetch_team_roster(nba_team_id: int, season_str: str) -> list[dict]:
    """Fetch and parse one team's CommonTeamRoster response.

    Returns a list of raw row dicts (one per roster slot).
    Raises on network or API failure — caller handles retry logic.
    """
    endpoint = CommonTeamRoster(
        team_id=nba_team_id,
        season=season_str,
        timeout=30,
    )
    df = endpoint.get_data_frames()[0]
    return df.to_dict(orient="records")


def _build_seed_row(row: dict, team_code: str, season: int) -> dict | None:
    """Map a CommonTeamRoster DataFrame row to the canonical seed shape.

    Returns None when the row lacks a usable PERSON_ID (e.g. blank rows).
    The seed shape is the stable contract read by the reconciler:
      external_id, first_name, last_name, full_name,
      nba_team_code, position, birth_date, season
    """
    person_id_raw = row.get("PERSON_ID") or row.get("PLAYER_ID")
    if not person_id_raw:
        return None
    try:
        external_id = int(person_id_raw)
    except (ValueError, TypeError):
        return None
    if external_id == 0:
        return None

    full_name = str(row.get("PLAYER", "")).strip()
    if not full_name:
        return None

    parts = full_name.split(" ", 1)
    first_name = parts[0]
    last_name = parts[1] if len(parts) > 1 else ""

    position = str(row.get("POSITION", "")).strip() or None
    birth_date = _parse_birthdate(str(row.get("BIRTH_DATE", "") or ""))

    return {
        "external_id": external_id,
        "first_name": first_name,
        "last_name": last_name,
        "full_name": full_name,
        "nba_team_code": team_code,
        "position": position,
        "birth_date": birth_date,
        "season": season,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a static roster seed file from nba_api for the DBA reconciler."
    )
    parser.add_argument(
        "--season",
        type=int,
        default=2024,
        help="Season start year (default: 2024 → 2024-25 season)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print results to stdout without writing the seed file",
    )
    args = parser.parse_args()

    season: int = args.season
    season_str = f"{season}-{str(season + 1)[2:]}"
    out_path = SEEDS_DIR / f"rosters_{season}.json"

    print(f"Building roster seed for season {season_str}...")
    print(f"Output: {out_path}\n")

    # Build a lookup: abbreviation → nba_api team id.
    nba_teams_list = nba_teams_static.get_teams()
    abbrev_to_id: dict[str, int] = {t["abbreviation"]: t["id"] for t in nba_teams_list}

    all_rows: list[dict] = []
    seen_ids: set[int] = set()  # deduplicate by external_id; first occurrence wins
    failed_teams: list[str] = []

    for code in NBA_TEAM_CODES:
        nba_team_id = abbrev_to_id.get(code)
        if nba_team_id is None:
            print(f"[WARN] {code}: no nba_api team ID found, skipping.")
            continue

        print(f"  {code} ...", end=" ", flush=True)
        try:
            raw_rows = _fetch_team_roster(nba_team_id, season_str)
        except Exception as exc:
            print(f"FAILED ({exc})")
            failed_teams.append(code)
            time.sleep(_RETRY_DELAY)
            continue

        team_count = 0
        for row in raw_rows:
            seed_row = _build_seed_row(row, code, season)
            if seed_row is None:
                continue
            eid = seed_row["external_id"]
            if eid in seen_ids:
                # Duplicate — player appeared on a prior team already (trade window).
                # Keep first occurrence which corresponds to earlier team in code list.
                continue
            seen_ids.add(eid)
            all_rows.append(seed_row)
            team_count += 1

        print(f"{team_count} players")
        time.sleep(_INTER_TEAM_DELAY)

    # Retry failed teams once with a longer delay.
    if failed_teams:
        print(f"\nRetrying {len(failed_teams)} failed team(s): {', '.join(failed_teams)}")
        time.sleep(_RETRY_DELAY)
        still_failed: list[str] = []
        for code in failed_teams:
            nba_team_id = abbrev_to_id.get(code)
            print(f"  {code} (retry) ...", end=" ", flush=True)
            try:
                raw_rows = _fetch_team_roster(nba_team_id, season_str)
            except Exception as exc:
                print(f"FAILED again ({exc})")
                still_failed.append(code)
                time.sleep(_RETRY_DELAY)
                continue

            team_count = 0
            for row in raw_rows:
                seed_row = _build_seed_row(row, code, season)
                if seed_row is None:
                    continue
                eid = seed_row["external_id"]
                if eid in seen_ids:
                    continue
                seen_ids.add(eid)
                all_rows.append(seed_row)
                team_count += 1

            print(f"{team_count} players")
            time.sleep(_INTER_TEAM_DELAY)

        if still_failed:
            print(f"\n[WARN] Could not fetch data for: {', '.join(still_failed)}")

    total = len(all_rows)
    print(f"\nTotal unique players: {total}")

    if total < 400:
        print(f"[WARN] Only {total} players collected — expected at least 400 across 30 teams.")

    if args.dry_run:
        print("\n[DRY RUN] First 5 rows:")
        for row in all_rows[:5]:
            print(f"  {json.dumps(row)}")
        print(f"\n[DRY RUN] Would write {total} rows to {out_path} — no file written.")
        return

    SEEDS_DIR.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_rows, f, indent=2, default=str)

    print(f"\nWrote {total} rows to {out_path}")


if __name__ == "__main__":
    main()
