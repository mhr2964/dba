"""Roster/lineup persistence and the 60s role-assignment cache used by batch sim.

Pure DB-read/write helpers with no discord dependency -- extracted from
batch_sim_runner.py. `_persist_game_result` and `_persist_injuries` stayed
behind (still in batch_sim_runner.py) because they interleave real DB writes
with inline discord.Embed announcements and have zero test coverage today;
splitting those needs the same characterization-test treatment as the
_maybe_post_* content functions, not a plain relocation.
"""
from __future__ import annotations

import os
import time as _time
from typing import List

from data.repositories import player_repo, team_repo
from services.role_service import ROLE_REGISTRY, get_or_derive_roles

_HEADLESS = os.environ.get("DBA_HEADLESS_MODE") == "1"

_ROLE_CACHE: dict[tuple[int, int, int], list[dict]] = {}
_ROLE_CACHE_TS: dict[tuple[int, int, int], float] = {}
_ROLE_CACHE_TTL: float = 60.0


async def _get_team_roles_cached(pool, league_id: int, team_id: int, season: int) -> list[dict]:
    """Fetch role assignments for a team, caching for 60 s to reduce DB load during batch sim."""
    key = (league_id, team_id, season)
    if key in _ROLE_CACHE and _time.monotonic() - _ROLE_CACHE_TS[key] < _ROLE_CACHE_TTL:
        return _ROLE_CACHE[key]
    rows = await get_or_derive_roles(pool, league_id, team_id, season)
    _ROLE_CACHE[key] = rows
    _ROLE_CACHE_TS[key] = _time.monotonic()
    return rows


def invalidate_role_cache(league_id: int, team_id: int | None = None, season: int | None = None) -> None:
    """Evict stale entries after roster changes (trades, injuries).  Called by Phase 3 hooks."""
    keys_to_drop = [
        k for k in _ROLE_CACHE
        if k[0] == league_id
        and (team_id is None or k[1] == team_id)
        and (season is None or k[2] == season)
    ]
    for k in keys_to_drop:
        _ROLE_CACHE.pop(k, None)
        _ROLE_CACHE_TS.pop(k, None)


async def _stamp_role_data(
    pool,
    league_id: int,
    team_id: int,
    season: int,
    players: list[dict],
    offensive_scheme: str,
) -> None:
    """Stamp _role_* fields onto each player dict so sim_engine can use them.

    Fields set on each player:
        _role              — role name string (e.g. "post_anchor")
        _role_touch_share  — base touch share from ROLE_REGISTRY (pre-scheme-synergy)
        _role_fga_3pa_pct  — role's 3PA fraction
        _role_fta_per_fga  — role's FTA per FGA ratio
        _role_def_role     — defensive_role string ("anchor"/"perimeter"/"general"/"passive")
        _role_minutes_tier — "starter"/"rotation"/"bench"/"depth"
        _role_tendencies   — list of tendency column names this role amplifies
    """
    assignments = await _get_team_roles_cached(pool, league_id, team_id, season)
    role_by_pid: dict[int, dict] = {a["player_id"]: a for a in assignments}

    # Pass 1: resolve role/registry for every player and apply scheme_synergy bump
    # BEFORE renormalising so the documented +15% relative gain survives.  If we
    # applied synergy after normalisation (old behaviour) the re-normalise step in
    # sim_engine would absorb ~1.4% of the bump, yielding only ~13.6% relative.
    stamped: list[tuple] = []  # (player_dict, role, touch_share, reg)
    for p in players:
        pid = p.get("id") or p.get("player_id")
        assignment = role_by_pid.get(pid)
        if assignment:
            role = assignment["role"]
            touch_share = float(assignment["touch_share"])  # Postgres returns Decimal
        else:
            # Fallback: player not yet in player_roles (shouldn't happen post-Phase-1)
            role = "glue_guy"
            touch_share = 0.08

        reg = ROLE_REGISTRY.get(role, ROLE_REGISTRY["glue_guy"])

        # Apply scheme_synergy modifier (+15%) before renormalising below.
        if offensive_scheme in reg.get("scheme_synergy", []):
            touch_share *= 1.15

        stamped.append((p, role, touch_share, reg))

    # Pass 2: renormalise so the team's touch shares still sum to 1.0.
    # This makes the synergy bump a true +15% relative shift (synergy player gets
    # a larger slice; everyone else proportionally less), matching the docstring.
    total_ts = sum(ts for _, _, ts, _ in stamped) or 1.0
    for p, role, touch_share, reg in stamped:
        p["_role"] = role
        p["_role_touch_share"] = round(touch_share / total_ts, 4)
        p["_role_fga_3pa_pct"] = reg["fga_3pa_pct"]
        p["_role_fta_per_fga"] = reg["fta_per_fga"]
        p["_role_def_role"] = reg["defensive_role"]
        p["_role_minutes_tier"] = reg["minutes_tier"]
        p["_role_tendencies"] = reg.get("tendencies_boosted", [])


async def _ensure_lineup(pool, league_id: int, team_id: int) -> None:
    """Auto-populate lineups for a team that has none, using top players by OVR."""
    count = await pool.fetchval(
        "SELECT COUNT(*) FROM lineups WHERE league_id=$1 AND team_id=$2",
        league_id,
        team_id,
    )
    if count > 0:
        return

    players = await player_repo.get_roster(pool, league_id, team_id)
    if not players:
        return

    for slot, player in enumerate(players[:15], start=1):
        await pool.execute(
            """
            INSERT INTO lineups (league_id, team_id, is_starter, slot, player_id, set_by)
            VALUES ($1, $2, $3, $4, $5, NULL)
            ON CONFLICT (league_id, team_id, slot) DO NOTHING
            """,
            league_id,
            team_id,
            slot <= 5,
            slot,
            player.id,
        )

    if _HEADLESS:
        try:
            _team_row = await pool.fetchrow(
                "SELECT nba_team_code FROM teams WHERE id = $1", team_id
            )
            _tc = _team_row["nba_team_code"] if _team_row else str(team_id)
            starters = [p for i, p in enumerate(players[:15]) if i < 5]
            bench = [p for i, p in enumerate(players[:15]) if i >= 5]
            _s_lines = [
                f"    S{i+1}: {p.full_name} OVR {p.overall} ({p.position})"
                for i, p in enumerate(starters)
            ]
            _b_lines = [
                f"    B{i+1}: {p.full_name} OVR {p.overall} ({p.position})"
                for i, p in enumerate(bench)
            ]
            print(
                f"CPU [{_tc}] — lineup auto-populated (top OVR order)\n"
                + "\n".join(_s_lines)
                + ("\n" + "\n".join(_b_lines) if _b_lines else "")
            )
        except Exception:
            pass  # never let logging break the sim


def _apply_directives(p: dict) -> dict:
    """Apply manager directives as effective-tendency overrides. Modifies in place."""
    shot_diet = p.get("shot_diet") or "auto"
    usage_mode = p.get("usage_mode") or "normal"
    defense_mode = p.get("defense_mode") or "standard"
    role_mode = p.get("role_mode") or "scorer"
    clutch_mode = p.get("clutch_mode") or "normal"

    def clamp(v: int) -> int:
        return max(0, min(100, v))

    if shot_diet == "force_3s":
        p["tendency_3pt"] = clamp(p.get("tendency_3pt", 50) + 25)
        p["tendency_mid"] = clamp(p.get("tendency_mid", 50) - 15)
        p["tendency_drive"] = clamp(p.get("tendency_drive", 50) - 10)
    elif shot_diet == "attack_rim":
        p["tendency_drive"] = clamp(p.get("tendency_drive", 50) + 25)
        p["tendency_3pt"] = clamp(p.get("tendency_3pt", 50) - 25)
        p["tendency_mid"] = clamp(p.get("tendency_mid", 50) - 10)
    elif shot_diet == "post_heavy":
        p["tendency_post"] = clamp(p.get("tendency_post", 20) + 30)
        p["tendency_3pt"] = clamp(p.get("tendency_3pt", 50) - 20)
    elif shot_diet == "midrange":
        p["tendency_mid"] = clamp(p.get("tendency_mid", 50) + 25)
        p["tendency_3pt"] = clamp(p.get("tendency_3pt", 50) - 15)

    if usage_mode == "feature":
        p["usage_weight"] = clamp(int(p.get("usage_weight", 50) * 1.4))
    elif usage_mode == "conserve":
        p["usage_weight"] = clamp(int(p.get("usage_weight", 50) * 0.6))

    if defense_mode == "lockdown":
        p["defensive_effort"] = clamp(p.get("defensive_effort", 50) + 20)
        # slight offensive penalty — reduce usage a touch
        p["usage_weight"] = clamp(p.get("usage_weight", 50) - 5)
    elif defense_mode == "off":
        p["defensive_effort"] = clamp(p.get("defensive_effort", 50) - 20)
        p["usage_weight"] = clamp(p.get("usage_weight", 50) + 5)

    if role_mode == "creator":
        p["tendency_pass"] = clamp(p.get("tendency_pass", 50) + 20)
        p["usage_weight"] = clamp(p.get("usage_weight", 50) + 5)
    elif role_mode == "spot_up":
        p["tendency_3pt"] = clamp(p.get("tendency_3pt", 50) + 15)
        p["tendency_pass"] = clamp(p.get("tendency_pass", 50) - 25)
    elif role_mode == "scorer":
        p["tendency_pass"] = clamp(p.get("tendency_pass", 50) - 10)
        p["usage_weight"] = clamp(p.get("usage_weight", 50) + 5)

    if clutch_mode == "hero":
        p["clutch_rating"] = clamp(p.get("clutch_rating", 50) + 20)
    elif clutch_mode == "hide":
        p["clutch_rating"] = clamp(p.get("clutch_rating", 50) - 30)
        p["usage_weight"] = clamp(int(p.get("usage_weight", 50) * 0.7))

    return p


def _apply_cpu_directives(players: list[dict], directives: dict[int, dict]) -> None:
    for p in players:
        pid = p.get("id")
        if pid is None or pid not in directives:
            continue
        d = directives[pid]
        p["shot_diet"] = d.get("shot_diet", "auto")
        p["usage_mode"] = d.get("usage_mode", "normal")
        p["defense_mode"] = d.get("defense_mode", "standard")
        p["role_mode"] = d.get("role_mode", "spot_up")
        p["clutch_mode"] = d.get("clutch_mode", "normal")


async def _load_lineup_for_team(pool, league_id: int, team_id: int) -> List[dict]:
    """Load players in lineup order for a team, returning dicts the sim engine expects.

    LEFT JOINs player_directives so tendency overrides are available pre-sim.
    _apply_directives is called on each player to fold directives into tendency fields.
    """
    rows = await pool.fetch(
        """
        SELECT p.*, l.is_starter, l.slot,
               pd.shot_diet, pd.usage_mode, pd.defense_mode, pd.role_mode, pd.clutch_mode
        FROM lineups l
        JOIN players p ON p.id = l.player_id
        LEFT JOIN player_directives pd ON pd.league_id = $1 AND pd.player_id = p.id
        WHERE l.league_id = $1 AND l.team_id = $2
        ORDER BY l.slot ASC
        """,
        league_id,
        team_id,
    )
    return [_apply_directives(dict(r)) for r in rows]


def _team_to_sim_dict(team: team_repo.Team, top8_avg_ovr: int = 75) -> dict:
    return {
        "team_id": team.id,
        "overall": top8_avg_ovr,
        "offense_rating": team.team_offense_rating or top8_avg_ovr,
        "defense_rating": team.team_defense_rating or top8_avg_ovr,
        "pace": team.pace or 100.0,
    }


async def _compute_team_ovr(pool, league_id: int, team_id: int) -> int:
    """Average OVR of the top-8 lineup slots (starters + primary bench).

    Returns 75 as a safe fallback when the team has no lineup rows or no
    players with a populated overall rating.
    """
    result = await pool.fetchval(
        """
        SELECT ROUND(AVG(p.overall))::INT
        FROM (
            SELECT p.overall
            FROM lineups l
            JOIN players p ON p.id = l.player_id
            WHERE l.league_id = $1 AND l.team_id = $2
            ORDER BY l.slot ASC
            LIMIT 8
        ) p
        """,
        league_id,
        team_id,
    )
    return int(result) if result is not None else 75
