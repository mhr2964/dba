"""Embed builders for /team posture, /team plan, /team philosophy, and league-wide
views (/league philosophy-map, /stats power-rankings extended, /trade league).

All builders read from plain dicts — no DB access.
"""
from __future__ import annotations

import datetime
from collections import defaultdict

import discord
from services.team_intel import PHILOSOPHY_DESCRIPTIONS

# ---------------------------------------------------------------------------
# Mode / urgency display mappings
# ---------------------------------------------------------------------------

_MODE_LABELS: dict[str, tuple[str, discord.Color]] = {
    "contending":     ("Contending",       discord.Color.green()),
    "play_in_fringe": ("Play-In Fringe",   discord.Color.gold()),
    "soft_rebuild":   ("Soft Rebuild",     discord.Color.orange()),
    "rebuilding":     ("Tanking / Developing", discord.Color.red()),
}

_URGENCY_LABELS: dict[str, str] = {
    "comfortable": "Comfortable",
    "pushing":     "Pushing",
    "desperate":   "Desperate",
    "tanking":     "Tanking",
}

_GOAL_LABELS: dict[str, str] = {
    "win_now":    "Win Now",
    "transition": "Transition",
    "rebuild":    "Rebuild",
    "tank":       "Tank",
}


# ---------------------------------------------------------------------------
# /team posture embed
# ---------------------------------------------------------------------------

def posture_embed(
    team_name: str,
    posture: dict,
    plan: dict | None,
    philosophy: str,
    recent_pivots: list[dict],
) -> discord.Embed:
    """Build the /team posture embed.

    Args:
        team_name: Display name e.g. "Los Angeles Lakers".
        posture:   Output of compute_posture().
        plan:      Output of get_team_plan() or None if no plan derived yet.
        philosophy: Coach philosophy string key.
        recent_pivots: Output of get_recent_pivots().
    """
    mode = posture.get("mode", "rebuilding")
    _, color = _MODE_LABELS.get(mode, ("Unknown", discord.Color.greyple()))

    embed = discord.Embed(
        title=f"{team_name} — Team Posture",
        color=color,
    )

    mode_label, _ = _MODE_LABELS.get(mode, (mode.replace("_", " ").title(), color))
    urgency_label = _URGENCY_LABELS.get(posture.get("urgency", "comfortable"), posture.get("urgency", ""))
    embed.add_field(name="Mode", value=mode_label, inline=True)
    embed.add_field(name="Urgency", value=urgency_label, inline=True)

    wins = posture.get("wins", 0)
    losses = posture.get("losses", 0)
    proj_wins = posture.get("projected_wins")
    conf_rank = posture.get("conf_rank")
    record_str = f"{wins}–{losses}"
    if proj_wins is not None:
        record_str += f" (proj. {proj_wins} W)"
    embed.add_field(name="Record", value=record_str, inline=True)

    if conf_rank is not None:
        embed.add_field(name="Conf Rank", value=f"#{conf_rank}", inline=True)

    # Plan goal + horizon
    if plan:
        goal_raw = plan.get("goal", "")
        goal_label = _GOAL_LABELS.get(goal_raw, goal_raw.replace("_", " ").title())
        horizon = plan.get("horizon_seasons", "?")
        embed.add_field(
            name="Franchise Goal",
            value=f"{goal_label} ({horizon} season{'s' if horizon != 1 else ''} out)",
            inline=False,
        )
    else:
        embed.add_field(name="Franchise Goal", value="No plan derived yet", inline=False)

    # Philosophy one-liner
    phil_data = PHILOSOPHY_DESCRIPTIONS.get(philosophy, {})
    emoji = phil_data.get("emoji", "")
    summary = phil_data.get("summary", philosophy.replace("_", " ").title())
    embed.add_field(
        name="Coach Philosophy",
        value=f"{emoji} **{philosophy.replace('_', ' ').title()}** — {summary}",
        inline=False,
    )

    # Plan stability
    if recent_pivots:
        pivot = recent_pivots[0]
        old_g = _GOAL_LABELS.get(pivot.get("old_goal", ""), pivot.get("old_goal", "?"))
        new_g = _GOAL_LABELS.get(pivot.get("new_goal", ""), pivot.get("new_goal", "?"))
        embed.add_field(
            name="Recent Pivot",
            value=f"Pivoted from **{old_g}** to **{new_g}**",
            inline=False,
        )
    else:
        embed.add_field(name="Plan Stability", value="Plan stable", inline=False)

    embed.set_footer(text="Posture computed live from current standings")
    return embed


# ---------------------------------------------------------------------------
# /team plan embed
# ---------------------------------------------------------------------------

def plan_embed(
    team_name: str,
    plan: dict,
) -> discord.Embed:
    """Build the /team plan embed — full franchise plan dashboard."""
    goal_raw = plan.get("goal", "")
    goal_label = _GOAL_LABELS.get(goal_raw, goal_raw.replace("_", " ").title())
    horizon = plan.get("horizon_seasons", "?")

    embed = discord.Embed(
        title=f"{team_name} — Franchise Plan",
        description=f"**{goal_label}** — {horizon} season{'s' if horizon != 1 else ''} out",
        color=discord.Color.blurple(),
    )

    # Asset targets
    asset_targets = plan.get("asset_targets") or []
    if isinstance(asset_targets, list) and asset_targets:
        targets_str = ", ".join(str(t).replace("_", " ") for t in asset_targets)
        embed.add_field(name="Pursuing", value=targets_str, inline=False)
    elif isinstance(asset_targets, dict):
        # Some plans store a dict shape — flatten keys
        targets_str = ", ".join(str(k).replace("_", " ") for k in asset_targets)
        embed.add_field(name="Pursuing", value=targets_str or "None", inline=False)

    # Core players
    core_names: list[str] = plan.get("core_names") or []
    if core_names:
        # Attempt to include OVR if plan has core_player_ids but no per-player OVR available here
        core_lines = "\n".join(f"• {n}" for n in core_names[:10])
        embed.add_field(name=f"Core ({len(core_names)})", value=core_lines, inline=False)
    else:
        embed.add_field(name="Core", value="None identified", inline=False)

    # Flex + Surplus counts (names not pre-fetched in get_team_plan for flex/surplus)
    flex_size = plan.get("flex_size", 0)
    surplus_size = plan.get("surplus_size", 0)
    if flex_size or surplus_size:
        bucket_lines = []
        if flex_size:
            bucket_lines.append(f"Flex: {flex_size} player{'s' if flex_size != 1 else ''}")
        if surplus_size:
            bucket_lines.append(f"Surplus / tradeable: {surplus_size} player{'s' if surplus_size != 1 else ''}")
        embed.add_field(name="Roster Buckets", value="\n".join(bucket_lines), inline=False)

    # Last assessed
    derived_at = plan.get("derived_at")
    if derived_at:
        date_str = derived_at.strftime("%Y-%m-%d") if hasattr(derived_at, "strftime") else str(derived_at)[:10]
        embed.set_footer(text=f"Last assessed: {date_str}")
    else:
        embed.set_footer(text="Assessment date unavailable")

    return embed


# ---------------------------------------------------------------------------
# /team philosophy embed
# ---------------------------------------------------------------------------

def philosophy_embed(
    team_name: str,
    philosophy: str,
    recent_role_changes: list[dict],
) -> discord.Embed:
    """Build the /team philosophy embed — focused single-screen philosophy view."""
    phil_data = PHILOSOPHY_DESCRIPTIONS.get(philosophy, {})
    emoji = phil_data.get("emoji", "")
    summary = phil_data.get("summary", philosophy.replace("_", " ").title())
    tendency = phil_data.get("tendency", "")

    display_key = philosophy.replace("_", " ").title()

    embed = discord.Embed(
        title=f"{team_name} — Coach Philosophy",
        description=f"{emoji} **{display_key}**",
        color=discord.Color.purple(),
    )

    embed.add_field(name="Overview", value=summary, inline=False)
    if tendency:
        embed.add_field(name="In Practice", value=tendency, inline=False)

    # Example tendency for well-known philosophies
    _EXAMPLES: dict[str, str] = {
        "star_maxer": "Locks top-2 OVR into iso_scorer/primary_initiator regardless of positional fit.",
        "egalitarian": "Refuses to assign iso/initiator even when a clear star is available.",
        "defense_first": "A 65 defense_tendency wing beats a 90 OVR scorer for a starting role.",
        "tendency_respecter": "Players are assigned exactly the role that matches their tendency profile.",
        "vet_overrater": "A 33-year-old OVR 78 vet is handed a primary role over a 22-year-old OVR 82.",
        "youth_developer": "A 23-year-old OVR 72 gets a feature role; the 34-year-old vet sits.",
        "chaos": "Roles change unpredictably each season even when the roster is identical.",
    }
    example = _EXAMPLES.get(philosophy)
    if example:
        embed.add_field(name="Example Behavior", value=example, inline=False)

    # Most recent human-locked role change (philosophy-driven)
    if recent_role_changes:
        recent = recent_role_changes[0]
        player_name = recent.get("player_name", "Unknown")
        new_role = (recent.get("new_role") or "?").replace("_", " ")
        assigned_by = recent.get("assigned_by") or "cpu"
        by_label = "human override" if str(assigned_by).startswith("human:") else "CPU"
        embed.add_field(
            name="Most Recent Role Change",
            value=f"**{player_name}** → {new_role} ({by_label})",
            inline=False,
        )

    return embed


# ---------------------------------------------------------------------------
# Division order used for /league philosophy-map
# ---------------------------------------------------------------------------

_DIVISION_ORDER: list[str] = [
    "Atlantic", "Central", "Southeast",
    "Northwest", "Pacific", "Southwest",
]


def philosophy_map_embed(
    teams: list[dict],
) -> discord.Embed:
    """Build the /league philosophy-map embed.

    Args:
        teams: List of dicts with keys:
            id, nba_team_code, conference, division, coach_philosophy
            Each row comes from a single bulk SELECT on the teams table.

    Layout: one embed field per division, teams listed inline with emoji + name.
    Fits comfortably within the 25-field / 6000-char Discord limits.
    """
    # Group teams by division, preserving standard division order.
    by_div: dict[str, list[dict]] = defaultdict(list)
    for t in teams:
        by_div[t["division"]].append(t)

    # Sort teams within each division by team code for consistency.
    for div_teams in by_div.values():
        div_teams.sort(key=lambda t: t["nba_team_code"])

    # Count unique philosophies across the league.
    unique_phils = {t["coach_philosophy"] for t in teams if t.get("coach_philosophy")}

    embed = discord.Embed(
        title="🏀 League Philosophy Map",
        description="Each team's coaching philosophy by division.",
        color=discord.Color.blurple(),
    )

    for division in _DIVISION_ORDER:
        div_teams = by_div.get(division, [])
        if not div_teams:
            continue

        lines: list[str] = []
        for t in div_teams:
            philo = t.get("coach_philosophy") or "tendency_respecter"
            phil_data = PHILOSOPHY_DESCRIPTIONS.get(philo, {})
            emoji = phil_data.get("emoji", "")
            # Use the raw key with underscores replaced so it stays compact.
            philo_short = philo.replace("_", " ")
            lines.append(f"`{t['nba_team_code']}` {emoji} {philo_short}")

        embed.add_field(
            name=division,
            value="\n".join(lines),
            inline=True,
        )

    embed.set_footer(
        text=f"{len(unique_phils)} philosophies represented • Re-randomized at league create"
    )
    return embed


# ---------------------------------------------------------------------------
# Trend badge for /stats power-rankings (posture-aware extension)
# ---------------------------------------------------------------------------

_TREND_BADGE: dict[str, str] = {
    "rising":  "🚀 Rising",
    "cooling": "📉 Cooling",
    "tanking": "🏳 Tanking",
    "holding": "🔒 Holding",
}

_MODE_SHORT: dict[str, str] = {
    "contending":     "Contending",
    "play_in_fringe": "Play-In",
    "soft_rebuild":   "Soft Rebuild",
    "rebuilding":     "Rebuilding",
    "developing":     "Developing",
}


def _trend_badge(posture: dict | None) -> str:
    """Derive a Trend badge from a team's posture dict."""
    if posture is None:
        return _TREND_BADGE["holding"]
    mode = posture.get("mode", "")
    urgency = posture.get("urgency", "")
    proj = posture.get("projected_wins")
    wins = posture.get("wins", 0) or 0
    losses = posture.get("losses", 0) or 0

    if mode in ("rebuilding", "soft_rebuild") and urgency == "tanking":
        return _TREND_BADGE["tanking"]

    if proj is not None:
        games_played = wins + losses
        if games_played > 0:
            current_pace = round((wins / games_played) * 82)
            if proj > current_pace:
                return _TREND_BADGE["rising"]
            if proj < current_pace:
                return _TREND_BADGE["cooling"]

    return _TREND_BADGE["holding"]


def power_rankings_posture_embed(
    ranked: list[dict],
    mode: str,
) -> discord.Embed:
    """Extended power-rankings embed with posture trend badges.

    Args:
        ranked: List of dicts, each containing:
            rank, team_code, wins, losses, last_10_wins, last_10_losses,
            score, trend (the existing ↑/↓/→ arrow),
            posture (dict from build_team_intel, or None),
            avg_ovr (float, team average OVR from players table).
        mode: "record" | "expected" | "talent"
    """
    _MODE_TITLES = {
        "record":   "Power Rankings",
        "expected": "Power Rankings — Expected Wins",
        "talent":   "Power Rankings — Pure Talent",
    }
    title = _MODE_TITLES.get(mode, "Power Rankings")
    embed = discord.Embed(title=title, color=discord.Color.gold())

    lines: list[str] = []
    for entry in ranked:
        record = f"{entry['wins']}–{entry['losses']}"
        trend_badge = _trend_badge(entry.get("posture"))
        team_mode = entry.get("posture", {}) or {}
        mode_short = _MODE_SHORT.get(team_mode.get("mode", ""), "")
        avg_ovr = entry.get("avg_ovr")
        ovr_str = f"OVR {avg_ovr:.0f}" if avg_ovr is not None else ""

        # Compact row: rank. badge CODE  record  mode  OVR
        parts = [
            f"`{entry['rank']:2}.`",
            trend_badge,
            f"**{entry['team_code']}**",
            record,
        ]
        if mode_short:
            parts.append(f"({mode_short})")
        if ovr_str:
            parts.append(ovr_str)

        lines.append("  ".join(parts))

    embed.description = "\n".join(lines) if lines else "No standings data yet."

    _FOOTER = {
        "record":   "Sorted by power score (win% × 0.6 + L10 × 0.4)",
        "expected": "Sorted by projected wins from posture model",
        "talent":   "Sorted by roster OVR average",
    }
    embed.set_footer(text=_FOOTER.get(mode, ""))
    return embed


# ---------------------------------------------------------------------------
# /league digest embed
# ---------------------------------------------------------------------------

_MODE_SHORT_DIGEST: dict[str, str] = {
    "contending":     "Contending",
    "play_in_fringe": "Play-In Fringe",
    "soft_rebuild":   "Soft Rebuild",
    "rebuilding":     "Rebuilding",
    "developing":     "Developing",
}

_GOAL_SHORT_DIGEST: dict[str, str] = {
    "win_now":    "Win Now",
    "transition": "Transition",
    "rebuild":    "Rebuild",
    "tank":       "Tank",
}


def league_digest_embed(
    digest: dict,
    since_batches: int = 7,
) -> discord.Embed:
    """Build the /league digest embed from build_league_digest() output.

    Args:
        digest:        Output of league_digest.build_league_digest().
        since_batches: Number of sim batches the window covers (for footer text).
    """
    embed = discord.Embed(
        title=f"🗞 Around the League — Last {since_batches} sim batches",
        color=discord.Color.blurple(),
    )

    # --- Mode shifts ---
    mode_changes: list[dict] = digest.get("mode_changes", [])
    if mode_changes:
        lines = []
        for mc in mode_changes[:8]:
            old = _MODE_SHORT_DIGEST.get(mc["old_mode"], mc["old_mode"].replace("_", " ").title())
            new = _MODE_SHORT_DIGEST.get(mc["new_mode"], mc["new_mode"].replace("_", " ").title())
            lines.append(f"• **{mc['team_code']}**  {old} → {new}")
        embed.add_field(
            name=f"Mode shifts ({len(mode_changes)})",
            value="\n".join(lines),
            inline=False,
        )
    else:
        embed.add_field(name="Mode shifts", value="None this window", inline=False)

    # --- Plan pivots ---
    plan_pivots: list[dict] = digest.get("plan_pivots", [])
    if plan_pivots:
        lines = []
        for pp in plan_pivots[:8]:
            old = _GOAL_SHORT_DIGEST.get(pp["old_goal"], pp["old_goal"].replace("_", " ").title())
            new = _GOAL_SHORT_DIGEST.get(pp["new_goal"], pp["new_goal"].replace("_", " ").title())
            lines.append(f"• **{pp['team_code']}**  {old} → {new}")
        embed.add_field(
            name=f"Plan pivots ({len(plan_pivots)})",
            value="\n".join(lines),
            inline=False,
        )
    else:
        embed.add_field(name="Plan pivots", value="None this window", inline=False)

    # --- Notable role swaps ---
    role_swaps: list[dict] = digest.get("role_swaps", [])
    if role_swaps:
        lines = []
        for rs in role_swaps[:6]:
            role_str = (rs.get("new_role") or "?").replace("_", " ")
            assigned_by = rs.get("assigned_by") or "cpu"
            by_label = "human override" if str(assigned_by).startswith("human:") else "CPU"
            lines.append(
                f"• **{rs['player_name']}** → {role_str} ({rs['team_code']}, {by_label})"
            )
        embed.add_field(
            name=f"Notable role swaps ({len(role_swaps)})",
            value="\n".join(lines),
            inline=False,
        )

    # --- Most-fired signals ---
    top_signals: list[dict] = digest.get("top_signals", [])
    if top_signals:
        lines = []
        for sig in top_signals[:5]:
            code = sig["code"].replace("_", " ")
            count = sig["count"]
            avg_delta = sig["avg_delta"]
            sign = "+" if avg_delta >= 0 else ""
            lines.append(f"`{code}`  ×{count}  Σ {sign}{avg_delta}")
        embed.add_field(
            name="Most-fired signals",
            value="\n".join(lines),
            inline=False,
        )

    # --- Loudest trades ---
    loud_trades: list[dict] = digest.get("loud_trades", [])
    if loud_trades:
        lines = []
        for lt in loud_trades:
            headline = lt.get("headline_signal", "").replace("_", " ")
            summary = lt.get("summary", f"Trade #{lt['trade_id']}")
            if headline:
                lines.append(f"• {summary}  — {headline}")
            else:
                lines.append(f"• {summary}")
        embed.add_field(
            name="Loudest trades",
            value="\n".join(lines),
            inline=False,
        )

    # --- Philosophy outputs ---
    phil_outputs: list[dict] = digest.get("philosophy_outputs", [])
    if phil_outputs:
        lines = [f"• {po['headline']}" for po in phil_outputs[:4]]
        embed.add_field(
            name="Philosophy-driven moves",
            value="\n".join(lines),
            inline=False,
        )

    embed.set_footer(text=f"Window: last {since_batches} sim batches (~{since_batches * 10} games)")
    return embed


# ---------------------------------------------------------------------------
# /trade league (league-wide activity feed)
# ---------------------------------------------------------------------------

_TRADE_STATUS_ICON: dict[str, str] = {
    "approved":           "📦",
    "pending_commissioner": "🟡",
    "pending_counterparty": "🟡",
    "declined":           "❌",
    "rejected":           "❌",
    "vetoed":             "❌",
}


def _relative_time(ts: datetime.datetime | None) -> str:
    """Return a human-readable relative string like '2 days ago'."""
    if ts is None:
        return "?"
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    delta = now - ts
    days = delta.days
    if days == 0:
        hours = delta.seconds // 3600
        return f"{hours}h ago" if hours > 0 else "just now"
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"


def league_trades_embed(
    trades: list[dict],
) -> discord.Embed:
    """Build the /league trades (activity feed) embed.

    Args:
        trades: List of dicts from the bulk trade query, each containing:
            id, status, updated_at (resolved_at or proposed_at),
            proposer_code, counterparty_code,
            proposer_assets (list[str] — player names / pick labels from proposer),
            counterparty_assets (list[str] — assets from counterparty).
    """
    embed = discord.Embed(
        title="Recent League Trades",
        color=discord.Color.blurple(),
    )

    if not trades:
        embed.description = "No trades found."
        embed.set_footer(text="Use /trade history <team> for per-team detail")
        return embed

    lines: list[str] = []
    for t in trades:
        icon = _TRADE_STATUS_ICON.get(t["status"], "📦")
        when = _relative_time(t.get("updated_at"))
        proposer = t.get("proposer_code", "???")
        counterparty = t.get("counterparty_code", "???")

        status_label = {
            "approved": "approved",
            "pending_commissioner": "pending approval",
            "pending_counterparty": "awaiting response",
            "declined": "declined",
            "rejected": "rejected",
            "vetoed": "vetoed",
        }.get(t["status"], t["status"])

        # Header line: icon #id  TEAM ↔ TEAM  status  when
        header = f"{icon} **#{t['id']}**  `{proposer}` ↔ `{counterparty}`  {status_label} {when}"
        lines.append(header)

        # Asset summary — one compact line per direction, truncated if too long.
        prop_assets = t.get("proposer_assets") or []
        cp_assets = t.get("counterparty_assets") or []

        def _summarize(assets: list[str], limit: int = 3) -> str:
            if not assets:
                return "nothing"
            shown = assets[:limit]
            rest = len(assets) - limit
            summary = ", ".join(shown)
            if rest > 0:
                summary += f" +{rest} more"
            return summary

        detail = (
            f"  `{proposer}` sends: {_summarize(prop_assets)}  |  "
            f"`{counterparty}` sends: {_summarize(cp_assets)}"
        )
        lines.append(detail)

    # Discord description cap is 4096 chars; truncate gracefully.
    description = "\n".join(lines)
    if len(description) > 3900:
        description = description[:3900] + "\n…"

    embed.description = description
    embed.set_footer(
        text="Use /trade history <team> for per-team detail • /trade pending for commissioner queue"
    )
    return embed
