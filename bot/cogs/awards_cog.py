from __future__ import annotations

import asyncio
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.embeds import awards_embeds
from core.errors import DBAError, PermissionError, safe_defer, safe_respond
from core.logging import get_logger
from data.db import get_pool
from data.repositories import league_repo, player_repo, team_repo
from services import awards_service

log = get_logger(__name__)

# Tracked background tasks — prevents GC cancelling in-flight coroutines and
# captures exceptions that would otherwise be silently swallowed.
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


_SINGLE_AWARDS = ["mvp", "dpoy", "roy", "6moy", "finals_mvp"]
_ALL_AWARD_CHOICES = [
    app_commands.Choice(name="MVP", value="mvp"),
    app_commands.Choice(name="DPOY", value="dpoy"),
    app_commands.Choice(name="ROY", value="roy"),
    app_commands.Choice(name="6MOY", value="6moy"),
    app_commands.Choice(name="All-NBA", value="all_nba"),
    app_commands.Choice(name="All-Star", value="all_star"),
    app_commands.Choice(name="Finals MVP", value="finals_mvp"),
]


async def _require_league(guild_id: int) -> league_repo.League:
    pool = await get_pool()
    league = await league_repo.get_by_guild(pool, guild_id)
    if not league:
        raise DBAError("No active league. Use `/league create` first.")
    return league


async def _require_commissioner(interaction: discord.Interaction) -> league_repo.League:
    league = await _require_league(interaction.guild_id)
    if league.commissioner_user_id != interaction.user.id:
        raise PermissionError("Only the commissioner can use this command.")
    return league


async def _require_team_manager(
    interaction: discord.Interaction, league: league_repo.League
) -> team_repo.Team:
    pool = await get_pool()
    team = await team_repo.get_by_manager(pool, league.id, interaction.user.id)
    if not team:
        raise PermissionError("You don't manage a team in this league.")
    return team


async def _post_to_news(
    guild: discord.Guild, league_id: int, pool, embed: discord.Embed
) -> None:
    channel_id = await league_repo.get_channel(pool, league_id, "league-news")
    if channel_id:
        channel = guild.get_channel(channel_id)
        if channel:
            await channel.send(embed=embed)


async def _fetch_top_candidates(
    pool, league_id: int, season: int, award_type: str
) -> list[dict]:
    """Returns up to 5 stat leaders as candidate dicts for the voting_open_embed."""
    rows = await pool.fetch(
        """
        SELECT
            p.id,
            p.first_name || ' ' || p.last_name   AS name,
            AVG(b.points)                          AS ppg,
            AVG(b.rebounds_off + b.rebounds_def)   AS rpg,
            AVG(b.assists)                          AS apg
        FROM players p
        JOIN game_box_scores b ON b.player_id = p.id
        JOIN games g ON g.id = b.game_id
        WHERE g.league_id = $1
          AND g.season    = $2
          AND g.season_type = 'regular'
          AND p.league_id   = $1
        GROUP BY p.id
        HAVING COUNT(b.id) > 20
        ORDER BY AVG(b.points) DESC
        LIMIT 5
        """,
        league_id,
        season,
    )
    return [
        {
            "player_id": r["id"],
            "name": r["name"],
            "ppg": float(r["ppg"] or 0),
            "rpg": float(r["rpg"] or 0),
            "apg": float(r["apg"] or 0),
        }
        for r in rows
    ]


class AwardsGroup(app_commands.Group, name="awards", description="League awards"):

    @app_commands.command(name="open", description="Open award voting (commissioner only)")
    @app_commands.describe(award_type="Which award to open voting for")
    @app_commands.choices(award_type=_ALL_AWARD_CHOICES)
    async def open_award(
        self,
        interaction: discord.Interaction,
        award_type: app_commands.Choice[str],
    ) -> None:
        await safe_defer(interaction)
        league = await _require_commissioner(interaction)

        pool = await get_pool()
        season = league.current_season
        value = award_type.value

        if value == "all_star":
            east_id, west_id = await awards_service.open_all_star_voting(league.id, season)
            for vid, at in [(east_id, "all_star_east"), (west_id, "all_star_west")]:
                candidates = await _fetch_top_candidates(pool, league.id, season, at)
                voting_row = {
                    "id": vid,
                    "league_id": league.id,
                    "season": season,
                    "award_type": at,
                    "status": "open",
                }
                embed = awards_embeds.voting_open_embed(voting_row, at, candidates)
                await _post_to_news(interaction.guild, league.id, pool, embed)
                _create_logged_task(
                    awards_service.generate_cpu_votes(vid, league.id, season)
                )
            await safe_respond(
                interaction,
                content="All-Star voting opened for East and West.",
                ephemeral=True,
            )
            return

        if value == "all_nba":
            voting_ids: list[int] = []
            for at in ["all_nba_1", "all_nba_2", "all_nba_3"]:
                vid = await awards_service.open_voting(league.id, season, at)
                voting_ids.append(vid)
                candidates = await _fetch_top_candidates(pool, league.id, season, at)
                voting_row = {
                    "id": vid,
                    "league_id": league.id,
                    "season": season,
                    "award_type": at,
                    "status": "open",
                }
                embed = awards_embeds.voting_open_embed(voting_row, at, candidates)
                await _post_to_news(interaction.guild, league.id, pool, embed)
                _create_logged_task(
                    awards_service.generate_cpu_votes(vid, league.id, season)
                )
            await safe_respond(
                interaction,
                content="All-NBA 1st/2nd/3rd team voting opened.",
                ephemeral=True,
            )
            return

        voting_id = await awards_service.open_voting(league.id, season, value)
        candidates = await _fetch_top_candidates(pool, league.id, season, value)
        voting_row = {
            "id": voting_id,
            "league_id": league.id,
            "season": season,
            "award_type": value,
            "status": "open",
        }
        embed = awards_embeds.voting_open_embed(voting_row, value, candidates)
        await _post_to_news(interaction.guild, league.id, pool, embed)
        _create_logged_task(
            awards_service.generate_cpu_votes(voting_id, league.id, season)
        )
        await safe_respond(
            interaction,
            content=f"{award_type.name} voting opened.",
            ephemeral=True,
        )

    @app_commands.command(name="vote", description="Cast your vote for an award")
    @app_commands.describe(
        award_type="Which award to vote for",
        player="Player name to vote for (e.g. LeBron James)",
        rank="All-NBA team rank: 1=First, 2=Second, 3=Third (required for All-NBA, ignored otherwise)",
    )
    @app_commands.choices(award_type=_ALL_AWARD_CHOICES)
    async def vote(
        self,
        interaction: discord.Interaction,
        award_type: app_commands.Choice[str],
        player: str,
        rank: Optional[int] = None,
    ) -> None:
        await safe_defer(interaction, ephemeral=True)
        league = await _require_league(interaction.guild_id)
        team = await _require_team_manager(interaction, league)

        pool = await get_pool()
        matches = await player_repo.search_by_name(pool, league.id, player)
        if not matches:
            raise DBAError(f"No player found matching '{player}'.")
        if len(matches) > 1:
            names = ", ".join(p.full_name for p in matches)
            raise DBAError(f"Multiple players match '{player}': {names}. Be more specific.")
        found_player = matches[0]
        player_id = found_player.id
        value = award_type.value

        # Resolve the actual award_type string for All-NBA (rank determines which).
        if value == "all_nba":
            if rank is None:
                raise DBAError(
                    "For All-NBA, you must specify `rank`: 1 (First), 2 (Second), or 3 (Third)."
                )
            if rank not in (1, 2, 3):
                raise DBAError("For All-NBA, rank must be 1 (First), 2 (Second), or 3 (Third).")
            at = f"all_nba_{rank}"
        elif value == "all_star":
            # A player belongs to exactly one conference, so there's no real
            # choice for the voter to make — derive east/west from their team.
            voted_team = await team_repo.get_by_id(pool, found_player.team_id)
            if not voted_team:
                raise DBAError(f"Could not determine a team for '{found_player.full_name}'.")
            at = "all_star_east" if voted_team.conference == "East" else "all_star_west"
        else:
            at = value

        voting_row = await pool.fetchrow(
            """
            SELECT id FROM award_votings
            WHERE league_id = $1 AND season = $2 AND award_type = $3 AND status = 'open'
            """,
            league.id,
            league.current_season,
            at,
        )
        if not voting_row:
            raise DBAError(f"No open {award_type.name} voting session found.")

        await awards_service.cast_vote(
            voting_id=voting_row["id"],
            team_id=team.id,
            player_id=player_id,
            rank=rank or 1,
        )

        await safe_respond(
            interaction,
            content=f"Your vote for **{found_player.full_name}** ({award_type.name}) has been recorded.",
            ephemeral=True,
        )

    @app_commands.command(name="close", description="Close voting and announce results (commissioner only)")
    @app_commands.describe(award_type="Which award to close")
    @app_commands.choices(award_type=_ALL_AWARD_CHOICES)
    async def close_award(
        self,
        interaction: discord.Interaction,
        award_type: app_commands.Choice[str],
    ) -> None:
        await safe_defer(interaction)
        league = await _require_commissioner(interaction)

        pool = await get_pool()
        value = award_type.value
        season = league.current_season

        if value == "all_nba":
            first_team: list[dict] = []
            second_team: list[dict] = []
            third_team: list[dict] = []

            for at, bucket in [
                ("all_nba_1", first_team),
                ("all_nba_2", second_team),
                ("all_nba_3", third_team),
            ]:
                vid_row = await pool.fetchrow(
                    "SELECT id FROM award_votings WHERE league_id=$1 AND season=$2 AND award_type=$3",
                    league.id, season, at,
                )
                if not vid_row:
                    continue
                results = await awards_service.close_voting(vid_row["id"])
                bucket.extend(results[:5])

            all_player_ids = list({
                r["player_id"]
                for bucket in [first_team, second_team, third_team]
                for r in bucket
            })
            players = {
                p.id: p
                for p in [await player_repo.get_by_id(pool, pid) for pid in all_player_ids]
                if p is not None
            }
            embed = awards_embeds.all_nba_embed(first_team, second_team, third_team, players)
            await _post_to_news(interaction.guild, league.id, pool, embed)
            await safe_respond(interaction, content="All-NBA teams announced.", ephemeral=True)
            return

        if value == "all_star":
            east_results: list[dict] = []
            west_results: list[dict] = []

            for at, bucket in [("all_star_east", east_results), ("all_star_west", west_results)]:
                vid_row = await pool.fetchrow(
                    "SELECT id FROM award_votings WHERE league_id=$1 AND season=$2 AND award_type=$3",
                    league.id, season, at,
                )
                if not vid_row:
                    continue
                results = await awards_service.close_voting(vid_row["id"])
                bucket.extend(results)

            all_player_ids = list({
                r["player_id"]
                for bucket in [east_results, west_results]
                for r in bucket
            })
            players = {
                p.id: p
                for p in [await player_repo.get_by_id(pool, pid) for pid in all_player_ids]
                if p is not None
            }
            embed = awards_embeds.all_star_embed(east_results, west_results, players)
            await _post_to_news(interaction.guild, league.id, pool, embed)
            await safe_respond(interaction, content="All-Star rosters announced.", ephemeral=True)
            return

        vid_row = await pool.fetchrow(
            "SELECT id FROM award_votings WHERE league_id=$1 AND season=$2 AND award_type=$3",
            league.id, season, value,
        )
        if not vid_row:
            raise DBAError(f"No {award_type.name} voting session found for season {season}.")

        results = await awards_service.close_voting(vid_row["id"])
        all_player_ids = [r["player_id"] for r in results]
        players = {
            p.id: p
            for p in [await player_repo.get_by_id(pool, pid) for pid in all_player_ids]
            if p is not None
        }
        embed = awards_embeds.award_result_embed(results, value, players)
        await _post_to_news(interaction.guild, league.id, pool, embed)
        await safe_respond(interaction, content=f"{award_type.name} results announced.", ephemeral=True)

    @app_commands.command(name="results", description="Show closed award results for a season")
    @app_commands.describe(
        award_type="Which award to look up",
        season="Season year (defaults to current season)",
    )
    @app_commands.choices(award_type=_ALL_AWARD_CHOICES)
    async def results(
        self,
        interaction: discord.Interaction,
        award_type: app_commands.Choice[str],
        season: Optional[int] = None,
    ) -> None:
        league = await _require_league(interaction.guild_id)
        pool = await get_pool()
        target_season = season if season is not None else league.current_season
        value = award_type.value

        if value == "all_nba":
            first_team: list[dict] = []
            second_team: list[dict] = []
            third_team: list[dict] = []
            for at, bucket in [
                ("all_nba_1", first_team),
                ("all_nba_2", second_team),
                ("all_nba_3", third_team),
            ]:
                bucket.extend(
                    await awards_service.get_closed_results(pool, league.id, target_season, at)
                )
            if not any([first_team, second_team, third_team]):
                await interaction.response.send_message(
                    f"No All-NBA results found for season {target_season}.", ephemeral=True
                )
                return
            all_player_ids = list({
                r["player_id"]
                for b in [first_team, second_team, third_team]
                for r in b
            })
            players = {
                p.id: p
                for p in [await player_repo.get_by_id(pool, pid) for pid in all_player_ids]
                if p is not None
            }
            embed = awards_embeds.all_nba_embed(first_team, second_team, third_team, players)
            await interaction.response.send_message(embed=embed)
            return

        if value == "all_star":
            east = await awards_service.get_closed_results(pool, league.id, target_season, "all_star_east")
            west = await awards_service.get_closed_results(pool, league.id, target_season, "all_star_west")
            if not east and not west:
                await interaction.response.send_message(
                    f"No All-Star results found for season {target_season}.", ephemeral=True
                )
                return
            all_player_ids = list({r["player_id"] for r in east + west})
            players = {
                p.id: p
                for p in [await player_repo.get_by_id(pool, pid) for pid in all_player_ids]
                if p is not None
            }
            embed = awards_embeds.all_star_embed(east, west, players)
            await interaction.response.send_message(embed=embed)
            return

        data = await awards_service.get_closed_results(pool, league.id, target_season, value)
        if not data:
            await interaction.response.send_message(
                f"No {award_type.name} results found for season {target_season}.", ephemeral=True
            )
            return
        all_player_ids = [r["player_id"] for r in data]
        players = {
            p.id: p
            for p in [await player_repo.get_by_id(pool, pid) for pid in all_player_ids]
            if p is not None
        }
        embed = awards_embeds.award_result_embed(data, value, players)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="all-star", description="Open All-Star voting for both conferences (commissioner only)")
    async def all_star(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction)
        league = await _require_commissioner(interaction)

        pool = await get_pool()
        east_id, west_id = await awards_service.open_all_star_voting(league.id, league.current_season)

        for vid, at in [(east_id, "all_star_east"), (west_id, "all_star_west")]:
            candidates = await _fetch_top_candidates(pool, league.id, league.current_season, at)
            voting_row = {
                "id": vid,
                "league_id": league.id,
                "season": league.current_season,
                "award_type": at,
                "status": "open",
            }
            embed = awards_embeds.voting_open_embed(voting_row, at, candidates)
            await _post_to_news(interaction.guild, league.id, pool, embed)
            _create_logged_task(
                awards_service.generate_cpu_votes(vid, league.id, league.current_season)
            )

        await safe_respond(interaction, content="All-Star voting opened for East and West.", ephemeral=True)


class AwardsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.bot.tree.add_command(AwardsGroup())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AwardsCog(bot))
