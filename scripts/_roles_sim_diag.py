"""Phase 2 diagnostic — validate role-based touch share against sim output.

Simulates ~20 synthetic games per team for CLE / HOU / MIN / DEN and prints:
    name | role | share | mpg | ppg | fga | fta | 3pa

This confirms that Mobley/Sengun/Gobert/Jokic PPG shifted from ~5 to realistic
post-anchor numbers (~15-19), and that top scorers no longer hoard 35+ FGA.

Usage:
    $env:DBA_HEADLESS_MODE = "1"
    .venv/Scripts/python.exe scripts/_roles_sim_diag.py

Connection pattern mirrors _starters_diag.py (asyncpg direct, dotenv).
"""
from __future__ import annotations

import asyncio
import io
import os
import sys

import asyncpg
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

load_dotenv()

from services import sim_engine  # noqa: E402
from services.role_service import ROLE_REGISTRY, get_or_derive_roles  # noqa: E402

_N_GAMES = 20  # synthetic games per matchup


async def _load_players(conn, league_id: int, team_id: int) -> list[dict]:
    """Load lineup as plain dicts (same columns sim_engine needs)."""
    rows = await conn.fetch(
        """
        SELECT p.*, l.is_starter, l.slot
        FROM lineups l
        JOIN players p ON p.id = l.player_id
        WHERE l.league_id = $1 AND l.team_id = $2
        ORDER BY l.slot ASC
        """,
        league_id,
        team_id,
    )
    return [dict(r) for r in rows]


def _stamp_roles_sync(players: list[dict], assignments: list[dict], scheme: str) -> None:
    """Attach _role_* fields to player dicts for the sim engine (sync version for script)."""
    role_by_pid = {a["player_id"]: a for a in assignments}
    for p in players:
        pid = p.get("id") or p.get("player_id")
        assignment = role_by_pid.get(pid)
        if assignment:
            role = assignment["role"]
            touch_share = float(assignment["touch_share"])  # Postgres returns Decimal
        else:
            role = "glue_guy"
            touch_share = 0.08

        reg = ROLE_REGISTRY.get(role, ROLE_REGISTRY["glue_guy"])
        if scheme in reg.get("scheme_synergy", []):
            touch_share = min(touch_share * 1.15, 1.0)

        p["_role"] = role
        p["_role_touch_share"] = touch_share
        p["_role_fga_3pa_pct"] = reg["fga_3pa_pct"]
        p["_role_fta_per_fga"] = reg["fta_per_fga"]
        p["_role_def_role"] = reg["defensive_role"]
        p["_role_minutes_tier"] = reg["minutes_tier"]
        p["_role_tendencies"] = reg.get("tendencies_boosted", [])


async def _sim_team_games(
    conn,
    league_id: int,
    team_id: int,
    season: int,
    team_code: str,
    n_games: int = _N_GAMES,
) -> dict[int, dict]:
    """Sim N synthetic games for a team against itself, return per-player accumulators."""
    players = await _load_players(conn, league_id, team_id)
    if not players:
        return {}

    assignments = await get_or_derive_roles(conn, league_id, team_id, season)
    # Default scheme: ball_movement (most common for tendency_respecter teams)
    scheme = "ball_movement"
    _stamp_roles_sync(players, assignments, scheme)

    team_dict = {
        "team_id": team_id,
        "overall": 80,
        "offense_rating": 80,
        "defense_rating": 80,
        "pace": 100.0,
    }
    strategy = {
        "offensive_scheme": scheme,
        "defensive_scheme": "man_to_man",
        "pace_adjustment": 0.0,
        "ppp_offense_mult": 1.0,
        "ppp_defense_mult": 1.0,
        "three_rate_adj": 0.0,
        "opp_three_rate_adj": 0.0,
        "turnover_adj": 0.0,
        "opp_turnover_adj": 0.0,
        "foul_adj": 0.0,
        "star_usage_mult": 1.0,
        "transition_aggression": "balanced",
        "bench_leash": "normal",
    }

    accum: dict[int, dict] = {}

    for game_i in range(n_games):
        seed = team_id * 1000 + game_i
        result = sim_engine.sim_game(
            team_dict,
            team_dict,
            players,
            players,
            seed,
            home_strategy=strategy,
            away_strategy=strategy,
        )
        # Use home_box (home team players)
        for line in result["home_box"]:
            pid = line["player_id"]
            if pid not in accum:
                accum[pid] = {
                    "gp": 0, "pts": 0, "fga": 0, "fta": 0, "tpa": 0, "min": 0.0
                }
            accum[pid]["gp"] += 1
            accum[pid]["pts"] += line["points"]
            accum[pid]["fga"] += line["fga"]
            accum[pid]["fta"] += line["fta"]
            accum[pid]["tpa"] += line["tpa"]
            accum[pid]["min"] += line["minutes"]

    return accum


async def go() -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    lg = await conn.fetchval("SELECT id FROM leagues ORDER BY id DESC LIMIT 1")
    if lg is None:
        print("No leagues found — run headless_test.py first to create a league.")
        await conn.close()
        return

    season = await conn.fetchval(
        "SELECT COALESCE(MAX(season), 2024) FROM games WHERE league_id = $1", lg
    )

    for team_code in ("CLE", "HOU", "MIN", "DEN"):
        team_id = await conn.fetchval(
            "SELECT id FROM teams WHERE nba_team_code = $1 AND league_id = $2",
            team_code,
            lg,
        )
        if not team_id:
            print(f"=== {team_code} — not found ===\n")
            continue

        # Get role assignments and player info
        assignments = await get_or_derive_roles(conn, lg, team_id, season)
        role_by_pid = {a["player_id"]: a for a in assignments}

        # Get starter names/positions/ovr
        starter_rows = await conn.fetch(
            """
            SELECT p.id, p.first_name || ' ' || p.last_name AS name,
                   p.position, p.overall, l.slot
            FROM lineups l
            JOIN players p ON p.id = l.player_id
            WHERE l.league_id = $1 AND l.team_id = $2 AND l.slot <= 5
            ORDER BY l.slot
            """,
            lg,
            team_id,
        )
        # Sim N games
        accum = await _sim_team_games(conn, lg, team_id, season, team_code, _N_GAMES)

        print(f"=== {team_code} === ({_N_GAMES} synthetic games, scheme: ball_movement)")
        print(
            f"{'name':<24}{'role':<22}{'share':<7}{'mpg':<7}{'ppg':<7}{'fga':<6}{'fta':<6}{'3pa':<5}"
        )
        print("-" * 92)

        # Print starters by slot order
        for row in starter_rows:
            pid = row["id"]
            a = role_by_pid.get(pid)
            if not a:
                continue
            acc = accum.get(pid)
            gp = acc["gp"] if acc else 0
            if gp == 0:
                ppg = fga = fta = tpa = mpg = 0.0
            else:
                ppg = acc["pts"] / gp
                fga = acc["fga"] / gp
                fta = acc["fta"] / gp
                tpa = acc["tpa"] / gp
                mpg = acc["min"] / gp

            touch_pct = f"{a['touch_share'] * 100:.1f}%"
            print(
                f"{row['name']:<24}{a['role']:<22}{touch_pct:<7}"
                f"{mpg:<7.1f}{ppg:<7.1f}{fga:<6.1f}{fta:<6.1f}{tpa:<5.1f}"
            )
        print()

    # Team-level sanity check: pull simmed PPG from actual game_box_scores if games exist
    games_exist = await conn.fetchval(
        "SELECT COUNT(*) FROM games WHERE league_id = $1 AND status = 'simmed'", lg
    )
    if games_exist and games_exist > 0:
        print("=== Team PPG sanity check (from DB — actual simmed games) ===")
        rows = await conn.fetch(
            """
            SELECT t.nba_team_code AS code,
                   ROUND(AVG(g_pts.total_pts)::numeric, 1) AS ppg
            FROM (
                SELECT b.team_id,
                       SUM(b.points) AS total_pts
                FROM game_box_scores b
                JOIN games g ON g.id = b.game_id
                WHERE g.league_id = $1 AND g.status = 'simmed'
                GROUP BY b.game_id, b.team_id
            ) g_pts
            JOIN teams t ON t.id = g_pts.team_id AND t.league_id = $1
            GROUP BY t.nba_team_code
            ORDER BY ppg DESC
            LIMIT 10
            """,
            lg,
        )
        print(f"{'team':<8}{'ppg':<8}")
        print("-" * 16)
        for r in rows:
            flag = " <-- CHECK" if float(r["ppg"]) < 95 or float(r["ppg"]) > 130 else ""
            print(f"{r['code']:<8}{r['ppg']!s:<8}{flag}")
        print()

    await conn.close()


if __name__ == "__main__":
    asyncio.run(go())
