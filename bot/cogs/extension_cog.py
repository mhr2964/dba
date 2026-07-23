"""Contract extension management slash commands (/extension offer, accept, ...).

Extracted from roster_cog.py (Phase 3 opportunistic cog split, see
HANDOFF.md) -- ExtensionGroup was already an independent top-level
group, registered directly on the bot tree by roster_cog.py's setup()
rather than composed into RosterCog, and had zero dependency on
RosterCog/LineupGroup's module-level helpers -- registered as its own
extension.
"""
from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.embeds import strategy_embeds
from core.errors import safe_defer, safe_respond
from core.logging import get_logger
from data.db import get_pool
from data.repositories import extension_repo, player_repo

log = get_logger(__name__)


class ExtensionGroup(app_commands.Group, name="extension", description="Contract extension management"):

    @app_commands.command(name="offer", description="Offer a contract extension to a player on your roster")
    @app_commands.describe(
        player="Player name (e.g. Luka Doncic)",
        years="New contract length in years",
        salary="Annual salary in dollars",
    )
    async def offer(
        self,
        interaction: discord.Interaction,
        player: str,
        years: int,
        salary: int,
    ) -> None:
        await safe_defer(interaction)
        pool = await get_pool()

        league_row = await pool.fetchrow(
            "SELECT id, current_season, current_phase, salary_cap FROM leagues WHERE discord_guild_id = $1 AND archived_at IS NULL",
            interaction.guild_id,
        )
        if not league_row:
            await safe_respond(interaction, content="No active league found.", ephemeral=True)
            return

        league_id: int = league_row["id"]
        current_season: int = league_row["current_season"]
        current_phase: str = league_row["current_phase"]
        salary_cap: int = league_row["salary_cap"]

        if current_phase not in ("REGULAR_SEASON_ACTIVE", "REGULAR_SEASON_POSTDEADLINE"):
            await safe_respond(
                interaction,
                content="Extensions can only be offered during the regular season.",
                ephemeral=True,
            )
            return

        team_row = await pool.fetchrow(
            "SELECT * FROM teams WHERE league_id = $1 AND manager_user_id = $2",
            league_id,
            interaction.user.id,
        )
        if not team_row:
            await safe_respond(interaction, content="You are not a team manager in this league.", ephemeral=True)
            return

        matches = await player_repo.search_by_name(pool, league_id, player)
        matches = [p for p in matches if p.team_id == team_row["id"]]
        if not matches:
            await safe_respond(interaction, content=f"No player matching '{player}' found on your roster.", ephemeral=True)
            return
        if len(matches) > 1:
            names = ", ".join(p.full_name for p in matches)
            await safe_respond(interaction, content=f"Multiple matches for '{player}': {names}. Be more specific.", ephemeral=True)
            return
        found_player = matches[0]
        player_id = found_player.id

        existing = await extension_repo.get_extension(pool, league_id, player_id)
        if existing:
            await safe_respond(
                interaction,
                content=f"**{found_player.full_name}** already has a pending extension. Cancel it first.",
                ephemeral=True,
            )
            return

        max_salary = int(salary_cap * 0.35)
        if salary > max_salary:
            await safe_respond(
                interaction,
                content=f"Salary ${salary:,} exceeds the max contract value (${max_salary:,}/yr = 35% of cap).",
                ephemeral=True,
            )
            return

        if years < 1 or years > 5:
            await safe_respond(interaction, content="Extension length must be between 1 and 5 years.", ephemeral=True)
            return

        # Check that this extension won't push the team over cap when it activates.
        # We check against current cap usage excluding the player's own contract since it will be superseded.
        current_contract = await player_repo.get_active_contract(pool, player_id)
        cap_used = await player_repo.get_team_cap_usage(pool, league_id, team_row["id"])
        own_salary = current_contract.salary if current_contract else 0
        projected_cap = cap_used - own_salary + salary
        if projected_cap > salary_cap:
            await safe_respond(
                interaction,
                content=f"This extension would put the team ${projected_cap - salary_cap:,} over the cap when it activates.",
                ephemeral=True,
            )
            return

        years_remaining = current_contract.years_remaining if current_contract else 0
        activates_after = current_season + years_remaining

        ext_id = await extension_repo.create_extension(
            pool,
            league_id=league_id,
            player_id=player_id,
            team_id=team_row["id"],
            salary=salary,
            years=years,
            season=current_season,
            activates_after=activates_after,
        )
        log.info(
            f"Extension offered: player={player_id} team={team_row['id']} "
            f"salary={salary} years={years} activates_after={activates_after} id={ext_id}"
        )

        ext_dict = {
            "id": ext_id,
            "new_salary": salary,
            "new_years": years,
            "signed_in_season": current_season,
            "activates_after_season": activates_after,
        }
        player_dict = {"full_name": found_player.full_name, "overall": found_player.overall}
        embed = strategy_embeds.extension_embed(player_dict, ext_dict, team_row)
        await safe_respond(interaction, embed=embed)

    @app_commands.command(name="view", description="View pending contract extensions for a team")
    @app_commands.describe(team="Team code (e.g. LAL). Defaults to your team.")
    async def view(self, interaction: discord.Interaction, team: Optional[str] = None) -> None:
        await safe_defer(interaction)
        pool = await get_pool()

        league_row = await pool.fetchrow(
            "SELECT id FROM leagues WHERE discord_guild_id = $1 AND archived_at IS NULL",
            interaction.guild_id,
        )
        if not league_row:
            await safe_respond(interaction, content="No active league found.", ephemeral=True)
            return
        league_id: int = league_row["id"]

        if team:
            team_row = await pool.fetchrow(
                "SELECT * FROM teams WHERE league_id = $1 AND UPPER(nba_team_code) = UPPER($2)",
                league_id,
                team.upper(),
            )
            if not team_row:
                await safe_respond(interaction, content=f"Team `{team.upper()}` not found.", ephemeral=True)
                return
        else:
            team_row = await pool.fetchrow(
                "SELECT * FROM teams WHERE league_id = $1 AND manager_user_id = $2",
                league_id,
                interaction.user.id,
            )
            if not team_row:
                await safe_respond(
                    interaction,
                    content="You don't manage a team. Provide a team code to view another team's extensions.",
                    ephemeral=True,
                )
                return

        extensions = await extension_repo.get_team_extensions(pool, league_id, team_row["id"])

        players_by_id: dict[int, dict] = {}
        for ext in extensions:
            p = await player_repo.get_by_id(pool, ext["player_id"])
            if p:
                players_by_id[p.id] = {"full_name": p.full_name}

        embed = strategy_embeds.extension_list_embed(extensions, players_by_id)
        await safe_respond(interaction, embed=embed)

    @app_commands.command(name="cancel", description="Cancel a pending contract extension")
    @app_commands.describe(player="Player name whose extension to cancel")
    async def cancel(self, interaction: discord.Interaction, player: str) -> None:
        await safe_defer(interaction)
        pool = await get_pool()

        league_row = await pool.fetchrow(
            "SELECT id, commissioner_user_id FROM leagues WHERE discord_guild_id = $1 AND archived_at IS NULL",
            interaction.guild_id,
        )
        if not league_row:
            await safe_respond(interaction, content="No active league found.", ephemeral=True)
            return
        league_id: int = league_row["id"]

        matches = await player_repo.search_by_name(pool, league_id, player)
        if not matches:
            await safe_respond(interaction, content=f"No player found matching '{player}'.", ephemeral=True)
            return
        if len(matches) > 1:
            names = ", ".join(p.full_name for p in matches)
            await safe_respond(interaction, content=f"Multiple matches for '{player}': {names}. Be more specific.", ephemeral=True)
            return
        found_player = matches[0]
        player_id = found_player.id

        ext = await extension_repo.get_extension(pool, league_id, player_id)
        if not ext:
            await safe_respond(interaction, content=f"No pending extension found for **{found_player.full_name}**.", ephemeral=True)
            return

        is_commissioner = interaction.user.id == league_row["commissioner_user_id"]
        team_row = await pool.fetchrow(
            "SELECT * FROM teams WHERE league_id = $1 AND manager_user_id = $2",
            league_id,
            interaction.user.id,
        )
        is_team_manager = team_row is not None and team_row["id"] == ext["team_id"]

        if not is_commissioner and not is_team_manager:
            await safe_respond(
                interaction,
                content="Only the team manager or commissioner can cancel an extension.",
                ephemeral=True,
            )
            return

        await extension_repo.cancel_extension(pool, league_id, player_id)
        log.info(f"Extension cancelled: player={player_id} league={league_id} by user={interaction.user.id}")
        await safe_respond(interaction, content=f"Extension for **{found_player.full_name}** has been cancelled.")


class ExtensionCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.bot.tree.add_command(ExtensionGroup())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ExtensionCog(bot))
