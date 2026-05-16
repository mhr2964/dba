from __future__ import annotations

"""Roster-year consistency reconciler (Slices 3 & 4).

Responsible for diffing a live league's roster against a season seed file and
optionally applying inserts + team moves so the league's roster matches the
real-NBA snapshot baked into the seed.

Public surface:
    load_seed(season)                             -> list[dict]
    diff_league_against_seed(pool, league_id, season) -> dict
    apply_reseed(pool, league_id, season, *, dry_run) -> dict
"""

import json
import os
import pathlib
import unicodedata
from typing import Optional

import asyncpg

from core.logging import get_logger
from data.repositories.roster_seed_repo import (
    bulk_set_team,
    get_all_players_for_league,
    mark_players_seeded,
    terminate_contract,
)
from services import import_service

log = get_logger(__name__)

_SEEDS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "data", "seeds")
)

# ---------------------------------------------------------------------------
# Stats-ratings lookup (nba_api player_id keyed by string -> OVR int)
# ---------------------------------------------------------------------------

_stats_ratings_cache: dict[int, dict[str, int]] = {}

# ---------------------------------------------------------------------------
# Stat-tendency computation from BDL base cache
# ---------------------------------------------------------------------------

# Keyed by season int -> {"G": {ast: avg, reb: avg, stl: avg, blk: avg}, "F": ..., "C": ...}
_position_avgs_cache: dict[int, dict[str, dict[str, float]]] = {}
# Keyed by season int -> {bdl_player_id: record}
_bdl_base_cache: dict[int, dict[int, dict]] = {}

_BDL_CACHE_DIR = pathlib.Path(__file__).parent.parent / "data" / "bdl_cache"
_TENDENCY_STATS = ("ast", "reb", "stl", "blk")
_DEFAULT_TENDENCIES = {"blk_tendency": 50, "stl_tendency": 50, "ast_tendency": 50, "reb_tendency": 50, "usage_weight": 50}


def _load_bdl_base(season: int) -> dict[int, dict]:
    """Load season_{season}_base.json and return {bdl_player_id: record}.
    Results are cached at module level (one load per season per process).
    """
    if season in _bdl_base_cache:
        return _bdl_base_cache[season]
    path = _BDL_CACHE_DIR / f"season_{season}_base.json"
    if not path.exists():
        _bdl_base_cache[season] = {}
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            records = json.load(f)
        by_id = {rec["player"]["id"]: rec for rec in records}
        _bdl_base_cache[season] = by_id
        return by_id
    except (json.JSONDecodeError, KeyError, OSError):
        _bdl_base_cache[season] = {}
        return {}


def _get_position_avgs(season: int, by_id: dict[int, dict]) -> dict[str, dict[str, float]]:
    """Compute per-position averages for ast/reb/stl/blk for a given season.
    Uses only players with gp >= 10 to filter out noise from injury-shortened
    seasons.  Cached so it runs once per season per process.
    """
    if season in _position_avgs_cache:
        return _position_avgs_cache[season]

    buckets: dict[str, dict[str, list[float]]] = {}
    for rec in by_id.values():
        stats = rec.get("stats", {})
        gp = int(stats.get("gp", 0) or 0)
        if gp < 10:
            continue
        pos = rec["player"].get("position") or "G"
        # Normalize multi-char positions (e.g. "PG", "SG" -> "G"; "PF", "SF" -> "F")
        if len(pos) > 1:
            pos = pos[-1]  # last char: 'G', 'F', or 'C'
        if pos not in ("G", "F", "C"):
            pos = "G"
        bucket = buckets.setdefault(pos, {s: [] for s in _TENDENCY_STATS})
        for stat in _TENDENCY_STATS:
            val = stats.get(stat)
            if val is not None:
                bucket[stat].append(float(val))

    avgs: dict[str, dict[str, float]] = {}
    for pos, stat_lists in buckets.items():
        avgs[pos] = {}
        for stat, vals in stat_lists.items():
            avgs[pos][stat] = sum(vals) / len(vals) if vals else 1.0

    # Fallback: if any position bucket is missing, use overall average
    all_avgs: dict[str, float] = {}
    for stat in _TENDENCY_STATS:
        all_vals: list[float] = []
        for rec in by_id.values():
            stats = rec.get("stats", {})
            if int(stats.get("gp", 0) or 0) < 10:
                continue
            v = stats.get(stat)
            if v is not None:
                all_vals.append(float(v))
        all_avgs[stat] = sum(all_vals) / len(all_vals) if all_vals else 1.0

    for pos in ("G", "F", "C"):
        if pos not in avgs:
            avgs[pos] = dict(all_avgs)
        else:
            for stat in _TENDENCY_STATS:
                if stat not in avgs[pos] or avgs[pos][stat] == 0.0:
                    avgs[pos][stat] = all_avgs.get(stat, 1.0)

    _position_avgs_cache[season] = avgs
    return avgs


def _compute_stat_tendencies(
    external_id: "int | str | None",
    season: int,
    position: str,
) -> dict[str, int]:
    """Return tendency + usage fields derived from BDL per-game stats.

    Returns:
        blk_tendency, stl_tendency, ast_tendency, reb_tendency — scaled 5–95
            (50 = position-average, >50 = above-average).
        usage_weight — scored as clamp(round((pts / 25.0) * 100), 10, 95).
            25 PPG → 100 (clamps to 95), 20 PPG → 80, 10 PPG → 40, 0 PPG → floored to 10.
            This is what the sim engine reads to weight scoring allocation;
            keeping it at the default 50 causes stars to underscore significantly.
            The /25 divisor (vs old /35) widens the gap between stars and role players.

    Falls back to all-50 defaults if the player is not found or the cache
    file is missing — existing rows without BDL data are unaffected.
    """
    if external_id is None:
        return dict(_DEFAULT_TENDENCIES)

    by_id = _load_bdl_base(season)
    if not by_id:
        return dict(_DEFAULT_TENDENCIES)

    bdl_id = int(external_id) if str(external_id).isdigit() else None
    if bdl_id is None or bdl_id not in by_id:
        return dict(_DEFAULT_TENDENCIES)

    rec = by_id[bdl_id]
    stats = rec.get("stats", {})

    # Normalize position to G/F/C
    pos = position or rec["player"].get("position") or "G"
    if len(pos) > 1:
        pos = pos[-1]
    if pos not in ("G", "F", "C"):
        pos = "G"

    pos_avgs = _get_position_avgs(season, by_id)
    p_avgs = pos_avgs.get(pos, {s: 1.0 for s in _TENDENCY_STATS})

    def _tend(stat: str) -> int:
        player_val = float(stats.get(stat) or 0.0)
        denom = p_avgs.get(stat) or 1.0
        raw = round((player_val / denom) * 50)
        return max(5, min(95, raw))

    pts = float(stats.get("pts") or 0.0)
    usage_weight = max(10, min(95, round((pts / 25.0) * 100)))

    return {
        "blk_tendency": _tend("blk"),
        "stl_tendency": _tend("stl"),
        "ast_tendency": _tend("ast"),
        "reb_tendency": _tend("reb"),
        "usage_weight": usage_weight,
    }


def _load_stats_ratings(season: int) -> dict[str, int]:
    """Load data/stats_ratings/{season}.json. Returns empty dict on any IO/parse error."""
    path = pathlib.Path(__file__).parent.parent / "data" / "stats_ratings" / f"{season}.json"
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _get_ovr_for_player(
    external_id: "int | str | None",
    season: int,
    player_name: str = "",
) -> int:
    """Return OVR with a three-tier priority:

    1. stats_ratings/{season}.json keyed by external_id string (most accurate).
    2. nba_2k_ratings.json keyed by normalized player name.
    3. Experience-band fallback (70 for unknown experience).
    """
    global _stats_ratings_cache
    if season not in _stats_ratings_cache:
        _stats_ratings_cache[season] = _load_stats_ratings(season)
    ratings = _stats_ratings_cache[season]

    if external_id is not None and str(external_id) in ratings:
        return ratings[str(external_id)]

    # Tier 2 + 3: delegate to the existing function which handles 2K file + exp fallback.
    return import_service._overall_from_2k(player_name, season, 0) if player_name else 70


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalize(name: str) -> str:
    """Lowercase, accent-strip, strip whitespace — same rule on both sides."""
    return (
        unicodedata.normalize("NFKD", name)
        .encode("ascii", "ignore")
        .decode()
        .lower()
        .strip()
    )


def _overall_from_ratings_file(
    season: int,
    player_name: str = "",
    exp: int = 0,
    external_id: "int | str | None" = None,
) -> int:
    """Return overall using a three-tier priority:

    1. stats_ratings/{season}.json (nba_api player_id key) — most accurate.
    2. nba_2k_ratings.json (player name key).
    3. 70 as a deterministic fallback.
    """
    try:
        return _get_ovr_for_player(external_id, season, player_name)
    except Exception:
        return 70


async def _get_team_id_by_code(
    pool: asyncpg.Pool, league_id: int, nba_team_code: str
) -> Optional[int]:
    row = await pool.fetchrow(
        "SELECT id FROM teams WHERE league_id = $1 AND nba_team_code = $2",
        league_id,
        nba_team_code.upper(),
    )
    return row["id"] if row else None


async def _get_team_code_by_id(
    pool: asyncpg.Pool, league_id: int, team_id: int
) -> Optional[str]:
    row = await pool.fetchrow(
        "SELECT nba_team_code FROM teams WHERE league_id = $1 AND id = $2",
        league_id,
        team_id,
    )
    return row["nba_team_code"] if row else None


async def _get_league_salary_cap_and_season(
    pool: asyncpg.Pool, league_id: int
) -> tuple[int, int]:
    row = await pool.fetchrow(
        "SELECT salary_cap, current_season FROM leagues WHERE id = $1",
        league_id,
    )
    if not row:
        raise ValueError(f"League {league_id} not found")
    return row["salary_cap"], row["current_season"]


async def _players_for_team(
    pool: asyncpg.Pool, league_id: int, team_id: int
) -> list[tuple[int, int]]:
    """Return [(player_id, overall), ...] ordered by overall desc for lineup gen."""
    rows = await pool.fetch(
        "SELECT id, overall FROM players WHERE league_id = $1 AND team_id = $2 ORDER BY overall DESC",
        league_id,
        team_id,
    )
    return [(r["id"], r["overall"]) for r in rows]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_seed(season: int) -> list[dict]:
    """Read data/seeds/rosters_{season}.json and return the array of seed rows.

    Raises FileNotFoundError with a descriptive message if the file is absent
    so callers can distinguish a missing seed from other IO errors.
    """
    path = os.path.join(_SEEDS_DIR, f"rosters_{season}.json")
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"No roster seed file found for season {season}. "
            f"Expected: {path}"
        )
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


async def diff_league_against_seed(
    pool: asyncpg.Pool, league_id: int, season: int
) -> dict:
    """Compare the live league roster against the season seed file.

    Returns a dict with four keys:
        to_insert      list[dict]  — seed rows with no matching DB player
        to_move        list[dict]  — players on the wrong team
                                     each entry: {seed_row, player, from_team_code, to_team_code}
        already_correct int        — count of players already on the right team
        unknown_in_db  list[dict] — DB players not present in the seed at all
    """
    seed_rows = load_seed(season)
    db_players = await get_all_players_for_league(pool, league_id)

    # Build DB look-up maps so we can resolve players without N×M queries.
    # external_id in DB is stored as TEXT (see repo); cast to str for comparison.
    db_by_ext_id: dict[str, dict] = {}
    db_by_norm_name: dict[str, dict] = {}
    for p in db_players:
        if p.get("external_id"):
            db_by_ext_id[str(p["external_id"])] = p
        norm = _normalize(f"{p['first_name']} {p['last_name']}")
        db_by_norm_name[norm] = p  # last writer wins on genuine duplicates — acceptable

    to_insert: list[dict] = []
    to_move: list[dict] = []
    already_correct = 0
    matched_db_ids: set[int] = set()

    for seed_row in seed_rows:
        ext_id = str(seed_row.get("external_id", ""))
        full_name = seed_row.get("full_name") or (
            f"{seed_row.get('first_name', '')} {seed_row.get('last_name', '')}".strip()
        )
        norm_name = _normalize(full_name)
        target_code = seed_row.get("nba_team_code", "").upper()

        # Try external_id first, then normalised name.
        db_player = db_by_ext_id.get(ext_id) if ext_id else None
        if db_player is None:
            db_player = db_by_norm_name.get(norm_name)

        if db_player is None:
            to_insert.append(seed_row)
            continue

        matched_db_ids.add(db_player["id"])

        # Resolve current team code for this player.
        current_code = await _get_team_code_by_id(pool, league_id, db_player["team_id"])

        if current_code and current_code.upper() == target_code:
            already_correct += 1
        else:
            to_move.append(
                {
                    "seed_row": seed_row,
                    "player": db_player,
                    "from_team_code": current_code,
                    "to_team_code": target_code,
                }
            )

    # Any DB player not matched by any seed row is "unknown_in_db".
    all_db_ids = {p["id"] for p in db_players}
    unknown_ids = all_db_ids - matched_db_ids
    unknown_in_db = [p for p in db_players if p["id"] in unknown_ids]

    return {
        "to_insert": to_insert,
        "to_move": to_move,
        "already_correct": already_correct,
        "unknown_in_db": unknown_in_db,
    }


async def apply_reseed(
    pool: asyncpg.Pool,
    league_id: int,
    season: int,
    *,
    dry_run: bool = False,
) -> dict:
    """Reconcile the league roster against the season seed file.

    Returns:
        {"inserted": int, "moved": int, "skipped": int, "errors": list[str]}

    When dry_run=True the diff is computed but no writes are issued; counts
    reflect what *would* happen so the caller can preview the operation.

    Per-player errors are collected rather than aborting the whole reseed —
    a single bad player row should not block the other 400+.  Each player
    operation is independent (the repo helpers each accept pool, not conn).
    """
    diff = await diff_league_against_seed(pool, league_id, season)

    to_insert = diff["to_insert"]
    to_move = diff["to_move"]

    inserted = 0
    moved = 0
    skipped = 0
    errors: list[str] = []

    if dry_run:
        return {
            "inserted": len(to_insert),
            "moved": len(to_move),
            "skipped": 0,
            "errors": [],
        }

    salary_cap, current_season = await _get_league_salary_cap_and_season(pool, league_id)

    # Track which teams were touched so we can regenerate lineups afterward.
    affected_team_ids: set[int] = set()

    # The repo helpers and import_service._insert_* all accept pool (not conn),
    # so a true single-connection transaction cannot wrap them without changing
    # their signatures.  Per-player try/except gives the "collect errors and
    # continue" semantics the spec requires, and individual INSERT/UPDATE calls
    # inside asyncpg are auto-committed at statement level, which is correct for
    # a best-effort reconcile operation.

    # --- INSERT new players ---
    for seed_row in to_insert:
        full_name = seed_row.get("full_name") or (
            f"{seed_row.get('first_name', '')} {seed_row.get('last_name', '')}".strip()
        )
        target_code = (seed_row.get("nba_team_code") or "").upper()

        try:
            team_id = await _get_team_id_by_code(pool, league_id, target_code)
            if team_id is None:
                errors.append(
                    f"INSERT skipped — unknown team code {target_code!r} "
                    f"for {full_name}"
                )
                skipped += 1
                continue

            position = import_service._normalize_position(
                seed_row.get("position", "")
            )
            overall = _overall_from_ratings_file(
                season,
                full_name,
                external_id=seed_row.get("external_id"),
            )
            attrs = import_service._generate_attributes(overall, position)
            hidden = import_service._generate_hidden(overall, exp=0)

            birth_date = import_service._parse_birthdate(
                seed_row.get("birth_date", "")
            )

            player_data = {
                "external_id": str(seed_row.get("external_id", "")),
                "first_name": seed_row.get("first_name", full_name.split()[0]),
                "last_name": seed_row.get(
                    "last_name",
                    " ".join(full_name.split()[1:]) or "Unknown",
                ),
                "position": position,
                "height_in": None,
                "weight_lb": None,
                "birth_date": birth_date,
                "years_pro": 0,
                "is_rookie": False,
                "overall": overall,
                **attrs,
                **hidden,
                "market_pref": import_service._market_pref(),
                "star_leverage": import_service._star_leverage(overall),
            }

            salary, contract_type, years = import_service._contract_salary(
                overall, salary_cap
            )

            player_id = await import_service._insert_player_row(
                pool, league_id, team_id, player_data
            )
            await import_service._insert_contract_row(
                pool, league_id, team_id, player_id,
                salary, contract_type, years, current_season,
            )
            await mark_players_seeded(pool, [player_id], season)

            tendencies = _compute_stat_tendencies(
                seed_row.get("external_id"), season, position
            )
            await pool.execute(
                """
                UPDATE players
                SET blk_tendency = $1,
                    stl_tendency = $2,
                    ast_tendency = $3,
                    reb_tendency = $4,
                    usage_weight  = $5
                WHERE id = $6
                """,
                tendencies["blk_tendency"],
                tendencies["stl_tendency"],
                tendencies["ast_tendency"],
                tendencies["reb_tendency"],
                tendencies["usage_weight"],
                player_id,
            )

            affected_team_ids.add(team_id)
            inserted += 1
            log.debug(f"reseed insert: {full_name} -> {target_code} (player_id={player_id})")

        except Exception as exc:
            errors.append(f"INSERT error for {full_name}: {exc}")
            log.warning(f"reseed insert failed for {full_name}: {exc}")

    # --- MOVE existing players ---
    for entry in to_move:
        seed_row = entry["seed_row"]
        db_player = entry["player"]
        to_code = entry["to_team_code"]
        full_name = seed_row.get("full_name") or (
            f"{seed_row.get('first_name', '')} {seed_row.get('last_name', '')}".strip()
        )

        try:
            new_team_id = await _get_team_id_by_code(pool, league_id, to_code)
            if new_team_id is None:
                errors.append(
                    f"MOVE skipped — unknown team code {to_code!r} "
                    f"for {full_name}"
                )
                skipped += 1
                continue

            player_id = db_player["id"]

            await bulk_set_team(pool, league_id, [player_id], new_team_id)
            await terminate_contract(pool, league_id, player_id)

            overall = db_player.get("overall") or _overall_from_ratings_file(
                season, full_name
            )
            salary, contract_type, years = import_service._contract_salary(
                overall, salary_cap
            )
            await import_service._insert_contract_row(
                pool, league_id, new_team_id, player_id,
                salary, contract_type, years, current_season,
            )
            await mark_players_seeded(pool, [player_id], season)

            move_position = db_player.get("position", "")
            move_ext_id = db_player.get("external_id") or seed_row.get("external_id")
            tendencies = _compute_stat_tendencies(move_ext_id, season, move_position)
            await pool.execute(
                """
                UPDATE players
                SET blk_tendency = $1,
                    stl_tendency = $2,
                    ast_tendency = $3,
                    reb_tendency = $4,
                    usage_weight  = $5
                WHERE id = $6
                """,
                tendencies["blk_tendency"],
                tendencies["stl_tendency"],
                tendencies["ast_tendency"],
                tendencies["reb_tendency"],
                tendencies["usage_weight"],
                player_id,
            )

            if entry.get("from_team_code"):
                old_team_id = await _get_team_id_by_code(
                    pool, league_id, entry["from_team_code"]
                )
                if old_team_id:
                    affected_team_ids.add(old_team_id)
            affected_team_ids.add(new_team_id)
            moved += 1
            log.debug(
                f"reseed move: {full_name} "
                f"{entry.get('from_team_code')} -> {to_code} "
                f"(player_id={player_id})"
            )

        except Exception as exc:
            errors.append(f"MOVE error for {full_name}: {exc}")
            log.warning(f"reseed move failed for {full_name}: {exc}")

    # --- Regenerate lineups for all affected teams (outside transaction is fine;
    #     lineup generation is idempotent via ON CONFLICT DO UPDATE). ---
    for team_id in affected_team_ids:
        try:
            players = await _players_for_team(pool, league_id, team_id)
            await import_service._generate_lineup(pool, league_id, team_id, players)
        except Exception as exc:
            errors.append(f"Lineup regeneration failed for team_id={team_id}: {exc}")
            log.warning(f"reseed lineup regen failed for team_id={team_id}: {exc}")

    log.info(
        f"apply_reseed: league={league_id} season={season} "
        f"inserted={inserted} moved={moved} skipped={skipped} errors={len(errors)}"
    )
    return {"inserted": inserted, "moved": moved, "skipped": skipped, "errors": errors}


def list_available_seed_years() -> list[int]:
    """Return sorted list of years that have a rosters_YYYY.json seed file."""
    years: list[int] = []
    try:
        for entry in os.listdir(_SEEDS_DIR):
            if entry.startswith("rosters_") and entry.endswith(".json"):
                try:
                    years.append(int(entry[len("rosters_"):-len(".json")]))
                except ValueError:
                    pass
    except OSError:
        pass
    return sorted(years)
