"""Player lookup slash commands (/player search).

Extracted from roster_cog.py (Phase 3 opportunistic cog split, see
HANDOFF.md) -- PlayerGroup was already an independent top-level group,
registered directly on the bot tree by roster_cog.py's setup() rather
than composed into RosterCog, and had zero dependency on
RosterCog/LineupGroup's module-level helpers -- registered as its own
extension.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core.errors import safe_defer, safe_respond
from core.logging import get_logger
from data.db import get_pool
from data.repositories import player_repo

log = get_logger(__name__)


class PlayerGroup(app_commands.Group, name="player", description="Player lookup tools"):

    @app_commands.command(name="search", description="Search for players by name and get their IDs")
    @app_commands.describe(name="Part of a player's name to search for (e.g. 'curry')")
    async def search(
        self,
        interaction: discord.Interaction,
        name: str,
    ) -> None:
        await safe_defer(interaction, ephemeral=True)
        pool = await get_pool()

        league_row = await pool.fetchrow(
            "SELECT id FROM leagues WHERE discord_guild_id = $1 AND archived_at IS NULL",
            interaction.guild_id,
        )
        if not league_row:
            await safe_respond(interaction, content="No active league.", ephemeral=True)
            return

        league_id: int = league_row["id"]
        matches = await player_repo.search_by_name(pool, league_id, name)
        matches = matches[:10]

        if not matches:
            await safe_respond(
                interaction,
                content=f"No players found matching '{name}'.",
                ephemeral=True,
            )
            return

        # Collect team codes for all matched players in one query.
        team_ids = {p.team_id for p in matches if p.team_id}
        team_code_by_id: dict[int, str] = {}
        if team_ids:
            rows = await pool.fetch(
                "SELECT id, nba_team_code FROM teams WHERE id = ANY($1::int[])",
                list(team_ids),
            )
            for row in rows:
                team_code_by_id[row["id"]] = row["nba_team_code"]

        lines = []
        for p in matches:
            team_label = team_code_by_id.get(p.team_id, "FA") if p.team_id else "FA"
            lines.append(
                f"{p.full_name} · {team_label} · {p.position} · OVR {p.overall} · ID: {p.id}"
            )

        separator = "─" * 22
        description = f"{separator}\n" + "\n".join(lines)

        embed = discord.Embed(
            title=f'Player Search: "{name}"',
            description=description,
            color=discord.Color.blurple(),
        )
        await safe_respond(interaction, embed=embed)


class PlayerCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.bot.tree.add_command(PlayerGroup())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PlayerCog(bot))
