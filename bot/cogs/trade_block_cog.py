"""Trade block management slash commands (/block add, remove, view, league).

Extracted from trade_cog.py (Phase 3 opportunistic cog split, see
HANDOFF.md) -- registered as its own extension so trade proposal/accept/
decline logic and trade-block listing logic aren't crammed into one file.
"""
from __future__ import annotations

import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.embeds import trade_embeds
from core.errors import safe_defer, safe_respond
from core.logging import get_logger
from data.db import get_pool
from data.repositories import league_repo, player_repo, team_repo, trade_block_repo
from services import league_service

log = get_logger(__name__)


class TradeBlockGroup(app_commands.Group, name="block", description="Trade block management"):

    @app_commands.command(name="add", description="Add a player to your trade block")
    @app_commands.describe(
        player="Player name to list (e.g. Luka Doncic)",
        asking_price="Optional asking salary (annual, in dollars)",
        note="Optional note for interested teams (max 100 chars)",
    )
    async def add(
        self,
        interaction: discord.Interaction,
        player: str,
        asking_price: Optional[int] = None,
        note: Optional[str] = None,
    ) -> None:
        await safe_defer(interaction)

        if note and len(note) > 100:
            await safe_respond(interaction, content="Note must be 100 characters or fewer.", ephemeral=True)
            return

        pool = await get_pool()
        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await safe_respond(interaction, content="No active league in this server.", ephemeral=True)
            return

        user_team = await team_repo.get_by_manager(pool, league.id, interaction.user.id)
        if not user_team:
            await safe_respond(interaction, content="You don't manage a team in this league.", ephemeral=True)
            return

        # Try numeric ID first so users can copy the ID shown in /roster output.
        try:
            player_int_id = int(player)
        except ValueError:
            player_int_id = None

        if player_int_id is not None:
            found_player = await player_repo.get_by_id(pool, player_int_id)
            if found_player is None or found_player.league_id != league.id:
                await safe_respond(
                    interaction,
                    content=f"No player with ID {player_int_id} found in this league.",
                    ephemeral=True,
                )
                return
            if found_player.team_id != user_team.id:
                owner_team = await team_repo.get_by_id(pool, found_player.team_id) if found_player.team_id else None
                team_label = owner_team.full_name if owner_team else "another team"
                await safe_respond(
                    interaction,
                    content=f"**{found_player.full_name}** plays for {team_label} — you can only block players on your own roster.",
                    ephemeral=True,
                )
                return
        else:
            matches = await player_repo.search_by_name(pool, league.id, player)
            roster_matches = [p for p in matches if p.team_id == user_team.id]
            if not roster_matches:
                # Secondary check: does the player exist on any other team?
                league_matches = [p for p in matches if p.team_id is not None]
                if league_matches:
                    other = league_matches[0]
                    owner_team = await team_repo.get_by_id(pool, other.team_id)
                    team_label = owner_team.full_name if owner_team else "another team"
                    await safe_respond(
                        interaction,
                        content=f"**{other.full_name}** plays for {team_label} — you can only block players on your own roster.",
                        ephemeral=True,
                    )
                else:
                    await safe_respond(
                        interaction,
                        content=f"No player matching '{player}' found on your roster.",
                        ephemeral=True,
                    )
                return
            if len(roster_matches) > 1:
                names = ", ".join(p.full_name for p in roster_matches)
                await safe_respond(
                    interaction,
                    content=f"Multiple matches for '{player}': {names}. Be more specific.",
                    ephemeral=True,
                )
                return
            found_player = roster_matches[0]

        player_id = found_player.id

        await trade_block_repo.add_to_block(
            pool, league.id, user_team.id, player_id, asking_price, note
        )

        # Compute age and fetch active contract for the enriched embed.
        _today = datetime.date.today()
        if found_player.birth_date:
            _age = _today.year - found_player.birth_date.year
            if (_today.month, _today.day) < (found_player.birth_date.month, found_player.birth_date.day):
                _age -= 1
        else:
            _age = None
        _contract = await player_repo.get_active_contract(pool, found_player.id)

        player_dict = {
            "full_name": found_player.full_name,
            "overall": found_player.overall,
            "position": found_player.position,
            "age": _age,
            "salary": _contract.salary if _contract else None,
            "years_remaining": _contract.years_remaining if _contract else None,
        }
        embed = trade_embeds.trade_block_added_embed(player_dict, user_team, asking_price, note)

        block_channel_id = await league_repo.get_channel(pool, league.id, "trade-block")
        if block_channel_id and interaction.channel_id == block_channel_id:
            # User is in #trade-block — channel.send is visible; ack ephemerally.
            ch = interaction.guild.get_channel(block_channel_id)
            if ch:
                await ch.send(embed=embed)
            await safe_respond(
                interaction,
                content=f"Added **{found_player.full_name}** to your trade block. Posted to #trade-block.",
                ephemeral=True,
            )
        else:
            # User is elsewhere — respond visibly, then post to the block channel too.
            await safe_respond(
                interaction,
                content=f"Added **{found_player.full_name}** to your trade block.",
                embed=embed,
            )
            if block_channel_id:
                ch = interaction.guild.get_channel(block_channel_id)
                if ch:
                    await ch.send(embed=embed)

    @app_commands.command(name="remove", description="Remove a player from your trade block")
    @app_commands.describe(player="Player name to remove from your trade block")
    async def remove(self, interaction: discord.Interaction, player: str) -> None:
        await safe_defer(interaction)

        pool = await get_pool()
        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await safe_respond(interaction, content="No active league in this server.", ephemeral=True)
            return

        user_team = await team_repo.get_by_manager(pool, league.id, interaction.user.id)
        if not user_team:
            await safe_respond(interaction, content="You don't manage a team in this league.", ephemeral=True)
            return

        # Try numeric ID first so users can copy the ID shown in /roster output.
        try:
            player_int_id = int(player)
        except ValueError:
            player_int_id = None

        if player_int_id is not None:
            found_player = await player_repo.get_by_id(pool, player_int_id)
            if found_player is None or found_player.league_id != league.id:
                await safe_respond(
                    interaction,
                    content=f"No player with ID {player_int_id} found in this league.",
                    ephemeral=True,
                )
                return
            if found_player.team_id != user_team.id:
                owner_team = await team_repo.get_by_id(pool, found_player.team_id) if found_player.team_id else None
                team_label = owner_team.full_name if owner_team else "another team"
                await safe_respond(
                    interaction,
                    content=f"**{found_player.full_name}** plays for {team_label} — you can only block players on your own roster.",
                    ephemeral=True,
                )
                return
        else:
            matches = await player_repo.search_by_name(pool, league.id, player)
            roster_matches = [p for p in matches if p.team_id == user_team.id]
            if not roster_matches:
                # Secondary check: does the player exist on any other team?
                league_matches = [p for p in matches if p.team_id is not None]
                if league_matches:
                    other = league_matches[0]
                    owner_team = await team_repo.get_by_id(pool, other.team_id)
                    team_label = owner_team.full_name if owner_team else "another team"
                    await safe_respond(
                        interaction,
                        content=f"**{other.full_name}** plays for {team_label} — you can only block players on your own roster.",
                        ephemeral=True,
                    )
                else:
                    await safe_respond(
                        interaction,
                        content=f"No player matching '{player}' found on your roster.",
                        ephemeral=True,
                    )
                return
            if len(roster_matches) > 1:
                names = ", ".join(p.full_name for p in roster_matches)
                await safe_respond(
                    interaction,
                    content=f"Multiple matches for '{player}': {names}. Be more specific.",
                    ephemeral=True,
                )
                return
            found_player = roster_matches[0]

        on_block = await trade_block_repo.is_on_block(pool, league.id, found_player.id)
        if not on_block:
            await safe_respond(interaction, content=f"**{found_player.full_name}** is not on your trade block.", ephemeral=True)
            return

        await trade_block_repo.remove_from_block(pool, league.id, found_player.id)
        await safe_respond(interaction, content=f"Removed **{found_player.full_name}** from your trade block.")

        block_channel_id = await league_repo.get_channel(pool, league.id, "trade-block")
        if block_channel_id:
            ch = interaction.guild.get_channel(block_channel_id)
            if ch:
                removed_embed = discord.Embed(
                    title="Trade Block — Player Removed",
                    description=f"**{found_player.full_name}** (OVR {found_player.overall}) has been removed from the trade block.",
                    color=discord.Color.greyple(),
                )
                removed_embed.add_field(name="Team", value=user_team.full_name, inline=True)
                await ch.send(embed=removed_embed)
                all_entries = await trade_block_repo.get_league_block(pool, league.id)
                entries_by_team: dict[int, list[dict]] = {}
                for entry in all_entries:
                    entries_by_team.setdefault(entry["team_id"], []).append(entry)
                teams_by_id: dict = {}
                players_by_id: dict = {}
                for entry in all_entries:
                    tid = entry["team_id"]
                    if tid not in teams_by_id:
                        t = await team_repo.get_by_id(pool, tid)
                        if t:
                            teams_by_id[tid] = t
                    pid = entry["player_id"]
                    if pid not in players_by_id:
                        p = await player_repo.get_by_id(pool, pid)
                        if p:
                            players_by_id[pid] = {"full_name": p.full_name, "overall": p.overall}
                league_embed = trade_embeds.trade_block_league_embed(entries_by_team, teams_by_id, players_by_id)
                league_embed.title = "League Block Snapshot"
                await ch.send(embed=league_embed)

    @app_commands.command(name="view", description="View a team's trade block")
    @app_commands.describe(team="Team code (defaults to your team)")
    async def view(self, interaction: discord.Interaction, team: Optional[str] = None) -> None:
        await safe_defer(interaction)

        pool = await get_pool()
        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await safe_respond(interaction, content="No active league in this server.", ephemeral=True)
            return

        if team:
            target_team = await team_repo.get_by_code(pool, league.id, team.upper())
            if not target_team:
                await safe_respond(interaction, content=f"Team `{team.upper()}` not found.", ephemeral=True)
                return
        else:
            target_team = await team_repo.get_by_manager(pool, league.id, interaction.user.id)
            if not target_team:
                await safe_respond(
                    interaction,
                    content="You don't manage a team. Provide a team code.",
                    ephemeral=True,
                )
                return

        entries = await trade_block_repo.get_team_block(pool, league.id, target_team.id)

        players_by_id: dict = {}
        for entry in entries:
            pid = entry["player_id"]
            if pid not in players_by_id:
                p = await player_repo.get_by_id(pool, pid)
                if p:
                    players_by_id[pid] = {"full_name": p.full_name, "overall": p.overall}

        embed = trade_embeds.trade_block_team_embed(target_team, entries, players_by_id)
        await safe_respond(interaction, embed=embed)

    @app_commands.command(name="league", description="Show all players on the trade block league-wide")
    async def league(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction)

        pool = await get_pool()
        league_obj = await league_service.get_league(interaction.guild_id)
        if not league_obj:
            await safe_respond(interaction, content="No active league in this server.", ephemeral=True)
            return

        all_entries = await trade_block_repo.get_league_block(pool, league_obj.id)

        entries_by_team: dict[int, list[dict]] = {}
        for entry in all_entries:
            tid = entry["team_id"]
            entries_by_team.setdefault(tid, []).append(entry)

        team_ids = list(entries_by_team.keys())
        teams_by_id: dict = {}
        player_ids: list[int] = []
        for entry in all_entries:
            player_ids.append(entry["player_id"])

        for tid in team_ids:
            t = await team_repo.get_by_id(pool, tid)
            if t:
                teams_by_id[tid] = t

        players_by_id: dict = {}
        for pid in player_ids:
            if pid not in players_by_id:
                p = await player_repo.get_by_id(pool, pid)
                if p:
                    players_by_id[pid] = {"full_name": p.full_name, "overall": p.overall}

        embed = trade_embeds.trade_block_league_embed(entries_by_team, teams_by_id, players_by_id)
        await safe_respond(interaction, embed=embed)


class TradeBlockCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.bot.tree.add_command(TradeBlockGroup())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TradeBlockCog(bot))
