"""Team management slash commands (/team assign, release, philosophy, ...).

Extracted from setup_cog.py (Phase 3 opportunistic cog split, see
HANDOFF.md) -- TeamGroup had zero dependency on anything LeagueGroup-only,
so it's registered as its own extension rather than staying composed
into SetupCog.
"""
from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.embeds import intel_embeds, league_embeds
from core.errors import safe_defer, safe_respond
from core.logging import get_logger
from data.db import get_pool
from data.repositories import game_repo, league_repo, team_repo
from services import league_service, team_intel

log = get_logger(__name__)


class TeamGroup(app_commands.Group, name="team", description="Team management commands"):

    @app_commands.command(name="assign", description="Assign a manager to a team")
    @app_commands.describe(
        team_code="NBA team code (e.g. LAL)",
        user="Discord member to assign (omit to assign yourself)",
        user_id="Discord user ID (alternative to mention)",
    )
    @app_commands.default_permissions(administrator=True)
    async def assign(
        self,
        interaction: discord.Interaction,
        team_code: str,
        user: Optional[discord.Member] = None,
        user_id: Optional[str] = None,
    ) -> None:
        await safe_defer(interaction)

        member = user
        if member is None and user_id is not None:
            try:
                member = interaction.guild.get_member(int(user_id))
            except ValueError:
                await safe_respond(
                    interaction,
                    content="That doesn't look like a valid Discord user ID.",
                    ephemeral=True,
                )
                return
        if member is None:
            member = interaction.user

        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await safe_respond(interaction, content="No active league. Use `/league create` first.", ephemeral=True)
            return

        team = await league_service.assign_manager(
            guild=interaction.guild,
            league=league,
            team_code=team_code,
            user=member,
        )

        embed = league_embeds.team_assigned(team, member)
        await safe_respond(interaction, embed=embed)

        pool = await get_pool()
        news_channel_id = await league_repo.get_channel(pool, league.id, "league-news")
        if news_channel_id:
            channel = interaction.guild.get_channel(news_channel_id)
            if channel and channel.id != interaction.channel_id:
                await channel.send(
                    f"📋 {member.mention} has claimed the **{team.full_name}** (`{team.nba_team_code}`)!"
                )

    @app_commands.command(name="remove", description="Remove a manager from a team")
    @app_commands.describe(team_code="NBA team code (e.g. LAL)")
    @app_commands.default_permissions(administrator=True)
    async def remove(
        self,
        interaction: discord.Interaction,
        team_code: str,
    ) -> None:
        await safe_defer(interaction)

        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await safe_respond(interaction, content="No active league.", ephemeral=True)
            return

        team = await league_service.remove_manager(
            guild=interaction.guild,
            league=league,
            team_code=team_code,
            requester=interaction.user,
        )

        embed = league_embeds.manager_removed(team)
        await safe_respond(interaction, embed=embed)

        pool = await get_pool()
        news_channel_id = await league_repo.get_channel(pool, league.id, "league-news")
        if news_channel_id:
            channel = interaction.guild.get_channel(news_channel_id)
            if channel and channel.id != interaction.channel_id:
                await channel.send(
                    f"🔄 The **{team.full_name}** (`{team.nba_team_code}`) is now CPU controlled."
                )

    @app_commands.command(name="list", description="Show all 30 teams with manager status")
    async def list(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction)
        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await safe_respond(interaction, content="No active league.", ephemeral=True)
            return

        pool = await get_pool()
        teams = await team_repo.get_all(pool, league.id)

        east = [t for t in teams if t.conference == "East"]
        west = [t for t in teams if t.conference == "West"]

        def team_line(t: team_repo.Team) -> str:
            if t.manager_user_id:
                return f"`{t.nba_team_code}` {t.full_name} — <@{t.manager_user_id}>"
            return f"`{t.nba_team_code}` {t.full_name} — CPU ({t.cpu_mode})"

        embed = discord.Embed(title=f"🏀 {league.name} — All Teams", color=discord.Color.orange())
        embed.add_field(
            name="Eastern Conference",
            value="\n".join(team_line(t) for t in east),
            inline=False,
        )
        embed.add_field(
            name="Western Conference",
            value="\n".join(team_line(t) for t in west),
            inline=False,
        )

        claimed = sum(1 for t in teams if t.manager_user_id is not None)
        embed.set_footer(text=f"{claimed} / 30 teams claimed")
        await safe_respond(interaction, embed=embed)

    @app_commands.command(name="rename", description="Rename a team's city and name")
    @app_commands.describe(
        name="New team name (max 20 chars)",
        city="New city name (max 20 chars)",
        team_code="Team code — commissioner only, omit to rename your own team",
    )
    async def rename(
        self,
        interaction: discord.Interaction,
        name: str,
        city: str,
        team_code: str | None = None,
    ) -> None:
        await safe_defer(interaction)

        if len(name) > 20:
            await safe_respond(interaction, content="Team name must be 20 characters or fewer.", ephemeral=True)
            return
        if len(city) > 20:
            await safe_respond(interaction, content="City name must be 20 characters or fewer.", ephemeral=True)
            return

        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await safe_respond(interaction, content="No active league in this server.", ephemeral=True)
            return

        pool = await get_pool()

        if team_code:
            if interaction.user.id != league.commissioner_user_id:
                await safe_respond(
                    interaction,
                    content="Only the commissioner can rename another team.",
                    ephemeral=True,
                )
                return
            target_team = await team_repo.get_by_code(pool, league.id, team_code.upper())
            if not target_team:
                await safe_respond(
                    interaction,
                    content=f"No team found with code **{team_code.upper()}**.",
                    ephemeral=True,
                )
                return
        else:
            target_team = await team_repo.get_by_manager(pool, league.id, interaction.user.id)
            if not target_team:
                await safe_respond(
                    interaction,
                    content="You don't manage a team in this league.",
                    ephemeral=True,
                )
                return

        old_full_name = target_team.full_name
        await team_repo.rename_team(pool, target_team.id, name, city)

        role_id = await league_repo.get_team_role(pool, league.id, target_team.id)
        if role_id:
            role = interaction.guild.get_role(role_id)
            if role:
                try:
                    await role.edit(name=f"{city} {name}", reason="DBA: team rename")
                except discord.Forbidden:
                    log.warning(f"Could not rename Discord role {role_id} for team {target_team.id}")

        new_full_name = f"{city} {name}"
        news_channel_id = await league_repo.get_channel(pool, league.id, "league-news")
        if news_channel_id:
            ch = interaction.guild.get_channel(news_channel_id)
            if ch:
                await ch.send(f"**{old_full_name}** has rebranded to **{new_full_name}**!")

        await safe_respond(
            interaction,
            content=f"Team renamed: **{old_full_name}** → **{new_full_name}**.",
        )

    # ------------------------------------------------------------------
    # Internal helper: resolve a team from optional code or caller's team
    # ------------------------------------------------------------------

    async def _resolve_intel_team(
        self,
        pool,
        league_id: int,
        user_id: int,
        team_code: str | None,
    ) -> dict | None:
        """Return team row dict or None if team_code given but not found."""
        if team_code:
            row = await pool.fetchrow(
                "SELECT * FROM teams WHERE league_id = $1 AND UPPER(nba_team_code) = UPPER($2)",
                league_id,
                team_code.upper(),
            )
            return dict(row) if row else None
        # Fall back to caller's team.
        row = await pool.fetchrow(
            "SELECT * FROM teams WHERE league_id = $1 AND manager_user_id = $2",
            league_id,
            user_id,
        )
        return dict(row) if row else None

    @app_commands.command(
        name="posture",
        description="View a team's current trade posture, franchise goal, and coach philosophy",
    )
    @app_commands.describe(code="Team code (e.g. LAL). Defaults to your team.")
    async def posture(
        self,
        interaction: discord.Interaction,
        code: str | None = None,
    ) -> None:
        await safe_defer(interaction)

        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await safe_respond(interaction, content="No active league in this server.", ephemeral=True)
            return

        pool = await get_pool()
        team = await self._resolve_intel_team(pool, league.id, interaction.user.id, code)

        if team is None:
            if code:
                await safe_respond(
                    interaction,
                    content=f"Team `{code.upper()}` not found in this league.",
                    ephemeral=True,
                )
            else:
                await safe_respond(
                    interaction,
                    content="You don't manage a team. Use `/team posture LAL` to view any team.",
                    ephemeral=True,
                )
            return

        team_id: int = team["id"]
        season: int = league.current_season

        try:
            posture_data = await team_intel.compute_posture(pool, league, team_id)
            plan = await team_intel.get_team_plan(pool, league.id, team_id, season)
            philosophy = await team_intel.get_team_philosophy(pool, team_id)
            recent_pivots = await team_intel.get_recent_pivots(pool, league.id, team_id, season)
        except Exception as exc:
            log.error(f"team posture fetch failed for team {team_id}: {exc}", exc_info=True)
            await safe_respond(
                interaction,
                content="Failed to compute posture — the data may not be ready yet.",
                ephemeral=True,
            )
            return

        team_name = f"{team['city']} {team['name']}"
        embed = intel_embeds.posture_embed(
            team_name=team_name,
            posture=posture_data,
            plan=plan,
            philosophy=philosophy,
            recent_pivots=recent_pivots,
        )
        await safe_respond(interaction, embed=embed)

    @app_commands.command(
        name="plan",
        description="View a team's full franchise plan: goal, asset targets, core/flex/surplus buckets",
    )
    @app_commands.describe(code="Team code (e.g. LAL). Defaults to your team.")
    async def plan(
        self,
        interaction: discord.Interaction,
        code: str | None = None,
    ) -> None:
        await safe_defer(interaction)

        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await safe_respond(interaction, content="No active league in this server.", ephemeral=True)
            return

        pool = await get_pool()
        team = await self._resolve_intel_team(pool, league.id, interaction.user.id, code)

        if team is None:
            if code:
                await safe_respond(
                    interaction,
                    content=f"Team `{code.upper()}` not found in this league.",
                    ephemeral=True,
                )
            else:
                await safe_respond(
                    interaction,
                    content="You don't manage a team. Use `/team plan LAL` to view any team.",
                    ephemeral=True,
                )
            return

        team_id: int = team["id"]

        try:
            plan = await team_intel.get_team_plan(pool, league.id, team_id, league.current_season)
        except Exception as exc:
            log.error(f"team plan fetch failed for team {team_id}: {exc}", exc_info=True)
            await safe_respond(
                interaction,
                content="Failed to retrieve the franchise plan.",
                ephemeral=True,
            )
            return

        if plan is None:
            team_name = f"{team['city']} {team['name']}"
            await safe_respond(
                interaction,
                content=(
                    f"No franchise plan has been derived yet for **{team_name}**. "
                    "The plan is generated automatically after the season begins."
                ),
                ephemeral=True,
            )
            return

        team_name = f"{team['city']} {team['name']}"
        embed = intel_embeds.plan_embed(team_name=team_name, plan=plan)
        await safe_respond(interaction, embed=embed)

    @app_commands.command(
        name="philosophy",
        description="View a team's coach philosophy and its effect on role assignments",
    )
    @app_commands.describe(code="Team code (e.g. LAL). Defaults to your team.")
    async def philosophy(
        self,
        interaction: discord.Interaction,
        code: str | None = None,
    ) -> None:
        await safe_defer(interaction)

        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await safe_respond(interaction, content="No active league in this server.", ephemeral=True)
            return

        pool = await get_pool()
        team = await self._resolve_intel_team(pool, league.id, interaction.user.id, code)

        if team is None:
            if code:
                await safe_respond(
                    interaction,
                    content=f"Team `{code.upper()}` not found in this league.",
                    ephemeral=True,
                )
            else:
                await safe_respond(
                    interaction,
                    content="You don't manage a team. Use `/team philosophy LAL` to view any team.",
                    ephemeral=True,
                )
            return

        team_id: int = team["id"]

        try:
            philosophy = await team_intel.get_team_philosophy(pool, team_id)
            recent_role_changes = await team_intel.get_recent_role_changes(
                pool, league.id, league.current_season, team_id
            )
        except Exception as exc:
            log.error(f"team philosophy fetch failed for team {team_id}: {exc}", exc_info=True)
            await safe_respond(
                interaction,
                content="Failed to retrieve philosophy data.",
                ephemeral=True,
            )
            return

        team_name = f"{team['city']} {team['name']}"
        embed = intel_embeds.philosophy_embed(
            team_name=team_name,
            philosophy=philosophy,
            recent_role_changes=recent_role_changes,
        )
        await safe_respond(interaction, embed=embed)

    @app_commands.command(
        name="ready",
        description="Mark yourself as ready for the next matchup",
    )
    async def ready(self, interaction: discord.Interaction) -> None:
        from phase.helpers import require_phase as _require_phase
        await safe_defer(interaction, ephemeral=True)
        pool = await get_pool()
        league = await league_repo.get_by_guild(pool, interaction.guild_id)
        if not league:
            await safe_respond(interaction, content="No active league.", ephemeral=True)
            return

        await _require_phase(league, "ready")

        team = await team_repo.get_by_manager(pool, league.id, interaction.user.id)
        if not team:
            await safe_respond(
                interaction,
                content="You don't manage a team in this league.",
                ephemeral=True,
            )
            return

        await game_repo.set_ready(pool, league.id, team.id, interaction.user.id, True)
        await safe_respond(interaction, content="Ready.", ephemeral=True)

        teams = await team_repo.get_all(pool, league.id)
        human_teams = [t for t in teams if t.manager_user_id is not None]
        ready_ids = set(await game_repo.get_ready_teams(pool, league.id))
        if human_teams and all(t.id in ready_ids for t in human_teams):
            await interaction.channel.send(
                "All managers are ready! Commissioner can run `/sim to-next-rival` to continue."
            )


class TeamCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.bot.tree.add_command(TeamGroup())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TeamCog(bot))
