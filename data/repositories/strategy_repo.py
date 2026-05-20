from __future__ import annotations

import asyncpg

_DEFAULT_STRATEGY: dict = {
    "offensive_pace": "balanced",
    "offensive_scheme": "balanced",
    "defensive_scheme": "man_to_man",
    "defensive_intensity": "normal",
    "star_usage": 50,
}


async def get_strategy(pool: asyncpg.Pool, league_id: int, team_id: int) -> dict:
    """Returns the team's strategy row, or default values if none is set."""
    row = await pool.fetchrow(
        """
        SELECT offensive_pace, offensive_scheme, defensive_scheme,
               defensive_intensity, star_usage
        FROM team_strategies
        WHERE league_id = $1 AND team_id = $2
        """,
        league_id,
        team_id,
    )
    if row is None:
        return dict(_DEFAULT_STRATEGY)
    return dict(row)


async def set_strategy(pool: asyncpg.Pool, league_id: int, team_id: int, **fields) -> None:
    """Upsert strategy fields. Only provided fields are written."""
    allowed = {"offensive_pace", "offensive_scheme", "defensive_scheme", "defensive_intensity", "star_usage", "coach_mode", "transition_aggression", "bench_leash"}
    filtered = {k: v for k, v in fields.items() if k in allowed}
    if not filtered:
        return

    # Build an upsert: insert defaults then update the specific fields.
    col_list = ", ".join(filtered.keys())
    val_placeholders = ", ".join(f"${i + 3}" for i in range(len(filtered)))
    update_clause = ", ".join(f"{k} = EXCLUDED.{k}" for k in filtered)

    await pool.execute(
        f"""
        INSERT INTO team_strategies (league_id, team_id, {col_list}, updated_at)
        VALUES ($1, $2, {val_placeholders}, NOW())
        ON CONFLICT (league_id, team_id)
        DO UPDATE SET {update_clause}, updated_at = NOW()
        """,
        league_id,
        team_id,
        *filtered.values(),
    )


async def reset_strategy(pool: asyncpg.Pool, league_id: int, team_id: int) -> None:
    """Delete the strategy row so defaults take effect."""
    await pool.execute(
        "DELETE FROM team_strategies WHERE league_id = $1 AND team_id = $2",
        league_id,
        team_id,
    )


async def get_player_minutes(pool: asyncpg.Pool, league_id: int, team_id: int) -> dict[int, int]:
    """Returns {player_id: target_minutes} for all players with non-zero targets."""
    rows = await pool.fetch(
        """
        SELECT player_id, target_minutes
        FROM player_minutes
        WHERE league_id = $1 AND team_id = $2 AND target_minutes > 0
        """,
        league_id,
        team_id,
    )
    return {r["player_id"]: r["target_minutes"] for r in rows}


async def set_player_minutes(
    pool: asyncpg.Pool, league_id: int, team_id: int, player_id: int, minutes: int
) -> None:
    """Upsert target minutes for one player. minutes=0 resets to auto."""
    await pool.execute(
        """
        INSERT INTO player_minutes (league_id, team_id, player_id, target_minutes)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (league_id, team_id, player_id)
        DO UPDATE SET target_minutes = EXCLUDED.target_minutes
        """,
        league_id,
        team_id,
        player_id,
        minutes,
    )


async def reset_player_minutes(pool: asyncpg.Pool, league_id: int, team_id: int) -> None:
    """Clear all per-player targets for a team."""
    await pool.execute(
        "DELETE FROM player_minutes WHERE league_id = $1 AND team_id = $2",
        league_id,
        team_id,
    )


async def get_team_minutes_plan(
    pool: asyncpg.Pool, league_id: int, team_id: int, player_ids: list[int], game_seed: int = 0
) -> dict[int, float]:
    """
    Build the actual minutes allocation for a game.

    Players with a non-zero target_minutes get exactly that many minutes.
    Remaining minutes (240 - sum of targets) are distributed to auto players
    proportionally by their overall rating. Result is normalized to sum to 240.
    """
    if not player_ids:
        return {}

    targets = await get_player_minutes(pool, league_id, team_id)

    # Fetch overall ratings for all players so auto-players can be weighted by OVR.
    rows = await pool.fetch(
        "SELECT id, overall FROM players WHERE id = ANY($1::int[])",
        player_ids,
    )
    ovr_by_id = {r["id"]: r["overall"] for r in rows}

    plan: dict[int, float] = {}
    targeted_minutes = 0.0

    for pid in player_ids:
        target = targets.get(pid, 0)
        if target > 0:
            capped = min(target, 48)
            plan[pid] = float(capped)
            targeted_minutes += capped

    remaining = max(0.0, 240.0 - targeted_minutes)
    auto_players = [pid for pid in player_ids if pid not in plan]

    if auto_players:
        # Use (OVR - 60) weighting so stars get ~35+ min and deep bench gets ~5.
        # Linear OVR produced too-flat distributions. Floor at 0.5 so no one gets zero.
        weights = []
        for pid in auto_players:
            base = max(0.5, ((float(ovr_by_id.get(pid, 50)) - 50.0) / 10.0) ** 1.4)
            # Per-player per-game noise ±20% — deterministic from player id + game seed
            # so the same game always produces the same minutes (reproducible), but
            # different games produce different distributions (realistic foul trouble, blowouts, rest).
            noise = 1.0 + 0.20 * ((hash(pid ^ game_seed) % 100 - 50) / 50.0)
            weights.append(base * noise)
        total_w = sum(weights) or 1.0
        for pid, w in zip(auto_players, weights):
            plan[pid] = remaining * w / total_w
    elif remaining > 0:
        # All players have targets but they don't sum to 240 — redistribute surplus proportionally.
        total_plan = sum(plan.values()) or 1.0
        scale = 240.0 / total_plan
        plan = {pid: m * scale for pid, m in plan.items()}

    # Normalize so the total is exactly 240.
    total = sum(plan.values())
    if total > 0 and abs(total - 240.0) > 0.01:
        scale = 240.0 / total
        plan = {pid: m * scale for pid, m in plan.items()}

    # Clamp pass: no player exceeds 42 min (starters) or 30 min (bench).
    # Iterate twice — first pass identifies starters as the top-5 by OVR.
    starter_pids = set()
    if ovr_by_id:
        sorted_by_ovr = sorted(player_ids, key=lambda pid: ovr_by_id.get(pid, 0), reverse=True)
        starter_pids = set(sorted_by_ovr[:5])

    for _ in range(2):
        overflow = 0.0
        for pid in list(plan):
            cap = 42.0 if pid in starter_pids else 30.0
            if plan[pid] > cap:
                overflow += plan[pid] - cap
                plan[pid] = cap
        if overflow > 0.0:
            uncapped = [pid for pid in plan if plan[pid] < (42.0 if pid in starter_pids else 30.0)]
            if uncapped:
                share = overflow / len(uncapped)
                for pid in uncapped:
                    cap = 42.0 if pid in starter_pids else 30.0
                    plan[pid] = min(plan[pid] + share, cap)
            # If no uncapped players remain, overflow is irredistributable.
            # Accept sum < 240 — per-player ceiling takes priority over total.

    # Hard ceiling: no single player exceeds 48 minutes (one game).
    plan = {pid: min(m, 48.0) for pid, m in plan.items()}

    return plan
