from __future__ import annotations

import asyncio
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.embeds import season_embeds
from bot.embeds import sim_embeds
from bot.embeds.sim_embeds import box_score_summary_embed, box_score_team_embed
from bot.ui.box_score_views import BoxScoreView
from core.errors import safe_defer, safe_respond
from core.logging import get_logger
from data.db import get_pool
from data.repositories import game_repo, league_repo, team_repo
from phase.helpers import get_league_or_error, require_commissioner, require_phase
from services import import_service, league_service, schedule_service

log = get_logger(__name__)


class SeasonGroup(app_commands.Group, name="season", description="Season management commands"):

    @app_commands.command(name="start", description="Commissioner: generate schedule and start the season")
    @app_commands.describe(season_year="Override the season year (defaults to current league season)")
    async def start(
        self,
        interaction: discord.Interaction,
        season_year: Optional[int] = None,
    ) -> None:
        await safe_defer(interaction)
        league = await get_league_or_error(interaction.guild_id)
        await require_commissioner(interaction, league)
        await require_phase(league, "season_start")

        season = season_year if season_year is not None else league.current_season
        try:
            game_count = await schedule_service.generate_season(league.id, season)
        except Exception as exc:
            log.error(f"/season start failed for league {league.id}: {exc}", exc_info=True)
            await safe_respond(
                interaction,
                content=f"Schedule generation failed: {exc}\n\nCheck that the league has 30 teams (`/team list`).",
                ephemeral=True,
            )
            return

        await league_service.advance_phase(league.id, "REGULAR_SEASON_ACTIVE")

        pool = await get_pool()
        await pool.execute(
            "UPDATE ready_status SET is_ready = FALSE, last_updated = NOW() WHERE league_id = $1",
            league.id,
        )

        rows = await pool.fetch(
            """
            SELECT g.game_index, g.scheduled_date, g.is_user_matchup,
                   ht.nba_team_code AS home_code, at.nba_team_code AS away_code
            FROM games g
            JOIN teams ht ON ht.id = g.home_team_id
            JOIN teams at ON at.id = g.away_team_id
            WHERE g.league_id = $1 AND g.season = $2
            ORDER BY g.game_index ASC
            LIMIT 5
            """,
            league.id,
            season,
        )
        first_games = [dict(r) for r in rows]

        # Reload league to reflect the updated phase for the embed
        updated_league = await league_repo.get_by_guild(pool, interaction.guild_id)
        target_league = updated_league if updated_league else league

        embed = season_embeds.season_started_embed(target_league, game_count, first_games)

        news_channel_id = await league_repo.get_channel(pool, league.id, "league-news")
        if news_channel_id:
            channel = interaction.guild.get_channel(news_channel_id)
            if channel:
                await channel.send(embed=embed)

        await safe_respond(interaction, embed=embed)
        log.info(f"Season {season} started for league {league.id} by {interaction.user.id}")

    @app_commands.command(name="import-players", description="Commissioner: import NBA rosters from nba_api")
    @app_commands.describe(season_year="Season start year to import (defaults to current league season)")
    async def import_players(
        self,
        interaction: discord.Interaction,
        season_year: Optional[int] = None,
    ) -> None:
        await safe_defer(interaction, ephemeral=True)

        league = await get_league_or_error(interaction.guild_id)
        await require_commissioner(interaction, league)

        season = season_year if season_year is not None else league.current_season

        # Acknowledge immediately — import takes 2-5 min and Discord's token expires in 15 min
        await safe_respond(
            interaction,
            content=(
                f"Importing NBA rosters for season **{season}**. This takes a few minutes. "
                "Results will be posted to **#league-news** when complete."
            ),
            ephemeral=True,
        )

        async def _run_import() -> None:
            try:
                result = await import_service.import_players_from_api(
                    league_id=league.id,
                    season=season,
                    guild=interaction.guild,
                )
                pool = await get_pool()
                news_channel_id = await league_repo.get_channel(pool, league.id, "league-news")
                if news_channel_id:
                    channel = interaction.guild.get_channel(news_channel_id)
                    if channel:
                        skipped = result.get("teams_skipped", 0)
                        imported = result["teams_imported"]
                        status_parts = [f"{imported} team(s) imported"]
                        if skipped:
                            status_parts.append(f"{skipped} already up to date")
                        embed = discord.Embed(
                            title="Player Import Complete",
                            color=discord.Color.green() if not result["errors"] else discord.Color.orange(),
                        )
                        embed.add_field(name="Teams", value=", ".join(status_parts), inline=True)
                        embed.add_field(name="Players Imported", value=str(result["players_imported"]), inline=True)
                        if result["errors"]:
                            error_text = "\n".join(result["errors"][:10])
                            if len(result["errors"]) > 10:
                                error_text += f"\n… and {len(result['errors']) - 10} more"
                            embed.add_field(name="Errors", value=error_text, inline=False)
                        await channel.send(embed=embed)
                log.info(
                    f"import-players: league={league.id} season={season} "
                    f"teams={result['teams_imported']} skipped={result.get('teams_skipped', 0)} "
                    f"players={result['players_imported']} errors={len(result['errors'])}"
                )
            except Exception as exc:
                log.error(f"import-players task failed: league={league.id} season={season} — {exc}", exc_info=True)
                try:
                    pool = await get_pool()
                    news_channel_id = await league_repo.get_channel(pool, league.id, "league-news")
                    if news_channel_id:
                        channel = interaction.guild.get_channel(news_channel_id)
                        if channel:
                            embed = discord.Embed(
                                title="Player Import Failed",
                                description=f"An unexpected error occurred: {exc}",
                                color=discord.Color.red(),
                            )
                            await channel.send(embed=embed)
                except Exception:
                    log.error("import-players: could not post failure embed", exc_info=True)

        asyncio.create_task(_run_import())
        log.info(f"import-players task started: league={league.id} season={season} by={interaction.user.id}")

    @app_commands.command(name="info", description="Show current season overview and top standings")
    async def info(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction)
        league = await get_league_or_error(interaction.guild_id)
        pool = await get_pool()

        games_played = await game_repo.get_current_index(pool, league.id, league.current_season)

        total_games_val = await pool.fetchval(
            "SELECT COUNT(*) FROM games WHERE league_id = $1 AND season = $2 AND season_type = 'regular'",
            league.id,
            league.current_season,
        )
        total_games = total_games_val or 0

        all_standings = await game_repo.get_standings(pool, league.id, league.current_season)
        east_top3 = [r for r in all_standings if r.get("conference") == "East"][:3]
        west_top3 = [r for r in all_standings if r.get("conference") == "West"][:3]

        embed = season_embeds.season_info_embed(
            league, games_played, total_games, east_top3, west_top3
        )
        await safe_respond(interaction, embed=embed)

    @app_commands.command(name="schedule", description="Show a team's next 10 scheduled games")
    @app_commands.describe(team_code="NBA team code (e.g. LAL) — defaults to your team")
    async def schedule(
        self,
        interaction: discord.Interaction,
        team_code: Optional[str] = None,
    ) -> None:
        await safe_defer(interaction)
        league = await get_league_or_error(interaction.guild_id)
        pool = await get_pool()

        if team_code:
            team = await team_repo.get_by_code(pool, league.id, team_code)
            if not team:
                await safe_respond(
                    interaction,
                    content=f"No team found with code **{team_code.upper()}**.",
                    ephemeral=True,
                )
                return
        else:
            team = await team_repo.get_by_manager(pool, league.id, interaction.user.id)
            if not team:
                await safe_respond(
                    interaction,
                    content="You don't manage a team. Provide a `team_code` argument.",
                    ephemeral=True,
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
            await safe_respond(interaction, content="No upcoming games found.", ephemeral=True)
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
        await safe_respond(interaction, embed=embed)


    @app_commands.command(name="standings", description="Show current standings")
    async def standings(self, interaction: discord.Interaction) -> None:
        pool = await get_pool()
        league = await league_repo.get_by_guild(pool, interaction.guild_id)
        if not league:
            await interaction.response.send_message("No active league.", ephemeral=True)
            return

        rows = await game_repo.get_standings(pool, league.id, league.current_season)
        teams = await team_repo.get_all(pool, league.id)
        teams_by_id = {t.id: t for t in teams}

        if not rows:
            rows = [
                {"team_id": t.id, "wins": 0, "losses": 0, "conference": t.conference,
                 "division": t.division, "streak": 0, "last_10_wins": 0, "last_10_losses": 0}
                for t in teams
            ]

        embed = sim_embeds.standings_embed(rows, teams_by_id)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="box-score", description="Show full box score for a simmed game")
    @app_commands.describe(
        game="Game number from /season schedule or recap footers (regular season)",
        game_id="Game ID from playoff recap footer (use this for playoff games)",
    )
    async def box_score(
        self,
        interaction: discord.Interaction,
        game: Optional[int] = None,
        game_id: Optional[int] = None,
    ) -> None:
        await safe_defer(interaction, ephemeral=True)

        if game is None and game_id is None:
            await safe_respond(
                interaction,
                content="Provide either `game` (game number) or `game_id` (for playoff games).",
                ephemeral=True,
            )
            return

        pool = await get_pool()
        league = await get_league_or_error(interaction.guild_id)

        if game_id is not None:
            game_row = await game_repo.get_game_by_id(pool, league.id, game_id)
            lookup_label = f"ID #{game_id}"
        else:
            game_row = await game_repo.get_game_by_index(
                pool, league.id, league.current_season, game
            )
            lookup_label = f"#{game}"

        if not game_row or game_row.get("status") != "simmed":
            await safe_respond(
                interaction,
                content=f"Game {lookup_label} hasn't been simmed yet (or doesn't exist).",
                ephemeral=True,
            )
            return

        box_rows = await game_repo.get_box_scores_for_game(pool, game_row["id"])
        if not box_rows:
            await safe_respond(
                interaction,
                content=f"No box score data found for game {lookup_label}.",
                ephemeral=True,
            )
            return

        away_team_id = game_row["away_team_id"]
        home_team_id = game_row["home_team_id"]
        away_code    = game_row["away_code"]
        home_code    = game_row["home_code"]

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
        await safe_respond(interaction, embed=summary_embed, view=view)


class SeasonCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.bot.tree.add_command(SeasonGroup())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SeasonCog(bot))
