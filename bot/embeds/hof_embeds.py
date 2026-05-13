from __future__ import annotations

from typing import Dict

import discord


def induction_embed(player: dict, record: dict) -> discord.Embed:
    """
    Announcement embed for a single HOF induction.
    player: dict with at least 'player_name' key.
    record: hall_of_fame row dict.
    """
    name = player.get("player_name", "Unknown Player")
    embed = discord.Embed(
        title=f"Hall of Fame Induction — {name}",
        description=record.get("induction_reason", ""),
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="Career Seasons",
        value=str(record.get("career_seasons", "?")),
        inline=True,
    )
    embed.add_field(
        name="Championships",
        value=str(record.get("career_championships", 0)),
        inline=True,
    )
    embed.add_field(
        name="MVP Votes",
        value=str(record.get("career_mvp_votes", 0)),
        inline=True,
    )
    embed.add_field(
        name="Peak OVR",
        value=str(record.get("career_overall_peak", "?")),
        inline=True,
    )
    embed.add_field(
        name="Inducted Season",
        value=str(record.get("inducted_season", "?")),
        inline=True,
    )
    embed.set_footer(text="Congratulations on a legendary career.")
    return embed


def hof_list_embed(
    inductees: list[dict],
    players_by_id: Dict[int, object],
) -> discord.Embed:
    """
    Full HOF list embed.
    inductees: list of hall_of_fame row dicts.
    players_by_id: {player_id: Player} for name lookup.
    """
    embed = discord.Embed(
        title="Hall of Fame",
        color=discord.Color.gold(),
    )

    if not inductees:
        embed.description = "No players have been inducted yet."
        return embed

    lines: list[str] = []
    for row in inductees:
        pid = row["player_id"]
        player = players_by_id.get(pid)
        name = (
            getattr(player, "full_name", None)
            or f"Player #{pid}"
        )
        season = row.get("inducted_season", "?")
        reason = row.get("induction_reason", "")
        lines.append(f"**{name}** (S{season}) — {reason}")

    embed.description = "\n".join(lines)
    return embed
