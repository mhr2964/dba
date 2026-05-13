import discord
from discord import app_commands
from discord.ext import commands
from core.logging import get_logger

log = get_logger(__name__)


class MetaCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="ping", description="Check bot latency")
    async def ping(self, interaction: discord.Interaction) -> None:
        latency = round(self.bot.latency * 1000)
        await interaction.response.send_message(
            f"Pong! `{latency}ms`",
            ephemeral=True,
        )

    @app_commands.command(name="about", description="About the DBA bot")
    async def about(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="Discord Basketball Association",
            description="A fully simulated NBA league. Claim a team, manage your roster, and compete for the championship.",
            color=discord.Color.orange(),
        )
        embed.add_field(name="Version", value="0.1.0 — Slice 0", inline=True)
        embed.set_footer(text="Use /league create to get started.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MetaCog(bot))
