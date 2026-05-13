from __future__ import annotations

from typing import Dict, List, Optional

import discord


def rollover_complete_embed(summary: dict) -> discord.Embed:
    embed = discord.Embed(
        title="Season Rollover Complete",
        color=discord.Color.teal(),
    )
    embed.add_field(
        name="Season Archived",
        value=str(summary["season_archived"]),
        inline=True,
    )
    embed.add_field(
        name="Next Season",
        value=str(summary["next_season"]),
        inline=True,
    )
    embed.add_field(
        name="Contracts Expired",
        value=str(summary["contracts_expired"]),
        inline=True,
    )
    embed.add_field(
        name="Players Progressed",
        value=str(summary["players_progressed"]),
        inline=True,
    )
    embed.set_footer(text="League is now in PRESEASON_READY phase.")
    return embed


def season_history_embed(
    record: dict,
    champion_team: Optional[object],
    mvp_player: Optional[object],
) -> discord.Embed:
    season = record.get("season", "?")
    embed = discord.Embed(
        title=f"Season {season} History",
        color=discord.Color.gold(),
    )

    champion_name = (
        getattr(champion_team, "full_name", None)
        or (f"Team #{record['champion_team_id']}" if record.get("champion_team_id") else "N/A")
    )
    embed.add_field(name="Champion", value=champion_name, inline=True)

    mvp_name = (
        getattr(mvp_player, "full_name", None)
        or (f"Player #{record['mvp_player_id']}" if record.get("mvp_player_id") else "N/A")
    )
    embed.add_field(name="MVP", value=mvp_name, inline=True)

    completed_at = record.get("completed_at")
    if completed_at:
        embed.set_footer(text=f"Completed {completed_at.strftime('%Y-%m-%d') if hasattr(completed_at, 'strftime') else completed_at}")

    return embed


def history_list_embed(
    records: List[dict],
    teams_by_id: Dict[int, object],
    players_by_id: Dict[int, object],
    season_extras: Optional[Dict[int, dict]] = None,
) -> discord.Embed:
    embed = discord.Embed(
        title="Season History",
        color=discord.Color.blurple(),
    )

    if not records:
        embed.description = "No completed seasons on record."
        return embed

    lines: List[str] = []
    for r in records:
        season = r.get("season", "?")

        champ_id = r.get("champion_team_id")
        champ = teams_by_id.get(champ_id) if champ_id else None
        champ_name = getattr(champ, "full_name", None) or (f"Team #{champ_id}" if champ_id else "N/A")

        mvp_id = r.get("mvp_player_id")
        mvp = players_by_id.get(mvp_id) if mvp_id else None
        mvp_name = getattr(mvp, "full_name", None) or (f"Player #{mvp_id}" if mvp_id else "N/A")

        line = f"**{season}** — Champ: {champ_name} | MVP: {mvp_name}"

        if season_extras:
            extras = season_extras.get(season, {})
            champ_wins = extras.get("champ_wins")
            champ_losses = extras.get("champ_losses")
            trade_count = extras.get("trade_count", 0)
            if champ_wins is not None:
                line += f" | Record: {champ_wins}-{champ_losses}"
            line += f" | Trades: {trade_count}"

        lines.append(line)

    embed.description = "\n".join(lines)
    return embed
