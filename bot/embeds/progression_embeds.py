from __future__ import annotations

from typing import List, Optional

import discord


def progression_summary_embed(
    processed: int,
    notable_improvers: List[dict],
    notable_decliners: List[dict],
    hof_inducted: Optional[list[dict]] = None,
) -> discord.Embed:
    """
    processed: total number of players who went through progression.
    notable_improvers: up to 3 dicts with keys 'name', 'position', 'before', 'after'.
    notable_decliners: up to 3 dicts with keys 'name', 'position', 'before', 'after'.
    hof_inducted: RO3 -- optional list of dicts from hof_service.check_and_induct
      (keys 'player_id', 'player_name', 'record'), now decided AFTER progression
      runs rather than inside rollover. Omitted/empty adds no field.
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

    if hof_inducted:
        lines = [
            f"**{p['player_name']}** — {p['record']['induction_reason']}"
            for p in hof_inducted
        ]
        embed.add_field(name="Hall of Fame Inductions", value="\n".join(lines), inline=False)

    return embed
