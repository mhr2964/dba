"""DB-touching data fetchers for ride-along trade panel narratives: pick-slot
projection, per-player role/position/form lookups, and the roster-synergy
detector. All async, all pool-backed.

Extracted from ra_reasoning.py (Phase 3 opportunistic split, see
HANDOFF.md) along with trade_narrative_lines.py.
"""
from __future__ import annotations

from core.logging import get_logger
from data.repositories import league_repo

log = get_logger(__name__)


_TOTAL_GAMES = 82


async def _compute_posture(pool, league: league_repo.League, team_id: int) -> dict:
    """Compute trade posture from standings + roster age.

    Delegates to team_intel.compute_posture — single source of truth.
    Return shape unchanged: {mode, urgency, projected_wins, conf_rank, avg_age,
    games_remaining, wins, losses}
    """
    from services import team_intel
    return await team_intel.compute_posture(pool, league, team_id)


# ---------------------------------------------------------------------------
# Pick projection
# ---------------------------------------------------------------------------

_PICK_VERDICT: list[tuple[tuple[int, int], str]] = [
    ((1, 4),   "lottery — top-4 prospect range"),
    ((5, 14),  "lottery — mid first"),
    ((15, 20), "late first — solid rotation player range"),
    ((21, 30), "late first — long-shot"),
    ((31, 60), "second round — developmental flier"),
]


async def _project_pick_slot(pool, league_id: int, pick: dict, current_season: int) -> tuple[int, str, int | None, int | None]:
    """Estimate draft slot for a pick.

    Returns (estimated_slot, verdict, projected_wins, projected_losses).
    Uses original team's current-season record; falls back to #15 if no data.
    """
    original_team_id = pick.get("original_team_id") or pick.get("current_team_id")
    round_num = pick.get("round", 1)

    if round_num != 1:
        # Second-round picks don't carry meaningful slot projections.
        return 45, "second round — developmental flier", None, None

    # Use the most recent season's standings to project record
    row = await pool.fetchrow(
        """
        SELECT sc.wins, sc.losses, t.conference
        FROM standings_cache sc
        JOIN teams t ON t.id = sc.team_id
        WHERE sc.league_id = $1 AND sc.team_id = $2 AND sc.season = $3
        """,
        league_id, original_team_id, current_season,
    )
    if not row or (row["wins"] + row["losses"]) == 0:
        return 15, "late first — modest", None, None

    wins = row["wins"]
    losses = row["losses"]
    games_played = wins + losses

    if games_played >= 10:
        proj_wins = round((wins / games_played) * _TOTAL_GAMES)
        proj_losses = _TOTAL_GAMES - proj_wins
    else:
        proj_wins = wins
        proj_losses = losses

    # Count teams with strictly more wins in same season → draft rank
    league_rank = await pool.fetchval(
        """
        SELECT COUNT(*) + 1
        FROM standings_cache sc2
        WHERE sc2.league_id = $1 AND sc2.season = $2
          AND sc2.wins > $3
        """,
        league_id, current_season, wins,
    )
    league_rank = int(league_rank) if league_rank is not None else 15

    # For picks in future seasons, we use current record as proxy
    slot = max(1, min(30, league_rank))

    verdict = "late first — modest"
    for (lo, hi), v in _PICK_VERDICT:
        if lo <= slot <= hi:
            verdict = v
            break

    return slot, verdict, proj_wins, proj_losses


# ---------------------------------------------------------------------------
# Player-fit helpers
# ---------------------------------------------------------------------------

async def _fetch_player_role_on_team(pool, league_id: int, season: int, player_id: int) -> dict:
    """Fetch player's most recent role in this league, regardless of which team derived it.

    After a trade, the player's role may not yet be re-derived on the new team.
    We fall back to whatever role entry exists so the narrative has something
    to work with rather than showing "unknown on ?".
    """
    row = await pool.fetchrow(
        """
        SELECT pr.role, pr.touch_share, t.coach_philosophy, t.nba_team_code, t.id AS team_id
        FROM player_roles pr
        JOIN teams t ON t.id = pr.team_id
        WHERE pr.league_id = $1 AND pr.season = $2 AND pr.player_id = $3
        ORDER BY pr.team_id DESC
        LIMIT 1
        """,
        league_id, season, player_id,
    )
    if not row:
        return {}
    return dict(row)


async def _fetch_position_depth(
    pool, league_id: int, season: int, team_id: int, position: str
) -> int:
    """Count players of a given position in the team's lineup."""
    count = await pool.fetchval(
        """
        SELECT COUNT(*)
        FROM lineups l
        JOIN players p ON p.id = l.player_id
        WHERE l.league_id = $1 AND l.team_id = $2 AND p.position = $3
        """,
        league_id, team_id, position,
    )
    return int(count or 0)


async def _fetch_player_full(pool, league_id: int, player_id: int) -> dict:
    """Fetch player fields needed for reasoning."""
    row = await pool.fetchrow(
        """
        SELECT p.id, p.first_name, p.last_name, p.position, p.overall,
               p.tendency_3pt, p.tendency_drive, p.tendency_pass,
               p.ast_tendency, p.reb_tendency, p.blk_tendency, p.stl_tendency,
               p.defense_tendency, p.usage_weight, p.defensive_archetype,
               EXTRACT(YEAR FROM AGE(p.birth_date))::int AS age,
               p.team_id
        FROM players p
        WHERE p.id = $1 AND p.league_id = $2
        """,
        player_id, league_id,
    )
    if not row:
        return {}
    return dict(row)


async def _fetch_top_role_at_position(
    pool, league_id: int, season: int, team_id: int, position: str
) -> dict | None:
    """Fetch the highest touch_share player_role row for a given position on this team."""
    row = await pool.fetchrow(
        """
        SELECT pr.player_id, pr.role, pr.touch_share,
               p.first_name || ' ' || p.last_name AS name, p.overall
        FROM player_roles pr
        JOIN players p ON p.id = pr.player_id
        WHERE pr.league_id = $1 AND pr.season = $2 AND pr.team_id = $3
          AND p.position = $4
        ORDER BY pr.touch_share DESC
        LIMIT 1
        """,
        league_id, season, team_id, position,
    )
    return dict(row) if row else None


async def _fetch_roster_median_ovr(pool, league_id: int, team_id: int) -> float:
    """Return the median OVR of players currently in the team's lineup.

    Used to decide whether an incoming player is a genuine upgrade or a depth
    filler.  Falls back to 75.0 on any error or empty roster.
    """
    try:
        rows = await pool.fetch(
            """
            SELECT p.overall
            FROM lineups l
            JOIN players p ON p.id = l.player_id
            WHERE l.league_id = $1 AND l.team_id = $2
              AND p.overall IS NOT NULL
            ORDER BY p.overall
            """,
            league_id, team_id,
        )
        ovrs = [r["overall"] for r in rows if r["overall"] is not None]
        if not ovrs:
            return 75.0
        mid = len(ovrs) // 2
        if len(ovrs) % 2 == 0:
            return (ovrs[mid - 1] + ovrs[mid]) / 2.0
        return float(ovrs[mid])
    except Exception as exc:
        log.debug("_fetch_roster_median_ovr failed team=%d: %s", team_id, exc)
        return 75.0


async def _fetch_player_form(pool, league_id: int, season: int, player_id: int, ovr: int) -> tuple[float, dict]:
    """Fetch form modifier + season stats for a single player.

    Delegates to trade_context_builder.compute_form_map (cached per league/season).
    Returns (form_modifier, stats_dict). Safe fallback on any error.
    """
    try:
        from services import trade_context_builder
        form_map = await trade_context_builder.compute_form_map(
            pool,
            player_ids=[player_id],
            ovr_map={player_id: ovr},
            position_map={player_id: ""},
            league_id=league_id,
            season=season,
        )
        return form_map.get(player_id, (1.0, {}))
    except Exception as exc:
        log.debug("_fetch_player_form failed pid=%d: %s", player_id, exc)
        return 1.0, {}


async def _synergy_line(
    pool,
    league_id: int,
    season: int,
    team_id: int,
    incoming_role: str | None,
) -> str | None:
    """Flag obvious synergy patterns: skill overlap with existing core, or complementary fit.

    Queries the team's current top-5 role assignments by touch_share.
    Returns a single plain sentence (no '• ' prefix — caller adds it).
    """
    if not incoming_role:
        return None
    # Roles that are generalist/utility — not worth flagging overlap for
    _SKIP_ROLES = {"developmental", "end_of_bench", "veteran_mentor", "secondary_creator"}
    if incoming_role in _SKIP_ROLES:
        return None

    try:
        existing_roles = await pool.fetch(
            """
            SELECT pr.role, p.first_name || ' ' || p.last_name AS name, pr.touch_share
            FROM player_roles pr
            JOIN players p ON p.id = pr.player_id
            WHERE pr.league_id = $1 AND pr.season = $2 AND pr.team_id = $3
            ORDER BY pr.touch_share DESC
            LIMIT 5
            """,
            league_id, season, team_id,
        )
    except Exception as exc:
        log.debug("_synergy_line query failed: %s", exc)
        return None

    # Same-role overlap → redundancy (skip secondary_creator since it's generic filler)
    same_role = [r for r in existing_roles if r["role"] == incoming_role]
    if same_role:
        top = same_role[0]
        role_label = incoming_role.replace("_", " ")
        return (
            f"creates overlap with {top['name']} ({role_label}) — "
            f"touches will have to split or one of them changes role."
        )

    # Complementary pairings — known good fits
    _COMPLEMENTARY: dict[str, list[str]] = {
        "primary_initiator":  ["catch_and_shoot", "movement_shooter", "rim_runner", "rim_protector", "floor_spacer"],
        "post_anchor":        ["movement_shooter", "wing_stopper", "floor_spacer", "catch_and_shoot"],
        "iso_scorer":         ["rim_protector", "wing_stopper", "catch_and_shoot", "screen_roller"],
        "movement_shooter":   ["post_anchor", "primary_initiator", "rim_runner"],
        "rim_protector":      ["primary_initiator", "wing_stopper", "on_ball_pest"],
        "wing_stopper":       ["iso_scorer", "post_anchor", "rim_protector"],
        "transition_engine":  ["rim_protector", "floor_spacer"],
    }
    for r in existing_roles:
        if incoming_role in _COMPLEMENTARY.get(r["role"], []):
            role_label = incoming_role.replace("_", " ")
            return (
                f"pairs naturally with {r['name']} ({r['role'].replace('_', ' ')}) — "
                f"the {role_label} role gives them the spacing/help they need."
            )

    return None
