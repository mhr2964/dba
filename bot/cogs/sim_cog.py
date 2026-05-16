from __future__ import annotations

import asyncio
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.embeds import sim_embeds
from bot.embeds.sim_embeds import box_score_summary_embed, box_score_team_embed
from bot.ui.box_score_views import BoxScoreView
from core.errors import DBAError, PermissionError
from core.logging import get_logger
from data.db import get_pool
from data.repositories import game_repo, league_repo, team_repo
from phase.helpers import get_league_or_error, require_commissioner, require_phase, require_all_ready, require_no_pending_trades
from services import batch_sim_runner, league_service

log = get_logger(__name__)


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


class SimGroup(app_commands.Group, name="sim", description="Advance the league simulation"):

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot


    @app_commands.command(name="rivalry", description="Sim CPU games until the next user matchup")
    async def rivalry(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
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
                value="Both managers `/ready`, then `/sim rivalry` again to play your matchup.",
                inline=False,
            )
        await interaction.followup.send(embed=embed)
        if summary["games_simmed"] > 0:
            await game_repo.reset_ready(pool, league.id)

    @app_commands.command(name="games", description="Sim until every team has played N more games")
    @app_commands.describe(
        count="Number of games for each team to play (1–20)",
        force="Skip the ready check and user-matchup warning (does not override phase restrictions)",
    )
    async def games(
        self,
        interaction: discord.Interaction,
        count: int,
        force: bool = False,
    ) -> None:
        await interaction.response.defer()
        league = await get_league_or_error(interaction.guild_id)
        await require_commissioner(interaction, league)
        await require_phase(league, "sim_games")
        if not force:
            await require_all_ready(interaction, league)
        await require_no_pending_trades(league)

        if count < 1 or count > 20:
            await interaction.followup.send("Count must be between 1 and 20.", ephemeral=True)
            return

        # Immediately acknowledge so the user isn't staring at "DBA is thinking..."
        # for the full duration of the sim (up to ~2 min for count=20).
        await interaction.followup.send(
            f"Simming up to {count} games per team — check #box-scores for live recaps.",
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
        await game_repo.reset_ready(pool, league.id)

    @app_commands.command(name="season", description="Sim to end of regular season")
    @app_commands.describe(force="Skip past user matchups without stopping")
    async def season(
        self,
        interaction: discord.Interaction,
        force: bool = False,
    ) -> None:
        await interaction.response.defer()
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
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

        # Immediately acknowledge — sim takes 15-20+ min and the Discord webhook expires at 15min.
        # Results post to #league-news when done rather than via followup.
        pool = await get_pool()
        news_channel_id = await league_repo.get_channel(pool, league.id, "league-news")
        await interaction.followup.send(
            "Season sim started — updates will appear in #box-scores as games complete, "
            "and the final summary will post to #league-news when done. "
            "This may take 15-20 minutes.",
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

        import asyncio as _asyncio
        _asyncio.create_task(_run_sim_and_post())

    @app_commands.command(name="deadline", description="Sim to the trade deadline and open the trade window")
    @app_commands.describe(force="Skip past user matchups before the deadline without stopping")
    async def deadline(
        self,
        interaction: discord.Interaction,
        force: bool = False,
    ) -> None:
        await interaction.response.defer()
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
            await interaction.followup.send(
                "No schedule found. Run `/season start` first.", ephemeral=True
            )
            return

        deadline_index = round(total * 0.67)
        current_index = await game_repo.get_current_index(pool, league.id, league.current_season)

        if current_index >= deadline_index:
            await interaction.followup.send(
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
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

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
        current_index = await game_repo.get_current_index(pool, league.id, league.current_season)
        next_games = await pool.fetch(
            "SELECT * FROM games WHERE league_id=$1 AND season=$2 AND game_index=$3",
            league.id, league.current_season, current_index + 1,
        )
        next_game = dict(next_games[0]) if next_games else None

        if next_game and next_game.get("is_user_matchup") and next_game.get("status") == "scheduled":
            # User vs user game — fire auto-sim in background, no commissioner needed
            asyncio.create_task(_auto_sim_and_advance(league, interaction.guild, pool))
        else:
            # CPU vs CPU next — batch-sim to the next rivalry
            asyncio.create_task(_batch_advance(league, interaction.guild))


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
        f"Force-readied {len(human_teams)} manager(s). Now run `/sim rivalry` to advance.", ephemeral=True
    )


@app_commands.command(name="standings", description="Show current standings")
async def standings_command(interaction: discord.Interaction) -> None:
    pool = await get_pool()
    league = await league_repo.get_by_guild(pool, interaction.guild_id)
    if not league:
        await interaction.response.send_message("No active league.", ephemeral=True)
        return

    rows = await game_repo.get_standings(pool, league.id, league.current_season)
    teams = await team_repo.get_all(pool, league.id)
    teams_by_id = {t.id: t for t in teams}

    if not rows:
        # Pre-season: synthesise 0-0 rows from the teams table so standings is useful from day 1.
        rows = [
            {"team_id": t.id, "wins": 0, "losses": 0, "conference": t.conference,
             "division": t.division, "streak": 0, "last_10_wins": 0, "last_10_losses": 0}
            for t in teams
        ]

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


@app_commands.command(name="box-score", description="Show full box score for a simmed game")
@app_commands.describe(game="Game number from /schedule or recap footers")
async def box_score_command(
    interaction: discord.Interaction,
    game: int,
) -> None:
    await interaction.response.defer(ephemeral=True)

    pool = await get_pool()
    league = await get_league_or_error(interaction.guild_id)

    game_row = await game_repo.get_game_by_index(pool, league.id, league.current_season, game)
    if not game_row or game_row.get("status") != "simmed":
        await interaction.followup.send(
            f"Game #{game} hasn't been simmed yet.", ephemeral=True
        )
        return

    box_rows = await game_repo.get_box_scores_for_game(pool, game_row["id"])
    if not box_rows:
        await interaction.followup.send(
            f"No box score data found for game #{game}.", ephemeral=True
        )
        return

    away_team_id = game_row["away_team_id"]
    home_team_id = game_row["home_team_id"]
    away_code    = game_row["away_code"]
    home_code    = game_row["home_code"]

    # Attach team_code to each row for top-performers labeling
    for row in box_rows:
        row["team_code"] = away_code if row["team_id"] == away_team_id else home_code

    away_box = [r for r in box_rows if r["team_id"] == away_team_id]
    home_box = [r for r in box_rows if r["team_id"] == home_team_id]

    summary_embed = box_score_summary_embed(game_row, away_box, home_box)
    away_embed    = box_score_team_embed(away_code, game_row["away_full_name"], away_box, game_row)
    home_embed    = box_score_team_embed(home_code, game_row["home_full_name"], home_box, game_row)

    view = BoxScoreView(
        summary_embed=summary_embed,
        away_embed=away_embed,
        home_embed=home_embed,
        away_code=away_code,
        home_code=home_code,
    )

    await interaction.followup.send(embed=summary_embed, view=view)


async def _auto_sim_and_advance(league, guild: discord.Guild, _pool) -> None:
    """
    Background task: sim the current user vs user matchup, post quarter-by-quarter
    feed to #box-scores, reset ready state, then batch-advance CPU games to the
    next user matchup and announce it to #league-news.

    The pool arg is ignored — a fresh pool is acquired inside to avoid holding a
    stale connection from the /ready handler.
    """
    pool = await get_pool()

    result = await batch_sim_runner.sim_single_matchup(
        league.id, guild, league.current_season
    )

    news_channel_id = await league_repo.get_channel(pool, league.id, "league-news")
    news_channel = guild.get_channel(news_channel_id) if news_channel_id else None

    if result is None:
        if news_channel:
            await news_channel.send(embed=sim_embeds.user_matchup_sim_failed_embed())
        return

    home_team = result["home_team"]
    away_team = result["away_team"]
    home_score: int = result["result"]["home_score"]
    away_score: int = result["result"]["away_score"]
    game_index: int = result["game"]["game_index"]

    hc = home_team.nba_team_code
    ac = away_team.nba_team_code
    res = result["result"]
    home_qs = [
        res.get("q1_home") or 0,
        res.get("q2_home") or 0,
        res.get("q3_home") or 0,
        res.get("q4_home") or home_score - (
            (res.get("q1_home") or 0) + (res.get("q2_home") or 0) + (res.get("q3_home") or 0)
        ),
    ]
    away_qs = [
        res.get("q1_away") or 0,
        res.get("q2_away") or 0,
        res.get("q3_away") or 0,
        res.get("q4_away") or away_score - (
            (res.get("q1_away") or 0) + (res.get("q2_away") or 0) + (res.get("q3_away") or 0)
        ),
    ]

    box_channel_id = await league_repo.get_channel(pool, league.id, "box-scores")
    box_channel = guild.get_channel(box_channel_id) if box_channel_id else None

    if box_channel:
        tipoff_embed = discord.Embed(
            title=f"🏀 TIPOFF — Game #{game_index}",
            color=discord.Color.orange(),
        )
        tipoff_embed.add_field(name="Away", value=f"`{ac}`", inline=True)
        tipoff_embed.add_field(name="@", value="—", inline=True)
        tipoff_embed.add_field(name="Home", value=f"`{hc}`", inline=True)
        tipoff_embed.set_footer(text=f"{away_team.full_name} vs {home_team.full_name}")
        await box_channel.send(embed=tipoff_embed)
        await asyncio.sleep(3)

        q1_embed = discord.Embed(title="End of Q1", color=discord.Color.light_gray())
        q1_embed.add_field(name=ac, value=str(away_qs[0]), inline=True)
        q1_embed.add_field(name="—", value="—", inline=True)
        q1_embed.add_field(name=hc, value=str(home_qs[0]), inline=True)
        await box_channel.send(embed=q1_embed)
        await asyncio.sleep(3)

        h2 = home_qs[0] + home_qs[1]
        a2 = away_qs[0] + away_qs[1]
        half_embed = discord.Embed(title="Halftime", color=discord.Color.light_gray())
        half_embed.add_field(name=ac, value=str(a2), inline=True)
        half_embed.add_field(name="—", value="—", inline=True)
        half_embed.add_field(name=hc, value=str(h2), inline=True)
        await box_channel.send(embed=half_embed)
        await asyncio.sleep(3)

        h3 = h2 + home_qs[2]
        a3 = a2 + away_qs[2]
        q3_embed = discord.Embed(title="End of Q3", color=discord.Color.light_gray())
        q3_embed.add_field(name=ac, value=str(a3), inline=True)
        q3_embed.add_field(name="—", value="—", inline=True)
        q3_embed.add_field(name=hc, value=str(h3), inline=True)
        await box_channel.send(embed=q3_embed)
        await asyncio.sleep(3)

        winner_name = home_team.full_name if home_score > away_score else away_team.full_name
        final_embed = discord.Embed(
            title=f"🏆 FINAL — {winner_name} wins!",
            color=discord.Color.green(),
        )
        final_embed.add_field(
            name=f"`{ac}` {away_team.full_name}", value=str(away_score), inline=True
        )
        final_embed.add_field(name="—", value="—", inline=True)
        final_embed.add_field(
            name=f"`{hc}` {home_team.full_name}", value=str(home_score), inline=True
        )
        final_embed.set_footer(text=f"Game #{game_index}")
        await box_channel.send(embed=final_embed)

        box_recap_embed = sim_embeds.batch_recap([result], f"Game #{game_index} — Box Score")
        await box_channel.send(embed=box_recap_embed)

    await game_repo.reset_ready(pool, league.id)

    summary = await batch_sim_runner.sim_until_rival(
        league.id, guild, league.current_season, suppress_matchup_alert=True
    )

    if news_channel:
        next_matchup = summary.get("next_matchup")
        if next_matchup:
            ht = await team_repo.get_by_id(pool, next_matchup["home_team_id"])
            at = await team_repo.get_by_id(pool, next_matchup["away_team_id"])
            home_mgr = guild.get_member(ht.manager_user_id) if ht and ht.manager_user_id else None
            away_mgr = guild.get_member(at.manager_user_id) if at and at.manager_user_id else None
            home_code = ht.nba_team_code if ht else "???"
            away_code = at.nba_team_code if at else "???"
            home_mention = home_mgr.mention if home_mgr else "(no manager)"
            away_mention = away_mgr.mention if away_mgr else "(no manager)"
            next_idx = next_matchup.get("game_index", "?")

            matchup_embed = discord.Embed(
                title="⚡ Upcoming Matchup",
                color=discord.Color.blue(),
            )
            matchup_embed.add_field(name="Away", value=f"`{away_code}` — {away_mention}", inline=True)
            matchup_embed.add_field(name="vs", value="—", inline=True)
            matchup_embed.add_field(name="Home", value=f"`{home_code}` — {home_mention}", inline=True)
            matchup_embed.set_footer(text=f"Game #{next_idx} · Both managers /ready to play")
            await news_channel.send(embed=matchup_embed)


async def _batch_advance(league, guild: discord.Guild) -> None:
    """Background task: when all managers ready but next game is CPU vs CPU, auto-sim to the next matchup."""
    pool = await get_pool()
    summary = await batch_sim_runner.sim_until_rival(
        league.id, guild, league.current_season, suppress_matchup_alert=True
    )

    news_channel_id = await league_repo.get_channel(pool, league.id, "league-news")
    news_channel = guild.get_channel(news_channel_id) if news_channel_id else None

    if news_channel:
        next_matchup = summary.get("next_matchup")
        if next_matchup:
            ht = await team_repo.get_by_id(pool, next_matchup["home_team_id"])
            at = await team_repo.get_by_id(pool, next_matchup["away_team_id"])
            home_mgr = guild.get_member(ht.manager_user_id) if ht and ht.manager_user_id else None
            away_mgr = guild.get_member(at.manager_user_id) if at and at.manager_user_id else None
            home_code = ht.nba_team_code if ht else "???"
            away_code = at.nba_team_code if at else "???"
            home_mention = home_mgr.mention if home_mgr else "(no manager)"
            away_mention = away_mgr.mention if away_mgr else "(no manager)"
            next_idx = next_matchup.get("game_index", "?")

            matchup_embed = discord.Embed(
                title="⚡ Upcoming Matchup",
                color=discord.Color.blue(),
            )
            matchup_embed.add_field(
                name="Away", value=f"`{away_code}` — {away_mention}", inline=True
            )
            matchup_embed.add_field(name="vs", value="—", inline=True)
            matchup_embed.add_field(
                name="Home", value=f"`{home_code}` — {home_mention}", inline=True
            )
            matchup_embed.set_footer(
                text=f"Game #{next_idx} · Both managers /ready to play"
            )
            await news_channel.send(embed=matchup_embed)


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
