from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.embeds import playoff_embeds
from core.errors import DBAError, PermissionError
from core.logging import get_logger
from data.db import get_pool
from data.repositories import league_repo, series_repo, team_repo
from phase.states import Phase
from services import league_service, playoff_service

log = get_logger(__name__)


async def _require_league(guild_id: int):
    pool = await get_pool()
    league = await league_repo.get_by_guild(pool, guild_id)
    if not league:
        raise DBAError("No active league found. Use `/league create` first.")
    return league


async def _require_commissioner(interaction: discord.Interaction):
    league = await _require_league(interaction.guild_id)
    if league.commissioner_user_id != interaction.user.id:
        raise PermissionError("Only the commissioner can use this command.")
    return league


class PlayoffsGroup(app_commands.Group, name="playoffs", description="Playoff management"):

    @app_commands.command(name="seed", description="Seed the playoffs from final standings")
    async def seed(self, interaction: discord.Interaction) -> None:
        league = await _require_commissioner(interaction)

        if league.current_phase != "REGULAR_SEASON_COMPLETE":
            await interaction.response.send_message(
                "Regular season must be complete before seeding playoffs. "
                f"Current phase: `{league.current_phase}`",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        pool = await get_pool()
        try:
            bracket = await playoff_service.seed_playoffs(league.id, league.current_season)
        except DBAError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        all_series = (
            bracket["playin_east"]
            + bracket["playin_west"]
            + bracket["east_bracket"]
            + bracket["west_bracket"]
        )

        teams = await team_repo.get_all(pool, league.id)
        teams_by_id = {t.id: t for t in teams}

        playin_series = bracket["playin_east"] + bracket["playin_west"]
        playin_em = playoff_embeds.playin_embed(playin_series, teams_by_id)

        r1_series = bracket["east_bracket"] + bracket["west_bracket"]
        r1_embed = playoff_embeds.bracket_embed(r1_series, teams_by_id)

        news_channel_id = await league_repo.get_channel(pool, league.id, "league-news")
        if news_channel_id:
            channel = interaction.guild.get_channel(news_channel_id)
            if channel:
                await channel.send(embed=playin_em)
                await channel.send(embed=r1_embed)

        await league_service.advance_phase(league.id, Phase.PLAYIN_ACTIVE.value)

        await interaction.followup.send(
            f"Playoffs seeded for season {league.current_season}. "
            f"Run `/playoffs sim-playin` to resolve the play-in tournament.",
            embeds=[playin_em, r1_embed],
        )

    @app_commands.command(name="sim-game", description="Sim the next game in a playoff series")
    @app_commands.describe(series_id="ID of the series to advance")
    async def sim_game(self, interaction: discord.Interaction, series_id: int) -> None:
        league = await _require_commissioner(interaction)
        await interaction.response.defer()

        pool = await get_pool()

        try:
            outcome = await playoff_service.sim_series_game(
                league.id, series_id, interaction.guild, league.current_season
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        series = outcome["series"]
        teams = await team_repo.get_all(pool, league.id)
        teams_by_id = {t.id: t for t in teams}

        high_team = teams_by_id.get(series.high_seed_team_id)
        low_team = teams_by_id.get(series.low_seed_team_id)

        series_em = playoff_embeds.series_embed(series, high_team, low_team)
        await interaction.followup.send(embed=series_em)

        if outcome["series_over"]:
            winner = teams_by_id.get(outcome["winner_team_id"])
            series_em.title = f"Series Over — {winner.full_name if winner else 'Winner'} Advance"
            news_channel_id = await league_repo.get_channel(pool, league.id, "league-news")
            if news_channel_id:
                channel = interaction.guild.get_channel(news_channel_id)
                if channel:
                    await channel.send(embed=series_em)

            next_round = await playoff_service.advance_playoff_round(league.id, league.current_season, guild=interaction.guild)

            if next_round == "champion":
                champ_embed = playoff_embeds.champion_embed(winner)
                if news_channel_id:
                    channel = interaction.guild.get_channel(news_channel_id)
                    if channel:
                        await channel.send(embed=champ_embed)
                await interaction.followup.send(embed=champ_embed)

            elif next_round != "pending":
                all_series = await series_repo.get_bracket(pool, league.id, league.current_season)
                bracket_em = playoff_embeds.bracket_embed(all_series, teams_by_id)
                bracket_em.title = f"Round {next_round} — Updated Bracket"
                if news_channel_id:
                    channel = interaction.guild.get_channel(news_channel_id)
                    if channel:
                        await channel.send(embed=bracket_em)
                await interaction.followup.send(embed=bracket_em)

    @app_commands.command(name="sim-round", description="Sim all remaining games in the current playoff round")
    async def sim_round(self, interaction: discord.Interaction) -> None:
        league = await _require_commissioner(interaction)
        await interaction.response.defer()

        pool = await get_pool()
        teams = await team_repo.get_all(pool, league.id)
        teams_by_id = {t.id: t for t in teams}

        all_series = await series_repo.get_bracket(pool, league.id, league.current_season)
        pending = [s for s in all_series if s.status == "active"]
        if not pending:
            await interaction.followup.send("No active series to sim.", ephemeral=True)
            return

        await interaction.followup.send(
            f"Simming {len(pending)} series — game recaps will appear in #box-scores. "
            "Full bracket posts when the round is complete.",
            ephemeral=True,
        )

        games_simmed = 0
        for series in pending:
            while True:
                try:
                    outcome = await playoff_service.sim_series_game(
                        league.id, series.id, interaction.guild, league.current_season
                    )
                except ValueError:
                    break
                games_simmed += 1
                if outcome["series_over"]:
                    break

        # Advance to the next round — creates next-round series rows and returns the round name.
        next_round = await playoff_service.advance_playoff_round(
            league.id, league.current_season, guild=interaction.guild
        )

        # Advance league phase to match the new round.
        _ROUND_TO_PHASE = {
            "r2_east": Phase.PLAYOFFS_R2,
            "r2_west": Phase.PLAYOFFS_R2,
            "conference_finals_east": Phase.CONFERENCE_FINALS,
            "conference_finals_west": Phase.CONFERENCE_FINALS,
            "nba_finals": Phase.NBA_FINALS,
            "champion": Phase.OFFSEASON_AWARDS_CLOSED,
        }
        for key, phase in _ROUND_TO_PHASE.items():
            if key in next_round:
                try:
                    await league_service.advance_phase(league.id, phase.value)
                except Exception:
                    pass
                break

        all_series = await series_repo.get_bracket(pool, league.id, league.current_season)
        bracket_em = playoff_embeds.bracket_embed(all_series, teams_by_id)

        news_channel_id = await league_repo.get_channel(pool, league.id, "league-news")

        if next_round == "champion":
            winner_id = next(
                (s.winner_team_id for s in all_series if s.round == "nba_finals"), None
            )
            winner = teams_by_id.get(winner_id) if winner_id else None
            champ_em = playoff_embeds.champion_embed(winner)
            if news_channel_id:
                channel = interaction.guild.get_channel(news_channel_id)
                if channel:
                    try:
                        await channel.send(embed=champ_em)
                    except Exception:
                        pass
            await interaction.followup.send(
                f"Round complete — {games_simmed} games simmed.", embed=champ_em
            )
        else:
            bracket_em.title = f"Round Complete — {games_simmed} games simmed"
            if news_channel_id:
                channel = interaction.guild.get_channel(news_channel_id)
                if channel:
                    try:
                        await channel.send(embed=bracket_em)
                    except Exception:
                        pass
            await interaction.followup.send(embed=bracket_em)

    @app_commands.command(name="sim-playin", description="Sim the entire play-in tournament")
    async def sim_playin(self, interaction: discord.Interaction) -> None:
        league = await _require_commissioner(interaction)
        await interaction.response.defer()

        pool = await get_pool()

        seeds = await playoff_service.sim_play_in(
            league.id, league.current_season, interaction.guild
        )

        teams = await team_repo.get_all(pool, league.id)
        teams_by_id = {t.id: t for t in teams}

        def _name(team_id: int) -> str:
            t = teams_by_id.get(team_id)
            return t.full_name if t else str(team_id)

        playin_series = await series_repo.get_series_by_round(
            pool, league.id, league.current_season, "play_in_east"
        ) + await series_repo.get_series_by_round(
            pool, league.id, league.current_season, "play_in_west"
        )

        result_embed = playoff_embeds.playin_embed(playin_series, teams_by_id)
        result_embed.add_field(
            name="East Seeds",
            value=f"7: {_name(seeds['east_7_seed'])}\n8: {_name(seeds['east_8_seed'])}",
            inline=True,
        )
        result_embed.add_field(
            name="West Seeds",
            value=f"7: {_name(seeds['west_7_seed'])}\n8: {_name(seeds['west_8_seed'])}",
            inline=True,
        )

        await league_service.advance_phase(league.id, Phase.PLAYOFFS_R1.value)

        news_channel_id = await league_repo.get_channel(pool, league.id, "league-news")
        if news_channel_id:
            channel = interaction.guild.get_channel(news_channel_id)
            if channel:
                await channel.send(embed=result_embed)

        await interaction.followup.send(embed=result_embed)

    @app_commands.command(name="bracket", description="Show the current playoff bracket")
    async def bracket(self, interaction: discord.Interaction) -> None:
        league = await _require_league(interaction.guild_id)
        pool = await get_pool()

        all_series = await series_repo.get_bracket(pool, league.id, league.current_season)
        if not all_series:
            await interaction.response.send_message(
                "No playoff bracket found. Run `/playoffs seed` first.", ephemeral=True
            )
            return

        teams = await team_repo.get_all(pool, league.id)
        teams_by_id = {t.id: t for t in teams}

        embed = playoff_embeds.bracket_embed(all_series, teams_by_id)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="series", description="Show a specific series")
    @app_commands.describe(series_id="ID of the series to display")
    async def series(self, interaction: discord.Interaction, series_id: int) -> None:
        await _require_league(interaction.guild_id)
        pool = await get_pool()

        s = await series_repo.get_series(pool, series_id)
        if s is None:
            await interaction.response.send_message(
                f"Series `{series_id}` not found.", ephemeral=True
            )
            return

        high_team = await team_repo.get_by_id(pool, s.high_seed_team_id)
        low_team = await team_repo.get_by_id(pool, s.low_seed_team_id)

        embed = playoff_embeds.series_embed(s, high_team, low_team)
        await interaction.response.send_message(embed=embed)


class PlayoffCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.bot.tree.add_command(PlayoffsGroup())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PlayoffCog(bot))
