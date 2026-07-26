"""
CPU trade-block heuristic service.

Computes which players CPU-controlled teams should list on the trade block
based on each team's build mode (rebuilding / contending / developing).

Only operates before the trade deadline — once TRADE_DEADLINE_OPEN or any
later phase is reached, listings are frozen (no refresh runs).
"""
from __future__ import annotations

import datetime
import logging
from collections import defaultdict
from typing import Optional

import asyncpg

from data.repositories import trade_block_repo, team_repo, player_repo, league_repo
from services import team_intel
from services.trade_value_math import player_trade_value

log = logging.getLogger(__name__)

# Phases at or after trade deadline — refresh must not run in any of these.
_BLOCKED_PHASES = frozenset({
    "TRADE_DEADLINE_OPEN",
    "REGULAR_SEASON_POSTDEADLINE",
    "REGULAR_SEASON_COMPLETE",
    "PLAYIN_ACTIVE",
    "PLAYOFFS_R1",
    "PLAYOFFS_R2",
    "CONFERENCE_FINALS",
    "NBA_FINALS",
    "OFFSEASON_AWARDS_OPEN",
    "OFFSEASON_AWARDS_CLOSED",
    "DRAFT_LOTTERY_DONE",
    "DRAFT_IN_PROGRESS",
    "POST_DRAFT_TRADES_OPEN",
    "FA_OPEN",
    "FA_CLOSED",
    "WAIVERS_OPEN",
    "PROGRESSION_PENDING",
})

_MAX_LISTED = 4

# TB1: posture.mode (from team_intel.build_team_intel) -> candidate-selection
# branch. Mirrors fa_service.py's _POSTURE_FA_TERMS mapping (FA1) -- same
# live-posture fix applied here so a team's actual current situation, not its
# creation-time cpu_mode column, drives which players get listed on the block.
# "transition" is never actually returned by trade_context_builder.compute_team_mode
# (only referenced in internal floor checks) but is mapped defensively in case
# that ever changes.
_POSTURE_BLOCK_BRANCH: dict[str, str] = {
    "contending": "contending",
    "play_in_fringe": "contending",
    "soft_rebuild": "rebuilding",
    "rebuilding": "rebuilding",
    "developing": "developing",
    "transition": "developing",
}


def _compute_age(birth_date: Optional[datetime.date], years_pro: int) -> int:
    """Return player age from birth_date when available, else approximate via years_pro."""
    if birth_date is not None:
        today = datetime.date.today()
        age = today.year - birth_date.year
        if (today.month, today.day) < (birth_date.month, birth_date.day):
            age -= 1
        return age
    return 20 + years_pro


async def _fetch_contract(pool: asyncpg.Pool, league_id: int, player_id: int) -> Optional[dict]:
    """Return active contract for a player as a plain dict, or None if missing."""
    row = await pool.fetchrow(
        """
        SELECT salary, years_remaining
        FROM contracts
        WHERE league_id = $1 AND player_id = $2 AND is_active = TRUE
        """,
        league_id,
        player_id,
    )
    if row is None:
        return None
    return {"salary": row["salary"], "years_remaining": row["years_remaining"]}


async def _fetch_salary_cap(pool: asyncpg.Pool, league_id: int) -> int:
    cap = await pool.fetchval("SELECT salary_cap FROM leagues WHERE id = $1", league_id)
    return int(cap) if cap else 136_000_000  # fallback to a known default


def _candidates_rebuilding(
    players: list[player_repo.Player],
    contracts: dict[int, dict],
    age_map: dict[int, int],
) -> list[tuple[player_repo.Player, dict, str]]:
    """
    Rebuilding rules:
      - Veterans age >= 30 with OVR >= 72
      - Expiring contracts (years_remaining == 1) with OVR >= 68
    """
    results: list[tuple[player_repo.Player, dict, str]] = []
    seen: set[int] = set()

    for p in players:
        c = contracts.get(p.id)
        if c is None:
            continue
        age = age_map[p.id]

        if p.id not in seen and age >= 30 and p.overall >= 72:
            results.append((p, c, "Rebuilding — veteran asset available"))
            seen.add(p.id)

        if p.id not in seen and c["years_remaining"] == 1 and p.overall >= 68:
            results.append((p, c, "Expiring contract"))
            seen.add(p.id)

    return results


def _candidates_contending(
    players: list[player_repo.Player],
    contracts: dict[int, dict],
    age_map: dict[int, int],
    salary_cap: int,
) -> list[tuple[player_repo.Player, dict, str]]:
    """
    Contending rules:
      - Bench players (roster rank 9-15 by OVR) who are under 28
      - Any player with salary > salary_cap * 0.12 AND OVR < 76
    """
    # players is already sorted by OVR DESC from get_roster
    results: list[tuple[player_repo.Player, dict, str]] = []
    seen: set[int] = set()

    bench = players[8:15]  # 0-indexed: slots 9-15 → indices 8-14
    for p in bench:
        c = contracts.get(p.id)
        if c is None:
            continue
        age = age_map[p.id]
        if age < 28:
            results.append((p, c, "Contending — young bench depth available"))
            seen.add(p.id)

    overpay_threshold = salary_cap * 0.12
    for p in players:
        if p.id in seen:
            continue
        c = contracts.get(p.id)
        if c is None:
            continue
        if c["salary"] > overpay_threshold and p.overall < 76:
            results.append((p, c, "Overpaid mid-tier player"))
            seen.add(p.id)

    return results


def _candidates_developing(
    players: list[player_repo.Player],
    contracts: dict[int, dict],
    age_map: dict[int, int],
) -> list[tuple[player_repo.Player, dict, str]]:
    """
    Developing rules:
      - Redundant position: if 3+ players at same position, list the lowest OVR one(s)
      - Veterans age >= 32 with OVR < 78
    """
    results: list[tuple[player_repo.Player, dict, str]] = []
    seen: set[int] = set()

    # Group by position, sorted OVR DESC (players list is already sorted)
    by_position: dict[str, list[player_repo.Player]] = defaultdict(list)
    for p in players:
        by_position[p.position].append(p)

    for pos, pos_players in by_position.items():
        if len(pos_players) >= 3:
            # List the lowest OVR player(s) — pos_players is OVR DESC so last entries are lowest
            excess = pos_players[2:]  # everything beyond the top-2 at this position
            for p in excess:
                if p.id in seen:
                    continue
                c = contracts.get(p.id)
                if c is None:
                    continue
                results.append((p, c, f"Redundant depth at {pos}"))
                seen.add(p.id)

    for p in players:
        if p.id in seen:
            continue
        age = age_map[p.id]
        if age >= 32 and p.overall < 78:
            c = contracts.get(p.id)
            if c is None:
                continue
            results.append((p, c, "Developing — aging veteran available"))
            seen.add(p.id)

    return results


def _pick_top(
    candidates: list[tuple[player_repo.Player, dict, str]],
    salary_cap: int,
    limit: int,
) -> list[tuple[player_repo.Player, dict, str]]:
    """Sort candidates by trade value descending, return top `limit`.

    Age is held at a neutral 28 for sorting — the real age is only needed for
    the asking_price calculation in refresh_team_block, where age_map is available.
    """
    def _sort_key(entry: tuple[player_repo.Player, dict, str]) -> float:
        p, c, _ = entry
        return player_trade_value({"overall": p.overall, "age": 28}, c, salary_cap)

    return sorted(candidates, key=_sort_key, reverse=True)[:limit]


async def refresh_team_block(pool: asyncpg.Pool, league_id: int, team_id: int) -> int:
    """
    Recompute trade-block listings for one CPU team.

    Returns number of players listed.
    Skips if team has a human manager (manager_user_id IS NOT NULL).
    """
    team = await team_repo.get_by_id(pool, team_id)
    if team is None:
        log.warning(f"refresh_team_block: team {team_id} not found")
        return 0

    if team.manager_user_id is not None:
        return 0  # human-managed; skip

    salary_cap = await _fetch_salary_cap(pool, league_id)
    players = await player_repo.get_roster(pool, league_id, team_id)

    if not players:
        return 0

    # Fetch contracts and ages in parallel-ish (sequential per player — acceptable at roster size)
    contracts: dict[int, dict] = {}
    age_map: dict[int, int] = {}
    for p in players:
        c = await _fetch_contract(pool, league_id, p.id)
        if c is not None:
            contracts[p.id] = c
        age_map[p.id] = _compute_age(p.birth_date, p.years_pro)

    # TB1: read live posture instead of the static team.cpu_mode column, which
    # is set once at team creation and never updated -- a team that started
    # "developing" but is now clearly tanking (or vice versa) kept generating
    # its trade-block list off its creation-time label forever.
    league_row = await pool.fetchrow("SELECT * FROM leagues WHERE id = $1", league_id)
    branch: Optional[str] = None
    if league_row is not None:
        league = league_repo._league_from_record(league_row)
        intel = await team_intel.build_team_intel(
            pool, league, league.current_season, [team_id], include=("posture",)
        )
        posture: dict = intel.get(team_id, {}).get("posture") or {}
        mode: Optional[str] = posture.get("mode")
        branch = _POSTURE_BLOCK_BRANCH.get(mode) if mode else None

    if branch is None:
        # Defensive fallback: no live posture data yet for this team (e.g. no
        # standings_cache row, or league not found) -- fall back to the old
        # static cpu_mode column rather than crashing (mirrors fa_service.py's
        # FA1 fallback).
        log.warning(
            f"refresh_team_block: no posture data for team {team_id} "
            f"(league {league_id}) — falling back to static cpu_mode"
        )
        cpu_mode = team.cpu_mode  # "rebuilding" | "contending" | "developing"
        if cpu_mode == "rebuilding":
            branch = "rebuilding"
        elif cpu_mode == "contending":
            branch = "contending"
        else:
            branch = "developing"

    if branch == "rebuilding":
        candidates = _candidates_rebuilding(players, contracts, age_map)
    elif branch == "contending":
        candidates = _candidates_contending(players, contracts, age_map, salary_cap)
    else:
        # "developing" is the default for any unrecognized branch as well
        candidates = _candidates_developing(players, contracts, age_map)

    top = _pick_top(candidates, salary_cap, _MAX_LISTED)

    await trade_block_repo.clear_team_block(pool, league_id, team_id)

    for p, c, note in top:
        asking_price = int(
            player_trade_value({"overall": p.overall, "age": age_map[p.id]}, c, salary_cap)
            * 1_000_000
        )
        await trade_block_repo.add_to_block(
            pool,
            league_id=league_id,
            team_id=team_id,
            player_id=p.id,
            asking_price=asking_price,
            note=note,
        )

    return len(top)


async def refresh_league(pool: asyncpg.Pool, league_id: int) -> dict:
    """
    Refresh all CPU teams' trade blocks for this league.

    Returns {"teams_updated": int, "players_listed": int}.
    Never runs during TRADE_DEADLINE_OPEN phase or later.
    """
    phase_row = await pool.fetchrow(
        "SELECT current_phase FROM leagues WHERE id = $1",
        league_id,
    )
    if phase_row is None:
        log.warning(f"refresh_league: league {league_id} not found")
        return {"teams_updated": 0, "players_listed": 0}

    current_phase: str = phase_row["current_phase"]
    if current_phase in _BLOCKED_PHASES:
        log.debug(
            f"refresh_league: skipping CPU block refresh for league {league_id} "
            f"— phase is {current_phase}"
        )
        return {"teams_updated": 0, "players_listed": 0}

    teams = await team_repo.get_all(pool, league_id)
    cpu_teams = [t for t in teams if t.manager_user_id is None]

    teams_updated = 0
    players_listed = 0

    for team in cpu_teams:
        try:
            listed = await refresh_team_block(pool, league_id, team.id)
            if listed > 0:
                teams_updated += 1
                players_listed += listed
        except Exception as exc:
            log.warning(
                f"refresh_league: failed to refresh block for team {team.id} "
                f"in league {league_id}: {exc}"
            )

    log.info(
        f"CPU block refresh complete — league {league_id}: "
        f"{teams_updated} teams updated, {players_listed} players listed"
    )
    return {"teams_updated": teams_updated, "players_listed": players_listed}
