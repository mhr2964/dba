"""CPU role assignment for player touch-share and offensive identity.

Each rostered player is assigned one role per (league, team, season).  The role
carries a base touch_share, shot-profile flags (fga_3pa_pct, fta_per_fga),
minutes tier, defensive responsibility, and which player tendencies it amplifies.

Phase 1: single philosophy implemented — 'tendency_respecter'.
          All other philosophies fall back to it.
Phase 2: sim engine reads player_roles.touch_share instead of raw usage_weight.
Phase 3: per-philosophy bias functions applied at scoring step — 30 CPU teams
         now reach DIFFERENT conclusions about the same player.

Public API
----------
derive_roles(conn, league_id, team_id, season, *, philosophy=None) -> list[dict]
    Compute without persisting.

persist_roles(conn, league_id, team_id, season, assignments) -> None
    UPSERT into player_roles; does NOT overwrite locked=TRUE rows.

get_or_derive_roles(conn, league_id, team_id, season) -> list[dict]
    Read existing; derive + persist if missing.

derive_and_persist_all(conn, league_id, season) -> None
    Bulk refresh for every team in the league.

Role taxonomy (ROLE_REGISTRY) and the pure scoring/derivation algorithm
(_score_role_fit, _derive_tendency_respecter) live in role_scoring.py
(Phase 3 opportunistic split, see HANDOFF.md) -- this module is now the
DB orchestration layer only. ROLE_REGISTRY is re-imported below so
`role_service.ROLE_REGISTRY` still resolves for external callers
(bot/cogs/coach_cog.py).
"""
from __future__ import annotations

import random
import re
from typing import Optional

from core.logging import get_logger
from services.philosophies import PHILOSOPHY_BIASES, assert_philosophy_constraint_sync  # noqa: F401
from services.role_scoring import ROLE_REGISTRY, _age_from_birth, _derive_tendency_respecter

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Coach philosophy enum
# ---------------------------------------------------------------------------

# PHILOSOPHY_BIASES and assert_philosophy_constraint_sync are imported from
# services.philosophies at the top of this file — kept as public re-exports.

COACH_PHILOSOPHIES: list[str] = list(PHILOSOPHY_BIASES.keys())


async def assert_role_constraint_sync(conn) -> None:
    """Assert that ROLE_REGISTRY keys are covered by the DB CHECK constraint.

    Call once at bot startup (after DB connection is established) to catch drift
    between ROLE_REGISTRY and the player_roles_role_check constraint before it
    causes role-assignment failures in production.

    Raises RuntimeError if any key in ROLE_REGISTRY is absent from the
    constraint's IN-list, so the problem is immediately visible in startup logs.
    """
    row = await conn.fetchrow(
        """
        SELECT pg_get_constraintdef(c.oid) AS def
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE c.conname = 'player_roles_role_check'
          AND t.relname = 'player_roles'
        """
    )
    if row is None:
        log.warning(
            "role_service: player_roles_role_check constraint not found — "
            "run migration 041 to add it"
        )
        return

    constraint_def = row["def"]

    def _in_constraint(key: str) -> bool:
        return bool(re.search(r"\b" + re.escape(key) + r"\b", constraint_def))

    missing = [k for k in ROLE_REGISTRY if not _in_constraint(k)]
    if missing:
        msg = (
            f"role_service: ROLE_REGISTRY ↔ CHECK constraint drift detected. "
            f"Keys missing from constraint: {missing}. "
            f"Create a new Alembic migration to add them."
        )
        log.error(msg)
        raise RuntimeError(msg)

    # Reverse check: warn if the DB constraint mentions values not in ROLE_REGISTRY.
    # Don't crash — the DB may temporarily have legacy values during migration.
    db_values = set(re.findall(r"'([^']+)'", constraint_def))
    legacy = db_values - set(ROLE_REGISTRY.keys())
    if legacy:
        log.warning(
            "role_service: DB CHECK constraint has values not in ROLE_REGISTRY "
            "(legacy migration values?): %s", sorted(legacy)
        )

    log.debug("role_service: role constraint sync OK (%d roles)", len(ROLE_REGISTRY))


# Veto-loop safety cap: if the user vetoes this many times without accepting,
# the last derivation is accepted automatically to avoid infinite loops.
MAX_VETO_ATTEMPTS = 7


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def derive_roles(
    conn,
    league_id: int,
    team_id: int,
    season: int,
    *,
    philosophy: Optional[str] = None,
) -> list[dict]:
    """Compute role assignments for one team WITHOUT persisting.

    Returns: [{player_id, role, touch_share, rationale}, ...] with touch_share
    normalised so the team total equals 1.0.  The rationale includes a
    [philosophy: <name>] marker for any non-tendency_respecter team.
    """
    # Read philosophy from teams table if not passed explicitly
    if philosophy is None:
        row = await conn.fetchrow(
            "SELECT coach_philosophy FROM teams WHERE id = $1",
            team_id,
        )
        philosophy = row["coach_philosophy"] if row else "tendency_respecter"

    # All non-implemented philosophies fall back to tendency_respecter in Phase 1.
    if philosophy not in COACH_PHILOSOPHIES:
        log.warning("role_service: unknown philosophy '%s', falling back", philosophy)
        philosophy = "tendency_respecter"

    # Fetch rostered players (slots 1-15). D3 Option B (docs/design/
    # draft-logic-rules.md): also pull is_rookie/years_pro (players table) and
    # pick_number (draft_selections, joined via drafts to scope to this
    # player's own draft -- a player is selected at most once, so this LEFT
    # JOIN resolves to at most one row per player regardless of league/season
    # filters) so _score_role_fit's rookie-pedigree bias has the signal it
    # needs. NULL pick_number/years_pro (undrafted seed players, legacy data)
    # is handled as a no-op by _rookie_pedigree_bonus.
    rows = await conn.fetch(
        """
        SELECT
            p.id            AS player_id,
            p.overall,
            p.birth_date,
            p.position,
            p.tendency_3pt,
            p.tendency_drive,
            p.tendency_pass,
            p.ast_tendency,
            p.reb_tendency,
            p.blk_tendency,
            p.stl_tendency,
            p.defense_tendency,
            p.usage_weight,
            p.defensive_archetype,
            p.is_rookie,
            p.years_pro,
            ds.pick_number,
            l.slot
        FROM lineups l
        JOIN players p ON p.id = l.player_id
        LEFT JOIN draft_selections ds ON ds.player_id = p.id
        WHERE l.league_id = $1
          AND l.team_id   = $2
          AND l.slot BETWEEN 1 AND 15
        ORDER BY l.slot
        """,
        league_id,
        team_id,
    )

    if not rows:
        log.warning(
            "role_service: no roster rows for league=%d team=%d season=%d",
            league_id, team_id, season,
        )
        return []

    players = []
    for r in rows:
        age = _age_from_birth(r["birth_date"], season)
        players.append({
            "player_id": r["player_id"],
            "overall": r["overall"],
            "birth_date": r["birth_date"],
            "_age": age,
            "position": r["position"],
            "tendency_3pt": r["tendency_3pt"],
            "tendency_drive": r["tendency_drive"],
            "tendency_pass": r["tendency_pass"],
            "ast_tendency": r["ast_tendency"],
            "reb_tendency": r["reb_tendency"],
            "blk_tendency": r["blk_tendency"],
            "stl_tendency": r["stl_tendency"],
            "defense_tendency": r["defense_tendency"],
            "usage_weight": r["usage_weight"],
            "defensive_archetype": r["defensive_archetype"],
            "is_rookie": r["is_rookie"],
            "years_pro": r["years_pro"],
            "pick_number": r["pick_number"],
            "slot": r["slot"],
        })

    team_context = {
        "league_id": league_id,
        "team_id": team_id,
        "season": season,
        "philosophy": philosophy,
    }

    # Phase 3: philosophy bias injected via team_context; all derivations route
    # through _derive_tendency_respecter which threads team_context into scoring.
    assignments = _derive_tendency_respecter(players, team_context)

    # Normalise touch_share so team total = 1.0
    total = sum(a["touch_share"] for a in assignments)
    if total > 0:
        for a in assignments:
            a["touch_share"] = round(a["touch_share"] / total, 4)

    return assignments


async def persist_roles(
    conn,
    league_id: int,
    team_id: int,
    season: int,
    assignments: list[dict],
) -> None:
    """UPSERT assignments into player_roles.

    Rows where locked=TRUE are left untouched — human overrides survive
    an automated re-derive.
    """
    # Belt-and-suspenders: catch invalid role names before they hit the DB CHECK
    # constraint (added in migration 041).  Keeps error context rich.
    for a in assignments:
        if a["role"] not in ROLE_REGISTRY:
            raise ValueError(
                f"Invalid role {a['role']!r} for player {a['player_id']} "
                f"(league={league_id} team={team_id} season={season})"
            )

    for a in assignments:
        await conn.execute(
            """
            INSERT INTO player_roles
                (league_id, team_id, season, player_id, role, touch_share, rationale,
                 assigned_by, locked, assigned_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'cpu', FALSE, NOW())
            ON CONFLICT (league_id, team_id, season, player_id) DO UPDATE
                SET role        = EXCLUDED.role,
                    touch_share = EXCLUDED.touch_share,
                    rationale   = EXCLUDED.rationale,
                    assigned_by = EXCLUDED.assigned_by,
                    assigned_at = NOW()
                WHERE player_roles.locked = FALSE
            """,
            league_id,
            team_id,
            season,
            a["player_id"],
            a["role"],
            a["touch_share"],
            a.get("rationale", ""),
        )


async def get_or_derive_roles(
    conn,
    league_id: int,
    team_id: int,
    season: int,
) -> list[dict]:
    """Return stored role assignments; derive + persist if none exist for this team/season."""
    rows = await conn.fetch(
        """
        SELECT player_id, role, touch_share, rationale
        FROM player_roles
        WHERE league_id = $1 AND team_id = $2 AND season = $3
        ORDER BY touch_share DESC
        """,
        league_id, team_id, season,
    )
    if rows:
        return [dict(r) for r in rows]

    assignments = await derive_roles(conn, league_id, team_id, season)
    await persist_roles(conn, league_id, team_id, season, assignments)
    return assignments


async def _fetch_player_display(conn, league_id: int, team_id: int, player_ids: list[int]) -> dict[int, dict]:
    """Return {player_id: {name, position, overall}} for display in ride-along prompts."""
    if not player_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT p.id,
               p.first_name || ' ' || p.last_name AS name,
               p.position,
               p.overall
        FROM players p
        JOIN lineups l ON l.player_id = p.id
        WHERE l.league_id = $1 AND l.team_id = $2 AND p.id = ANY($3::int[])
        """,
        league_id, team_id, player_ids,
    )
    return {r["id"]: {"name": r["name"], "position": r["position"], "overall": r["overall"]} for r in rows}


async def _enrich_assignments(conn, league_id: int, team_id: int, assignments: list[dict]) -> list[dict]:
    """Merge player display info (name, position, overall) into assignment dicts for ride-along."""
    pids = [a["player_id"] for a in assignments]
    display = await _fetch_player_display(conn, league_id, team_id, pids)
    enriched = []
    for a in assignments:
        info = display.get(a["player_id"], {})
        enriched.append({**a, **info})
    return enriched


async def derive_and_persist_all_for_team(
    conn,
    league_id: int,
    team_id: int,
    season: int,
    *,
    silent_emit: bool = False,
) -> None:
    """Re-derive and persist role assignments for a single team.

    Called after roster changes (trades, free agent signings) to keep
    player_roles consistent with the new lineup.  Existing locked=TRUE rows
    are preserved by persist_roles' UPSERT guard.

    When DBA_RIDE_ALONG=1 and RIDE_ALONG_ROLE_PAUSE!=0, surfaces the
    assignment table interactively.  If the user types 'v', re-derives with a
    different philosophy and loops until accepted (capped at MAX_VETO_ATTEMPTS).

    Parameters
    ----------
    silent_emit : bool
        When True and ride-along is active, role events are written to the JSONL
        log but the input() pause is skipped.  Pass True from trade-execution
        call sites so automated re-derives don't block mid-trade.  Manual calls
        (e.g. /coach role assign) should leave this False (default) so the user
        sees each result interactively.
    """
    from services.ride_along import is_role_pause_enabled, emit_role_assignment, emit_role_change

    # Snapshot existing roles BEFORE any writes so we can detect deltas later.
    prior_rows = await conn.fetch(
        """
        SELECT player_id, role, touch_share
        FROM player_roles
        WHERE league_id = $1 AND team_id = $2 AND season = $3
        """,
        league_id, team_id, season,
    )
    prior_map: dict[int, dict] = {r["player_id"]: dict(r) for r in prior_rows}

    # Fetch team info for ride-along display.
    team_row = await conn.fetchrow(
        "SELECT nba_team_code, coach_philosophy FROM teams WHERE id = $1", team_id
    )
    team_code = team_row["nba_team_code"] if team_row else f"team#{team_id}"
    base_philosophy = (team_row["coach_philosophy"] if team_row else None) or "tendency_respecter"

    try:
        # Veto loop: if ride-along role pause is on (and not silent), let the user
        # re-derive with a different philosophy seed until they accept.
        # Cap at MAX_VETO_ATTEMPTS to prevent an infinite loop.
        current_philosophy = base_philosophy
        veto_count = 0
        assignments: list[dict] = []
        while True:
            assignments = await derive_roles(
                conn, league_id, team_id, season, philosophy=current_philosophy
            )

            pause_active = is_role_pause_enabled() and not silent_emit
            if pause_active and assignments:
                enriched = await _enrich_assignments(conn, league_id, team_id, assignments)
                action = emit_role_assignment(
                    league_id=league_id,
                    team_id=team_id,
                    team_code=team_code,
                    philosophy=current_philosophy,
                    assignments=enriched,
                )
                if action == "veto":
                    veto_count += 1
                    if veto_count >= MAX_VETO_ATTEMPTS:
                        print(
                            f"   [veto limit ({MAX_VETO_ATTEMPTS}) reached for {team_code} — "
                            f"keeping last derivation with philosophy: {current_philosophy}]"
                        )
                    else:
                        # Re-randomize to a DIFFERENT philosophy for this team only.
                        other_philosophies = [p for p in COACH_PHILOSOPHIES if p != current_philosophy]
                        if not other_philosophies:
                            # Degenerate: only one philosophy in the list — stop looping.
                            print(f"   [no alternative philosophies available; accepting {current_philosophy}]")
                        else:
                            current_philosophy = random.choice(other_philosophies)
                            print(f"   [re-deriving {team_code} with philosophy: {current_philosophy}]")
                            continue  # loop back and re-derive
            elif is_role_pause_enabled() and silent_emit and assignments:
                # silent_emit path: log the assignment event to JSONL but skip input().
                enriched = await _enrich_assignments(conn, league_id, team_id, assignments)
                emit_role_assignment(
                    league_id=league_id,
                    team_id=team_id,
                    team_code=team_code,
                    philosophy=current_philosophy,
                    assignments=enriched,
                )
            break

        # Atomic write: DELETE stale rows + INSERT/UPDATE new rows in one transaction.
        # Keeping these in the same transaction means a Ctrl-C or unexpected error
        # during the veto loop (before this block) leaves the prior rows intact —
        # player_roles is never emptied before the replacement is confirmed.
        async with conn.transaction():
            await conn.execute(
                """
                DELETE FROM player_roles
                WHERE league_id = $1 AND team_id = $2 AND season = $3
                  AND player_id NOT IN (
                      SELECT player_id FROM lineups
                      WHERE league_id = $1 AND team_id = $2
                  )
                """,
                league_id, team_id, season,
            )
            await persist_roles(conn, league_id, team_id, season, assignments)

        # Detect and surface role deltas (changes vs prior state).
        # Always log to JSONL; only pause for input when not in silent mode.
        if prior_map and is_role_pause_enabled():
            display_map = await _fetch_player_display(
                conn, league_id, team_id, [a["player_id"] for a in assignments]
            )
            deltas: list[dict] = []
            for a in assignments:
                pid = a["player_id"]
                prior = prior_map.get(pid)
                if prior and prior["role"] != a["role"]:
                    info = display_map.get(pid, {})
                    deltas.append({
                        "player_id": pid,
                        "name": info.get("name", f"player#{pid}"),
                        "old_role": prior["role"],
                        "old_touch_share": prior["touch_share"],
                        "new_role": a["role"],
                        "new_touch_share": a["touch_share"],
                    })
            if deltas:
                emit_role_change(
                    league_id=league_id,
                    team_id=team_id,
                    team_code=team_code,
                    philosophy=current_philosophy,
                    deltas=deltas,
                    reason="Post-trade / lineup re-derive",
                    pause=not silent_emit,
                )

        log.debug(
            "role_service: re-derived roles for league=%d team=%d season=%d",
            league_id, team_id, season,
        )
    except Exception as exc:
        log.warning(
            "role_service: derive_and_persist_all_for_team failed "
            "league=%d team=%d season=%d — %s",
            league_id, team_id, season, exc,
        )


async def derive_and_persist_all(conn, league_id: int, season: int) -> None:
    """Derive and persist role assignments for every team in the league.

    Called at season turnover from franchise_plan_service.derive_and_persist_all.
    When ride-along role pauses are active, each team's assignments are surfaced
    interactively with veto-loop support (same as derive_and_persist_all_for_team).
    """
    from services.ride_along import is_role_pause_enabled, emit_role_assignment

    team_rows = await conn.fetch(
        """
        SELECT id, nba_team_code, coach_philosophy
        FROM teams WHERE league_id = $1 ORDER BY nba_team_code
        """,
        league_id,
    )
    count = 0
    for t in team_rows:
        try:
            team_code = t["nba_team_code"]
            current_philosophy = (t["coach_philosophy"] or "tendency_respecter")
            veto_count = 0

            while True:
                assignments = await derive_roles(
                    conn, league_id, t["id"], season, philosophy=current_philosophy
                )

                if is_role_pause_enabled() and assignments:
                    enriched = await _enrich_assignments(conn, league_id, t["id"], assignments)
                    action = emit_role_assignment(
                        league_id=league_id,
                        team_id=t["id"],
                        team_code=team_code,
                        philosophy=current_philosophy,
                        assignments=enriched,
                    )
                    if action == "veto":
                        veto_count += 1
                        if veto_count >= MAX_VETO_ATTEMPTS:
                            print(
                                f"   [veto limit ({MAX_VETO_ATTEMPTS}) reached for {team_code} — "
                                f"keeping last derivation with philosophy: {current_philosophy}]"
                            )
                        else:
                            other_philosophies = [p for p in COACH_PHILOSOPHIES if p != current_philosophy]
                            if not other_philosophies:
                                print(f"   [no alternative philosophies available; accepting {current_philosophy}]")
                            else:
                                current_philosophy = random.choice(other_philosophies)
                                print(f"   [re-deriving {team_code} with philosophy: {current_philosophy}]")
                                continue
                break

            await persist_roles(conn, league_id, t["id"], season, assignments)
            count += 1
        except Exception as exc:
            log.warning(
                "role_service derive failed: league=%d team=%s season=%d — %s",
                league_id, t["nba_team_code"], season, exc,
            )
    log.info(
        "derive_and_persist_all: league=%d season=%d roles derived for %d/%d teams",
        league_id, season, count, len(team_rows),
    )
