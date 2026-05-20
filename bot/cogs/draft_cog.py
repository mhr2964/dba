from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.embeds import draft_embeds
from bot.ui.draft_views import DraftPickView, _build_summary
from core.errors import safe_defer
from core.logging import get_logger
from data.db import get_pool
from data.repositories import draft_repo, league_repo
from services import draft_service, league_service

log = get_logger(__name__)


class DraftGroup(app_commands.Group, name="draft", description="Draft management commands"):

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot

    @app_commands.command(name="lottery", description="Run the draft lottery (commissioner only)")
    @app_commands.default_permissions(administrator=True)
    async def lottery(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction)

        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await interaction.followup.send("No active league found.", ephemeral=True)
            return

        pool = await get_pool()
        await draft_repo.create_draft(pool, league.id, league.current_season)

        ordered_picks = await draft_service.run_lottery(league.id, league.current_season)

        embed = draft_embeds.lottery_result_embed(ordered_picks)
        await interaction.followup.send(embed=embed)

        news_channel_id = await league_repo.get_channel(pool, league.id, "league-news")
        if news_channel_id:
            channel = interaction.guild.get_channel(news_channel_id)
            if channel and channel.id != interaction.channel_id:
                await channel.send(embed=embed)

    @app_commands.command(name="advance", description="Advance the draft (commissioner only)")
    @app_commands.default_permissions(administrator=True)
    async def advance(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction)

        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await interaction.followup.send("No active league found.", ephemeral=True)
            return

        pool = await get_pool()
        await draft_service.ensure_draft_class(league.id, league.current_season)

        state = await draft_service.advance_pick(league.id, league.current_season, interaction.guild, bot=self.bot)

        if state["status"] == "complete":
            draft = await draft_repo.get_draft(pool, league.id, league.current_season)
            selections = await draft_repo.get_selections(pool, draft.id)
            summary = await _build_summary(pool, selections)
            embed = draft_embeds.draft_complete_embed(summary)
            await interaction.followup.send(embed=embed)
            return

        if state["status"] == "human_on_clock":
            team = state["team"]
            view = DraftPickView(
                league_id=league.id,
                season=league.current_season,
                team_id=team.id,
                prospects=state["prospects"],
                bot=self.bot,
            )
            embed = draft_embeds.on_the_clock_embed(team, state["pick_number"], state["prospects"])

            news_channel_id = await league_repo.get_channel(pool, league.id, "league-news")
            if news_channel_id:
                channel = interaction.guild.get_channel(news_channel_id)
                if channel:
                    await channel.send(embed=embed, view=view)

            await interaction.followup.send(
                f"Pick #{state['pick_number']} posted to league news.", ephemeral=True
            )

    @app_commands.command(name="board", description="Show the current draft board")
    async def board(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction, ephemeral=True)

        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await interaction.followup.send("No active league found.", ephemeral=True)
            return

        pool = await get_pool()
        draft = await draft_repo.get_draft(pool, league.id, league.current_season)
        if not draft:
            await interaction.followup.send("No draft found for this season.", ephemeral=True)
            return

        selections = await draft_repo.get_selections(pool, draft.id)
        if not selections:
            await interaction.followup.send("No picks have been made yet.", ephemeral=True)
            return

        summary = await _build_summary(pool, selections)
        embed = draft_embeds.draft_complete_embed(summary)
        embed.title = f"Draft Board — Season {league.current_season} (Pick {draft.current_pick_number - 1} of ?)"
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="prospects", description="Show available draft prospects")
    @app_commands.describe(position="Filter by position (PG, SG, SF, PF, C)")
    async def prospects(
        self, interaction: discord.Interaction, position: Optional[str] = None
    ) -> None:
        await safe_defer(interaction, ephemeral=True)

        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await interaction.followup.send("No active league found.", ephemeral=True)
            return

        pool = await get_pool()
        available = await draft_repo.get_available_prospects(pool, league.id, league.current_season)

        if position:
            pos = position.upper()
            available = [p for p in available if p["position"] == pos]

        if not available:
            label = f" at {position.upper()}" if position else ""
            await interaction.followup.send(f"No available prospects{label}.", ephemeral=True)
            return

        top = available[:20]
        lines = [
            f"**{i + 1}.** {p['first_name']} {p['last_name']} ({p['position']}) — OVR {p['overall']}"
            for i, p in enumerate(top)
        ]

        embed = discord.Embed(
            title="Available Draft Prospects",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        pos_label = f" — {position.upper()}" if position else ""
        embed.set_footer(text=f"Showing top {len(top)} of {len(available)} available{pos_label}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="class", description="Generate or show the draft class for a year (commissioner only)")
    @app_commands.describe(year="Draft class year (e.g. 2029)")
    @app_commands.default_permissions(administrator=True)
    async def draft_class(self, interaction: discord.Interaction, year: int) -> None:
        await safe_defer(interaction, ephemeral=True)

        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await interaction.followup.send("No active league found.", ephemeral=True)
            return

        count = await draft_service.ensure_draft_class(league.id, year)

        pool = await get_pool()
        top_prospects = await draft_repo.get_available_prospects(pool, league.id, year)

        lines = [
            f"**{i + 1}.** {p['first_name']} {p['last_name']} ({p['position']}) — OVR {p['overall']}"
            for i, p in enumerate(top_prospects[:15])
        ]

        embed = discord.Embed(
            title=f"{year} Draft Class",
            description="\n".join(lines) or "No prospects found.",
            color=discord.Color.purple(),
        )
        embed.set_footer(text=f"{count} total prospects in this class")
        await interaction.followup.send(embed=embed, ephemeral=True)


class DraftCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.bot.tree.add_command(DraftGroup(bot))

    async def cog_load(self) -> None:
        """Re-register any active draft views after bot restart.

        discord.py needs a view instance in the store for each stable custom_id
        so that button/select interactions received after a restart are routed
        correctly. The placeholder view carries no real state — the actual pick
        validation is done inside _ProspectSelect.callback at interaction time.
        """
        pool = await get_pool()
        rows = await pool.fetch(
            "SELECT league_id, season FROM drafts WHERE status = 'in_progress'"
        )
        for row in rows:
            view = DraftPickView(
                league_id=row["league_id"],
                season=row["season"],
                team_id=0,
                prospects=[],
                bot=self.bot,
            )
            self.bot.add_view(view)
            log.info(
                f"cog_load: re-registered DraftPickView for league {row['league_id']} season {row['season']}"
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DraftCog(bot))
