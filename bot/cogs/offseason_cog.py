from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.embeds import history_embeds, hof_embeds, progression_embeds
from core.errors import DBAError, PermissionError, PhaseError
from core.logging import get_logger
from data.db import get_pool
from data.repositories import history_repo, hof_repo, league_repo, player_repo, team_repo
from phase.states import Phase
from services import league_service, progression_service, rollover_service

log = get_logger(__name__)


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


async def _post_to_news(
    guild: discord.Guild, league_id: int, pool, embed: discord.Embed
) -> None:
    channel_id = await league_repo.get_channel(pool, league_id, "league-news")
    if channel_id:
        channel = guild.get_channel(channel_id)
        if channel:
            await channel.send(embed=embed)


class OffseasonGroup(app_commands.Group, name="offseason", description="Offseason management"):

    @app_commands.command(name="progression", description="Run player progression (commissioner only)")
    @app_commands.default_permissions(administrator=True)
    async def progression(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        league = await _require_commissioner(interaction)

        if league.current_phase != Phase.PROGRESSION_PENDING.value:
            await interaction.followup.send(
                f"League must be in PROGRESSION_PENDING phase. Current phase: {league.current_phase}",
                ephemeral=True,
            )
            return

        pool = await get_pool()

        # snapshot OVR before progression to compute notable changes
        before_rows = await pool.fetch(
            "SELECT id, first_name, last_name, position, overall FROM players "
            "WHERE league_id = $1 AND roster_status = 'active'",
            league.id,
        )
        before_map: dict[int, dict] = {
            r["id"]: {
                "name": f"{r['first_name']} {r['last_name']}",
                "position": r["position"],
                "overall": r["overall"],
            }
            for r in before_rows
        }

        processed = await progression_service.run_progression(league.id, league.current_season)

        await league_service.advance_phase(league.id, Phase.FA_OPEN.value)

        # snapshot OVR after to find notable movers
        after_rows = await pool.fetch(
            "SELECT id, overall FROM players "
            "WHERE league_id = $1 AND roster_status = 'active'",
            league.id,
        )
        after_map: dict[int, int] = {r["id"]: r["overall"] for r in after_rows}

        changes: list[dict] = []
        for pid, before_info in before_map.items():
            after_ovr = after_map.get(pid)
            if after_ovr is None:
                continue
            delta = after_ovr - before_info["overall"]
            if delta == 0:
                continue
            changes.append(
                {
                    "name": before_info["name"],
                    "position": before_info["position"],
                    "before": before_info["overall"],
                    "after": after_ovr,
                    "delta": delta,
                }
            )

        improvers = sorted(
            [c for c in changes if c["delta"] > 0], key=lambda c: -c["delta"]
        )[:3]
        decliners = sorted(
            [c for c in changes if c["delta"] < 0], key=lambda c: c["delta"]
        )[:3]

        embed = progression_embeds.progression_summary_embed(processed, improvers, decliners)

        news_channel_id = await league_repo.get_channel(pool, league.id, "league-news")
        if news_channel_id:
            channel = interaction.guild.get_channel(news_channel_id)
            if channel and channel.id != interaction.channel_id:
                await channel.send(embed=embed)

        await interaction.followup.send(embed=embed)

        log.info(
            "Progression run by %s: %d players (league %d, season %d)",
            interaction.user.id,
            processed,
            league.id,
            league.current_season,
        )


    @app_commands.command(name="rollover", description="Run full season rollover (commissioner only)")
    async def rollover(self, interaction: discord.Interaction) -> None:
        league = await _require_commissioner(interaction)

        if league.current_phase != Phase.PROGRESSION_PENDING.value:
            raise PhaseError(
                f"Rollover requires PROGRESSION_PENDING phase. Current: {league.current_phase}"
            )

        await interaction.response.defer()

        summary = await rollover_service.run_rollover(league.id)

        pool = await get_pool()
        news_embed = history_embeds.rollover_complete_embed(summary)
        await _post_to_news(interaction.guild, league.id, pool, news_embed)

        reply_embed = history_embeds.rollover_complete_embed(summary)
        await interaction.followup.send(embed=reply_embed)

    @app_commands.command(name="history", description="View season history records")
    @app_commands.describe(season="Season year to look up (omit for last 5 seasons)")
    async def history(
        self,
        interaction: discord.Interaction,
        season: Optional[int] = None,
    ) -> None:
        league = await _require_league(interaction.guild_id)
        pool = await get_pool()

        if season is not None:
            record = await history_repo.get_season(pool, league.id, season)
            if not record:
                raise DBAError(f"No history record found for season {season}.")

            champion_team = None
            if record.get("champion_team_id"):
                champion_team = await team_repo.get_by_id(pool, record["champion_team_id"])

            mvp_player = None
            if record.get("mvp_player_id"):
                mvp_player = await player_repo.get_by_id(pool, record["mvp_player_id"])

            # Champion's record and trade count enrich the single-season view.
            champ_record: Optional[dict] = None
            if record.get("champion_team_id"):
                champ_record = await pool.fetchrow(
                    """
                    SELECT wins, losses
                    FROM standings_cache
                    WHERE league_id = $1 AND season = $2 AND team_id = $3
                    """,
                    league.id,
                    season,
                    record["champion_team_id"],
                )

            trade_count: int = await pool.fetchval(
                "SELECT COUNT(*) FROM trades WHERE league_id = $1 AND status = 'approved' AND season = $2",
                league.id,
                season,
            ) or 0

            embed = history_embeds.season_history_embed(record, champion_team, mvp_player)
            if champ_record:
                embed.add_field(
                    name="Champion Record",
                    value=f"{champ_record['wins']}-{champ_record['losses']}",
                    inline=True,
                )
            embed.add_field(name="Trades That Season", value=str(trade_count), inline=True)
            await interaction.response.send_message(embed=embed)
            return

        records = await history_repo.get_all_seasons(pool, league.id)
        records = records[:5]

        if not records:
            await interaction.response.send_message(
                "No season history on record yet.", ephemeral=True
            )
            return

        all_team_ids = list({
            r["champion_team_id"]
            for r in records
            if r.get("champion_team_id")
        } | {
            r["regular_season_wins_leader_id"]
            for r in records
            if r.get("regular_season_wins_leader_id")
        })
        all_player_ids = list({
            r["mvp_player_id"]
            for r in records
            if r.get("mvp_player_id")
        } | {
            r["finals_mvp_player_id"]
            for r in records
            if r.get("finals_mvp_player_id")
        })

        teams_by_id = {
            t.id: t
            for t in [await team_repo.get_by_id(pool, tid) for tid in all_team_ids]
            if t is not None
        }
        players_by_id = {
            p.id: p
            for p in [await player_repo.get_by_id(pool, pid) for pid in all_player_ids]
            if p is not None
        }

        # Fetch champion record and trade count for each season in the list view.
        season_extras: dict[int, dict] = {}
        for r in records:
            s = r["season"]
            champ_row = None
            if r.get("champion_team_id"):
                champ_row = await pool.fetchrow(
                    """
                    SELECT wins, losses
                    FROM standings_cache
                    WHERE league_id = $1 AND season = $2 AND team_id = $3
                    """,
                    league.id,
                    s,
                    r["champion_team_id"],
                )
            trade_count = await pool.fetchval(
                "SELECT COUNT(*) FROM trades WHERE league_id = $1 AND status = 'approved' AND season = $2",
                league.id,
                s,
            ) or 0
            season_extras[s] = {
                "champ_wins": champ_row["wins"] if champ_row else None,
                "champ_losses": champ_row["losses"] if champ_row else None,
                "trade_count": int(trade_count),
            }

        embed = history_embeds.history_list_embed(
            records, teams_by_id, players_by_id, season_extras=season_extras
        )
        await interaction.response.send_message(embed=embed)


    @app_commands.command(name="hof", description="View the Hall of Fame inductees for this league")
    async def hof(self, interaction: discord.Interaction) -> None:
        league = await _require_league(interaction.guild_id)
        pool = await get_pool()

        inductees = await hof_repo.get_all(pool, league.id)

        player_ids = [row["player_id"] for row in inductees]
        players_by_id = {}
        for pid in player_ids:
            p = await player_repo.get_by_id(pool, pid)
            if p:
                players_by_id[pid] = p

        embed = hof_embeds.hof_list_embed(inductees, players_by_id)
        await interaction.response.send_message(embed=embed)


class OffseasonCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.bot.tree.add_command(OffseasonGroup())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(OffseasonCog(bot))
