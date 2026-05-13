from __future__ import annotations

from typing import List

import discord


def progression_summary_embed(
    processed: int,
    notable_improvers: List[dict],
    notable_decliners: List[dict],
) -> discord.Embed:
    """
    processed: total number of players who went through progression.
    notable_improvers: up to 3 dicts with keys 'name', 'position', 'before', 'after'.
    notable_decliners: up to 3 dicts with keys 'name', 'position', 'before', 'after'.
    """
    embed = discord.Embed(
        title="Offseason Progression Complete",
        description=f"{processed} players processed.",
        color=discord.Color.green(),
    )

    if notable_improvers:
        lines = [
            f"**{p['name']}** ({p['position']}) {p['before']} → **{p['after']}** OVR"
            for p in notable_improvers[:3]
        ]
        embed.add_field(name="Top Risers", value="\n".join(lines), inline=False)

    if notable_decliners:
        lines = [
            f"**{p['name']}** ({p['position']}) {p['before']} → **{p['after']}** OVR"
            for p in notable_decliners[:3]
        ]
        embed.add_field(name="Biggest Declines", value="\n".join(lines), inline=False)

    return embed
