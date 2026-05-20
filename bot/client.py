import discord
from discord import app_commands
from discord.ext import commands
from core.logging import get_logger
from core.errors import handle_app_command_error
from data.db import get_pool, close_pool

log = get_logger(__name__)

# Tracks interaction ids the tree has already accepted. Symptoms observed
# across the 2026-05-18 session: a single user click on /sim rivalry produced
# both a "Waiting for managers to /ready" rejection AND ran the sim (counter
# advanced in bot.log, date moved). One callback invocation can't take both
# branches — so the dispatcher is invoking the callback twice per interaction.
# Prior attempt assigned this check to self.tree.interaction_check as an
# instance attribute; that path never logged a DEDUPE warning, suggesting
# discord.py's bound-method resolution didn't pick up the attribute. This
# subclass overrides interaction_check as a proper method on the tree.
_handled_interactions: set[int] = set()
_MAX_HANDLED = 5000


class DedupeTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        log.info(
            f"IC: id={interaction.id} type={interaction.type} "
            f"cmd={getattr(interaction.command, 'qualified_name', '?')} "
            f"user={interaction.user.id}"
        )
        if interaction.id in _handled_interactions:
            log.warning(
                f"DEDUPE: interaction {interaction.id} (type={interaction.type}, "
                f"command={getattr(interaction.command, 'qualified_name', '?')}) "
                f"already handled — skipping duplicate dispatch"
            )
            return False
        _handled_interactions.add(interaction.id)
        if len(_handled_interactions) > _MAX_HANDLED:
            _handled_interactions.clear()
            _handled_interactions.add(interaction.id)
        return True

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
    "bot.cogs.coach_cog",
]


class DBABot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents, tree_cls=DedupeTree)
        self.tree.on_error = handle_app_command_error

    async def setup_hook(self) -> None:
        pool = await get_pool()

        # Validate that PHILOSOPHY_BIASES and ROLE_REGISTRY keys are covered by
        # their respective DB CHECK constraints.  Drift (e.g. a new entry added
        # without a migration) is caught here before it reaches production.
        # Non-fatal: the bot continues so a missing constraint doesn't hard-crash,
        # but the error is logged loudly at ERROR level.
        try:
            from services import role_service as _rs
            async with pool.acquire() as _conn:
                await _rs.assert_philosophy_constraint_sync(_conn)
                await _rs.assert_role_constraint_sync(_conn)
        except RuntimeError as _exc:
            log.error(
                "CHECK constraint out of sync — create a migration. Detail: %s", _exc
            )
        except Exception as _exc:
            log.error("Constraint check failed unexpectedly: %s", _exc)

        for cog in COGS:
            await self.load_extension(cog)
            log.info(f"Loaded cog: {cog}")
        await self.tree.sync()
        log.info("Slash commands synced")
        all_cmds = list(self.tree.walk_commands())
        from collections import Counter
        name_counts = Counter(c.qualified_name for c in all_cmds)
        dups = {n: c for n, c in name_counts.items() if c > 1}
        log.info(f"Tree commands: total={len(all_cmds)}, unique={len(name_counts)}, duplicates={dups}")
        log.info(f"Tree class: {type(self.tree).__name__} (expected DedupeTree)")

    async def on_ready(self) -> None:
        log.info(f"DBA online as {self.user} ({self.user.id}) — on_ready FIRED (shard={self.shard_id}, latency={self.latency:.3f}s, ws_id={id(self.ws)})")
        await self.change_presence(activity=discord.Game(name="Basketball"))
        # Guild-specific sync is instant — ensures new commands show up immediately after every deploy
        for guild in self.guilds:
            try:
                await self.tree.sync(guild=guild)
                log.info(f"Synced commands to guild {guild.id}")
            except Exception as e:
                log.warning(f"Failed to sync commands to guild {guild.id}: {e}")


    async def close(self) -> None:
        await close_pool()
        await super().close()
