"""Shared helpers for ride-along trade panels.

Lives in its own module so both ``trade_service`` (accept/reject/counter)
and ``cpu_trade_service`` (propose, three-team) can use the same role-display
block without creating a circular import between the two.
"""
from __future__ import annotations


async def build_ra_role_block(
    pool,
    league_id: int,
    season: int,
    team_player_map: dict[str, list[int]],
) -> dict[str, list[str]]:
    """
    Build the roles-affected block for a ride-along trade panel.

    Returns a dict keyed by team_code where each value is a list of
    "PlayerName: role (X%) [coach: philosophy]" strings for the players
    moving in/out of that team.

    Roles capture how the coach uses each player.  A trade that swaps
    an iso_scorer for a rim_protector reshapes more than salary or OVR —
    that context is what the user wants to see when approving.
    """
    out: dict[str, list[str]] = {}
    if not team_player_map:
        return out
    all_pids: list[int] = []
    for pids in team_player_map.values():
        all_pids.extend(pids)
    if not all_pids:
        return out

    rows = await pool.fetch(
        """
        SELECT pr.player_id, pr.team_id, pr.role, pr.touch_share,
               p.first_name || ' ' || p.last_name AS name,
               t.coach_philosophy
        FROM player_roles pr
        JOIN players p ON p.id = pr.player_id
        JOIN teams   t ON t.id = pr.team_id
        WHERE pr.league_id = $1 AND pr.season = $2
          AND pr.player_id = ANY($3::int[])
        """,
        league_id, season, all_pids,
    )
    by_pid = {r["player_id"]: r for r in rows}

    for team_code, pids in team_player_map.items():
        team_lines: list[str] = []
        for pid in pids:
            r = by_pid.get(pid)
            if r is None:
                team_lines.append(f"player#{pid}: (no role yet)")
                continue
            share_pct = float(r["touch_share"]) * 100
            team_lines.append(
                f"{r['name']}: {r['role']} ({share_pct:.0f}%) "
                f"[coach: {r['coach_philosophy']}]"
            )
        if team_lines:
            out[team_code] = team_lines
    return out
