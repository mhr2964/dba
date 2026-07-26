from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from bot.cogs.admin_data_ops import (
    edit_player,
    force_fa_sign,
    purge_server,
    recalculate_standings,
    reseed_rosters,
    rollback_game,
)
from core.errors import safe_defer, safe_respond
from core.logging import get_logger
from data.db import get_pool
from data.repositories import admin_repo, game_repo, league_repo, team_repo
from phase.helpers import get_league_or_error, require_commissioner
from services import import_service, league_service, roster_seed_service

log = get_logger(__name__)


class AdminGroup(app_commands.Group, name="admin", description="Commissioner admin tools"):

    def __init__(self) -> None:
        super().__init__()
        self.add_command(edit_player)
        self.add_command(rollback_game)
        self.add_command(recalculate_standings)
        self.add_command(force_fa_sign)
        self.add_command(reseed_rosters)
        self.add_command(purge_server)

    @app_commands.command(
        name="ensure-channels",
        description="Commissioner: create any missing DBA channels for this league",
    )
    async def ensure_channels(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction, ephemeral=True)

        league = await get_league_or_error(interaction.guild_id)
        await require_commissioner(interaction, league)

        pool = await get_pool()
        created = await league_service.ensure_channels(interaction.guild, pool, league.id)

        if created:
            await safe_respond(
                interaction,
                content=f"Created {len(created)} missing channel(s): {', '.join(f'#{c}' for c in created)}",
                ephemeral=True,
            )
        else:
            await safe_respond(
                interaction,
                content="All expected channels already exist — nothing to do.",
                ephemeral=True,
            )


    @app_commands.command(
        name="rebuild-lineups",
        description="Commissioner: regenerate starting lineups for all teams (top-OVR ordering)",
    )
    async def rebuild_lineups(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction, ephemeral=True)

        league = await get_league_or_error(interaction.guild_id)
        await require_commissioner(interaction, league)

        pool = await get_pool()
        teams = await team_repo.get_all(pool, league.id)

        n_success = 0
        errors: list[str] = []
        for team in teams:
            try:
                players_sorted = await roster_seed_service._players_for_team(pool, league.id, team.id)
                await import_service._generate_lineup(pool, league.id, team.id, players_sorted)
                n_success += 1
            except Exception as exc:
                team_label = getattr(team, "nba_team_code", str(team.id))
                log.error(f"rebuild-lineups: failed for team {team_label} — {exc}", exc_info=True)
                errors.append(f"{team_label}: {exc}")

        embed = discord.Embed(
            title="Lineups Rebuilt",
            color=discord.Color.green() if not errors else discord.Color.orange(),
        )
        embed.add_field(name="Teams updated", value=str(n_success), inline=True)
        embed.add_field(name="League ID", value=str(league.id), inline=True)
        if errors:
            error_text = "\n".join(errors[:10])
            if len(errors) > 10:
                error_text += f"\n… and {len(errors) - 10} more"
            embed.add_field(name="Errors", value=error_text, inline=False)

        await safe_respond(interaction, embed=embed)

        await admin_repo.log_commissioner_action(
            pool,
            league_id=league.id,
            user_id=interaction.user.id,
            action_type="rebuild_lineups",
            target_ref=str(league.id),
            detail=f"Rebuilt lineups for {n_success}/{len(teams)} teams. Errors: {len(errors)}",
        )


    @app_commands.command(
        name="force-ready",
        description="Commissioner: mark all managers as ready (testing / catch-up)",
    )
    async def force_ready(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction, ephemeral=True)
        pool = await get_pool()
        league = await league_repo.get_by_guild(pool, interaction.guild_id)
        if not league:
            await safe_respond(interaction, content="No active league.", ephemeral=True)
            return

        if interaction.user.id != league.commissioner_user_id:
            await safe_respond(
                interaction,
                content="Only the commissioner can force-ready.",
                ephemeral=True,
            )
            return

        teams = await team_repo.get_all(pool, league.id)
        human_teams = [t for t in teams if t.manager_user_id is not None]

        if not human_teams:
            await safe_respond(interaction, content="No managers to ready up.", ephemeral=True)
            return

        for team in human_teams:
            await game_repo.set_ready(pool, league.id, team.id, team.manager_user_id, True)

        await safe_respond(
            interaction,
            content=f"Force-readied {len(human_teams)} manager(s). Now run `/sim to-next-rival` to advance.",
            ephemeral=True,
        )


    @app_commands.command(
        name="restart",
        description="Bot owner: restart the bot process",
    )
    async def restart_bot(self, interaction: discord.Interaction) -> None:
        # Defer IMMEDIATELY — this MUST be the first await. Discord's
        # 3-second ack window starts when the user clicks, not when we
        # receive the gateway event, so we have ~150ms of bot-side
        # budget if Discord's delivery was responsive. Anything before
        # this (is_owner, DB lookups) eats into that budget.
        try:
            await interaction.response.defer(ephemeral=True)
            log.info(f"restart: defer OK for interaction {interaction.id}")
        except discord.HTTPException as exc:
            # Interaction already expired before we could ack. Discord
            # will show "Application did not respond" — nothing we can
            # do; restart still proceeds.
            log.warning(f"restart: defer FAILED for interaction {interaction.id} ({exc})")

        # Permission check (after defer so it doesn't eat ack budget).
        bot = interaction.client
        is_owner = await bot.is_owner(interaction.user)
        if not is_owner:
            league = await league_repo.get_by_guild(await get_pool(), interaction.guild_id)
            if not league or interaction.user.id != league.commissioner_user_id:
                try:
                    await interaction.edit_original_response(
                        content="Only the bot owner or league commissioner can restart the bot."
                    )
                    log.info(f"restart: permission-denied edit OK for interaction {interaction.id}")
                except discord.HTTPException as exc:
                    log.warning(f"restart: permission-denied edit FAILED for interaction {interaction.id} ({exc})")
                return

        # Replace the "DBA is thinking..." placeholder with the restart
        # message. Uses the same interaction token (valid for 15 minutes
        # post-defer), so this works even if the defer itself was slow.
        try:
            await interaction.edit_original_response(
                content="Restarting now — back in ~5 seconds."
            )
            log.info(f"restart: restarting-now edit OK for interaction {interaction.id}")
        except discord.HTTPException as exc:
            log.warning(f"restart: restarting-now edit FAILED for interaction {interaction.id} ({exc})")

        try:
            marker = {
                "interaction_id": interaction.id,
                "interaction_token": interaction.token,
                "application_id": interaction.application_id,
                "user_id": interaction.user.id,
                "channel_id": interaction.channel_id,
                "saved_at": datetime.utcnow().isoformat(),
            }
            Path(".restart_marker.json").write_text(json.dumps(marker))
            log.info(f"restart: marker written for interaction {interaction.id}")
        except Exception as exc:
            log.warning(f"restart: failed to write marker ({exc})")

        log.warning(
            f"Bot restart requested by {interaction.user} ({interaction.user.id})"
        )

        # Exit with code 42 — run.py watchdog sees this and respawns main.py.
        # All the previous self-spawn approaches (subprocess.Popen with
        # CREATE_NEW_CONSOLE, cmd /c start, etc.) failed silently from inside
        # the asyncio loop because the spawned child inherited handles that
        # got cleaned up when the parent died. The watchdog pattern moves the
        # respawn responsibility OUT of the dying process.
        #
        # If the bot is launched with `python main.py` instead of
        # `python run.py`, no watchdog is listening — the bot will exit and
        # stay down. That's a one-time launch-command change for the user.
        await interaction.client.close()
        os._exit(42)


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.bot.tree.add_command(AdminGroup())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
