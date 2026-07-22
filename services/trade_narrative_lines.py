"""Pure sentence-builder helpers for ride-along trade panel narratives --
no DB access, no async. Called by ra_reasoning.build_team_perspective to
turn plan/posture/player data into coach-voice bullets.

Extracted from ra_reasoning.py (Phase 3 opportunistic split, see
HANDOFF.md) along with trade_reasoning_fetchers.py.
"""
from __future__ import annotations


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
