from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.embeds import fa_embeds
from core.errors import safe_defer, safe_respond
from core.logging import get_logger
from data.db import get_pool
from data.repositories import league_repo, player_repo, team_repo
from services import fa_service, league_service

log = get_logger(__name__)


class FAGroup(app_commands.Group, name="fa", description="Free agency commands"):

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot

    @app_commands.command(name="open", description="Open free agency (commissioner only)")
    @app_commands.default_permissions(administrator=True)
    async def fa_open(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction)

        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await safe_respond(interaction, content="No active league found.", ephemeral=True)
            return

        state = await fa_service.open_fa(league.id, league.current_season)

        embed = fa_embeds.fa_day_open_embed(state, state["current_day"], league)
        await safe_respond(interaction, embed=embed)

        pool = await get_pool()
        news_channel_id = await league_repo.get_channel(pool, league.id, "league-news")
        if news_channel_id:
            channel = interaction.guild.get_channel(news_channel_id)
            if channel and channel.id != interaction.channel_id:
                await channel.send(embed=embed)

    @app_commands.command(name="offer", description="Submit a free-agent offer (team manager only)")
    @app_commands.describe(
        player="Player name to offer (e.g. LeBron James)",
        salary="Annual salary in dollars (e.g. 10000000)",
        years="Contract length in years",
    )
    async def fa_offer(
        self,
        interaction: discord.Interaction,
        player: str,
        salary: int,
        years: int,
    ) -> None:
        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await interaction.response.send_message("No active league found.", ephemeral=True)
            return

        pool = await get_pool()
        team = await team_repo.get_by_manager(pool, league.id, interaction.user.id)
        if not team:
            await interaction.response.send_message(
                "You are not a team manager in this league.", ephemeral=True
            )
            return

        if years < 1 or years > 5:
            await interaction.response.send_message(
                "Contract length must be between 1 and 5 years.", ephemeral=True
            )
            return

        if salary < 1_000_000:
            await interaction.response.send_message(
                "Salary must be at least $1,000,000.", ephemeral=True
            )
            return

        matches = await player_repo.search_by_name(pool, league.id, player)
        fa_matches = [p for p in matches if p.team_id is None]
        if not fa_matches:
            await interaction.response.send_message(
                f"No free agent found matching '{player}'.", ephemeral=True
            )
            return
        if len(fa_matches) > 1:
            names = ", ".join(p.full_name for p in fa_matches)
            await interaction.response.send_message(
                f"Multiple free agents match '{player}': {names}. Be more specific.", ephemeral=True
            )
            return
        found_player = fa_matches[0]

        offer_id = await fa_service.submit_offer(
            league_id=league.id,
            season=league.current_season,
            team_id=team.id,
            player_id=found_player.id,
            salary=salary,
            years=years,
        )

        await interaction.response.send_message(
            f"Offer submitted (ID: {offer_id}): ${salary:,}/yr × {years}yr to **{found_player.full_name}**.",
            ephemeral=True,
        )

    @app_commands.command(name="advance", description="Process today's responses and advance to next FA day (commissioner only)")
    @app_commands.default_permissions(administrator=True)
    async def fa_advance(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction)

        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await safe_respond(interaction, content="No active league found.", ephemeral=True)
            return

        decisions = await fa_service.advance_to_responses(
            league.id, league.current_season, interaction.guild
        )

        responses_embed = fa_embeds.fa_responses_embed(decisions)
        await safe_respond(interaction, embed=responses_embed)

        new_state = await fa_service.advance_day(league.id, league.current_season)

        if new_state.get("phase") == "complete":
            closed_embed = fa_embeds.fa_closed_embed(
                signed_count=sum(1 for d in decisions if d["decision"] == "signed"),
                waived_count=new_state.get("waived_count", 0),
                retired_count=new_state.get("retired_count", 0),
            )
            pool = await get_pool()
            news_channel_id = await league_repo.get_channel(pool, league.id, "league-news")
            if news_channel_id:
                channel = interaction.guild.get_channel(news_channel_id)
                if channel:
                    await channel.send(embed=closed_embed)
        else:
            day_embed = fa_embeds.fa_day_open_embed(new_state, new_state["current_day"], league)
            pool = await get_pool()
            news_channel_id = await league_repo.get_channel(pool, league.id, "league-news")
            if news_channel_id:
                channel = interaction.guild.get_channel(news_channel_id)
                if channel:
                    await channel.send(embed=day_embed)

    @app_commands.command(name="counter-accept", description="Accept a player's counter offer (team manager only)")
    @app_commands.describe(counter_id="Counter offer ID")
    async def counter_accept(self, interaction: discord.Interaction, counter_id: int) -> None:
        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await interaction.response.send_message("No active league found.", ephemeral=True)
            return

        pool = await get_pool()
        team = await team_repo.get_by_manager(pool, league.id, interaction.user.id)
        if not team:
            await interaction.response.send_message(
                "You are not a team manager in this league.", ephemeral=True
            )
            return

        result = await fa_service.respond_to_counter(
            league_id=league.id, counter_id=counter_id, team_id=team.id, accept=True
        )
        await interaction.response.send_message(
            f"Counter accepted — **{result['player_name']}** has been signed.", ephemeral=True
        )

    @app_commands.command(name="counter-decline", description="Decline a player's counter offer (team manager only)")
    @app_commands.describe(counter_id="Counter offer ID")
    async def counter_decline(self, interaction: discord.Interaction, counter_id: int) -> None:
        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await interaction.response.send_message("No active league found.", ephemeral=True)
            return

        pool = await get_pool()
        team = await team_repo.get_by_manager(pool, league.id, interaction.user.id)
        if not team:
            await interaction.response.send_message(
                "You are not a team manager in this league.", ephemeral=True
            )
            return

        result = await fa_service.respond_to_counter(
            league_id=league.id, counter_id=counter_id, team_id=team.id, accept=False
        )
        await interaction.response.send_message(
            f"Counter declined for **{result['player_name']}**.", ephemeral=True
        )

    @app_commands.command(name="close", description="Manually close free agency (commissioner only)")
    @app_commands.default_permissions(administrator=True)
    async def fa_close(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction)

        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await safe_respond(interaction, content="No active league found.", ephemeral=True)
            return

        summary = await fa_service.close_fa(league.id, league.current_season)
        embed = fa_embeds.fa_closed_embed(
            signed_count=summary.get("signed_count", 0),
            waived_count=summary["waived_count"],
            retired_count=summary["retired_count"],
        )
        await safe_respond(interaction, embed=embed)

        pool = await get_pool()
        news_channel_id = await league_repo.get_channel(pool, league.id, "league-news")
        if news_channel_id:
            channel = interaction.guild.get_channel(news_channel_id)
            if channel and channel.id != interaction.channel_id:
                await channel.send(embed=embed)


class WaiverGroup(app_commands.Group, name="waiver", description="Waiver wire commands"):

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot

    @app_commands.command(name="claim", description="Claim a waived player (team manager only)")
    @app_commands.describe(player="Player name to claim off waivers")
    async def claim(self, interaction: discord.Interaction, player: str) -> None:
        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await interaction.response.send_message("No active league found.", ephemeral=True)
            return

        pool = await get_pool()
        team = await team_repo.get_by_manager(pool, league.id, interaction.user.id)
        if not team:
            await interaction.response.send_message(
                "You are not a team manager in this league.", ephemeral=True
            )
            return

        matches = await player_repo.search_by_name(pool, league.id, player)
        waiver_matches = [p for p in matches if p.team_id is None]
        if not waiver_matches:
            await interaction.response.send_message(
                f"No waived player found matching '{player}'.", ephemeral=True
            )
            return
        if len(waiver_matches) > 1:
            names = ", ".join(p.full_name for p in waiver_matches)
            await interaction.response.send_message(
                f"Multiple matches for '{player}': {names}. Be more specific.", ephemeral=True
            )
            return
        found_player = waiver_matches[0]

        await fa_service.claim_waiver(league.id, team.id, found_player.id)
        await interaction.response.send_message(
            f"**{found_player.full_name}** claimed off waivers and signed at the minimum ($1,100,000 / 1 yr).",
            ephemeral=True,
        )


class FACog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.bot.tree.add_command(FAGroup(bot))
        self.bot.tree.add_command(WaiverGroup(bot))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FACog(bot))
