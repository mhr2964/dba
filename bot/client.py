import discord
from discord.ext import commands
from core.logging import get_logger
from core.errors import handle_app_command_error
from data.db import get_pool, close_pool

log = get_logger(__name__)

COGS = [
    "bot.cogs.meta_cog",
    "bot.cogs.setup_cog",
    "bot.cogs.roster_cog",
    "bot.cogs.sim_cog",
    "bot.cogs.trade_cog",
    "bot.cogs.playoff_cog",
    "bot.cogs.awards_cog",
    "bot.cogs.draft_cog",
    "bot.cogs.season_cog",
    "bot.cogs.offseason_cog",
    "bot.cogs.fa_cog",
    "bot.cogs.stats_cog",
    "bot.cogs.strategy_cog",
    "bot.cogs.admin_cog",
    "bot.cogs.directive_cog",
]


class DBABot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.tree.on_error = handle_app_command_error

    async def setup_hook(self) -> None:
        await get_pool()
        for cog in COGS:
            await self.load_extension(cog)
            log.info(f"Loaded cog: {cog}")
        await self.tree.sync()
        log.info("Slash commands synced")

    async def on_ready(self) -> None:
        log.info(f"DBA online as {self.user} ({self.user.id})")
        await self.change_presence(activity=discord.Game(name="Basketball"))

    async def close(self) -> None:
        await close_pool()
        await super().close()
