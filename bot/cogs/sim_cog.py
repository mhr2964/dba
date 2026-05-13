from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.embeds import sim_embeds
from core.errors import DBAError, PermissionError
from core.logging import get_logger
from data.db import get_pool
from data.repositories import game_repo, league_repo, team_repo
from phase.helpers import get_league_or_error, require_commissioner, require_phase, require_all_ready, require_no_pending_trades
from services import batch_sim_runner, league_service

log = get_logger(__name__)


class SimGroup(app_commands.Group, name="sim", description="Advance the league simulation"):

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot


    @app_commands.command(name="rivalry", description="Sim CPU games until the next user matchup")
    async def rivalry(self, interaction: discord.Interaction) -> None:
        league = await get_league_or_error(interaction.guild_id)
        await require_commissioner(interaction, league)
        await require_phase(league, "sim_rivalry")
        await require_all_ready(interaction, league)
        await require_no_pending_trades(league)

        await interaction.response.defer()

        pool = await get_pool()
        summary = await batch_sim_runner.sim_until_rival(
            league.id,
            interaction.guild,
            league.current_season,
            bot=self.bot,
        )

        next_game = summary.get("next_matchup")
        embed = discord.Embed(title="Sim Complete", color=discord.Color.green())
        embed.add_field(name="Games Simmed", value=str(summary["games_simmed"]), inline=True)
        if next_game:
            embed.add_field(
                name="Stopped At",
                value=f"Game #{next_game['game_index']} (user matchup)",
                inline=True,
            )
        else:
            embed.add_field(name="Stopped At", value="End of schedule", inline=True)

        await interaction.followup.send(embed=embed)
        await game_repo.reset_ready(pool, league.id)

    @app_commands.command(name="games", description="Sim exactly N games from the current position")
    @app_commands.describe(
        count="Number of games to sim (1–20)",
        force="Skip past user matchups without stopping",
    )
    async def games(
        self,
        interaction: discord.Interaction,
        count: int,
        force: bool = False,
    ) -> None:
        league = await get_league_or_error(interaction.guild_id)
        await require_commissioner(interaction, league)
        await require_phase(league, "sim_rivalry")
        await require_all_ready(interaction, league)
        await require_no_pending_trades(league)

        if count < 1 or count > 20:
            await interaction.response.send_message(
                "Count must be between 1 and 20.", ephemeral=True
            )
            return

        pool = await get_pool()
        current_index = await game_repo.get_current_index(pool, league.id, league.current_season)
        to_index = current_index + count

        if not force:
            user_matchups = await batch_sim_runner.check_user_matchups_in_range(
                pool, league.id, league.current_season, current_index + 1, to_index
            )
            if user_matchups:
                embed = sim_embeds.user_matchup_warning(user_matchups)
                embed.set_footer(
                    text="Use /sim games force:True to sim through them anyway."
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

        await interaction.response.defer()

        summary = await batch_sim_runner.sim_range(
            league.id,
            interaction.guild,
            league.current_season,
            to_index,
            bot=self.bot,
        )

        if summary.get("warning"):
            embed = sim_embeds.user_matchup_warning(summary["user_matchups"])
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title="Sim Complete",
            description=f"Simmed {summary['games_simmed']} game(s).",
            color=discord.Color.green(),
        )
        if summary.get("season_complete"):
            embed.add_field(
                name="Season Status",
                value="Regular season is now complete.",
                inline=False,
            )
        await interaction.followup.send(embed=embed)
        await game_repo.reset_ready(pool, league.id)

    @app_commands.command(name="season", description="Sim to end of regular season")
    @app_commands.describe(force="Skip past user matchups without stopping")
    async def season(
        self,
        interaction: discord.Interaction,
        force: bool = False,
    ) -> None:
        league = await get_league_or_error(interaction.guild_id)
        await require_commissioner(interaction, league)
        await require_phase(league, "sim_season")
        await require_all_ready(interaction, league)
        await require_no_pending_trades(league)

        pool = await get_pool()
        current_index = await game_repo.get_current_index(pool, league.id, league.current_season)

        if not force:
            user_matchups = await batch_sim_runner.check_user_matchups_in_range(
                pool, league.id, league.current_season, current_index + 1, 10000
            )
            if user_matchups:
                embed = sim_embeds.user_matchup_warning(user_matchups)
                embed.set_footer(
                    text="Use /sim season force:True to sim through them, or /sim rivalry to stop at each."
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

        await interaction.response.defer()

        summary = await batch_sim_runner.sim_range(
            league.id,
            interaction.guild,
            league.current_season,
            10000,
        )

        if summary.get("warning"):
            embed = sim_embeds.user_matchup_warning(summary["user_matchups"])
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title="Regular Season Complete",
            description=f"Simmed {summary['games_simmed']} games.",
            color=discord.Color.green(),
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="deadline", description="Sim to the trade deadline and open the trade window")
    @app_commands.describe(force="Skip past user matchups before the deadline without stopping")
    async def deadline(
        self,
        interaction: discord.Interaction,
        force: bool = False,
    ) -> None:
        league = await get_league_or_error(interaction.guild_id)
        await require_commissioner(interaction, league)
        await require_phase(league, "sim_deadline")
        await require_no_pending_trades(league)

        pool = await get_pool()

        total = await pool.fetchval(
            "SELECT COUNT(*) FROM games WHERE league_id = $1 AND season = $2 AND season_type = 'regular'",
            league.id,
            league.current_season,
        )
        if not total:
            await interaction.response.send_message(
                "No schedule found. Run `/season start` first.", ephemeral=True
            )
            return

        deadline_index = round(total * 0.67)
        current_index = await game_repo.get_current_index(pool, league.id, league.current_season)

        if current_index >= deadline_index:
            await interaction.response.send_message(
                f"Already at or past the trade deadline (game {deadline_index} of {total}). "
                "Use `/league advance TRADE_DEADLINE_OPEN` to open the window manually.",
                ephemeral=True,
            )
            return

        if not force:
            user_matchups = await batch_sim_runner.check_user_matchups_in_range(
                pool, league.id, league.current_season, current_index + 1, deadline_index
            )
            if user_matchups:
                embed = sim_embeds.user_matchup_warning(user_matchups)
                embed.set_footer(
                    text="Use /sim deadline force:True to sim through them, or handle them with /sim rivalry first."
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

        await interaction.response.defer()

        summary = await batch_sim_runner.sim_range(
            league.id,
            interaction.guild,
            league.current_season,
            deadline_index,
            bot=self.bot,
        )

        if summary.get("warning"):
            embed = sim_embeds.user_matchup_warning(summary["user_matchups"])
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        await league_service.advance_phase(league.id, "TRADE_DEADLINE_OPEN")

        news_channel_id = await league_repo.get_channel(pool, league.id, "league-news")
        if news_channel_id:
            channel = interaction.guild.get_channel(news_channel_id)
            if channel:
                await channel.send(
                    "🚨 **Trade deadline is open!** Managers can now propose and execute trades. "
                    "The commissioner will use `/league advance REGULAR_SEASON_POSTDEADLINE` to close the window."
                )

        embed = discord.Embed(
            title="Trade Deadline Open",
            color=discord.Color.gold(),
            description=(
                f"Simmed **{summary['games_simmed']}** game(s) to the trade deadline "
                f"(game {deadline_index} of {total}).\n\n"
                "The trade window is now **open**. When all trades are done, the commissioner "
                "should run `/league advance REGULAR_SEASON_POSTDEADLINE` to resume simming."
            ),
        )
        await interaction.followup.send(embed=embed)


@app_commands.command(name="ready", description="Mark yourself as ready for the next matchup")
async def ready_command(interaction: discord.Interaction) -> None:
    pool = await get_pool()
    league = await league_repo.get_by_guild(pool, interaction.guild_id)
    if not league:
        await interaction.response.send_message("No active league.", ephemeral=True)
        return

    await require_phase(league, "ready")

    team = await team_repo.get_by_manager(pool, league.id, interaction.user.id)
    if not team:
        await interaction.response.send_message(
            "You don't manage a team in this league.", ephemeral=True
        )
        return

    await game_repo.set_ready(pool, league.id, team.id, interaction.user.id, True)
    await interaction.response.send_message("Ready.", ephemeral=True)

    teams = await team_repo.get_all(pool, league.id)
    human_teams = [t for t in teams if t.manager_user_id is not None]
    ready_ids = set(await game_repo.get_ready_teams(pool, league.id))
    if human_teams and all(t.id in ready_ids for t in human_teams):
        news_channel_id = await league_repo.get_channel(pool, league.id, "league-news")
        if news_channel_id:
            channel = interaction.guild.get_channel(news_channel_id)
            if channel:
                await channel.send("All managers are ready. The commissioner can now advance the sim.")


@app_commands.command(name="forceready", description="Commissioner: mark all managers as ready (testing)")
async def force_ready_command(interaction: discord.Interaction) -> None:
    pool = await get_pool()
    league = await league_repo.get_by_guild(pool, interaction.guild_id)
    if not league:
        await interaction.response.send_message("No active league.", ephemeral=True)
        return

    if interaction.user.id != league.commissioner_user_id:
        await interaction.response.send_message("Only the commissioner can force-ready.", ephemeral=True)
        return

    teams = await team_repo.get_all(pool, league.id)
    human_teams = [t for t in teams if t.manager_user_id is not None]

    if not human_teams:
        await interaction.response.send_message("No managers to ready up.", ephemeral=True)
        return

    for team in human_teams:
        await game_repo.set_ready(pool, league.id, team.id, team.manager_user_id, True)

    await interaction.response.send_message(
        f"Force-readied {len(human_teams)} manager(s).", ephemeral=True
    )


@app_commands.command(name="standings", description="Show current standings")
async def standings_command(interaction: discord.Interaction) -> None:
    pool = await get_pool()
    league = await league_repo.get_by_guild(pool, interaction.guild_id)
    if not league:
        await interaction.response.send_message("No active league.", ephemeral=True)
        return

    rows = await game_repo.get_standings(pool, league.id, league.current_season)
    if not rows:
        await interaction.response.send_message("No standings data yet.", ephemeral=True)
        return

    teams = await team_repo.get_all(pool, league.id)
    teams_by_id = {t.id: t for t in teams}

    embed = sim_embeds.standings_embed(rows, teams_by_id)
    await interaction.response.send_message(embed=embed)


@app_commands.command(name="schedule", description="Show a team's next 10 scheduled games")
@app_commands.describe(team_code="NBA team code (e.g. LAL) — defaults to your team")
async def schedule_command(
    interaction: discord.Interaction,
    team_code: Optional[str] = None,
) -> None:
    pool = await get_pool()
    league = await league_repo.get_by_guild(pool, interaction.guild_id)
    if not league:
        await interaction.response.send_message("No active league.", ephemeral=True)
        return

    if team_code:
        team = await team_repo.get_by_code(pool, league.id, team_code)
        if not team:
            await interaction.response.send_message(
                f"No team found with code **{team_code.upper()}**.", ephemeral=True
            )
            return
    else:
        team = await team_repo.get_by_manager(pool, league.id, interaction.user.id)
        if not team:
            await interaction.response.send_message(
                "You don't manage a team. Provide a `team_code` argument.", ephemeral=True
            )
            return

    rows = await pool.fetch(
        """
        SELECT g.*, ht.nba_team_code AS home_code, at.nba_team_code AS away_code
        FROM games g
        JOIN teams ht ON ht.id = g.home_team_id
        JOIN teams at ON at.id = g.away_team_id
        WHERE g.league_id = $1
          AND g.season = $2
          AND g.status = 'scheduled'
          AND (g.home_team_id = $3 OR g.away_team_id = $3)
        ORDER BY g.game_index ASC
        LIMIT 10
        """,
        league.id,
        league.current_season,
        team.id,
    )

    if not rows:
        await interaction.response.send_message("No upcoming games found.", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"{team.full_name} — Next 10 Games",
        color=discord.Color.orange(),
    )
    lines = []
    for r in rows:
        is_home = r["home_team_id"] == team.id
        opp_code = r["away_code"] if is_home else r["home_code"]
        venue = "vs" if is_home else "@"
        user_marker = " [USER]" if r["is_user_matchup"] else ""
        lines.append(
            f"Game #{r['game_index']} — {r['scheduled_date']}  {venue} **{opp_code}**{user_marker}"
        )
    embed.description = "\n".join(lines)
    await interaction.response.send_message(embed=embed)


class SimCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.bot.tree.add_command(SimGroup(bot))
        self.bot.tree.add_command(ready_command)
        self.bot.tree.add_command(force_ready_command)
        self.bot.tree.add_command(standings_command)
        self.bot.tree.add_command(schedule_command)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SimCog(bot))
