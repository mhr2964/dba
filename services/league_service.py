from __future__ import annotations

import json
import os
from typing import Optional

import discord

from core.errors import DBAError
from core.logging import get_logger
from data.db import get_pool
from data.repositories import league_repo, team_repo, trade_repo

log = get_logger(__name__)

CHANNEL_ROLES = ["league-news", "box-scores", "standings", "transactions", "trade-block"]

_SEEDS_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "seeds", "nba_teams.json")


def _load_nba_teams() -> list[dict]:
    path = os.path.normpath(_SEEDS_PATH)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


async def create(
    guild: discord.Guild,
    commissioner: discord.Member,
    name: str,
    season_year: int,
) -> league_repo.League:
    pool = await get_pool()

    existing = await league_repo.get_by_guild(pool, guild.id)
    if existing:
        raise DBAError(f"This server already has an active league: **{existing.name}**.")

    category = await guild.create_category(f"🏀 {name}")

    channel_ids: dict[str, int] = {}
    for role in CHANNEL_ROLES:
        ch = await category.create_text_channel(role)
        channel_ids[role] = ch.id

    commissioner_role = await guild.create_role(
        name="DBA Commissioner",
        color=discord.Color.gold(),
        hoist=True,
        reason=f"DBA league '{name}' created",
    )
    await commissioner.add_roles(commissioner_role, reason="Assigned as DBA Commissioner")

    league = await league_repo.create(
        pool,
        guild_id=guild.id,
        name=name,
        season_year=season_year,
        commissioner_id=commissioner.id,
    )

    for role, channel_id in channel_ids.items():
        await league_repo.add_channel(pool, league.id, role, channel_id)

    await league_repo.add_role(
        pool, league.id, "commissioner", None, commissioner_role.id
    )

    nba_teams = _load_nba_teams()
    for team_data in nba_teams:
        team = await team_repo.create(pool, league.id, team_data)
        discord_role = await guild.create_role(
            name=f"{team_data['city']} {team_data['name']}",
            reason=f"DBA team role for {team.full_name}",
        )
        await league_repo.add_role(pool, league.id, "team", team.id, discord_role.id)

    await trade_repo.seed_picks_for_league(pool, league.id, season_year)
    await _post_onboarding_guide(guild, pool, league.id, name)
    log.info(f"League '{name}' created in guild {guild.id} by {commissioner.id}")
    return league


async def _post_onboarding_guide(guild: discord.Guild, pool, league_id: int, name: str) -> None:
    channel_id = await league_repo.get_channel(pool, league_id, "league-news")
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if not channel:
        return

    embed = discord.Embed(
        title=f"🏀 Welcome to {name}!",
        description="The league is live. Here's how to get started:",
        color=discord.Color.orange(),
    )
    embed.add_field(
        name="Step 1 — Import Players",
        value="`/season import-players` — loads real NBA rosters (~2 min). Do this first.",
        inline=False,
    )
    embed.add_field(
        name="Step 2 — Claim Teams",
        value="`/team assign @user TEAMCODE` — assign managers (e.g., `/team assign @koby LAL`)\n`/team list` — see all 30 teams",
        inline=False,
    )
    embed.add_field(
        name="Step 3 — Start the Season",
        value="`/season start` — generates the 82-game schedule and begins play",
        inline=False,
    )
    embed.add_field(
        name="Step 4 — Sim Games",
        value="All managers `/ready` up, then commissioner runs `/sim rivalry` to advance to the first human matchup",
        inline=False,
    )
    embed.add_field(
        name="Useful Commands",
        value="`/league status` · `/standings` · `/roster [team]` · `/help` · `/strategy view`",
        inline=False,
    )
    embed.set_footer(text="Good luck! 🏆 Use /help at any time for phase-aware command guidance.")
    await channel.send(embed=embed)


async def assign_manager(
    guild: discord.Guild,
    league: league_repo.League,
    team_code: str,
    user: discord.Member,
) -> team_repo.Team:
    pool = await get_pool()

    team = await team_repo.get_by_code(pool, league.id, team_code)
    if not team:
        raise DBAError(f"No team found with code **{team_code.upper()}**.")

    if team.manager_user_id is not None:
        old_member = guild.get_member(team.manager_user_id)
        if old_member:
            old_role_id = await league_repo.get_team_role(pool, league.id, team.id)
            if old_role_id:
                old_role = guild.get_role(old_role_id)
                if old_role:
                    await old_member.remove_roles(old_role, reason="DBA manager replaced")

    existing_team = await team_repo.get_by_manager(pool, league.id, user.id)
    if existing_team:
        raise DBAError(
            f"{user.mention} already manages the **{existing_team.full_name}**. Remove them first."
        )

    await team_repo.set_manager(pool, team.id, user.id)
    await team_repo.log_manager_change(pool, team.id, user.id, assigned_by=user.id)

    role_id = await league_repo.get_team_role(pool, league.id, team.id)
    if role_id:
        role = guild.get_role(role_id)
        if role:
            await user.add_roles(role, reason=f"DBA: assigned manager of {team.full_name}")

    team.manager_user_id = user.id
    return team


async def remove_manager(
    guild: discord.Guild,
    league: league_repo.League,
    team_code: str,
    requester: discord.Member,
) -> team_repo.Team:
    pool = await get_pool()

    team = await team_repo.get_by_code(pool, league.id, team_code)
    if not team:
        raise DBAError(f"No team found with code **{team_code.upper()}**.")

    if team.manager_user_id is None:
        raise DBAError(f"**{team.full_name}** has no manager — it's already CPU controlled.")

    member = guild.get_member(team.manager_user_id)
    if member:
        role_id = await league_repo.get_team_role(pool, league.id, team.id)
        if role_id:
            role = guild.get_role(role_id)
            if role:
                await member.remove_roles(role, reason=f"DBA: manager removed from {team.full_name}")

    await team_repo.set_manager(pool, team.id, None)
    await team_repo.log_manager_change(pool, team.id, None, assigned_by=requester.id)

    log.info(f"Manager removed from team {team.id} by {requester.id}")
    team.manager_user_id = None
    return team


async def get_league(guild_id: int) -> Optional[league_repo.League]:
    pool = await get_pool()
    return await league_repo.get_by_guild(pool, guild_id)


async def advance_phase(league_id: int, new_phase: str) -> None:
    """Update the league's current_phase in DB."""
    from phase.states import Phase  # deferred to avoid circular import
    pool = await get_pool()
    await pool.execute(
        "UPDATE leagues SET current_phase = $1 WHERE id = $2",
        Phase(new_phase).value,
        league_id,
    )
