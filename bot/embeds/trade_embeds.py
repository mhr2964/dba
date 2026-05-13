from __future__ import annotations

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
        description=f"**{player['full_name']}** (OVR {player.get('overall', '?')}) is now on the trade block.",
        color=discord.Color.orange(),
    )
    embed.add_field(name="Team", value=team.full_name, inline=True)
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
        if entry.get("asking_price") is not None:
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
            if entry.get("asking_price") is not None:
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
    return f"- Unknown asset"


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
) -> discord.Embed:
    embed = discord.Embed(
        title=f"Trade #{trade.id} — Grades",
        color=discord.Color.green(),
    )
    embed.add_field(name=team_a.full_name, value=f"**{grade_a}**", inline=True)
    embed.add_field(name=team_b.full_name, value=f"**{grade_b}**", inline=True)
    embed.add_field(name="Analysis", value=rationale, inline=False)
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
