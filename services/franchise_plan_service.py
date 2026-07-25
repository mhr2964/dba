"""Franchise plan derivation and persistence.

A franchise_plan captures a CPU team's multi-year strategic direction:
  goal          — win_now | transition | rebuild | tank
  horizon       — seasons until expected payoff
  core/flex/surplus player buckets
  asset_targets — what the team is hunting in trades

Phase 1: plans are derived and stored only.
Phase 2 wires plans into trade decision logic.
Phase 3 adds targeted counterparty scanning before proposals.
Phase 4 adds reassessment checkpoints + pivot eligibility so plans carry
         strategic commitment between sim batches rather than recomputing
         blindly every time.

Public API
----------
derive_plan(pool, league_id, team_id, season) -> dict
    Compute without persisting.

persist_plan(pool, plan) -> int
    Upsert; returns plan id.

get_plan(pool, league_id, team_id, season) -> dict | None
    Read stored plan; None if absent.

get_or_derive(pool, league_id, team_id, season) -> dict
    Read existing; derive + persist if missing.

derive_and_persist_all(pool, league_id, season, current_game_index=None) -> int
    Bulk refresh for all CPU teams.  Phase 4: respects plan stickiness —
    only re-derives at checkpoint windows or when a pivot condition fires.

Pure decision/classification logic lives in franchise_plan_math.py,
production-stat fetching lives in franchise_plan_production.py (Phase 3
opportunistic split, see HANDOFF.md) -- this module is now the
orchestration layer only.
"""
from __future__ import annotations

import datetime
from typing import Optional

from core.logging import get_logger
from data.repositories import franchise_plan_repo, player_repo, team_repo
from services import trade_context_builder
from services.franchise_plan_math import (
    _ASSET_TARGETS,
    _build_rationale,
    _calc_age,
    _categorise_players,
    _combined_tier,
    _defensive_tier,
    _derive_goal_and_horizon,
    _is_reassessment_checkpoint,
    _production_tier,
    _project_wins,
    _should_pivot,
)
from services.franchise_plan_production import _fetch_season_production

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def derive_plan(
    pool, league_id: int, team_id: int, season: int
) -> dict:
    """Compute a fresh franchise plan.  Does NOT persist — caller decides."""

    # 1. Roster — active players only.
    # IL/inactive/two-way players skew avg_age and star flags; exclude them here.
    # We do NOT modify get_roster itself because many other callers need the
    # unfiltered list (salary accounting, full-team lookups, etc.).
    players = await player_repo.get_roster(pool, league_id, team_id)
    players = [p for p in players if p.roster_status == "active"]

    roster: list[dict] = []
    archetype_map: dict[int, str | None] = {}
    for p in players:
        age = _calc_age(p.birth_date, season)
        roster.append({
            "id": p.id,           # used by _fetch_season_production / BDL fallback
            "player_id": p.id,    # used by _categorise_players and callers
            "first_name": p.first_name,
            "last_name": p.last_name,
            "age": age,
            "overall": p.overall,
            "position": p.position,
            "full_name": p.full_name,
        })
        archetype_map[p.id] = p.defensive_archetype

    ovr_list = [r["overall"] for r in roster]

    # 2. Record from standings_cache + team conference
    sc_row = await pool.fetchrow(
        """
        SELECT sc.wins, sc.losses, t.conference
        FROM standings_cache sc
        JOIN teams t ON t.id = sc.team_id
        WHERE sc.league_id=$1 AND sc.team_id=$2 AND sc.season=$3
        """,
        league_id, team_id, season,
    )
    wins = sc_row["wins"] if sc_row else 0
    losses = sc_row["losses"] if sc_row else 0
    conference = sc_row["conference"] if sc_row else None
    games_played = wins + losses
    projected_wins = _project_wins(wins, losses, games_played)

    # Conference rank: peers with strictly more wins → 1-based rank
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
            league_id, season, conference, wins,
        )
        conf_rank = int(rank_val) if rank_val is not None else None

    # 3. Avg age of top-8 by OVR
    top8 = sorted(roster, key=lambda p: p["overall"], reverse=True)[:8]
    valid_ages = [p["age"] for p in top8 if p["age"] is not None]
    avg_age = sum(valid_ages) / len(valid_ages) if valid_ages else 27.0

    # Team mode — shared 5-bucket classification; computed here so avg_age is available.
    # star_count derived from roster already loaded above (OVR >= 85 threshold matches
    # cpu_trade_posture._compute_team_posture).  plan_goal is None here because this IS
    # the derive step — the goal doesn't exist yet.  B7 posture floor still fires via
    # star_count; the plan_goal floor only applies when re-evaluating an existing plan.
    _derive_star_count = sum(1 for r in roster if r["overall"] >= 85)
    team_mode = trade_context_builder.compute_team_mode(
        projected_wins if games_played >= 10 else None,
        avg_age,
        conf_rank,
        star_count=_derive_star_count,
        plan_goal=None,  # REQUIRED for B7 posture floor — goal not yet derived at this step
    )

    # 4. Star flags
    has_young_star = any(
        p["overall"] >= 88 and p["age"] is not None and p["age"] <= 25
        for p in roster
    )
    has_elite_star = any(p["overall"] >= 90 for p in roster)
    has_any_star = any(p["overall"] >= 88 for p in roster)
    # Prime-window star: OVR >= 88 AND 26 <= age <= 28 — the most common championship
    # window that was previously misclassified as transition.
    has_prime_age_star = any(
        p["overall"] >= 88 and p["age"] is not None and 26 <= p["age"] <= 28
        for p in roster
    )

    # Best star name for rationale (prefer prime-age, then young)
    prime_stars = [
        p for p in sorted(roster, key=lambda x: x["overall"], reverse=True)
        if p["overall"] >= 88 and p["age"] is not None and 26 <= p["age"] <= 28
    ]
    young_stars = [
        p for p in sorted(roster, key=lambda x: x["overall"], reverse=True)
        if p["overall"] >= 88 and p["age"] is not None and p["age"] <= 25
    ]
    star_name: Optional[str] = (
        (prime_stars or young_stars or [None])[0] or {}
    ).get("full_name")
    if not star_name:
        top_star = next((p for p in sorted(roster, key=lambda x: x["overall"], reverse=True) if p["overall"] >= 88), None)
        star_name = top_star["full_name"] if top_star else None

    # 5. Draft picks (R1, next 3 seasons)
    r1_count = await pool.fetchval(
        """
        SELECT COUNT(*) FROM draft_picks
        WHERE league_id = $1 AND current_team_id = $2
          AND round = 1 AND season > $3 AND season <= $3 + 3
        """,
        league_id, team_id, season,
    )
    r1_picks_next3 = int(r1_count or 0)

    # 6. Derive goal + horizon
    goal, horizon = _derive_goal_and_horizon(
        projected_wins=projected_wins,
        avg_age=avg_age,
        has_young_star=has_young_star,
        has_elite_star=has_elite_star,
        has_any_star=has_any_star,
        r1_picks_next3=r1_picks_next3,
        games_played=games_played,
        ovr_list=ovr_list,
        prime_age_star=has_prime_age_star,
        mode=team_mode,
        conf_rank=conf_rank,
    )

    # 7. Player categorisation — augmented with current-season production.
    #    Players with GP < 10 fall back to prior-season BDL cache so that stars
    #    like Haliburton aren't bucketed as 'flex' at game 0 of a fresh league.
    #    recently_acquired_ids: players acquired within the last 60 sim games
    #    are eligible for "flip_asset" shop_intent if they land in surplus.
    production_map = await _fetch_season_production(pool, league_id, season, roster)
    _recently_acq_ids: set[int] = set()
    try:
        _sim_date_row = await pool.fetchrow(
            "SELECT MAX(scheduled_date) AS sim_date FROM games WHERE league_id = $1 AND status = 'simmed'",
            league_id,
        )
        _sim_date = _sim_date_row["sim_date"] if _sim_date_row else None
        if _sim_date is not None:
            _acq_rows = await pool.fetch(
                """SELECT id FROM players
                   WHERE team_id IN (SELECT id FROM teams WHERE league_id=$1)
                   AND last_traded_at IS NOT NULL
                   AND last_traded_at >= $2""",
                league_id,
                _sim_date - datetime.timedelta(days=60),
            )
            _recently_acq_ids = {r["id"] for r in _acq_rows}
    except Exception as exc:
        log.debug("flip_asset: skipping recently-acquired query — %s", exc)

    core_ids, flex_ids, surplus_ids, youth_overrides, shop_intent = _categorise_players(
        goal, roster, avg_age,
        production_map=production_map,
        archetype_map=archetype_map,
        recently_acquired_ids=_recently_acq_ids,
    )

    # 8. Asset targets
    asset_targets = _ASSET_TARGETS[goal]

    # 9. Rationale
    rationale = _build_rationale(
        goal=goal,
        star_name=star_name,
        avg_age=avg_age,
        projected_wins=projected_wins,
        r1_picks_next3=r1_picks_next3,
        target_year=season + horizon,
        has_any_star=has_any_star,
    )

    # 10. Snapshot of derivation inputs.
    #     production_tiers captures {player_id: {off, def, combined, archetype}} for
    #     players with enough GP to classify — useful for diagnostics and ride-along
    #     rationale without a separate DB re-query.
    production_tiers: dict[str, dict] = {}
    for pid, stats in production_map.items():
        off_t = _production_tier(stats)
        def_t = _defensive_tier(stats)
        arch  = archetype_map.get(pid)
        comb  = _combined_tier(off_t, def_t, arch)
        # Only persist when we have enough data to be meaningful.
        if off_t != "unknown" or def_t != "unknown":
            production_tiers[str(pid)] = {
                "off":       off_t,
                "def":       def_t,
                "combined":  comb,
                "archetype": arch,
            }
    derived_from_record = {
        "wins": wins,
        "losses": losses,
        "games_played": games_played,
        "projected_wins": round(projected_wins, 1),
        "avg_age_top8": round(avg_age, 2),
        "has_young_star": has_young_star,
        "has_elite_star": has_elite_star,
        "has_prime_age_star": has_prime_age_star,
        "r1_picks_next3": r1_picks_next3,
        "roster_size": len(roster),
        "production_tiers": production_tiers,
        "youth_overrides": {str(pid): reason for pid, reason in youth_overrides.items()},
        # shop_intent: why each surplus player is available.  Keyed by str(player_id)
        # for JSON-serialisation compatibility (JSONB column).
        "shop_intent": {str(pid): reason for pid, reason in shop_intent.items()},
    }

    return {
        "league_id": league_id,
        "team_id": team_id,
        "season": season,
        "goal": goal,
        "horizon_seasons": horizon,
        "core_player_ids": core_ids,
        "flex_player_ids": flex_ids,
        "surplus_player_ids": surplus_ids,
        "asset_targets": asset_targets,
        "rationale": rationale,
        "derived_from_record": derived_from_record,
    }


async def persist_plan(pool, plan: dict) -> int:
    """Upsert the plan row.  Returns the plan id.

    Pivot metadata (pivot_from_goal, pivot_reason, pivot_game_index) are merged
    into derived_from_record before the upsert so they survive across restarts
    without a schema change — they're ephemeral annotations, not structural fields.
    """
    _pivot_keys = ("pivot_from_goal", "pivot_reason", "pivot_game_index")
    _has_pivot = any(k in plan for k in _pivot_keys)
    if _has_pivot:
        plan = dict(plan)  # shallow copy — don't mutate caller's dict
        dfr = dict(plan.get("derived_from_record") or {})
        for k in _pivot_keys:
            if k in plan:
                dfr[k] = plan[k]
        plan["derived_from_record"] = dfr
    return await franchise_plan_repo.upsert_plan(pool, plan)


async def get_plan(pool, league_id: int, team_id: int, season: int) -> Optional[dict]:
    """Read latest stored plan.  Returns None if never derived."""
    return await franchise_plan_repo.get_plan(pool, league_id, team_id, season)


async def get_or_derive(pool, league_id: int, team_id: int, season: int) -> dict:
    """Return stored plan; derive + persist if absent."""
    existing = await get_plan(pool, league_id, team_id, season)
    if existing is not None:
        return existing
    plan = await derive_plan(pool, league_id, team_id, season)
    await persist_plan(pool, plan)
    return plan


async def derive_and_persist_all(
    pool,
    league_id: int,
    season: int,
    current_game_index: Optional[int] = None,
) -> int:
    """Bulk refresh for all CPU teams.  Returns count re-derived.

    Phase 4 stickiness — for each CPU team:
    1. Read existing plan + last_derived_game_index.
    2. Check _is_reassessment_checkpoint:
       - 'sticky': skip (plan stays as-is).
       - checkpoint reason is 'mid_season_checkpoint': run _should_pivot first;
         only re-derive if a pivot condition fires.
       - any other checkpoint: always re-derive.
    3. On re-derive, log pivot events (old_goal → new_goal) as FRANCHISE-PIVOT.

    Human-managed teams are skipped — they don't use CPU franchise logic.
    current_game_index is passed by sim batch runners; None means offseason/admin call.
    """
    teams = await team_repo.get_all(pool, league_id)
    cpu_teams = [t for t in teams if t.manager_user_id is None]

    count = 0
    skipped = 0

    for team in cpu_teams:
        try:
            existing = await get_plan(pool, league_id, team.id, season)
            last_idx = existing.get("last_derived_game_index") if existing else None

            # Determine games_played + games_remaining for checkpoint detection.
            sc_row = await pool.fetchrow(
                "SELECT wins, losses FROM standings_cache "
                "WHERE league_id=$1 AND team_id=$2 AND season=$3",
                league_id, team.id, season,
            )
            wins = sc_row["wins"] if sc_row else 0
            losses = sc_row["losses"] if sc_row else 0
            games_played = wins + losses

            # Total regular season games: used to compute games_remaining.
            total_row = await pool.fetchrow(
                "SELECT COUNT(*) AS cnt FROM games "
                "WHERE league_id=$1 AND season=$2 AND season_type='regular'",
                league_id, season,
            )
            total_games = int(total_row["cnt"]) if total_row else 82
            games_remaining = max(0, total_games - games_played)

            should_derive, reason = _is_reassessment_checkpoint(
                games_played, games_remaining, last_idx
            )

            if not should_derive:
                skipped += 1
                continue

            # Mid-season checkpoint: pivot-gated — only re-derive if a pivot fires.
            if reason == "mid_season_checkpoint" and existing is not None:
                projected_wins = _project_wins(wins, losses, games_played)
                current_record = {
                    "wins": wins,
                    "losses": losses,
                    "games_played": games_played,
                    "projected_wins": projected_wins,
                }
                # Count R1 picks for rebuild → tank pivot check.
                r1_count = await pool.fetchval(
                    """
                    SELECT COUNT(*) FROM draft_picks
                    WHERE league_id=$1 AND current_team_id=$2
                      AND round=1 AND season > $3 AND season <= $3 + 3
                    """,
                    league_id, team.id, season,
                )
                r1_banked = int(r1_count or 0)

                pivot_ok, _new_goal, _pivot_reason = _should_pivot(
                    existing, current_record, r1_banked
                )
                if not pivot_ok:
                    skipped += 1
                    continue
                # Fall through — pivot fires; full re-derive below.

            old_goal = existing.get("goal") if existing else None
            plan = await derive_plan(pool, league_id, team.id, season)
            plan["last_derived_game_index"] = current_game_index

            new_goal = plan["goal"]
            if old_goal is not None and old_goal != new_goal:
                # Derive a short reason label — prefer the pivot reason if we
                # just confirmed a mid-season pivot; otherwise label by checkpoint.
                if reason == "mid_season_checkpoint":
                    # Re-compute pivot reason to surface in the log line.
                    projected_wins_now = plan["derived_from_record"].get("projected_wins", 0.0)
                    r1_b = plan["derived_from_record"].get("r1_picks_next3", 0)
                    _, _, _log_reason = _should_pivot(
                        {"goal": old_goal},
                        {"projected_wins": projected_wins_now},
                        r1_b,
                    )
                    pivot_log_reason = _log_reason or reason
                else:
                    pivot_log_reason = reason

                log.info(
                    "[FRANCHISE-PIVOT] league=%d team=%s season=%d game=%s "
                    "%s %s → %s — \"%s\"",
                    league_id,
                    team.nba_team_code,
                    season,
                    str(current_game_index) if current_game_index is not None else "offseason",
                    team.nba_team_code,
                    old_goal,
                    new_goal,
                    pivot_log_reason,
                )
                # Embed pivot metadata in the plan dict so plan_alignment blocks
                # (cpu_trade_service) can surface it in headless/ride-along output
                # without a separate DB lookup.
                plan["pivot_from_goal"] = old_goal
                plan["pivot_reason"] = pivot_log_reason
                plan["pivot_game_index"] = current_game_index
            else:
                # Carry forward any existing pivot metadata so it stays visible
                # until the next reassessment window clears it.
                # NOTE: persist_plan embeds pivot keys inside derived_from_record
                # before upserting; _parse_plan does NOT lift them back to the top
                # level.  Read from derived_from_record, not from existing directly.
                if existing and isinstance(existing.get("derived_from_record"), dict):
                    _src = existing["derived_from_record"]
                    for _k in ("pivot_from_goal", "pivot_reason", "pivot_game_index"):
                        if _k in _src:
                            plan[_k] = _src[_k]
                log.debug(
                    "franchise_plan refreshed: league=%d team=%s season=%d "
                    "game=%s goal=%s reason=%s",
                    league_id, team.nba_team_code, season,
                    str(current_game_index) if current_game_index is not None else "offseason",
                    new_goal, reason,
                )

            await persist_plan(pool, plan)
            count += 1

        except Exception as exc:
            # FP2: this failure leaves the team's plan stale (last_derived_game_index
            # never advances in the DB since persist didn't happen), which means the
            # checkpoint gate above will naturally re-attempt derivation for this team
            # on every subsequent call (top-level or, post-FP1, sub-batch) until it
            # succeeds -- there is no permanent-staleness risk requiring a separate
            # retry mechanism. Scoped to observability only: log.error (a stale plan
            # silently feeds trade decisions -- worth surfacing above warning level)
            # with exc_info for a real traceback, team code for readability, and the
            # game index so a failure can be correlated to the sim batch that caused it.
            log.error(
                "franchise_plan derive failed: league=%d team=%s(id=%d) season=%d "
                "game=%s — %s",
                league_id, team.nba_team_code, team.id, season,
                str(current_game_index) if current_game_index is not None else "offseason",
                exc,
                exc_info=True,
            )

    log.info(
        "derive_and_persist_all: league=%d season=%d derived=%d skipped=%d/%d CPU teams",
        league_id, season, count, skipped, len(cpu_teams),
    )

    # Phase 1: derive CPU role assignments for all teams after plans settle.
    # Phase 2 wires roles into the sim engine touch-share calculation.
    try:
        from services import role_service  # local import to avoid circular dep
        await role_service.derive_and_persist_all(pool, league_id, season)
    except Exception as exc:
        log.warning(
            "role_service.derive_and_persist_all failed: league=%d season=%d — %s",
            league_id, season, exc,
        )

    return count
