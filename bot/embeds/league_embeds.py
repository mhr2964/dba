from __future__ import annotations

import discord

from data.repositories.league_repo import League
from data.repositories.team_repo import Team


def created(league: League) -> discord.Embed:
    embed = discord.Embed(
        title="🏀 League Created",
        color=discord.Color.orange(),
    )
    embed.add_field(name="League", value=league.name, inline=True)
    embed.add_field(name="Season", value=str(league.start_season_year), inline=True)
    embed.add_field(name="Teams Seeded", value="30", inline=True)
    embed.add_field(
        name="Channels Created",
        value="league-news, box-scores, standings, transactions, trade-block",
        inline=False,
    )
    embed.set_footer(text="Use /team assign to claim teams.")
    return embed


def team_assigned(team: Team, user: discord.Member) -> discord.Embed:
    embed = discord.Embed(
        title=f"{team.full_name} — Manager Assigned",
        color=discord.Color.green(),
    )
    embed.add_field(name="Manager", value=user.mention, inline=True)
    embed.add_field(name="Team Code", value=team.nba_team_code, inline=True)
    return embed


def manager_removed(team: Team) -> discord.Embed:
    embed = discord.Embed(
        title=f"{team.full_name} — Manager Removed",
        color=discord.Color.yellow(),
    )
    embed.add_field(name="Status", value="Now CPU controlled", inline=True)
    embed.add_field(name="CPU Mode", value=team.cpu_mode.capitalize(), inline=True)
    return embed
