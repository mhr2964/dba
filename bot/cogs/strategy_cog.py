from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.embeds import strategy_embeds
from core.errors import DBAError
from core.logging import get_logger
from data.db import get_pool
from data.repositories import player_repo, strategy_repo, team_repo
from services import strategy_service

log = get_logger(__name__)


async def _get_league_id(pool, guild_id: int) -> int:
    row = await pool.fetchrow(
        "SELECT id FROM leagues WHERE discord_guild_id = $1 AND archived_at IS NULL", guild_id
    )
    if not row:
        raise DBAError("No active league in this server.")
    return row["id"]


async def _require_team_manager(pool, league_id: int, user_id: int) -> dict:
    """Return team row for the managing user or raise DBAError."""
    row = await pool.fetchrow(
        "SELECT * FROM teams WHERE league_id = $1 AND manager_user_id = $2",
        league_id,
        user_id,
    )
    if not row:
        raise DBAError("You are not a team manager in this league.")
    return dict(row)


async def _resolve_team_for_view(pool, league_id: int, team_code: Optional[str], user_id: int) -> dict:
    """Return team row for the given code, or fall back to the user's managed team."""
    if team_code:
        row = await pool.fetchrow(
            "SELECT * FROM teams WHERE league_id = $1 AND UPPER(nba_team_code) = UPPER($2)",
            league_id,
            team_code,
        )
        if not row:
            raise DBAError(f"Team `{team_code.upper()}` not found.")
        return dict(row)
    row = await pool.fetchrow(
        "SELECT * FROM teams WHERE league_id = $1 AND manager_user_id = $2",
        league_id,
        user_id,
    )
    if not row:
        raise DBAError("You don't manage a team. Provide a team code to view another team's strategy.")
    return dict(row)


class StrategyGroup(app_commands.Group, name="strategy", description="Team strategy settings"):

    @app_commands.command(name="pace", description="Set your team's offensive pace")
    @app_commands.describe(value="Pace style")
    @app_commands.choices(value=[
        app_commands.Choice(name="Slow", value="slow"),
        app_commands.Choice(name="Balanced", value="balanced"),
        app_commands.Choice(name="Fast", value="fast"),
        app_commands.Choice(name="Run & Gun", value="run_and_gun"),
    ])
    async def pace(self, interaction: discord.Interaction, value: app_commands.Choice[str]) -> None:
        await interaction.response.defer()
        pool = await get_pool()
        league_id = await _get_league_id(pool, interaction.guild_id)
        team = await _require_team_manager(pool, league_id, interaction.user.id)

        await strategy_repo.set_strategy(pool, league_id, team["id"], offensive_pace=value.value)
        strategy = await strategy_repo.get_strategy(pool, league_id, team["id"])
        modifiers = await strategy_service.get_sim_modifiers(pool, league_id, team["id"])
        embed = strategy_embeds.strategy_embed(team, strategy, modifiers)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="offense", description="Set your team's offensive scheme")
    @app_commands.describe(scheme="Offensive scheme")
    @app_commands.choices(scheme=[
        app_commands.Choice(name="Isolation", value="isolation"),
        app_commands.Choice(name="Ball Movement", value="ball_movement"),
        app_commands.Choice(name="Three Heavy", value="three_heavy"),
        app_commands.Choice(name="Inside Out", value="inside_out"),
        app_commands.Choice(name="Balanced", value="balanced"),
    ])
    async def offense(self, interaction: discord.Interaction, scheme: app_commands.Choice[str]) -> None:
        await interaction.response.defer()
        pool = await get_pool()
        league_id = await _get_league_id(pool, interaction.guild_id)
        team = await _require_team_manager(pool, league_id, interaction.user.id)

        await strategy_repo.set_strategy(pool, league_id, team["id"], offensive_scheme=scheme.value)
        strategy = await strategy_repo.get_strategy(pool, league_id, team["id"])
        modifiers = await strategy_service.get_sim_modifiers(pool, league_id, team["id"])
        embed = strategy_embeds.strategy_embed(team, strategy, modifiers)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="defense", description="Set your team's defensive scheme")
    @app_commands.describe(scheme="Defensive scheme")
    @app_commands.choices(scheme=[
        app_commands.Choice(name="Man-to-Man", value="man_to_man"),
        app_commands.Choice(name="Zone", value="zone"),
        app_commands.Choice(name="Press", value="press"),
        app_commands.Choice(name="Switch All", value="switch_all"),
    ])
    async def defense(self, interaction: discord.Interaction, scheme: app_commands.Choice[str]) -> None:
        await interaction.response.defer()
        pool = await get_pool()
        league_id = await _get_league_id(pool, interaction.guild_id)
        team = await _require_team_manager(pool, league_id, interaction.user.id)

        await strategy_repo.set_strategy(pool, league_id, team["id"], defensive_scheme=scheme.value)
        strategy = await strategy_repo.get_strategy(pool, league_id, team["id"])
        modifiers = await strategy_service.get_sim_modifiers(pool, league_id, team["id"])
        embed = strategy_embeds.strategy_embed(team, strategy, modifiers)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="intensity", description="Set your team's defensive intensity")
    @app_commands.describe(level="Intensity level")
    @app_commands.choices(level=[
        app_commands.Choice(name="Conservative", value="conservative"),
        app_commands.Choice(name="Normal", value="normal"),
        app_commands.Choice(name="Aggressive", value="aggressive"),
    ])
    async def intensity(self, interaction: discord.Interaction, level: app_commands.Choice[str]) -> None:
        await interaction.response.defer()
        pool = await get_pool()
        league_id = await _get_league_id(pool, interaction.guild_id)
        team = await _require_team_manager(pool, league_id, interaction.user.id)

        await strategy_repo.set_strategy(pool, league_id, team["id"], defensive_intensity=level.value)
        strategy = await strategy_repo.get_strategy(pool, league_id, team["id"])
        modifiers = await strategy_service.get_sim_modifiers(pool, league_id, team["id"])
        embed = strategy_embeds.strategy_embed(team, strategy, modifiers)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="star-usage", description="Control how much shots go to your top-2 players (0-100)")
    @app_commands.describe(value="Star usage value (0 = spread it out, 100 = funnel everything to stars)")
    async def star_usage(self, interaction: discord.Interaction, value: int) -> None:
        await interaction.response.defer()
        if not (0 <= value <= 100):
            await interaction.followup.send("Star usage must be between 0 and 100.", ephemeral=True)
            return

        pool = await get_pool()
        league_id = await _get_league_id(pool, interaction.guild_id)
        team = await _require_team_manager(pool, league_id, interaction.user.id)

        await strategy_repo.set_strategy(pool, league_id, team["id"], star_usage=value)
        strategy = await strategy_repo.get_strategy(pool, league_id, team["id"])
        modifiers = await strategy_service.get_sim_modifiers(pool, league_id, team["id"])
        embed = strategy_embeds.strategy_embed(team, strategy, modifiers)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="view", description="View a team's current strategy")
    @app_commands.describe(team="Team code (e.g. LAL). Defaults to your team.")
    async def view(self, interaction: discord.Interaction, team: Optional[str] = None) -> None:
        await interaction.response.defer()
        pool = await get_pool()
        league_id = await _get_league_id(pool, interaction.guild_id)
        team_row = await _resolve_team_for_view(pool, league_id, team, interaction.user.id)

        strategy = await strategy_repo.get_strategy(pool, league_id, team_row["id"])
        modifiers = await strategy_service.get_sim_modifiers(pool, league_id, team_row["id"])
        embed = strategy_embeds.strategy_embed(team_row, strategy, modifiers)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="reset", description="Reset your team's strategy to defaults")
    async def reset(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        pool = await get_pool()
        league_id = await _get_league_id(pool, interaction.guild_id)
        team = await _require_team_manager(pool, league_id, interaction.user.id)

        await strategy_repo.reset_strategy(pool, league_id, team["id"])
        strategy = await strategy_repo.get_strategy(pool, league_id, team["id"])
        modifiers = await strategy_service.get_sim_modifiers(pool, league_id, team["id"])
        embed = strategy_embeds.strategy_embed(team, strategy, modifiers)
        embed.title = f"{team.get('city', '')} {team.get('name', '')} — Strategy Reset to Defaults"
        await interaction.followup.send(embed=embed)


class MinutesGroup(app_commands.Group, name="minutes", description="Player minutes management"):

    @app_commands.command(name="set", description="Set target minutes for a player (0 = auto)")
    @app_commands.describe(
        player="Player name (e.g. LeBron James)",
        minutes="Target minutes per game (0-48, 0 = auto)",
    )
    async def set_minutes(self, interaction: discord.Interaction, player: str, minutes: int) -> None:
        await interaction.response.defer()
        if not (0 <= minutes <= 48):
            await interaction.followup.send("Minutes must be between 0 and 48.", ephemeral=True)
            return

        pool = await get_pool()
        league_id = await _get_league_id(pool, interaction.guild_id)
        team = await _require_team_manager(pool, league_id, interaction.user.id)

        matches = await player_repo.search_by_name(pool, league_id, player)
        if not matches:
            await interaction.followup.send(f"No player found matching '{player}'.", ephemeral=True)
            return
        if len(matches) > 1:
            names = ", ".join(f"{p.first_name} {p.last_name}" for p in matches[:5])
            await interaction.followup.send(
                f"Multiple players match '{player}': {names}. Be more specific.", ephemeral=True
            )
            return

        found_player = matches[0]
        if found_player.team_id != team["id"]:
            await interaction.followup.send("That player is not on your team.", ephemeral=True)
            return

        await strategy_repo.set_player_minutes(pool, league_id, team["id"], found_player.id, minutes)

        # Warn if total exceeds 240 but still allow it (will normalize at sim time).
        all_targets = await strategy_repo.get_player_minutes(pool, league_id, team["id"])
        total = sum(all_targets.values())
        label = str(minutes) if minutes > 0 else "Auto"
        msg = f"Set target minutes for **{found_player.full_name}** to **{label}**."
        if total > 240:
            msg += f"\n**Warning:** Team total target minutes ({total}) exceed 240 and will be normalized at sim time."
        await interaction.followup.send(msg)

    @app_commands.command(name="view", description="View player minutes targets for a team")
    @app_commands.describe(team="Team code (e.g. LAL). Defaults to your team.")
    async def view_minutes(self, interaction: discord.Interaction, team: Optional[str] = None) -> None:
        await interaction.response.defer()
        pool = await get_pool()
        league_id = await _get_league_id(pool, interaction.guild_id)
        team_row = await _resolve_team_for_view(pool, league_id, team, interaction.user.id)

        roster = await player_repo.get_roster(pool, league_id, team_row["id"])
        player_ids = [p.id for p in roster]
        targets = await strategy_repo.get_player_minutes(pool, league_id, team_row["id"])
        minutes_plan = await strategy_repo.get_team_minutes_plan(pool, league_id, team_row["id"], player_ids)

        players_for_embed = []
        for p in roster:
            d = {
                "id": p.id,
                "full_name": p.full_name,
                "first_name": p.first_name,
                "last_name": p.last_name,
                "position": p.position,
                "overall": p.overall,
                "_target_minutes": targets.get(p.id, 0),
            }
            players_for_embed.append(d)

        embed = strategy_embeds.minutes_embed(team_row, players_for_embed, minutes_plan)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="reset", description="Clear all target minutes for your team (set all to Auto)")
    async def reset_minutes(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        pool = await get_pool()
        league_id = await _get_league_id(pool, interaction.guild_id)
        team = await _require_team_manager(pool, league_id, interaction.user.id)

        await strategy_repo.reset_player_minutes(pool, league_id, team["id"])
        await interaction.followup.send("All player minutes targets cleared. Minutes will be auto-distributed at sim time.")


async def setup(bot: commands.Bot) -> None:
    strategy_group = StrategyGroup()
    minutes_group = MinutesGroup()
    bot.tree.add_command(strategy_group)
    bot.tree.add_command(minutes_group)
