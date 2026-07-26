from __future__ import annotations

import collections
import datetime

import discord

from data.repositories.trade_repo import Trade, TradeAsset
from data.repositories.team_repo import Team


def trade_block_added_embed(
    player: dict,
    team: Team,
    asking_price: int | None,
    note: str | None,
) -> discord.Embed:
    embed = discord.Embed(
        title="Trade Block — Player Listed",
        description=f"**{player['full_name']}** is now on the trade block.",
        color=discord.Color.orange(),
    )
    embed.add_field(name="Team", value=team.full_name, inline=True)
    embed.add_field(name="OVR", value=str(player.get("overall", "?")), inline=True)
    if player.get("position"):
        embed.add_field(name="Position", value=player["position"], inline=True)
    if player.get("age") is not None:
        embed.add_field(name="Age", value=str(player["age"]), inline=True)
    if player.get("salary") is not None:
        yrs = player.get("years_remaining", "?")
        embed.add_field(
            name="Contract",
            value=f"${player['salary']:,} / {yrs}yr{'s' if yrs != 1 else ''}",
            inline=True,
        )
    if asking_price is not None:
        embed.add_field(name="Asking Price", value=f"${asking_price:,}", inline=True)
    if note:
        embed.add_field(name="Note", value=note, inline=False)
    return embed


def trade_block_team_embed(
    team: Team,
    block_entries: list[dict],
    players_by_id: dict,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"{team.full_name} — Trade Block",
        color=discord.Color.orange(),
    )
    if not block_entries:
        embed.description = "No players on the trade block."
        return embed

    lines: list[str] = []
    for entry in block_entries:
        pid = entry["player_id"]
        player = players_by_id.get(pid)
        name = player["full_name"] if player else f"Player #{pid}"
        ovr = player.get("overall", "?") if player else "?"
        line = f"**{name}** (OVR {ovr})"
        # TB3: asking_price is only ever a real dollar figure for human-submitted
        # entries. CPU-generated entries (team.manager_user_id is None) store an
        # unscaled player_trade_value comparison score there post-TB2, so never
        # render it for CPU teams regardless of the stored value. See
        # docs/design/trade-block-logic-rules.md TB3.
        if team.manager_user_id is not None and entry.get("asking_price") is not None:
            line += f" — asking ${entry['asking_price']:,}"
        if entry.get("note"):
            line += f"\n  _{entry['note']}_"
        lines.append(line)

    embed.description = "\n\n".join(lines)
    return embed


def trade_block_league_embed(
    entries_by_team: dict[int, list[dict]],
    teams_by_id: dict,
    players_by_id: dict,
) -> discord.Embed:
    embed = discord.Embed(
        title="League Trade Block",
        description="Players currently available across all teams:",
        color=discord.Color.blurple(),
    )
    if not entries_by_team:
        embed.description = "No players on the trade block league-wide."
        return embed

    for team_id, entries in entries_by_team.items():
        team = teams_by_id.get(team_id)
        team_name = team.full_name if team else f"Team #{team_id}"
        lines: list[str] = []
        for entry in entries:
            pid = entry["player_id"]
            player = players_by_id.get(pid)
            name = player["full_name"] if player else f"Player #{pid}"
            ovr = player.get("overall", "?") if player else "?"
            line = f"`{name}` OVR {ovr}"
            # TB3: same CPU-vs-human gate as trade_block_team_embed, but per-entry
            # since a league embed mixes teams -- look up each entry's own team.
            if team is not None and team.manager_user_id is not None and entry.get("asking_price") is not None:
                line += f" — ${entry['asking_price']:,}"
            if entry.get("note"):
                line += f" _{entry['note']}_"
            lines.append(line)
        embed.add_field(name=team_name, value="\n".join(lines), inline=False)

    return embed

_STATUS_COLORS = {
    "pending_counterparty": discord.Color.yellow(),
    "pending_commissioner": discord.Color.orange(),
    "approved": discord.Color.green(),
    "vetoed": discord.Color.red(),
    "declined": discord.Color.dark_gray(),
    "expired": discord.Color.light_gray(),
}

_STATUS_LABELS = {
    "pending_counterparty": "Awaiting Counterparty",
    "pending_commissioner": "Awaiting Commissioner",
    "approved": "Approved",
    "vetoed": "Vetoed",
    "declined": "Declined",
    "expired": "Expired",
}


def _asset_line(asset: TradeAsset, players_by_id: dict, picks_by_id: dict) -> str:
    if asset.asset_type == "player" and asset.player_id:
        player = players_by_id.get(asset.player_id)
        if player:
            return f"- {player.get('full_name', f'Player #{asset.player_id}')} (OVR {player.get('overall', '?')})"
        return f"- Player #{asset.player_id}"
    if asset.asset_type == "pick" and asset.pick_id:
        pick = picks_by_id.get(asset.pick_id)
        if pick:
            return f"- {pick.get('season', '?')} Rd {pick.get('round', '?')} pick"
        return f"- Pick #{asset.pick_id}"
    return "- Unknown asset"


def trade_card(
    trade: Trade,
    assets: list[TradeAsset],
    proposer_team: Team,
    counterparty_team: Team,
    players_by_id: dict,
    picks_by_id: dict,
) -> discord.Embed:
    color = _STATUS_COLORS.get(trade.status, discord.Color.blurple())
    embed = discord.Embed(
        title=f"Trade #{trade.id} — {_STATUS_LABELS.get(trade.status, trade.status)}",
        color=color,
    )

    proposer_assets = [a for a in assets if a.from_team_id == trade.proposer_team_id]
    counterparty_assets = [a for a in assets if a.from_team_id == trade.counterparty_team_id]

    proposer_lines = "\n".join(_asset_line(a, players_by_id, picks_by_id) for a in proposer_assets) or "Nothing"
    counterparty_lines = "\n".join(_asset_line(a, players_by_id, picks_by_id) for a in counterparty_assets) or "Nothing"

    embed.add_field(
        name=f"{proposer_team.full_name} gives",
        value=proposer_lines,
        inline=True,
    )
    embed.add_field(
        name=f"{counterparty_team.full_name} gives",
        value=counterparty_lines,
        inline=True,
    )

    if trade.cpu_evaluator_score is not None:
        embed.add_field(
            name="CPU Evaluation",
            value=f"Score: {trade.cpu_evaluator_score:.1f}\n{trade.cpu_rationale or ''}",
            inline=False,
        )

    embed.set_footer(text=f"Proposed {trade.proposed_at.strftime('%Y-%m-%d %H:%M UTC')} | Season {trade.season}")
    return embed


def trade_proposed(trade: Trade, proposer_team: Team, counterparty_team: Team) -> discord.Embed:
    embed = discord.Embed(
        title="Trade Proposed",
        description=(
            f"**{proposer_team.full_name}** has proposed a trade with **{counterparty_team.full_name}**."
        ),
        color=discord.Color.yellow(),
    )
    embed.add_field(name="Trade ID", value=str(trade.id), inline=True)
    embed.add_field(name="Season", value=str(trade.season), inline=True)
    embed.add_field(name="Status", value=_STATUS_LABELS.get(trade.status, trade.status), inline=True)
    embed.set_footer(text="Use /trade accept or /trade decline to respond.")
    return embed


def trade_result(trade: Trade, action: str) -> discord.Embed:
    color_map = {
        "approved": discord.Color.green(),
        "vetoed": discord.Color.red(),
        "declined": discord.Color.dark_gray(),
    }
    color = color_map.get(action, discord.Color.blurple())
    embed = discord.Embed(
        title=f"Trade #{trade.id} — {action.capitalize()}",
        color=color,
    )
    if trade.commissioner_reason:
        embed.add_field(name="Reason", value=trade.commissioner_reason, inline=False)
    if trade.resolved_at:
        embed.set_footer(text=f"Resolved {trade.resolved_at.strftime('%Y-%m-%d %H:%M UTC')}")
    return embed


def trade_grade_embed(
    trade: Trade,
    grade_a: str,
    grade_b: str,
    team_a: Team,
    team_b: Team,
    rationale: str,
    ai_reasoning: dict[str, str] | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"Trade #{trade.id} — Grades",
        color=discord.Color.green(),
    )
    embed.add_field(name=team_a.full_name, value=f"**{grade_a}**", inline=True)
    embed.add_field(name=team_b.full_name, value=f"**{grade_b}**", inline=True)
    embed.add_field(name="Analysis", value=rationale, inline=False)
    if ai_reasoning:
        reasoning_a = ai_reasoning.get("team_a", "")
        reasoning_b = ai_reasoning.get("team_b", "")
        if reasoning_a:
            embed.add_field(name=f"{team_a.nba_team_code} Analysis", value=reasoning_a, inline=False)
        if reasoning_b:
            embed.add_field(name=f"{team_b.nba_team_code} Analysis", value=reasoning_b, inline=False)
    return embed


def trade_history_embed(
    trades: list[dict],
    team: Team,
    players_by_id: dict,
) -> discord.Embed:
    """
    Compact trade history list.
    Each entry: 'Season YYYY: Gave [names] | Got [names]'
    trades is a list of {trade: Trade, assets: list[TradeAsset], other_team_id: int}.
    players_by_id maps player_id -> dict(full_name, overall).
    """
    embed = discord.Embed(
        title=f"{team.full_name} — Trade History",
        color=discord.Color.blurple(),
    )
    if not trades:
        embed.description = "No approved trades found."
        return embed

    lines: list[str] = []
    for entry in trades:
        trade: Trade = entry["trade"]
        assets = entry["assets"]

        gave = [
            a for a in assets
            if a.from_team_id == team.id and a.asset_type == "player"
        ]
        got = [
            a for a in assets
            if a.to_team_id == team.id and a.asset_type == "player"
        ]

        def _names(asset_list: list) -> str:
            parts = []
            for a in asset_list:
                if a.player_id and a.player_id in players_by_id:
                    parts.append(players_by_id[a.player_id]["full_name"])
                elif a.player_id:
                    parts.append(f"Player #{a.player_id}")
            return ", ".join(parts) if parts else "picks/other"

        gave_str = _names(gave)
        got_str = _names(got)

        date_str = (
            trade.resolved_at.strftime("%Y-%m-%d")
            if trade.resolved_at
            else "?"
        )
        lines.append(
            f"**Season {trade.season}** ({date_str})\n"
            f"  Gave: {gave_str}\n"
            f"  Got:  {got_str}"
        )

    embed.description = "\n\n".join(lines)
    return embed


def trade_why_embed(
    trade: Trade,
    context_signals: dict,
    proposer_team: Team,
    counterparty_team: Team,
    player_names: dict[int, str],
) -> discord.Embed:
    """Embed for /trade why <trade_id> — shows per-player CPU reasoning signals.

    player_names: player_id -> display name (e.g. "D. Mitchell")
    context_signals: the JSONB blob from trades.context_signals
    """
    _ts_ago = ""
    if trade.proposed_at:
        delta = datetime.datetime.utcnow() - trade.proposed_at.replace(tzinfo=None)
        days = delta.days
        if days == 0:
            _ts_ago = "today"
        elif days == 1:
            _ts_ago = "1 day ago"
        else:
            _ts_ago = f"{days} days ago"

    status_label = _STATUS_LABELS.get(trade.status, trade.status)
    color = _STATUS_COLORS.get(trade.status, discord.Color.blurple())

    embed = discord.Embed(
        title=f"Trade #{trade.id} — CPU Reasoning",
        color=color,
    )
    embed.add_field(
        name="Status",
        value=f"{status_label} ({_ts_ago})" if _ts_ago else status_label,
        inline=True,
    )
    embed.add_field(
        name="Teams",
        value=f"{proposer_team.nba_team_code} ↔ {counterparty_team.nba_team_code}",
        inline=True,
    )

    per_player: dict = context_signals.get("per_player") or {}
    mod_per_player: dict = context_signals.get("context_modifier_per_player") or {}

    if not per_player:
        embed.description = "No signal detail recorded for this trade."
        embed.set_footer(text="Use /trade signals <team> for aggregate patterns.")
        return embed

    embed.description = "Signals that drove the CPU's decision:"

    for pid_str, signals in per_player.items():
        try:
            pid = int(pid_str)
        except ValueError:
            pid = -1
        display_name = player_names.get(pid, f"Player #{pid_str}")
        mod = mod_per_player.get(pid_str)
        mod_str = f"  Total context modifier: {mod:.2f}" if mod is not None else ""

        lines: list[str] = []
        for sig in signals:
            code = sig.get("code", "?")
            delta = sig.get("delta", 0.0)
            reason = sig.get("reason", "")
            sign = "+" if delta >= 0 else ""
            lines.append(f"• {code} [Δ{sign}{delta:.2f}] — {reason}")

        field_value = "\n".join(lines)
        if mod_str:
            field_value += f"\n{mod_str}"
        if not field_value.strip():
            field_value = "(no signals fired)"

        embed.add_field(name=display_name, value=field_value[:1024], inline=False)

    computed_at = context_signals.get("computed_at", "")
    footer_parts = ["Signals computed at trade evaluation time."]
    if computed_at:
        try:
            _dt = datetime.datetime.fromisoformat(computed_at.replace("Z", "+00:00"))
            footer_parts.append(f"Computed {_dt.strftime('%Y-%m-%d %H:%M UTC')}.")
        except Exception:
            pass
    footer_parts.append("Use /trade signals <team> for aggregate patterns.")
    embed.set_footer(text=" ".join(footer_parts))
    return embed


def trade_signals_embed(
    team: Team,
    trade_count: int,
    most_fired: list[tuple[str, int, float]],
    top_positive: list[tuple[str, float]],
    top_negative: list[tuple[str, float]],
) -> discord.Embed:
    """Embed for /trade signals <team_code> — aggregate signal patterns.

    most_fired: list of (code, count, avg_delta) sorted by count desc
    top_positive: list of (code, cumulative_delta) sorted by delta desc
    top_negative: list of (code, cumulative_delta) sorted by delta asc (most negative first)
    """
    embed = discord.Embed(
        title=f"{team.nba_team_code} — Recent Trade Signal Patterns",
        description=f"Based on last {trade_count} trade(s) with signal data:",
        color=discord.Color.blurple(),
    )

    if not most_fired and not top_positive and not top_negative:
        embed.description = (
            f"No signal data found for {team.nba_team_code}. "
            "Signals tracking started 2026-05-20; trades before this date have no signal history."
        )
        embed.set_footer(text="Use /trade why <id> for full breakdown of a specific trade.")
        return embed

    if most_fired:
        lines = []
        for code, count, avg_delta in most_fired:
            sign = "+" if avg_delta >= 0 else ""
            lines.append(f"`{code}` ×{count}  (avg Δ{sign}{avg_delta:.2f})")
        embed.add_field(
            name="Most-fired signals",
            value="\n".join(lines) or "(none)",
            inline=False,
        )

    if top_positive:
        lines = []
        for code, cum_delta in top_positive:
            lines.append(f"`{code}`  Σ +{cum_delta:.2f}")
        embed.add_field(
            name="Top positive drivers (cumulative Δ)",
            value="\n".join(lines) or "(none)",
            inline=True,
        )

    if top_negative:
        lines = []
        for code, cum_delta in top_negative:
            lines.append(f"`{code}`  Σ {cum_delta:.2f}")
        embed.add_field(
            name="Top negative drivers (cumulative Δ)",
            value="\n".join(lines) or "(none)",
            inline=True,
        )

    embed.set_footer(text="Use /trade why <id> for full breakdown of any specific trade.")
    return embed


def _aggregate_signals(trade_rows: list[dict]) -> tuple[
    list[tuple[str, int, float]],
    list[tuple[str, float]],
    list[tuple[str, float]],
]:
    """Aggregate signal statistics from a list of trade rows with context_signals.

    Returns (most_fired, top_positive, top_negative) where:
      most_fired   = [(code, count, avg_delta)] top-5 by frequency
      top_positive = [(code, cumulative_delta)] top-3 by cumulative positive delta
      top_negative = [(code, cumulative_delta)] top-3 most negative cumulative delta
    """
    code_counts: dict[str, int] = collections.Counter()
    code_deltas: dict[str, list[float]] = collections.defaultdict(list)

    for row in trade_rows:
        signals_blob: dict = row.get("context_signals") or {}
        per_player: dict = signals_blob.get("per_player") or {}
        for _pid_str, sigs in per_player.items():
            for sig in sigs:
                code = sig.get("code", "")
                delta = sig.get("delta", 0.0)
                if not code:
                    continue
                code_counts[code] += 1
                code_deltas[code].append(delta)

    most_fired: list[tuple[str, int, float]] = []
    for code, count in sorted(code_counts.items(), key=lambda x: x[1], reverse=True)[:5]:
        deltas = code_deltas[code]
        avg = sum(deltas) / len(deltas) if deltas else 0.0
        most_fired.append((code, count, round(avg, 3)))

    cumulative: dict[str, float] = {
        code: round(sum(deltas), 3)
        for code, deltas in code_deltas.items()
    }
    top_positive = sorted(
        [(c, d) for c, d in cumulative.items() if d > 0],
        key=lambda x: x[1], reverse=True,
    )[:3]
    top_negative = sorted(
        [(c, d) for c, d in cumulative.items() if d < 0],
        key=lambda x: x[1],
    )[:3]

    return most_fired, top_positive, top_negative


def pending_queue(trades_list: list[dict]) -> discord.Embed:
    embed = discord.Embed(
        title="Pending Trades — Commissioner Queue",
        color=discord.Color.orange(),
    )
    if not trades_list:
        embed.description = "No trades pending commissioner review."
        return embed

    lines = []
    for entry in trades_list:
        trade: Trade = entry["trade"]
        proposer: Team = entry["proposer_team"]
        counterparty: Team = entry["counterparty_team"]
        lines.append(
            f"**#{trade.id}** — {proposer.full_name} ↔ {counterparty.full_name} "
            f"(proposed {trade.proposed_at.strftime('%m/%d')})"
        )

    embed.description = "\n".join(lines)
    embed.set_footer(text=f"{len(trades_list)} trade(s) pending review")
    return embed
