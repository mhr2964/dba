from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from bot.cogs.sim_legacy_aliases import (
    box_score_command,
    force_ready_command,
    ready_command,
    schedule_command,
    standings_command,
)
from bot.embeds import sim_embeds
from core.errors import safe_defer, safe_respond
from core.logging import get_logger
from data.db import get_pool
from data.repositories import game_repo, league_repo
from phase.helpers import get_league_or_error, require_commissioner, require_phase, require_all_ready, require_no_pending_trades
from services import batch_sim_runner, league_service

log = get_logger(__name__)

_background_tasks: set[asyncio.Task] = set()


def _create_logged_task(coro) -> asyncio.Task:
    """Schedule coro as a background task; log any exception it raises."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        exc = t.exception() if not t.cancelled() else None
        if exc is not None:
            log.error("Background task raised an exception", exc_info=exc)

    task.add_done_callback(_on_done)
    return task


async def _target_index_for_n_more_per_team(pool, league_id: int, n_more: int) -> int:
    """
    Return the game_index where every team has played N more games than their current count.

    Walks upcoming unplayed games in game_index order, tracking per-team played counts.
    Safety cap: at most current max game_index + (n_more * 15 + 10) to prevent runaway.
    """
    # Current per-team played counts from standings cache.
    sc_rows = await pool.fetch(
        "SELECT team_id, wins + losses AS played FROM standings_cache WHERE league_id = $1",
        league_id,
    )
    base_played: dict[int, int] = {r["team_id"]: r["played"] for r in sc_rows}

    # At season start standings_cache is empty — seed from the teams table so the
    # walk below tracks correctly instead of using the n_more*15 approximation.
    if not base_played:
        team_rows = await pool.fetch(
            "SELECT id FROM teams WHERE league_id = $1", league_id
        )
        base_played = {r["id"]: 0 for r in team_rows}

    target_played: dict[int, int] = {tid: v + n_more for tid, v in base_played.items()}

    # Pending games in order.
    pending = await pool.fetch(
        """
        SELECT id AS game_id, game_index, home_team_id, away_team_id
        FROM games
        WHERE league_id = $1 AND status != 'simmed'
        ORDER BY game_index ASC
        """,
        league_id,
    )
    if not pending:
        return 0

    # Safety cap.
    max_idx = pending[-1]["game_index"]
    cap = max_idx + n_more * 15 + 10

    in_progress: dict[int, int] = {tid: 0 for tid in base_played}
    last_idx = 0

    for row in pending:
        if row["game_index"] > cap:
            break
        home_id = row["home_team_id"]
        away_id = row["away_team_id"]
        in_progress[home_id] = in_progress.get(home_id, 0) + 1
        in_progress[away_id] = in_progress.get(away_id, 0) + 1
        last_idx = row["game_index"]
        # Check if all teams have reached their target.
        if all(
            (base_played.get(tid, 0) + in_progress.get(tid, 0)) >= target_played.get(tid, n_more)
            for tid in target_played
        ):
            return row["game_index"]

    return last_idx


_SIM_DEP_WARNED: set[int] = set()


async def _send_sim_dep_warning(
    interaction: discord.Interaction,
    old: str,
    new: str,
) -> None:
    uid = interaction.user.id
    if uid in _SIM_DEP_WARNED:
        return
    if len(_SIM_DEP_WARNED) > 1000:
        _SIM_DEP_WARNED.clear()
    _SIM_DEP_WARNED.add(uid)
    try:
        await interaction.followup.send(
            f"**Heads up:** `{old}` has moved to `{new}`. "
            "The old path will be removed after the next season rollover.",
            ephemeral=True,
        )
    except Exception:
        pass


class SimGroup(app_commands.Group, name="sim", description="Advance the league simulation"):

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot


    @app_commands.command(name="to-next-rival", description="Sim CPU games until the next user matchup")
    async def to_next_rival(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction)
        league = await get_league_or_error(interaction.guild_id)
        await require_commissioner(interaction, league)
        await require_phase(league, "sim_rivalry")
        await require_all_ready(interaction, league)
        await require_no_pending_trades(league)

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

        if next_game:
            embed.add_field(
                name="Next",
                value="When all managers are `/team ready`, `/sim to-next-rival` will play through your matchup.",
                inline=False,
            )
        await safe_respond(interaction, embed=embed)
        if summary.get("user_matchups_simmed", 0) > 0:
            await game_repo.reset_ready(pool, league.id)

    @app_commands.command(name="rivalry", description="[MOVED] Use /sim to-next-rival instead")
    async def rivalry_legacy(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction)
        await _send_sim_dep_warning(interaction, old="/sim rivalry", new="/sim to-next-rival")
        await self.to_next_rival.callback(self, interaction)

    @app_commands.command(name="run", description="Sim until every team has played N more games")
    @app_commands.describe(
        count="Number of games for each team to play (1–20)",
        force="Skip the ready check and user-matchup warning (does not override phase restrictions)",
    )
    async def run(
        self,
        interaction: discord.Interaction,
        count: int,
        force: bool = False,
    ) -> None:
        await safe_defer(interaction, ephemeral=True)
        league = await get_league_or_error(interaction.guild_id)
        await require_commissioner(interaction, league)
        await require_phase(league, "sim_games")
        if not force:
            await require_all_ready(interaction, league)
        await require_no_pending_trades(league)

        if count < 1 or count > 20:
            await safe_respond(interaction, content="Count must be between 1 and 20.", ephemeral=True)
            return

        # Immediately acknowledge so the user isn't staring at "DBA is thinking..."
        # for the full duration of the sim (up to ~2 min for count=20).
        await safe_respond(
            interaction,
            content=f"Simming up to {count} games per team — check #box-scores for live recaps.",
            ephemeral=True,
        )

        pool = await get_pool()

        # Guard: no schedule means no games to sim.  Without this check the runner
        # would see 0 pending games, conclude the season is complete, and auto-advance
        # the phase — leaving the league in an unrecoverable state.
        total_scheduled = await game_repo.get_total_regular_season_games(
            pool, league.id, league.current_season
        )
        if total_scheduled == 0:
            await interaction.followup.send(
                "No schedule found. Run `/season start` to generate the schedule before simming games.",
                ephemeral=True,
            )
            return

        current_index = await game_repo.get_current_index(pool, league.id, league.current_season)
        to_index = await _target_index_for_n_more_per_team(pool, league.id, count)

        if not force:
            user_matchups = await batch_sim_runner.check_user_matchups_in_range(
                pool, league.id, league.current_season, current_index + 1, to_index
            )
            if user_matchups:
                embed = sim_embeds.user_matchup_warning(user_matchups)
                embed.set_footer(text="Use /sim games force:True to sim through them anyway.")
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

        summary = await batch_sim_runner.sim_range(
            league.id,
            interaction.guild,
            league.current_season,
            to_index,
            bot=self.bot,
            force=force,
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
            embed.add_field(name="Season Status", value="Regular season is now complete.", inline=False)
        await interaction.followup.send(embed=embed)
        if summary.get("user_matchups_simmed", 0) > 0:
            await game_repo.reset_ready(pool, league.id)

    @app_commands.command(name="games", description="[MOVED] Use /sim run instead")
    @app_commands.describe(
        count="Number of games for each team to play (1–20)",
        force="Skip the ready check and user-matchup warning",
    )
    async def games_legacy(
        self,
        interaction: discord.Interaction,
        count: int,
        force: bool = False,
    ) -> None:
        await safe_defer(interaction, ephemeral=True)
        await _send_sim_dep_warning(interaction, old="/sim games", new="/sim run")
        await self.run.callback(self, interaction, count, force)

    @app_commands.command(name="to-end", description="Sim to end of regular season")
    @app_commands.describe(force="Skip past user matchups without stopping")
    async def to_end(
        self,
        interaction: discord.Interaction,
        force: bool = False,
    ) -> None:
        await safe_defer(interaction)
        league = await get_league_or_error(interaction.guild_id)
        await require_commissioner(interaction, league)
        await require_phase(league, "sim_season")
        if not force:
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
                await safe_respond(interaction, embed=embed)
                return

        # Immediately acknowledge — sim takes 15-20+ min and the Discord webhook expires at 15min.
        # Results post to #league-news when done rather than via followup.
        pool = await get_pool()
        news_channel_id = await league_repo.get_channel(pool, league.id, "league-news")
        await safe_respond(
            interaction,
            content=(
                "Season sim started — updates will appear in #box-scores as games complete, "
                "and the final summary will post to #league-news when done.\n\n"
                "**Note:** the sim will auto-pause at the trade deadline and post a \U0001f6a8 notification "
                "to #league-news. Run `/sim season force:True` again after making any trades to complete the season."
            ),
            ephemeral=True,
        )

        async def _run_sim_and_post() -> None:
            summary = await batch_sim_runner.sim_range(
                league.id,
                interaction.guild,
                league.current_season,
                10000,
                force=force,
            )
            if news_channel_id:
                ch = interaction.guild.get_channel(news_channel_id)
                if ch:
                    if summary.get("warning"):
                        em = sim_embeds.user_matchup_warning(summary["user_matchups"])
                        await ch.send(embed=em)
                    else:
                        em = discord.Embed(
                            title="✅ Regular Season Complete",
                            description=f"Simmed {summary['games_simmed']} games. Use `/standings` to see the final standings, then `/playoffs seed` to start the playoffs.",
                            color=discord.Color.green(),
                        )
                        await ch.send(embed=em)

        _create_logged_task(_run_sim_and_post())

    @app_commands.command(name="season", description="[MOVED] Use /sim to-end instead")
    @app_commands.describe(force="Skip past user matchups without stopping")
    async def season_legacy(
        self,
        interaction: discord.Interaction,
        force: bool = False,
    ) -> None:
        await safe_defer(interaction)
        await _send_sim_dep_warning(interaction, old="/sim season", new="/sim to-end")
        await self.to_end.callback(self, interaction, force)

    @app_commands.command(name="to-deadline", description="Sim to the trade deadline and open the trade window")
    @app_commands.describe(force="Skip past user matchups before the deadline without stopping")
    async def to_deadline(
        self,
        interaction: discord.Interaction,
        force: bool = False,
    ) -> None:
        await safe_defer(interaction)
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
            await safe_respond(
                interaction,
                content="No schedule found. Run `/season start` first.",
                ephemeral=True,
            )
            return

        deadline_index = round(total * 0.67)
        current_index = await game_repo.get_current_index(pool, league.id, league.current_season)

        if current_index >= deadline_index:
            await safe_respond(
                interaction,
                content=(
                    f"Already at or past the trade deadline (game {deadline_index} of {total}). "
                    "Use `/league advance TRADE_DEADLINE_OPEN` to open the window manually."
                ),
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
                await safe_respond(interaction, embed=embed)
                return

        summary = await batch_sim_runner.sim_range(
            league.id,
            interaction.guild,
            league.current_season,
            deadline_index,
            bot=self.bot,
            force=force,
        )

        if summary.get("warning"):
            embed = sim_embeds.user_matchup_warning(summary["user_matchups"])
            await safe_respond(interaction, embed=embed)
            return

        await league_service.advance_phase(league.id, "TRADE_DEADLINE_OPEN")

        news_channel_id = await league_repo.get_channel(pool, league.id, "league-news")
        if news_channel_id:
            channel = interaction.guild.get_channel(news_channel_id)
            if channel and channel.id != interaction.channel_id:
                await channel.send(embed=sim_embeds.trade_deadline_open_embed())

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
        await safe_respond(interaction, embed=embed)

    @app_commands.command(name="deadline", description="[MOVED] Use /sim to-deadline instead")
    @app_commands.describe(force="Skip past user matchups before the deadline without stopping")
    async def deadline_legacy(
        self,
        interaction: discord.Interaction,
        force: bool = False,
    ) -> None:
        await safe_defer(interaction)
        await _send_sim_dep_warning(interaction, old="/sim deadline", new="/sim to-deadline")
        await self.to_deadline.callback(self, interaction, force)


class SimCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.bot.tree.add_command(SimGroup(bot))
        self.bot.tree.add_command(ready_command)
        self.bot.tree.add_command(force_ready_command)
        self.bot.tree.add_command(standings_command)
        self.bot.tree.add_command(schedule_command)
        self.bot.tree.add_command(box_score_command)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SimCog(bot))
