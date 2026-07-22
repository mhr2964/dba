"""Cross-stream team intelligence aggregation.

Provides a single place to compute/fetch posture, franchise plan, coaching
philosophy, recent plan pivots, and recent role changes for any team.
Consumed by Workstreams A (/team posture/plan/philosophy), B (columnist),
and C (digest, philosophy map, power rankings).

Public API
----------
compute_posture(pool, league, team_id) -> dict
get_team_plan(pool, league_id, team_id, season) -> dict | None
get_team_philosophy(pool, team_id) -> str
get_recent_pivots(pool, league_id, team_id, season, *, limit) -> list[dict]
get_recent_role_changes(pool, league_id, season, team_id, *, days) -> list[dict]
build_team_intel(pool, league, season, team_ids, *, include) -> dict[int, dict]

Design notes
------------
- compute_posture is the per-team path (4 DB queries). Fine for single-team
  commands; the bulk path (build_team_intel) collapses to ≤5 queries total.
- Pivots are extracted from derived_from_record JSONB (no history table exists).
  A franchise_plan_history table would be needed for full pivot lineage; flagged
  as a design gap.
- Role changes surface player_roles rows where assigned_at > NOW() - INTERVAL;
  there is no player_roles_history table, so old→new transitions are not tracked.
  Flagged as a design gap.
"""
from __future__ import annotations

import json

from core.logging import get_logger
from data.repositories import league_repo
from services import franchise_plan_service

log = get_logger(__name__)

_TOTAL_GAMES = 82
_DEFAULT_PHILOSOPHY = "tendency_respecter"

# ---------------------------------------------------------------------------
# Philosophy descriptions — consumed by /team philosophy and strategy_embed
# ---------------------------------------------------------------------------

PHILOSOPHY_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "star_maxer": {
        "emoji": "⭐",
        "summary": "Top-2 OVR players preferred for iso_scorer / primary_initiator regardless of fit.",
        "tendency": "Concentrates touches on stars; bench depth is starved.",
    },
    "egalitarian": {
        "emoji": "\U0001f504",
        "summary": "Penalizes iso/initiator roles; rewards secondary_creator and movement_shooter.",
        "tendency": "Touches distribute across many secondary contributors.",
    },
    "defense_first": {
        "emoji": "\U0001f6e1",
        "summary": "Anyone with defense_tendency >= 60 pushed toward defensive role even if they could score.",
        "tendency": "Reshapes lineups to prioritize defenders.",
    },
    "tendency_respecter": {
        "emoji": "\U0001f4c8",
        "summary": "Matches role to actual player tendencies. Best-case competent coach.",
        "tendency": "No bias — pure tendency-driven assignment.",
    },
    "vet_overrater": {
        "emoji": "\U0001f9d3",
        "summary": "Aging vets (31+) get inflated role expectations; young players to developmental.",
        "tendency": "Star vets get bigger roles than tendencies warrant.",
    },
    "youth_developer": {
        "emoji": "\U0001f476",
        "summary": "Young players (≤24) pushed to feature roles regardless of OVR.",
        "tendency": "Old vets pushed to mentor/bench; young players get growth reps.",
    },
    "chaos": {
        "emoji": "\U0001f3b2",
        "summary": "Deterministic per-(player, role, season) randomization. Reproducible but illogical.",
        "tendency": "Wildly varied role assignments; expect Jokic at two_way_wing.",
    },
}


# ---------------------------------------------------------------------------
# compute_posture — per-team path (4 queries)
# ---------------------------------------------------------------------------

async def compute_posture(
    pool,
    league: league_repo.League,
    team_id: int,
) -> dict:
    """Compute trade posture from standings + roster age.

    Lifted verbatim from what is now trade_reasoning_fetchers._compute_posture.
    Returns:
        {mode, urgency, projected_wins, conf_rank, avg_age,
         games_remaining, wins, losses}
    """
    from services import trade_context_builder  # local: avoids circular import chain

    row = await pool.fetchrow(
        """
        SELECT sc.wins, sc.losses, t.conference
        FROM standings_cache sc
        JOIN teams t ON t.id = sc.team_id
        WHERE sc.league_id = $1 AND sc.team_id = $2 AND sc.season = $3
        """,
        league.id, team_id, league.current_season,
    )
    wins = row["wins"] if row else 0
    losses = row["losses"] if row else 0
    conference = row["conference"] if row else None
    games_played = wins + losses

    projected_wins: int | None = None
    if games_played >= 10:
        projected_wins = round((wins / games_played) * _TOTAL_GAMES)

    conf_rank: int | None = None
    if conference:
        rank_val = await pool.fetchval(
            """
            SELECT COUNT(*) + 1
            FROM standings_cache sc2
            JOIN teams t2 ON t2.id = sc2.team_id
            WHERE sc2.league_id = $1 AND sc2.season = $2
              AND t2.conference = $3
              AND sc2.wins > $4
            """,
            league.id, league.current_season, conference, wins,
        )
        conf_rank = int(rank_val) if rank_val is not None else None

    age_rows = await pool.fetch(
        """
        SELECT EXTRACT(YEAR FROM AGE(p.birth_date))::int AS age
        FROM lineups l
        JOIN players p ON p.id = l.player_id
        WHERE l.league_id = $1 AND l.team_id = $2
          AND p.birth_date IS NOT NULL
        ORDER BY l.slot ASC
        LIMIT 8
        """,
        league.id, team_id,
    )
    ages = [r["age"] for r in age_rows if r["age"] is not None]
    avg_age = sum(ages) / len(ages) if ages else 27.0

    # Query star count (OVR >= 85) and plan goal for posture floor logic.
    star_count_val = await pool.fetchval(
        """
        SELECT COUNT(*)
        FROM lineups l
        JOIN players p ON p.id = l.player_id
        WHERE l.league_id = $1 AND l.team_id = $2 AND p.overall >= 85
        """,
        league.id, team_id,
    )
    star_count = int(star_count_val or 0)

    plan_goal_row = await pool.fetchrow(
        """
        SELECT goal FROM franchise_plans
        WHERE league_id = $1 AND team_id = $2 AND season = $3
        """,
        league.id, team_id, league.current_season,
    )
    plan_goal = plan_goal_row["goal"] if plan_goal_row else None

    mode = trade_context_builder.compute_team_mode(
        projected_wins, avg_age, conf_rank,
        star_count=star_count, plan_goal=plan_goal,
    )

    games_remaining = max(0, _TOTAL_GAMES - games_played)
    in_top4 = conf_rank is not None and conf_rank <= 4
    in_top6 = conf_rank is not None and conf_rank <= 6
    in_top10 = conf_rank is not None and conf_rank <= 10

    if mode in ("rebuilding", "soft_rebuild"):
        urgency = "tanking"
    elif mode == "contending":
        if in_top4:
            urgency = "comfortable" if (games_remaining > 20 or conf_rank <= 2) else "pushing"
        elif in_top6:
            urgency = "comfortable" if games_remaining > 20 else "pushing"
        else:
            urgency = "pushing"
    elif mode == "play_in_fringe":
        if in_top10 and games_remaining < 20:
            urgency = "desperate"
        else:
            urgency = "pushing"
    else:
        urgency = "comfortable"

    return {
        "mode": mode,
        "urgency": urgency,
        "projected_wins": projected_wins,
        "conf_rank": conf_rank,
        "avg_age": avg_age,
        "games_remaining": games_remaining,
        "wins": wins,
        "losses": losses,
    }


# ---------------------------------------------------------------------------
# get_team_plan — thin wrapper over franchise_plan_service.get_plan
# ---------------------------------------------------------------------------

async def get_team_plan(
    pool,
    league_id: int,
    team_id: int,
    season: int,
) -> dict | None:
    """Return the stored franchise plan with normalized display fields.

    Adds bucket sizes and resolves core_player_ids → core_names via a single
    batch query. Returns None if no plan has been derived yet.
    """
    plan = await franchise_plan_service.get_plan(pool, league_id, team_id, season)
    if plan is None:
        return None

    # Add bucket sizes alongside ID lists.
    plan = dict(plan)
    plan["core_size"] = len(plan.get("core_player_ids") or [])
    plan["flex_size"] = len(plan.get("flex_player_ids") or [])
    plan["surplus_size"] = len(plan.get("surplus_player_ids") or [])

    # Resolve core IDs → names in one batch.
    core_ids: list[int] = plan.get("core_player_ids") or []
    if core_ids:
        rows = await pool.fetch(
            """
            SELECT id,
                   first_name || ' ' || last_name AS full_name
            FROM players
            WHERE id = ANY($1::int[])
            """,
            core_ids,
        )
        name_map = {r["id"]: r["full_name"] for r in rows}
        plan["core_names"] = [name_map.get(pid, f"player#{pid}") for pid in core_ids]
    else:
        plan["core_names"] = []

    return plan


# ---------------------------------------------------------------------------
# get_team_philosophy
# ---------------------------------------------------------------------------

async def get_team_philosophy(pool, team_id: int) -> str:
    """Return teams.coach_philosophy. Defaults to 'tendency_respecter' if NULL."""
    val = await pool.fetchval(
        "SELECT coach_philosophy FROM teams WHERE id = $1",
        team_id,
    )
    return val or _DEFAULT_PHILOSOPHY


# ---------------------------------------------------------------------------
# get_recent_pivots — extracted from derived_from_record JSONB
# ---------------------------------------------------------------------------

async def get_recent_pivots(
    pool,
    league_id: int,
    team_id: int,
    season: int,
    *,
    limit: int = 3,
) -> list[dict]:
    """Return recent franchise plan pivots for this team.

    DESIGN GAP: There is no franchise_plan_history table. Pivot metadata is
    embedded in the current plan's derived_from_record JSONB by
    franchise_plan_service (keys: pivot_from_goal, pivot_reason,
    pivot_game_index). This means at most one pivot per season is surfaced.

    Returns list of {pivot_date, old_goal, new_goal, reason} dicts.
    Returns empty list when no pivot metadata is present.
    """
    row = await pool.fetchrow(
        """
        SELECT derived_from_record, derived_at, goal
        FROM franchise_plans
        WHERE league_id = $1 AND team_id = $2 AND season = $3
        """,
        league_id, team_id, season,
    )
    if not row:
        return []

    dfr: dict = row["derived_from_record"] or {}
    if isinstance(dfr, str):
        import json
        dfr = json.loads(dfr)

    pivot_from = dfr.get("pivot_from_goal")
    pivot_reason = dfr.get("pivot_reason")
    if not pivot_from:
        return []

    return [{
        "pivot_date": row["derived_at"].isoformat() if row["derived_at"] else None,
        "old_goal": pivot_from,
        "new_goal": row["goal"],
        "reason": pivot_reason or "",
    }][:limit]


# ---------------------------------------------------------------------------
# get_recent_role_changes
# ---------------------------------------------------------------------------

async def get_recent_role_changes(
    pool,
    league_id: int,
    season: int,
    team_id: int,
    *,
    days: int = 14,
) -> list[dict]:
    """Return recently-assigned player roles for this team.

    DESIGN GAP: There is no player_roles_history table, so old→new transitions
    are not tracked. This returns player_roles rows where assigned_at is within
    the last `days` days. The old_role field will always be None until a history
    table is added.

    Each entry: {player_name, old_role, new_role, assigned_by, assigned_at}
    """
    rows = await pool.fetch(
        """
        SELECT pr.player_id,
               p.first_name || ' ' || p.last_name AS player_name,
               pr.role         AS new_role,
               pr.assigned_by,
               pr.assigned_at
        FROM player_roles pr
        JOIN players p ON p.id = pr.player_id
        WHERE pr.league_id  = $1
          AND pr.team_id    = $2
          AND pr.season     = $3
          AND pr.assigned_at > NOW() - ($4::int * INTERVAL '1 day')
        ORDER BY pr.assigned_at DESC
        """,
        league_id, team_id, season, int(days),
    )
    return [
        {
            "player_name": r["player_name"],
            "old_role": None,  # no history table — old value unavailable
            "new_role": r["new_role"],
            "assigned_by": r["assigned_by"],
            "assigned_at": r["assigned_at"].isoformat() if r["assigned_at"] else None,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# build_team_intel — bulk path, ≤5 DB queries for N teams
# ---------------------------------------------------------------------------

async def build_team_intel(
    pool,
    league: league_repo.League,
    season: int,
    team_ids: list[int],
    *,
    include: tuple[str, ...] = (
        "posture", "plan", "philosophy", "recent_pivots", "recent_role_changes"
    ),
) -> dict[int, dict]:
    """Bulk-fetch intel for many teams in one call.

    Returns {team_id: {posture, plan, philosophy, recent_pivots, recent_role_changes}}.
    The `include` tuple lets callers skip slices they don't need (e.g. columnists
    with tight prompt budgets can omit recent_role_changes).

    Query budget for all 30 teams (worst-case, all slices included):
      1. standings_cache + conf rank via window function — 1 query
      2. lineups avg age per team — 1 query
      3. franchise_plans for all teams — 1 query (pivots extracted in Python, 0 extra)
      4. core player name batch — 1 query (only when plan slice included and core IDs exist)
      5. teams.coach_philosophy — 1 query
      6. player_roles recent changes — 1 query
      Total: ≤6 queries worst-case; ≤5 when "plan" is excluded or all teams have empty cores.
      Each query is parameterised with the full team_id array — no per-team loops.
    """
    if not team_ids:
        return {}

    result: dict[int, dict] = {tid: {} for tid in team_ids}

    # ------------------------------------------------------------------
    # Query 1: standings for all teams in one shot, including conf rank.
    # Rank is computed via a window function so we don't loop.
    # ------------------------------------------------------------------
    if "posture" in include:
        from services import trade_context_builder  # local: avoids circular import

        sc_rows = await pool.fetch(
            """
            WITH all_standings AS (
                SELECT sc.team_id,
                       sc.wins,
                       sc.losses,
                       t.conference,
                       (
                           SELECT COUNT(*) + 1
                           FROM standings_cache sc2
                           JOIN teams t2 ON t2.id = sc2.team_id
                           WHERE sc2.league_id = sc.league_id
                             AND sc2.season    = sc.season
                             AND t2.conference = t.conference
                             AND sc2.wins      > sc.wins
                       )::int AS conf_rank
                FROM standings_cache sc
                JOIN teams t ON t.id = sc.team_id
                WHERE sc.league_id = $1
                  AND sc.season    = $2
            )
            SELECT * FROM all_standings
            WHERE team_id = ANY($3::int[])
            """,
            league.id, season, team_ids,
        )
        sc_by_team: dict[int, dict] = {r["team_id"]: dict(r) for r in sc_rows}

        # ------------------------------------------------------------------
        # Query 2: avg lineup age per team — one batch query.
        # ------------------------------------------------------------------
        age_rows = await pool.fetch(
            """
            SELECT l.team_id,
                   AVG(EXTRACT(YEAR FROM AGE(p.birth_date)))::float AS avg_age
            FROM lineups l
            JOIN players p ON p.id = l.player_id
            WHERE l.league_id = $1
              AND l.team_id   = ANY($2::int[])
              AND p.birth_date IS NOT NULL
            GROUP BY l.team_id
            """,
            league.id, team_ids,
        )
        avg_age_by_team: dict[int, float] = {
            r["team_id"]: r["avg_age"] for r in age_rows
        }

        # ------------------------------------------------------------------
        # Query 2b: star count (OVR >= 85) per team — drives posture floor.
        # ------------------------------------------------------------------
        star_rows = await pool.fetch(
            """
            SELECT l.team_id, COUNT(*) AS star_count
            FROM lineups l
            JOIN players p ON p.id = l.player_id
            WHERE l.league_id = $1
              AND l.team_id   = ANY($2::int[])
              AND p.overall   >= 85
            GROUP BY l.team_id
            """,
            league.id, team_ids,
        )
        star_count_by_team: dict[int, int] = {
            r["team_id"]: int(r["star_count"]) for r in star_rows
        }

        # ------------------------------------------------------------------
        # Query 2c: franchise plan goals (for plan_goal floor override).
        # Only needed when "posture" slice is included; reuses the plan query
        # if "plan" is also included — but that runs later, so fetch minimally.
        # ------------------------------------------------------------------
        plan_goal_rows = await pool.fetch(
            """
            SELECT team_id, goal
            FROM franchise_plans
            WHERE league_id = $1 AND season = $2
              AND team_id   = ANY($3::int[])
            """,
            league.id, season, team_ids,
        )
        plan_goal_by_team: dict[int, str | None] = {
            r["team_id"]: r["goal"] for r in plan_goal_rows
        }

        for tid in team_ids:
            sc = sc_by_team.get(tid, {})
            wins = sc.get("wins", 0) or 0
            losses = sc.get("losses", 0) or 0
            conf_rank = sc.get("conf_rank")
            games_played = wins + losses

            projected_wins: int | None = None
            if games_played >= 10:
                projected_wins = round((wins / games_played) * _TOTAL_GAMES)

            avg_age: float = avg_age_by_team.get(tid, 27.0) or 27.0
            star_count: int = star_count_by_team.get(tid, 0)
            plan_goal: str | None = plan_goal_by_team.get(tid)
            mode = trade_context_builder.compute_team_mode(
                projected_wins, avg_age, conf_rank,
                star_count=star_count, plan_goal=plan_goal,
            )

            games_remaining = max(0, _TOTAL_GAMES - games_played)
            in_top4 = conf_rank is not None and conf_rank <= 4
            in_top6 = conf_rank is not None and conf_rank <= 6
            in_top10 = conf_rank is not None and conf_rank <= 10

            if mode in ("rebuilding", "soft_rebuild"):
                urgency = "tanking"
            elif mode == "contending":
                if in_top4:
                    urgency = "comfortable" if (games_remaining > 20 or conf_rank <= 2) else "pushing"
                elif in_top6:
                    urgency = "comfortable" if games_remaining > 20 else "pushing"
                else:
                    urgency = "pushing"
            elif mode == "play_in_fringe":
                if in_top10 and games_remaining < 20:
                    urgency = "desperate"
                else:
                    urgency = "pushing"
            else:
                urgency = "comfortable"

            result[tid]["posture"] = {
                "mode": mode,
                "urgency": urgency,
                "projected_wins": projected_wins,
                "conf_rank": conf_rank,
                "avg_age": avg_age,
                "games_remaining": games_remaining,
                "wins": wins,
                "losses": losses,
            }

    # ------------------------------------------------------------------
    # Query 3: franchise plans for all teams.
    # ------------------------------------------------------------------
    if "plan" in include or "recent_pivots" in include:
        from data.repositories import franchise_plan_repo

        plan_rows = await pool.fetch(
            """
            SELECT fp.*
            FROM franchise_plans fp
            WHERE fp.league_id = $1
              AND fp.season    = $2
              AND fp.team_id   = ANY($3::int[])
            """,
            league.id, season, team_ids,
        )
        raw_plans: dict[int, dict] = {
            r["team_id"]: franchise_plan_repo._parse_plan(dict(r))
            for r in plan_rows
        }

        # Resolve core player names in a single batch across all teams.
        if "plan" in include:
            all_core_ids: list[int] = []
            for plan in raw_plans.values():
                all_core_ids.extend(plan.get("core_player_ids") or [])
            all_core_ids = list(dict.fromkeys(all_core_ids))  # deduplicate, preserve order

            name_map: dict[int, str] = {}
            if all_core_ids:
                name_rows = await pool.fetch(
                    """
                    SELECT id, first_name || ' ' || last_name AS full_name
                    FROM players WHERE id = ANY($1::int[])
                    """,
                    all_core_ids,
                )
                name_map = {r["id"]: r["full_name"] for r in name_rows}

            for tid in team_ids:
                plan = raw_plans.get(tid)
                if plan is None:
                    result[tid]["plan"] = None
                    continue
                plan = dict(plan)
                core_ids: list[int] = plan.get("core_player_ids") or []
                plan["core_size"] = len(core_ids)
                plan["flex_size"] = len(plan.get("flex_player_ids") or [])
                plan["surplus_size"] = len(plan.get("surplus_player_ids") or [])
                plan["core_names"] = [
                    name_map.get(pid, f"player#{pid}") for pid in core_ids
                ]
                result[tid]["plan"] = plan

        if "recent_pivots" in include:
            for tid in team_ids:
                plan = raw_plans.get(tid)
                if plan is None:
                    result[tid]["recent_pivots"] = []
                    continue
                dfr = plan.get("derived_from_record") or {}
                if isinstance(dfr, str):
                    try:
                        dfr = json.loads(dfr)
                    except (json.JSONDecodeError, TypeError):
                        log.warning("team_intel: malformed derived_from_record for team %d — skipping pivots", tid)
                        dfr = {}
                pivot_from = dfr.get("pivot_from_goal")
                if not pivot_from:
                    result[tid]["recent_pivots"] = []
                    continue
                derived_at = plan.get("derived_at")
                result[tid]["recent_pivots"] = [{
                    "pivot_date": derived_at.isoformat() if hasattr(derived_at, "isoformat") else str(derived_at) if derived_at else None,
                    "old_goal": pivot_from,
                    "new_goal": plan.get("goal", "?"),
                    "reason": dfr.get("pivot_reason") or "",
                }]

    # ------------------------------------------------------------------
    # Query 4: coach_philosophy for all teams.
    # ------------------------------------------------------------------
    if "philosophy" in include:
        phil_rows = await pool.fetch(
            """
            SELECT id, coach_philosophy
            FROM teams
            WHERE id = ANY($1::int[])
            """,
            team_ids,
        )
        for r in phil_rows:
            result[r["id"]]["philosophy"] = r["coach_philosophy"] or _DEFAULT_PHILOSOPHY
        # Fill any teams not returned (shouldn't happen, but be safe).
        for tid in team_ids:
            if "philosophy" not in result[tid]:
                result[tid]["philosophy"] = _DEFAULT_PHILOSOPHY

    # ------------------------------------------------------------------
    # Query 5: recent role changes across all teams.
    # ------------------------------------------------------------------
    if "recent_role_changes" in include:
        role_rows = await pool.fetch(
            """
            SELECT pr.team_id,
                   pr.player_id,
                   p.first_name || ' ' || p.last_name AS player_name,
                   pr.role         AS new_role,
                   pr.assigned_by,
                   pr.assigned_at
            FROM player_roles pr
            JOIN players p ON p.id = pr.player_id
            WHERE pr.league_id = $1
              AND pr.season    = $2
              AND pr.team_id   = ANY($3::int[])
              AND pr.assigned_at > NOW() - INTERVAL '14 days'
            ORDER BY pr.assigned_at DESC
            """,
            league.id, season, team_ids,
        )
        # Group by team.
        changes_by_team: dict[int, list[dict]] = {tid: [] for tid in team_ids}
        for r in role_rows:
            changes_by_team[r["team_id"]].append({
                "player_name": r["player_name"],
                "old_role": None,  # no history table — old value unavailable
                "new_role": r["new_role"],
                "assigned_by": r["assigned_by"],
                "assigned_at": r["assigned_at"].isoformat() if r["assigned_at"] else None,
            })
        for tid in team_ids:
            result[tid]["recent_role_changes"] = changes_by_team.get(tid, [])

    return result


# ---------------------------------------------------------------------------
# snapshot_all_teams — called at every batch flush in batch_sim_runner
# ---------------------------------------------------------------------------

async def snapshot_all_teams(
    pool,
    league_id: int,
    season: int,
    sim_batch_index: int,
) -> int:
    """Insert a team_state_snapshots row for every CPU team in the league.

    Idempotent: uses ON CONFLICT DO UPDATE so crash-and-retry of the same batch
    refreshes the row rather than raising a unique-constraint error.

    Returns the number of rows upserted.

    Query cost: 1 call to build_team_intel (≤6 queries for all teams) +
    one bulk INSERT … ON CONFLICT DO UPDATE.  Cost is bounded and does not
    grow with batch size.
    """
    # Resolve league object so build_team_intel can use league.current_season.
    raw = await pool.fetchrow(
        "SELECT * FROM leagues WHERE id = $1",
        league_id,
    )
    if not raw:
        log.warning(f"snapshot_all_teams: league {league_id} not found — skipping")
        return 0

    league = league_repo._league_from_record(raw)

    # Fetch all team IDs in the league (CPU and human — posture is useful for both).
    team_id_rows = await pool.fetch(
        "SELECT id FROM teams WHERE league_id = $1",
        league_id,
    )
    team_ids = [r["id"] for r in team_id_rows]
    if not team_ids:
        return 0

    intel = await build_team_intel(
        pool,
        league,
        season,
        team_ids,
        include=("posture", "plan", "philosophy"),
    )

    rows: list[tuple] = []
    for tid in team_ids:
        data = intel.get(tid, {})
        posture: dict = data.get("posture") or {}
        plan: dict | None = data.get("plan")
        philosophy: str | None = data.get("philosophy")

        rows.append((
            league_id,
            tid,
            season,
            sim_batch_index,
            posture.get("mode"),
            posture.get("urgency"),
            posture.get("projected_wins"),
            posture.get("conf_rank"),
            posture.get("avg_age"),
            plan.get("goal") if plan else None,
            plan.get("horizon_seasons") if plan else None,
            philosophy,
        ))

    if not rows:
        return 0

    await pool.executemany(
        """
        INSERT INTO team_state_snapshots (
            league_id, team_id, season, sim_batch_index,
            mode, urgency, projected_wins, conf_rank, avg_age,
            plan_goal, plan_horizon, philosophy
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        ON CONFLICT (league_id, team_id, season, sim_batch_index)
        DO UPDATE SET
            mode           = EXCLUDED.mode,
            urgency        = EXCLUDED.urgency,
            projected_wins = EXCLUDED.projected_wins,
            conf_rank      = EXCLUDED.conf_rank,
            avg_age        = EXCLUDED.avg_age,
            plan_goal      = EXCLUDED.plan_goal,
            plan_horizon   = EXCLUDED.plan_horizon,
            philosophy     = EXCLUDED.philosophy,
            snapshot_at    = NOW()
        """,
        rows,
    )
    log.debug(
        f"snapshot_all_teams: upserted {len(rows)} rows "
        f"(league={league_id} season={season} batch={sim_batch_index})"
    )
    return len(rows)
