"""Trade-reasoning generator for ride-along trade panels.

Replaces key-value field dumps with motivation-driven, coach-voice bullets.
Each team gets a 5-9 bullet read-out: a scene-setter, per-player "why we want
them / why we're moving them" lines, pick/cap lines in plain English, and a
closing alignment verdict.

Public API
----------
build_team_perspective(pool, league, season, perspective_team, other_team,
                       players_in, players_out, picks_in, picks_out, *,
                       decision_context=None) -> list[str]

build_perspective_header(pool, league, season, team) -> str

render_trade_panel(pool, league, season, perspectives, *, flow_summary,
                   decision_label, scores_line=None) -> dict
"""
from __future__ import annotations

from typing import Any

from core.logging import get_logger
from data.repositories import league_repo, player_repo, team_repo
from services import franchise_plan_service

log = get_logger(__name__)

_TOTAL_GAMES = 82

# ---------------------------------------------------------------------------
# Internal helpers — posture (delegates to team_intel to avoid duplication)
# ---------------------------------------------------------------------------


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

    Delegates to trade_evaluator.compute_form_map (cached per league/season).
    Returns (form_modifier, stats_dict). Safe fallback on any error.
    """
    try:
        from services import trade_evaluator
        form_map = await trade_evaluator.compute_form_map(
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


# ---------------------------------------------------------------------------
# Narrative sentence builders — scene-setter
# ---------------------------------------------------------------------------

def _window_line(plan: dict, posture: dict) -> str:
    """Bullet 1: frame the team's strategic horizon in coach voice."""
    goal = plan.get("goal", "transition")
    horizon = plan.get("horizon_seasons", 2)
    proj_wins = posture.get("projected_wins")
    conf_rank = posture.get("conf_rank")
    wins = posture.get("wins", 0)
    losses = posture.get("losses", 0)

    rank_str = f"#{conf_rank} in conference" if conf_rank else ""
    if proj_wins is not None:
        pace_str = f"{proj_wins}-win pace"
    elif wins + losses > 0:
        pace_str = f"{wins}-{losses} right now"
    else:
        pace_str = "early in the season"

    targets = plan.get("asset_targets") or []
    target_str = (", ".join(targets[:2]) or "the right pieces") if targets else "the right pieces"

    if goal == "win_now":
        rank_clause = f" ({rank_str})" if rank_str else ""
        if horizon <= 1:
            return (
                f"Window's closing — this is it. {pace_str}{rank_clause}. "
                f"We need {target_str} now, not next year."
            )
        return (
            f"Window's open — {pace_str}{rank_clause}. "
            f"Prime years, no time to waste. Hunting {target_str}."
        )
    if goal == "tank":
        return (
            f"We're in full tank mode — {pace_str}. "
            f"Losses are fine. We want {target_str} coming back."
        )
    if goal == "rebuild":
        return (
            f"Year {'one' if horizon >= 3 else 'two'} of a rebuild — {pace_str}. "
            f"Trading vets, stockpiling {target_str}. Timeline is {horizon} seasons out."
        )
    if goal == "transition":
        rank_clause = f" ({rank_str})" if rank_str else ""
        return (
            f"Threading the needle — {pace_str}{rank_clause}. "
            f"Not fully committed to rebuilding yet. Looking for {target_str}."
        )
    # soft_rebuild or anything else
    return (
        f"Retooling — {pace_str}. Moving aging vets, trying to grab {target_str}."
    )


def _posture_mode_label(mode: str, urgency: str) -> str:
    """Human-readable posture label."""
    mode_labels = {
        "contending": "contending",
        "play_in_fringe": "play-in fringe",
        "soft_rebuild": "soft rebuild",
        "rebuilding": "rebuilding",
        "developing": "developing",
    }
    return f"{mode_labels.get(mode, mode)}/{urgency}"


def _bucket_for_player(player_id: int, plan: dict) -> str:
    """Return 'core', 'flex', 'surplus', or 'unlisted'."""
    if player_id in (plan.get("core_player_ids") or []):
        return "core"
    if player_id in (plan.get("flex_player_ids") or []):
        return "flex"
    if player_id in (plan.get("surplus_player_ids") or []):
        return "surplus"
    return "unlisted"


def _key_tendency_label(player: dict) -> tuple[str, int]:
    """Return (label, value) for the player's strongest tendency signal."""
    candidates = [
        ("pass tendency", player.get("tendency_pass", 50) or 50),
        ("3-point tendency", player.get("tendency_3pt", 50) or 50),
        ("drive tendency", player.get("tendency_drive", 50) or 50),
        ("block tendency", player.get("blk_tendency", 50) or 50),
        ("rebound tendency", player.get("reb_tendency", 50) or 50),
    ]
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0]


# ---------------------------------------------------------------------------
# Context helpers — nuance bullets (max one per player on top of primary)
# ---------------------------------------------------------------------------

def _overperforming_line_incoming(name: str, form_mod: float, ovr: int) -> str | None:
    """Flag when a player is running above his OVR — buy before the market catches on."""
    if form_mod >= 1.10:
        return (
            f"{name}'s playing above his {ovr} OVR right now (form {form_mod:.2f}) — "
            f"buy on the trend before the market catches up."
        )
    return None


def _overperforming_line_outgoing(name: str, form_mod: float, ovr: int) -> str | None:
    """Flag sell-high window when outgoing player is running hot."""
    if form_mod >= 1.10:
        return (
            f"{name}'s playing above his OVR (form {form_mod:.2f}) — "
            f"sell-high window is now."
        )
    return None


# Role → minimum expected tendencies for a clean fit.
# Values are the threshold a player should clear; 10 pts below = mismatch, 10 above = strong match.
_ROLE_EXPECTED_TENDENCIES: dict[str, dict[str, int]] = {
    "primary_initiator":  {"tendency_pass": 50, "ast_tendency": 55},
    "iso_scorer":         {"tendency_drive": 45, "usage_weight": 70},
    "post_anchor":        {"reb_tendency": 50},
    "movement_shooter":   {"tendency_3pt": 50},
    "slashing_lead":      {"tendency_drive": 55},
    "catch_and_shoot":    {"tendency_3pt": 55},
    "rim_protector":      {"blk_tendency": 45},
    "wing_stopper":       {"defense_tendency": 55},
    "on_ball_pest":       {"stl_tendency": 45, "defense_tendency": 50},
    "two_way_wing":       {"defense_tendency": 45, "tendency_drive": 40},
    "two_way_big":        {"blk_tendency": 40, "reb_tendency": 45},
    "wing_creator":       {"tendency_drive": 45, "tendency_3pt": 40},
    "spark_plug_scorer":  {"usage_weight": 60, "tendency_drive": 40},
    "floor_spacer":       {"tendency_3pt": 55},
    "screen_roller":      {"reb_tendency": 45},
    "rim_runner":         {"reb_tendency": 50},
    "pick_and_pop":       {"tendency_3pt": 45},
    "transition_engine":  {"tendency_drive": 50},
}


def _role_fit_line(player: dict, current_role: str) -> str | None:
    """Surface a mismatch or strong match between a player's tendencies and their role.

    Only fires when the signal is unambiguous — all misses (mismatch) or all matches (fit).
    Keeps the bullet factual rather than speculative.
    """
    if not current_role or current_role not in _ROLE_EXPECTED_TENDENCIES:
        return None
    expected = _ROLE_EXPECTED_TENDENCIES[current_role]
    misses = [
        tend for tend, threshold in expected.items()
        if (player.get(tend) or 0) < threshold - 10
    ]
    matches = [
        tend for tend, threshold in expected.items()
        if (player.get(tend) or 0) >= threshold + 10
    ]
    if misses and not matches:
        role_label = current_role.replace("_", " ")
        return (
            f"miscast as {role_label} on his old team — "
            f"his profile doesn't match the role's demands."
        )
    if matches and not misses:
        role_label = current_role.replace("_", " ")
        return f"his tendencies are a clean fit for the {role_label} role."
    return None


def _defense_quality_line(player: dict) -> str | None:
    """Add a defensive-impact note when stats put the player at elite or floor level for their archetype."""
    archetype = player.get("defensive_archetype")
    if not archetype or archetype in ("non_defender", "generalist", None):
        return None
    blk = player.get("blk_tendency") or 0
    stl = player.get("stl_tendency") or 0
    defense = player.get("defense_tendency") or 0

    # Elite signals (DPOY-tier output for the archetype)
    if archetype == "rim_protector" and blk >= 70:
        return "elite shot-blocker — top-tier rim protection at any tempo."
    if archetype in ("wing_stopper", "on_ball_pest") and (stl >= 65 or defense >= 70):
        return "one of the best perimeter defenders at his position."
    if archetype == "two_way_big" and blk >= 60 and defense >= 60:
        return "genuinely two-way — anchors the paint AND can guard in space."
    if archetype == "size_defender" and (blk >= 60 or defense >= 65):
        return "real shot-altering presence — changes how opponents attack the paint."

    # Floor-of-archetype: tagged as defender but the numbers don't back it up
    if archetype == "rim_protector" and blk < 40:
        return "tagged as a rim_protector but the actual block rate doesn't back it up."
    if archetype == "wing_stopper" and defense < 40 and stl < 40:
        return "listed as a wing stopper but the defensive numbers don't support the billing."

    return None


def _window_fit_line_incoming(player: dict, plan: dict) -> str | None:
    """Check whether the player's prime aligns with the team's contention window."""
    age = player.get("age") or 28
    horizon = plan.get("horizon_seasons", 2)
    goal = plan.get("goal", "")
    age_at_horizon_end = age + horizon

    if goal in ("rebuild", "tank"):
        if age_at_horizon_end <= 28:
            return (
                f"will be {age_at_horizon_end} when our window opens — "
                f"prime years align with contention."
            )
        if age_at_horizon_end >= 33:
            return (
                f"will be {age_at_horizon_end} when our window opens — "
                f"past his prime by then; doesn't fit the timeline."
            )

    if goal == "win_now":
        if age <= 28:
            return "in prime now, several seasons of contention left with him on the roster."
        if age >= 33:
            return (
                f"at {age} he's a 1-2 season rental — "
                f"works for the immediate window but not beyond."
            )

    return None


def _window_fit_line_outgoing(player: dict, plan: dict) -> str | None:
    """Surface timeline rationale for moving a player out."""
    age = player.get("age") or 28
    goal = plan.get("goal", "")
    if goal in ("rebuild", "tank") and age >= 31:
        return (
            f"at {age}, he doesn't see our window — "
            f"converting him to assets makes sense."
        )
    if goal == "win_now" and age <= 24:
        return "young enough to be a future asset but we need wins now — trading future for present."
    return None


def _scheme_implication_line(current_role: str | None, philosophy: str | None) -> str | None:
    """Hint at the coaching adjustment when this player joins, based on role + philosophy."""
    if not current_role or not philosophy:
        return None
    if current_role == "primary_initiator" and philosophy == "egalitarian":
        return (
            "slots into our egalitarian system as a connector — "
            "touches distribute through him, not just to him."
        )
    if current_role == "iso_scorer" and philosophy == "star_maxer":
        return "feeds our star-maxer scheme — we'll run more isolation sets to free him up."
    if current_role == "rim_protector" and philosophy == "defense_first":
        return (
            "unlocks switching/aggressive scheme — "
            "having him behind us lets perimeter guys press."
        )
    if current_role == "movement_shooter":
        return "forces opposing defenses to chase off-ball; opens driving lanes for our handlers."
    if current_role in ("two_way_wing", "wing_stopper") and philosophy == "defense_first":
        return "gives our defense another switchable piece — exactly what we scheme around."
    if current_role == "post_anchor" and philosophy == "tendency_respecter":
        return "fits our tendency-based sets — post catches will open cutters and shooters."
    return None


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


def _pick_context_bullet(player: dict, context_candidates: list[str | None]) -> str | None:
    """Select the highest-priority non-None context candidate from the ordered list."""
    for candidate in context_candidates:
        if candidate is not None:
            return candidate
    return None


# ---------------------------------------------------------------------------
# Motivation helpers — incoming players
# ---------------------------------------------------------------------------

def _motivation_incoming(
    player: dict,
    form_mod: float,
    stats: dict,
    plan: dict,
    posture: dict,
    depth: int,
    top_at_pos: dict | None,
    current_team_code: str,
    current_role: str,
    roster_median_ovr: float = 75.0,
) -> str:
    """Return a coach-voice motivation sentence for an incoming player.

    Tries each motivation pattern in priority order; returns the first match.
    Falls back to a neutral rotation line if none fire.
    """
    name = f"{player.get('first_name', '')} {player.get('last_name', '')}".strip()
    pos = player.get("position", "?")
    ovr = player.get("overall", 0)
    age = player.get("age") or 27
    goal = plan.get("goal", "transition")
    asset_targets = plan.get("asset_targets") or []

    ppg = stats.get("ppg", 0.0)
    rpg = stats.get("rpg", 0.0)
    apg = stats.get("apg", 0.0)

    # ── 1. Leading-scorer / lead-role upgrade ──────────────────────────────
    # Fire when incoming OVR >= 82 AND (no incumbent at position OR incumbent OVR lower).
    if ovr >= 82:
        if top_at_pos is None:
            stat_str = f"{ppg:.0f} PPG" if ppg >= 8 else f"OVR {ovr}"
            return (
                f"{name} slides right in as our primary {pos} — "
                f"no one there right now, and {stat_str} is exactly what we've been missing."
            )
        inc_ovr = top_at_pos.get("overall", 0) or 0
        inc_name = top_at_pos.get("name", "our current guy")
        if ovr > inc_ovr + 3:
            stat_str = f"{ppg:.0f}/{rpg:.0f}/{apg:.0f}" if ppg >= 5 else f"OVR {ovr}"
            return (
                f"{name} is an upgrade over {inc_name} at {pos} — "
                f"{stat_str} sim, that's a better option for us."
            )

    # ── 2. Roster scarcity / fills explicit gap ────────────────────────────
    if depth <= 1:
        return (
            f"We've been thin at {pos} all year — "
            f"even at OVR {ovr}, {name} gives us real depth we don't have."
        )

    # ── 3. Long-term timeline fit (young + rebuild/tank) ──────────────────
    if goal in ("rebuild", "tank") and age <= 25:
        tend_label, tend_val = _key_tendency_label(player)
        if tend_val >= 60:
            return (
                f"At {age} with a high {tend_label} ({tend_val}), "
                f"{name} is exactly the kind of bet our rebuild needs — he'll grow into our timeline."
            )
        return (
            f"{name}'s {age} years old — on our rebuild, his upside fits where we're headed."
        )

    # ── 4. Plan-fit upgrade (matches asset_targets) ───────────────────────
    if pos in ("SF", "SG") and any(t in asset_targets for t in ("role_players", "veterans")):
        def_arch = player.get("defensive_archetype") or ""
        if def_arch and player.get("tendency_3pt", 50) >= 55:
            return (
                f"{name}'s a 3-and-D wing — that's exactly what our "
                f"'{'role_players' if 'role_players' in asset_targets else 'veterans'}' target list calls for."
            )
    if "young_u23" in asset_targets and age <= 23:
        return (
            f"Young asset — {name}'s {age}, fits our 'young_u23' target profile. "
            f"OVR {ovr} and room to grow."
        )

    # ── 5. Buy-low / underperforming (market undervaluing) ────────────────
    if form_mod < 0.92 and ovr >= 78:
        return (
            f"Coming off a down stretch (form {form_mod:.2f}), the market's undervaluing {name} — "
            f"we think he bounces back."
        )

    # ── 6. Fit with system (tendency alignment) ───────────────────────────
    tend_label, tend_val = _key_tendency_label(player)
    if tend_label == "pass tendency" and tend_val >= 60:
        return (
            f"His high pass tendency ({tend_val}) fits our system — "
            f"he was wasted as an iso option on {current_team_code}."
        )
    if tend_label in ("3-point tendency", "drive tendency") and tend_val >= 62 and goal == "win_now":
        return (
            f"High {tend_label} ({tend_val}) — that offensive punch is what we need "
            f"to push for a championship."
        )

    # ── 7. Fills rotation depth (fallback) ────────────────────────────────
    if current_role in ("developmental", "bench_reserve"):
        return (
            f"{name} was buried in a limited role on {current_team_code} — "
            f"OVR {ovr}, he gets a real shot here."
        )

    # Honest framing when the player is at or below roster median and
    # not producing at a meaningful level — "fits the scheme" is misleading
    # when the player genuinely won't move the needle.
    # Production tier gates: a 5 PPG / OVR 76 player is "depth", not an upgrade.
    # We use ppg < 8 as the meaningful-production cutoff for upgrade framing;
    # the stat_note shown in the fallback sentence still uses ppg >= 5 so it
    # appears when production is at least visible (e.g. 7 PPG showing).
    _production_tier = (
        "star" if ppg >= 20 or ovr >= 88
        else "producer" if ppg >= 12 or ovr >= 82
        else "role" if ppg >= 8 or ovr >= 78
        else "depth"
    )
    _is_non_upgrade = (
        ovr <= roster_median_ovr
        and _production_tier in ("role", "depth")
        and ppg < 8  # below meaningful production threshold — not a needle-mover
    )
    if _is_non_upgrade:
        return (
            f"{name} is a modest depth add at {pos} (OVR {ovr}) — "
            f"won't move the needle but fills a roster spot."
        )

    stat_note = f" ({ppg:.0f} PPG this year)" if ppg >= 5 else ""
    return (
        f"{name} rounds out our rotation at {pos}{stat_note}. "
        f"Solid OVR {ovr}, fits the scheme."
    )


# ---------------------------------------------------------------------------
# Motivation helpers — outgoing players
# ---------------------------------------------------------------------------

def _motivation_outgoing(
    player: dict,
    form_mod: float,
    plan: dict,
    posture: dict,
    bucket: str,
    pos_depth: int,
) -> str | None:
    """Return a coach-voice motivation sentence for an outgoing player.

    Returns None if no motivation fires clearly (caller skips the bullet).
    """
    name = f"{player.get('first_name', '')} {player.get('last_name', '')}".strip()
    pos = player.get("position", "?")
    ovr = player.get("overall", 0)
    age = player.get("age") or 27
    goal = plan.get("goal", "transition")

    # ── 1. Form / underperformance — move before market catches on ─────────
    if form_mod < 0.92:
        return (
            f"{name}'s been underperforming his OVR lately (form {form_mod:.2f}) — "
            f"moving him before the market catches up."
        )

    # ── 2. Age-vs-timeline mismatch ────────────────────────────────────────
    if age >= 31 and goal in ("rebuild", "tank"):
        return (
            f"He's {age} — on a {plan.get('horizon_seasons', 3)}-year rebuild, "
            f"his prime years are wasted here."
        )

    # ── 3. Asset extraction (older vet with trade value) ──────────────────
    if age >= 29 and goal in ("rebuild", "tank") and ovr >= 78:
        return (
            f"Cashing in {name}'s trade value while we can — "
            f"at {age}, his stock only goes down from here."
        )

    # ── 4. Plan-goal mismatch ──────────────────────────────────────────────
    if goal == "win_now" and player.get("usage_weight", 0.5) < 0.35:
        return (
            f"We're chasing a title — {name}'s off-ball profile doesn't get us there."
        )
    if goal in ("rebuild", "tank") and ovr >= 82 and age >= 28:
        return (
            f"We're not trying to win right now — "
            f"{name}'s OVR {ovr} is too good to sit on this roster while we tank."
        )

    # ── 5. Role redundancy (position saturated) ───────────────────────────
    if pos_depth >= 4:
        return (
            f"We've got {pos_depth} {pos}s on the roster — one too many. "
            f"{name} goes so someone else gets real minutes."
        )

    # ── 6. Surplus on plan ────────────────────────────────────────────────
    if bucket == "surplus":
        return (
            f"{name} was bucketed surplus on our plan — clean exit for a vet we didn't need anymore."
        )

    # ── 7. Flex piece being moved for assets ─────────────────────────────
    if bucket == "flex":
        if goal in ("rebuild", "tank"):
            return (
                "Flex piece going out for assets — fits our retool perfectly."
            )
        return (
            f"{name}'s a flex piece; we're moving him for the right return."
        )

    # Core or unlisted — skip bullet rather than saying nothing meaningful
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def build_perspective_header(
    pool,
    league: league_repo.League,
    season: int,
    team: team_repo.Team,
) -> str:
    """One-line meta header: coach | plan | posture."""
    try:
        coach = team.cpu_mode or "unknown"
        # Fetch actual coach_philosophy from teams table
        phil = await pool.fetchval(
            "SELECT coach_philosophy FROM teams WHERE id = $1", team.id
        )
        if phil:
            coach = phil

        plan = await franchise_plan_service.get_or_derive(pool, league.id, team.id, season)
        posture = await _compute_posture(pool, league, team.id)

        plan_label = f"{plan.get('goal', '?')} h:{plan.get('horizon_seasons', '?')}"
        posture_label = _posture_mode_label(posture.get("mode", "developing"), posture.get("urgency", "comfortable"))

        return f"coach: {coach}  |  plan: {plan_label}  |  posture: {posture_label}"
    except Exception as exc:
        log.debug("build_perspective_header failed for team %d: %s", team.id, exc)
        return f"coach: {team.cpu_mode or '?'}  |  plan: ?  |  posture: ?"


async def build_team_perspective(
    pool,
    league: league_repo.League,
    season: int,
    perspective_team: team_repo.Team,
    other_team: team_repo.Team,
    players_in: list[int],
    players_out: list[int],
    picks_in: list[dict],
    picks_out: list[dict],
    *,
    decision_context: dict | None = None,
    context_signals_map: dict[int, list[dict]] | None = None,
) -> list[str]:
    """
    Build a narrative read of this trade from perspective_team's POV.

    Returns a list of bullet-point strings, each starting with "• ".
    Target: 5-9 bullets.  Skips lines when data is absent or irrelevant.
    """
    bullets: list[str] = []

    try:
        plan = await franchise_plan_service.get_or_derive(pool, league.id, perspective_team.id, season)
    except Exception as exc:
        log.debug("plan fetch failed for %d: %s", perspective_team.id, exc)
        plan = {}

    try:
        posture = await _compute_posture(pool, league, perspective_team.id)
    except Exception as exc:
        log.debug("posture fetch failed for %d: %s", perspective_team.id, exc)
        posture = {"mode": "developing", "urgency": "comfortable"}

    # ── Bullet 1: Scene-setter / window line ───────────────────────────────
    try:
        bullets.append("• " + _window_line(plan, posture))
    except Exception as exc:
        log.debug("window_line failed: %s", exc)

    # Fetch the receiving team's coach_philosophy once — used for scheme bullets
    try:
        _team_philosophy: str | None = await pool.fetchval(
            "SELECT coach_philosophy FROM teams WHERE id = $1", perspective_team.id
        )
    except Exception:
        _team_philosophy = None

    # Roster median OVR for the receiving team — used to gate honest "depth add"
    # framing vs "fits the scheme" for incoming players who are not upgrades.
    _roster_median_ovr: float = await _fetch_roster_median_ovr(
        pool, league.id, perspective_team.id
    )

    # ── Bullets: Incoming player motivation lines ──────────────────────────
    # Each player: 1 primary bullet + at most 1 context bullet.
    # Context priority: role-mismatch > scheme > synergy > defense-elite > window-fit > role-fit-positive > overperformance
    for pid in players_in:
        try:
            p = await _fetch_player_full(pool, league.id, pid)
            if not p:
                continue
            pos = p.get("position", "?")
            ovr = p.get("overall", 75)
            name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()

            # Form data (uses cached compute_form_map)
            form_mod, stats = await _fetch_player_form(
                pool, league.id, season, pid, ovr
            )

            # Position depth on the receiving team (pre-trade snapshot)
            depth = await _fetch_position_depth(
                pool, league.id, season, perspective_team.id, pos
            )

            # Who's currently atop that position on this team
            top_at_pos = await _fetch_top_role_at_position(
                pool, league.id, season, perspective_team.id, pos
            )

            # Current role / team context for the player
            current_role_info = await _fetch_player_role_on_team(pool, league.id, season, pid)
            current_role = current_role_info.get("role", "unknown")
            current_team_code = current_role_info.get("nba_team_code") or ""
            # Fallback: player has no player_roles row yet (fresh trade target,
            # pre-derive). Look up team code directly from player.team_id.
            if not current_team_code and p.get("team_id"):
                _tc = await pool.fetchval(
                    "SELECT nba_team_code FROM teams WHERE id = $1",
                    p["team_id"],
                )
                current_team_code = _tc or "their old team"
            if not current_team_code:
                current_team_code = "their old team"

            line = _motivation_incoming(
                player=p,
                form_mod=form_mod,
                stats=stats,
                plan=plan,
                posture=posture,
                depth=depth,
                top_at_pos=top_at_pos,
                current_team_code=current_team_code,
                current_role=current_role,
                roster_median_ovr=_roster_median_ovr,
            )
            bullets.append(f"• {line}")

            # ── Context bullet (one per player, highest-priority wins) ──────
            # When context_signals_map is present (pre-computed by cpu_should_accept),
            # read the reason text directly from the signals that drove the math.
            # This guarantees narrative ↔ value-math alignment.
            # Fall back to local detector calls when signals map is absent.
            try:
                _ctx_signals: list[dict] = (context_signals_map or {}).get(pid) or []
                if _ctx_signals:
                    # Render the highest-delta signal as the context bullet.
                    # Priority order: negative signals first (actionable), then positive.
                    _negative = [s for s in _ctx_signals if s.get("delta", 0) < 0]
                    _positive = [s for s in _ctx_signals if s.get("delta", 0) >= 0]
                    # Sort negatives by most negative delta; positives by highest delta.
                    _negative.sort(key=lambda s: s.get("delta", 0))
                    _positive.sort(key=lambda s: s.get("delta", 0), reverse=True)
                    _ordered = _negative + _positive
                    if _ordered:
                        _top_sig = _ordered[0]
                        _sig_reason = _top_sig.get("reason", "")
                        _sig_code = _top_sig.get("code", "")
                        _sig_delta = _top_sig.get("delta", 0.0)
                        _delta_tag = f" [Δ{_sig_delta:+.2f}]"
                        bullets.append(f"  -> {name}: {_sig_reason}{_delta_tag}")
                else:
                    # Fallback: re-detect using local helpers (no pool-computed signals).
                    _role_fit = _role_fit_line(p, current_role)
                    _role_mismatch: str | None = None
                    _role_match: str | None = None
                    if _role_fit:
                        if "doesn't match" in _role_fit:
                            _role_mismatch = f"{name} {_role_fit}"
                        else:
                            _role_match = f"{name}: {_role_fit}"

                    _scheme = _scheme_implication_line(current_role, _team_philosophy)
                    _synergy = await _synergy_line(
                        pool, league.id, season, perspective_team.id, current_role
                    )
                    _def_quality = _defense_quality_line(p)
                    _def_elite: str | None = None
                    if _def_quality and ("elite" in _def_quality or "best perimeter" in _def_quality or "genuinely two-way" in _def_quality):
                        _def_elite = f"{name}: {_def_quality}"
                    _win_fit = _window_fit_line_incoming(p, plan)
                    _overperf = _overperforming_line_incoming(name, form_mod, ovr)

                    ctx = _pick_context_bullet(p, [
                        _role_mismatch,      # 1. role-mismatch (negative, actionable)
                        _scheme,             # 2. scheme implication
                        _synergy,            # 3. roster synergy / overlap
                        _def_elite,          # 4. elite defense signal
                        _win_fit,            # 5. window-fit / timeline
                        _role_match,         # 6. role-fit positive
                        _overperf,           # 7. overperformance (interesting but lowest)
                    ])
                    if ctx:
                        bullets.append(f"  -> {ctx}")
            except Exception as exc:
                log.debug("incoming_context failed pid=%d: %s", pid, exc)

        except Exception as exc:
            log.debug("incoming_motivation failed pid=%d: %s", pid, exc)

    # ── Bullets: Outgoing player motivation lines ─────────────────────────
    # Each player: 1 primary bullet + at most 1 context bullet (overperf or window-out).
    for pid in players_out:
        try:
            p = await _fetch_player_full(pool, league.id, pid)
            if not p:
                continue
            ovr = p.get("overall", 75)
            pos = p.get("position", "?")
            name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            bucket = _bucket_for_player(pid, plan)

            form_mod, _stats = await _fetch_player_form(
                pool, league.id, season, pid, ovr
            )
            # Depth on THIS team (pre-trade)
            pos_depth = await _fetch_position_depth(
                pool, league.id, season, perspective_team.id, pos
            )

            line = _motivation_outgoing(
                player=p,
                form_mod=form_mod,
                plan=plan,
                posture=posture,
                bucket=bucket,
                pos_depth=pos_depth,
            )
            if line:
                bullets.append(f"• {line}")

            # ── Context bullet for outgoing (overperf sell-high > window-out) ─
            try:
                _overperf_out = _overperforming_line_outgoing(name, form_mod, ovr)
                _win_out = _window_fit_line_outgoing(p, plan)
                ctx_out = _pick_context_bullet(p, [_overperf_out, _win_out])
                if ctx_out and line:  # only add context if primary line fired
                    bullets.append(f"  ->{ctx_out}")
            except Exception as exc:
                log.debug("outgoing_context failed pid=%d: %s", pid, exc)

        except Exception as exc:
            log.debug("outgoing_motivation failed pid=%d: %s", pid, exc)

    # ── Bullets: Pick-in lines ─────────────────────────────────────────────
    for pick in picks_in:
        try:
            pick_season = pick.get("season", season + 1)
            pick_round = pick.get("round", 1)
            orig_team_id = pick.get("original_team_id") or pick.get("current_team_id")

            # Resolve original team code
            orig_code = await pool.fetchval(
                "SELECT nba_team_code FROM teams WHERE id = $1", orig_team_id
            ) if orig_team_id else None
            orig_str = f" (via {orig_code})" if orig_code and orig_code != perspective_team.nba_team_code else ""

            slot, verdict, proj_w, proj_l = await _project_pick_slot(
                pool, league.id, pick, season
            )

            if pick_round != 1:
                bullets.append(
                    f"• Getting a 2nd-rounder{orig_str} projected around #{slot} — "
                    f"basically a developmental flier, but we'll take it."
                )
            elif slot <= 14:
                record_note = f" ({proj_w}-{proj_l} pace)" if proj_w is not None else ""
                bullets.append(
                    f"• {pick_season} first-rounder{orig_str} projects #{slot}{record_note} — "
                    f"that's real lottery value, meaningful pickup."
                )
            else:
                record_note = f" ({proj_w}-{proj_l} pace)" if proj_w is not None else ""
                bullets.append(
                    f"• {pick_season} first-rounder{orig_str} projects #{slot}{record_note} — "
                    f"late first, but a pick's a pick."
                )
        except Exception as exc:
            log.debug("pick_in_line failed: %s", exc)

    # ── Bullets: Pick-out lines ────────────────────────────────────────────
    for pick in picks_out:
        try:
            pick_season = pick.get("season", season + 1)
            pick_round = pick.get("round", 1)

            slot, verdict, proj_w, proj_l = await _project_pick_slot(
                pool, league.id, pick, season
            )

            if pick_round != 1:
                bullets.append(
                    f"• Losing a 2nd-rounder projected around #{slot} — "
                    f"basically a flier we won't miss."
                )
            elif slot >= 20:
                bullets.append(
                    f"• Giving up our {pick_season} first projected #{slot} — "
                    f"late pick, cost is modest."
                )
            else:
                bullets.append(
                    f"• Giving up our {pick_season} first projected #{slot} — "
                    f"that's real asset cost, has to be worth it."
                )
        except Exception as exc:
            log.debug("pick_out_line failed: %s", exc)

    # ── Cap math line ──────────────────────────────────────────────────────
    try:
        cap = league.salary_cap
        current_usage = await player_repo.get_team_cap_usage(pool, league.id, perspective_team.id)

        sal_in = 0
        sal_in_yrs: list[int] = []
        for pid in players_in:
            c = await player_repo.get_active_contract(pool, pid)
            if c:
                sal_in += c.salary
                sal_in_yrs.append(c.years_remaining)

        sal_out = 0
        for pid in players_out:
            c = await player_repo.get_active_contract(pool, pid)
            if c:
                sal_out += c.salary

        delta = sal_in - sal_out

        if not (delta == 0 and not players_in and not players_out):
            delta_m = round(abs(delta) / 1_000_000, 1)
            avg_yrs = round(sum(sal_in_yrs) / len(sal_in_yrs), 1) if sal_in_yrs else 0

            new_usage_m = round((current_usage + delta) / 1_000_000, 1)
            cap_m = round(cap / 1_000_000, 1)

            if new_usage_m > cap_m:
                if delta > 0:
                    yrs_note = f"{avg_yrs:.0f}y on the contract" if avg_yrs > 0 else "contract"
                    bullets.append(
                        f"• Cap-wise this is tight — adds ${delta_m}M ({yrs_note}), "
                        f"puts us at ${new_usage_m}M against a ${cap_m}M cap. Over the line."
                    )
                else:
                    bullets.append(
                        f"• Cap situation: we're still over after this at ${new_usage_m}M — "
                        f"saves ${delta_m}M but doesn't fix the crunch."
                    )
            elif new_usage_m > cap_m * 0.95:
                direction = f"adds ${delta_m}M" if delta > 0 else f"saves ${delta_m}M"
                bullets.append(
                    f"• Cap-wise — {direction}, leaves us at ${new_usage_m}M. Tight but workable."
                )
            elif delta < 0:
                bullets.append(
                    f"• Cap-wise this opens ${round(-delta / 1_000_000, 1)}M in space — "
                    f"drops us to ${new_usage_m}M, gives us breathing room."
                )
            else:
                yrs_note = f", {avg_yrs:.0f}y on the deal" if avg_yrs > 0 else ""
                bullets.append(
                    f"• Cap-wise we're comfortable — ${delta_m}M added{yrs_note}, "
                    f"plenty of room at ${new_usage_m}M / ${cap_m}M cap."
                )
    except Exception as exc:
        log.debug("cap_math_line failed: %s", exc)

    # ── Closing alignment line ─────────────────────────────────────────────
    # Value-ratio override: when the caller supplies team_ratio (value_received /
    # value_given from this team's POV), decisive math overrides plan_alignment
    # so the Net verdict reflects reality rather than just plan bucket logic.
    # Thresholds: ratio < 0.92 → step back; ratio > 1.08 → advances.
    # Neutral band (0.92–1.08) falls through to the plan_alignment verdict below.
    try:
        _team_ratio: float | None = (decision_context or {}).get("team_ratio")
        _ratio_net_fired = False
        if _team_ratio is not None:
            if _team_ratio < 0.92:
                bullets.append(
                    f"• Net: step back — we'd give up real value here (ratio {_team_ratio:.2f})."
                )
                _ratio_net_fired = True
            elif _team_ratio > 1.08:
                bullets.append(
                    f"• Net: advances — we get more than we send (ratio {_team_ratio:.2f})."
                )
                _ratio_net_fired = True

        if not _ratio_net_fired:
            goal = plan.get("goal", "transition")
            core_ids = set(plan.get("core_player_ids") or [])
            surplus_ids = set(plan.get("surplus_player_ids") or [])
            flex_ids = set(plan.get("flex_player_ids") or [])

            core_lost = sum(1 for pid in players_out if pid in core_ids)
            surplus_cleared = sum(1 for pid in players_out if pid in surplus_ids)
            flex_lost = sum(1 for pid in players_out if pid in flex_ids)

            picks_gained = len(picks_in)

            if core_lost > 0:
                bullets.append(
                    "• Net: this is a step back — we're giving up core piece(s). "
                    "Hard pass unless we're missing something."
                )
            elif goal in ("tank", "rebuild") and surplus_cleared > 0 and (picks_gained > 0 or players_in):
                bullets.append(
                    "• Net: this is a plan-advancing move — sheds surplus, gets younger, "
                    "adds the assets our rebuild needs."
                )
            elif goal == "win_now" and not core_lost and not flex_lost and players_in:
                bullets.append(
                    "• Net: adds a piece without touching the core window. We like this move."
                )
            elif surplus_cleared > 0:
                bullets.append(
                    "• Net: clears a guy we didn't need anymore for a player who fits. Move on."
                )
            elif flex_lost and not core_lost and goal not in ("win_now",):
                bullets.append(
                    "• Net: lateral move. Acceptable, not exciting — "
                    "flex for return at the right value."
                )
            else:
                bullets.append(
                    "• Net: fits the plan without moving the needle much. "
                    "Acceptable if the price is right."
                )
    except Exception as exc:
        log.debug("plan_alignment_line failed: %s", exc)

    # ── CPU model reason (if provided) ────────────────────────────────────
    if decision_context:
        cpu_reason = decision_context.get("cpu_reason")
        decision_label = decision_context.get("decision_label")
        if cpu_reason and decision_label:
            bullets.append(f"• Model says {decision_label}: {cpu_reason}")

    # Clamp to 9 bullets max.
    # Drop context (↳) lines from the back first — they're supplementary.
    # Structural bullets (scene-setter, cap, alignment) are preserved as long as possible.
    if len(bullets) > 9:
        # Pass 1: collect everything, mark context lines
        ctx_indices = [i for i, b in enumerate(bullets) if b.startswith("  -> ")]
        # Remove context lines from the back until we're at 9
        removed = set()
        for idx in reversed(ctx_indices):
            if len(bullets) - len(removed) <= 9:
                break
            removed.add(idx)
        bullets = [b for i, b in enumerate(bullets) if i not in removed]
        # Hard cap if still over (very unusual — many players in the trade)
        if len(bullets) > 9:
            bullets = bullets[:9]

    return bullets


async def pick_ids_to_dicts(pool, pick_ids: list[int]) -> list[dict]:
    """Resolve a list of pick IDs to full pick dicts (season, round, original_team_id, etc.)."""
    if not pick_ids:
        return []
    rows = await pool.fetch(
        "SELECT * FROM draft_picks WHERE id = ANY($1::int[])",
        pick_ids,
    )
    by_id = {r["id"]: dict(r) for r in rows}
    return [by_id[pid] for pid in pick_ids if pid in by_id]


async def render_trade_panel(
    pool,
    league: league_repo.League,
    season: int,
    perspectives: list[tuple[str, Any, dict]],
    *,
    flow_summary: str,
    decision_label: str,
    scores_line: str | None = None,
) -> dict:
    """
    Top-level orchestrator for trade panels.

    perspectives: list of (label, team, swap_dict) where swap_dict has:
      players_in, players_out, picks_in, picks_out, cpu_reason (optional),
      context_signals_per_player (optional): dict[player_id → list[signal_dict]]
      team_ratio_for_perspective (optional): float — value_received / value_given
          from THIS team's POV.  When provided, overrides the plan_alignment Net
          verdict when value math is decisive (ratio < 0.92 or > 1.08).

    Returns a dict suitable for ride_along.prompt_decision's `details` param.
    The dict survives json.dumps (no sets, no dataclasses).
    """
    out: dict[str, Any] = {"flow": flow_summary}

    for label, team, swap in perspectives:
        players_in: list[int] = swap.get("players_in") or []
        players_out: list[int] = swap.get("players_out") or []
        picks_in: list[dict] = swap.get("picks_in") or []
        picks_out: list[dict] = swap.get("picks_out") or []
        cpu_reason: str | None = swap.get("cpu_reason")
        # Context signals pre-computed by cpu_should_accept — keyed by player_id (int).
        # Values are lists of dicts with keys: delta, reason, code.
        context_signals_map: dict[int, list[dict]] = swap.get("context_signals_per_player") or {}
        # Per-perspective team value ratio: value_received / value_given for this team.
        # None when the caller didn't supply score data (e.g. pure propose panels).
        team_ratio_for_perspective: float | None = swap.get("team_ratio_for_perspective")

        # Determine other team: look for first team in perspectives with a different id.
        other_team = None
        for other_label, other_t, _ in perspectives:
            if other_t.id != team.id:
                other_team = other_t
                break
        if other_team is None:
            other_team = team  # degenerate fallback

        decision_context: dict | None = None
        if cpu_reason or team_ratio_for_perspective is not None:
            decision_context = {
                "cpu_reason": cpu_reason,
                "decision_label": decision_label,
                "team_ratio": team_ratio_for_perspective,
            }

        try:
            header = await build_perspective_header(pool, league, season, team)
            bullets = await build_team_perspective(
                pool, league, season,
                perspective_team=team,
                other_team=other_team,
                players_in=players_in,
                players_out=players_out,
                picks_in=picks_in,
                picks_out=picks_out,
                decision_context=decision_context,
                context_signals_map=context_signals_map,
            )
        except Exception as exc:
            log.warning("render_trade_panel perspective %s failed: %s", label, exc)
            header = "coach: ?  |  plan: ?  |  posture: ?"
            bullets = [f"• (perspective unavailable: {exc})"]

        out[label] = {
            "_header": header,
            "_bullets": bullets,
        }

        # Include context signals in the JSONL output under the perspective key
        # so session logs show exactly which signals fired and their deltas.
        if context_signals_map:
            out[label]["context_signals"] = {
                str(pid): signals
                for pid, signals in context_signals_map.items()
            }

    if scores_line:
        out["score"] = scores_line

    return out
