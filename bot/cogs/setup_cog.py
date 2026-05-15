from __future__ import annotations

from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.embeds import league_embeds, season_embeds
from bot.embeds.info_embeds import audit_embed, help_embed, status_embed
from core.errors import DBAError
from core.logging import get_logger
from data.db import get_pool
from data.repositories import admin_repo, game_repo, league_repo, team_repo, trade_repo
from phase.guards import all_humans_ready, no_pending_trades
from phase.helpers import get_league_or_error, require_commissioner
from phase.transitions import ALLOWED
from services import league_service

_DATA_ROOT = Path(__file__).parent.parent.parent / "data"


def _supported_seasons() -> list[int]:
    """Return seasons that have either a pre-built ratings file or full BDL cache."""
    seasons = []
    for year in range(2024, 2011, -1):
        ratings_file = _DATA_ROOT / "stats_ratings" / f"{year}.json"
        if ratings_file.exists():
            seasons.append(year)
            continue
        # Accept if BDL cache covers the 3-season peak window.
        cache_ok = all(
            (_DATA_ROOT / "bdl_cache" / f"season_{s}_{t}.json").exists()
            for s in [year, year - 1, year - 2]
            for t in ("base", "usage")
        )
        if cache_ok:
            seasons.append(year)
    return seasons


log = get_logger(__name__)


class LeagueGroup(app_commands.Group, name="league", description="League management commands"):

    @app_commands.command(name="create", description="Create a new DBA league for this server")
    @app_commands.describe(name="League name", season="Starting season year")
    @app_commands.default_permissions(administrator=True)
    async def create(
        self,
        interaction: discord.Interaction,
        name: str,
        season: int,
    ) -> None:
        await interaction.response.defer()

        supported = _supported_seasons()
        if season not in supported:
            valid_range = f"{min(supported)}-{max(supported)}" if supported else "none available"
            raise DBAError(
                f"Season {season} is not supported. "
                f"Available seasons: {valid_range}. "
                "Run `fetch_bdl_cache.py` and `build_stats_ratings.py` to add more seasons."
            )

        league = await league_service.create(
            guild=interaction.guild,
            commissioner=interaction.user,
            name=name,
            season_year=season,
        )

        embed = league_embeds.created(league)
        await interaction.followup.send(embed=embed)

        pool = await get_pool()
        news_channel_id = await league_repo.get_channel(pool, league.id, "league-news")
        if news_channel_id:
            channel = interaction.guild.get_channel(news_channel_id)
            if channel:
                await channel.send(
                    f"🏀 **{league.name}** is live! Season {league.start_season_year} begins. "
                    f"Use `/team assign` to claim your franchise."
                )

    @create.autocomplete("season")
    async def create_season_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        return [
            app_commands.Choice(name=f"{y}-{str(y + 1)[2:]} Season", value=y)
            for y in _supported_seasons()
            if not current or str(y).startswith(current)
        ][:25]

    @app_commands.command(name="info", description="Show current league info")
    async def info(self, interaction: discord.Interaction) -> None:
        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await interaction.response.send_message(
                "No active league found. Use `/league create` to set one up.",
                ephemeral=True,
            )
            return

        pool = await get_pool()
        teams = await team_repo.get_all(pool, league.id)
        claimed = sum(1 for t in teams if t.manager_user_id is not None)

        embed = discord.Embed(
            title=f"🏀 {league.name}",
            color=discord.Color.orange(),
        )
        embed.add_field(name="Phase", value=league.current_phase, inline=True)
        embed.add_field(name="Season", value=str(league.current_season), inline=True)
        embed.add_field(
            name="Commissioner",
            value=f"<@{league.commissioner_user_id}>",
            inline=True,
        )
        embed.add_field(name="Teams Claimed", value=f"{claimed} / 30", inline=True)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="phase", description="Show current phase and what's available or blocking")
    async def phase(self, interaction: discord.Interaction) -> None:
        league = await get_league_or_error(interaction.guild_id)
        pool = await get_pool()

        available_commands = [
            cmd for cmd, phases in ALLOWED.items()
            if any(p.value == league.current_phase for p in phases)
        ]

        from phase.states import Phase as _Phase
        READY_PHASES = {
            _Phase.REGULAR_SEASON_ACTIVE, _Phase.REGULAR_SEASON_POSTDEADLINE,
            _Phase.PLAYIN_ACTIVE, _Phase.PLAYOFFS_R1, _Phase.PLAYOFFS_R2,
            _Phase.CONFERENCE_FINALS, _Phase.NBA_FINALS,
        }
        blockers: list[str] = []
        if _Phase(league.current_phase) in READY_PHASES:
            all_ready, unready_ids = await all_humans_ready(pool, league.id)
            if not all_ready:
                rows = await pool.fetch(
                    "SELECT manager_user_id FROM teams WHERE id = ANY($1::int[])",
                    unready_ids,
                )
                mentions = ", ".join(
                    f"<@{r['manager_user_id']}>" for r in rows if r["manager_user_id"]
                )
                blockers.append(f"{len(unready_ids)} manager(s) not ready: {mentions}")

        clean, pending_count = await no_pending_trades(pool, league.id)
        if not clean:
            blockers.append(f"{pending_count} trade(s) pending commissioner review")

        embed = season_embeds.phase_status_embed(league, available_commands, blockers)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="status", description="Show a live dashboard of the league's current state")
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await interaction.followup.send(
                "No active league in this server. Use `/league create` to start one.",
                ephemeral=True,
            )
            return

        pool = await get_pool()
        teams = await team_repo.get_all(pool, league.id)
        teams_human = sum(1 for t in teams if t.manager_user_id is not None)

        pending_trades = await trade_repo.count_pending_commissioner(pool, league.id)

        ready_team_ids = await game_repo.get_ready_teams(pool, league.id)
        ready_count = len(ready_team_ids)
        human_count = teams_human

        current_idx = await game_repo.get_current_index(pool, league.id, league.current_season)
        next_matchup = await game_repo.get_user_matchup_ahead(
            pool, league.id, league.current_season, current_idx
        )
        next_matchup_str: str | None = None
        if next_matchup:
            home_id = next_matchup["home_team_id"]
            away_id = next_matchup["away_team_id"]
            teams_by_id = {t.id: t for t in teams}
            home = teams_by_id.get(home_id)
            away = teams_by_id.get(away_id)
            home_code = home.nba_team_code if home else str(home_id)
            away_code = away.nba_team_code if away else str(away_id)
            next_matchup_str = f"`{away_code}` @ `{home_code}` (game #{next_matchup['game_index']})"

        standings_rows = await pool.fetch(
            "SELECT * FROM standings_cache WHERE league_id = $1 AND season = $2 ORDER BY wins DESC, losses ASC",
            league.id,
            league.current_season,
        )
        teams_by_id = {t.id: t for t in teams}
        east_leader: str | None = None
        west_leader: str | None = None
        for row in standings_rows:
            team = teams_by_id.get(row["team_id"])
            if not team:
                continue
            record = f"{row['wins']}–{row['losses']}"
            if row["conference"] == "East" and east_leader is None:
                east_leader = f"{team.nba_team_code} ({record})"
            elif row["conference"] == "West" and west_leader is None:
                west_leader = f"{team.nba_team_code} ({record})"
            if east_leader and west_leader:
                break

        embed = status_embed(
            league=league,
            teams_total=len(teams),
            teams_human=teams_human,
            pending_trades=pending_trades,
            ready_count=ready_count,
            human_count=human_count,
            next_matchup_str=next_matchup_str,
            east_leader=east_leader,
            west_leader=west_leader,
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="audit", description="Commissioner: view recent commissioner actions")
    async def audit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        league = await get_league_or_error(interaction.guild_id)
        await require_commissioner(interaction, league)

        pool = await get_pool()
        actions = await admin_repo.get_recent_commissioner_actions(pool, league.id)
        embed = audit_embed(actions)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="advance", description="Commissioner: manually advance the league phase")
    @app_commands.describe(phase_name="Target phase name (e.g. REGULAR_SEASON_ACTIVE)")
    async def advance(self, interaction: discord.Interaction, phase_name: str) -> None:
        from phase.states import Phase as _Phase

        _VALID_PHASES = ", ".join(f"`{p.value}`" for p in _Phase)

        league = await get_league_or_error(interaction.guild_id)
        await require_commissioner(interaction, league)

        # Validate phase name before touching the DB.  Phase(unknown) raises ValueError,
        # which the global error handler catches as "Something went wrong" with no context.
        try:
            _Phase(phase_name)
        except ValueError:
            await interaction.response.send_message(
                f"Unknown phase `{phase_name}`.\nValid phases: {_VALID_PHASES}",
                ephemeral=True,
            )
            return

        if phase_name == league.current_phase:
            await interaction.response.send_message(
                f"League is already in phase `{phase_name}`.",
                ephemeral=True,
            )
            return

        await league_service.advance_phase(league.id, phase_name)
        await interaction.response.send_message(
            f"Phase advanced from `{league.current_phase}` to `{phase_name}`.",
        )

    @app_commands.command(name="delete", description="Permanently delete the league and all its data")
    @app_commands.describe(confirm_name="Type the exact league name to confirm")
    @app_commands.default_permissions(administrator=True)
    async def delete(self, interaction: discord.Interaction, confirm_name: str) -> None:
        await interaction.response.defer(ephemeral=True)

        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await interaction.followup.send("No active league in this server.", ephemeral=True)
            return

        if confirm_name != league.name:
            await interaction.followup.send(
                f"Confirmation failed — you typed **{confirm_name}** but the league is **{league.name}**.\n"
                "Pass the exact league name to confirm deletion.",
                ephemeral=True,
            )
            return

        pool = await get_pool()

        # Delete Discord channels and their shared category
        channel_rows = await pool.fetch(
            "SELECT discord_channel_id FROM league_channels WHERE league_id = $1",
            league.id,
        )
        category = None
        for row in channel_rows:
            ch = interaction.guild.get_channel(row["discord_channel_id"])
            if ch:
                if category is None and ch.category:
                    category = ch.category
                try:
                    await ch.delete(reason="DBA league deleted")
                except discord.HTTPException:
                    pass
        if category:
            try:
                await category.delete(reason="DBA league deleted")
            except discord.HTTPException:
                pass

        # Delete Discord roles (commissioner + 30 team roles).
        # Fetch live from Discord to avoid stale cache misses.
        role_rows = await pool.fetch(
            "SELECT discord_role_id FROM league_roles WHERE league_id = $1",
            league.id,
        )
        if role_rows:
            live_roles = {r.id: r for r in await interaction.guild.fetch_roles()}
            for row in role_rows:
                role = live_roles.get(row["discord_role_id"])
                if role:
                    try:
                        await role.delete(reason="DBA league deleted")
                    except discord.HTTPException:
                        pass

        # Single DELETE cascades to all child tables
        try:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
        except Exception as exc:
            log.error(f"Failed to delete league {league.id} from DB: {exc}", exc_info=True)
            await interaction.followup.send(
                "Discord channels/roles were deleted, but the league record could not be removed "
                f"from the database. Contact the server admin. Error: {exc}",
                ephemeral=True,
            )
            return

        # Verify the row is actually gone — a silent failure here would re-wedge the server.
        still_exists = await pool.fetchval("SELECT id FROM leagues WHERE id = $1", league.id)
        if still_exists:
            log.critical(
                f"League {league.id} DELETE appeared to succeed but row still exists — "
                "possible FK constraint or trigger preventing deletion."
            )
            await interaction.followup.send(
                "Warning: the DELETE command ran without error but the league record still exists "
                "in the database. Contact the server admin immediately.",
                ephemeral=True,
            )
            return

        log.info(f"League '{league.name}' (id={league.id}) deleted by {interaction.user.id}")

        await interaction.followup.send(
            f"League **{league.name}** has been permanently deleted. All data wiped.",
            ephemeral=True,
        )


class TeamGroup(app_commands.Group, name="team", description="Team management commands"):

    @app_commands.command(name="assign", description="Assign a manager to a team")
    @app_commands.describe(
        user="Discord member to assign (mention)",
        user_id="Discord user ID (alternative to mention)",
        team_code="NBA team code (e.g. LAL)",
    )
    @app_commands.default_permissions(administrator=True)
    async def assign(
        self,
        interaction: discord.Interaction,
        team_code: str,
        user: Optional[discord.Member] = None,
        user_id: Optional[str] = None,
    ) -> None:
        await interaction.response.defer()

        member = user
        if member is None and user_id is not None:
            member = interaction.guild.get_member(int(user_id))
        if member is None:
            await interaction.followup.send("Provide either a user mention or a user_id.", ephemeral=True)
            return

        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await interaction.followup.send("No active league. Use `/league create` first.", ephemeral=True)
            return

        team = await league_service.assign_manager(
            guild=interaction.guild,
            league=league,
            team_code=team_code,
            user=member,
        )

        embed = league_embeds.team_assigned(team, member)
        await interaction.followup.send(embed=embed)

        pool = await get_pool()
        news_channel_id = await league_repo.get_channel(pool, league.id, "league-news")
        if news_channel_id:
            channel = interaction.guild.get_channel(news_channel_id)
            if channel:
                await channel.send(
                    f"📋 {member.mention} has claimed the **{team.full_name}** (`{team.nba_team_code}`)!"
                )

    @app_commands.command(name="remove", description="Remove a manager from a team")
    @app_commands.describe(team_code="NBA team code (e.g. LAL)")
    @app_commands.default_permissions(administrator=True)
    async def remove(
        self,
        interaction: discord.Interaction,
        team_code: str,
    ) -> None:
        await interaction.response.defer()

        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await interaction.followup.send("No active league.", ephemeral=True)
            return

        team = await league_service.remove_manager(
            guild=interaction.guild,
            league=league,
            team_code=team_code,
            requester=interaction.user,
        )

        embed = league_embeds.manager_removed(team)
        await interaction.followup.send(embed=embed)

        pool = await get_pool()
        news_channel_id = await league_repo.get_channel(pool, league.id, "league-news")
        if news_channel_id:
            channel = interaction.guild.get_channel(news_channel_id)
            if channel:
                await channel.send(
                    f"🔄 The **{team.full_name}** (`{team.nba_team_code}`) is now CPU controlled."
                )

    @app_commands.command(name="list", description="Show all 30 teams with manager status")
    async def list(self, interaction: discord.Interaction) -> None:
        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await interaction.response.send_message("No active league.", ephemeral=True)
            return

        pool = await get_pool()
        teams = await team_repo.get_all(pool, league.id)

        east = [t for t in teams if t.conference == "East"]
        west = [t for t in teams if t.conference == "West"]

        def team_line(t: team_repo.Team) -> str:
            if t.manager_user_id:
                return f"`{t.nba_team_code}` {t.full_name} — <@{t.manager_user_id}>"
            return f"`{t.nba_team_code}` {t.full_name} — CPU ({t.cpu_mode})"

        embed = discord.Embed(title=f"🏀 {league.name} — All Teams", color=discord.Color.orange())
        embed.add_field(
            name="Eastern Conference",
            value="\n".join(team_line(t) for t in east),
            inline=False,
        )
        embed.add_field(
            name="Western Conference",
            value="\n".join(team_line(t) for t in west),
            inline=False,
        )

        claimed = sum(1 for t in teams if t.manager_user_id is not None)
        embed.set_footer(text=f"{claimed} / 30 teams claimed")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="rename", description="Rename a team's city and name")
    @app_commands.describe(
        name="New team name (max 20 chars)",
        city="New city name (max 20 chars)",
        team_code="Team code — commissioner only, omit to rename your own team",
    )
    async def rename(
        self,
        interaction: discord.Interaction,
        name: str,
        city: str,
        team_code: str | None = None,
    ) -> None:
        await interaction.response.defer()

        if len(name) > 20:
            await interaction.followup.send("Team name must be 20 characters or fewer.", ephemeral=True)
            return
        if len(city) > 20:
            await interaction.followup.send("City name must be 20 characters or fewer.", ephemeral=True)
            return

        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await interaction.followup.send("No active league in this server.", ephemeral=True)
            return

        pool = await get_pool()

        if team_code:
            if interaction.user.id != league.commissioner_user_id:
                await interaction.followup.send(
                    "Only the commissioner can rename another team.", ephemeral=True
                )
                return
            target_team = await team_repo.get_by_code(pool, league.id, team_code.upper())
            if not target_team:
                await interaction.followup.send(
                    f"No team found with code **{team_code.upper()}**.", ephemeral=True
                )
                return
        else:
            target_team = await team_repo.get_by_manager(pool, league.id, interaction.user.id)
            if not target_team:
                await interaction.followup.send(
                    "You don't manage a team in this league.", ephemeral=True
                )
                return

        old_full_name = target_team.full_name
        await team_repo.rename_team(pool, target_team.id, name, city)

        role_id = await league_repo.get_team_role(pool, league.id, target_team.id)
        if role_id:
            role = interaction.guild.get_role(role_id)
            if role:
                try:
                    await role.edit(name=f"{city} {name}", reason="DBA: team rename")
                except discord.Forbidden:
                    log.warning(f"Could not rename Discord role {role_id} for team {target_team.id}")

        new_full_name = f"{city} {name}"
        news_channel_id = await league_repo.get_channel(pool, league.id, "league-news")
        if news_channel_id:
            ch = interaction.guild.get_channel(news_channel_id)
            if ch:
                await ch.send(f"**{old_full_name}** has rebranded to **{new_full_name}**!")

        await interaction.followup.send(
            f"Team renamed: **{old_full_name}** → **{new_full_name}**."
        )


_COMMAND_GROUP_PHASES: dict[str, list[str]] = {
    "sim": ["REGULAR_SEASON_ACTIVE", "REGULAR_SEASON_POSTDEADLINE",
            "PLAYIN_ACTIVE", "PLAYOFFS_R1", "PLAYOFFS_R2", "CONFERENCE_FINALS", "NBA_FINALS"],
    "trade": ["REGULAR_SEASON_ACTIVE", "TRADE_DEADLINE_OPEN",
              "REGULAR_SEASON_POSTDEADLINE", "POST_DRAFT_TRADES_OPEN"],
    "ready": ["REGULAR_SEASON_ACTIVE", "REGULAR_SEASON_POSTDEADLINE",
              "PLAYIN_ACTIVE", "PLAYOFFS_R1", "PLAYOFFS_R2", "CONFERENCE_FINALS", "NBA_FINALS"],
    "standings": ["REGULAR_SEASON_ACTIVE", "TRADE_DEADLINE_OPEN",
                  "REGULAR_SEASON_POSTDEADLINE", "REGULAR_SEASON_COMPLETE",
                  "PLAYIN_ACTIVE", "PLAYOFFS_R1", "PLAYOFFS_R2",
                  "CONFERENCE_FINALS", "NBA_FINALS"],
    "roster": ["*"],
    "stats": ["*"],
    "fa": ["FA_OPEN", "FA_CLOSED"],
    "draft": ["DRAFT_IN_PROGRESS"],
    "offseason": ["OFFSEASON_AWARDS_OPEN", "OFFSEASON_AWARDS_CLOSED",
                  "DRAFT_LOTTERY_DONE", "POST_DRAFT_TRADES_OPEN",
                  "PROGRESSION_PENDING"],
}


class SetupCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.bot.tree.add_command(LeagueGroup())
        self.bot.tree.add_command(TeamGroup())

    @app_commands.command(name="help", description="Show available commands for the current phase")
    async def help(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        league = await league_service.get_league(interaction.guild_id)
        if not league:
            no_league_embed = discord.Embed(
                title="Getting Started with DBA",
                description=(
                    "No league has been created yet.\n\n"
                    "**Step 1:** Use `/league create` to set up your league.\n"
                    "**Step 2:** Use `/team assign` to invite managers.\n"
                    "**Step 3:** Use `/season start` to build the schedule."
                ),
                color=discord.Color.blurple(),
            )
            await interaction.followup.send(embed=no_league_embed, ephemeral=True)
            return

        phase = league.current_phase
        available: list[str] = []
        unavailable: list[str] = []
        for group, phases in _COMMAND_GROUP_PHASES.items():
            if phases == ["*"] or phase in phases:
                available.append(group)
            else:
                unavailable.append(group)

        embed = help_embed(
            current_phase=phase,
            available_groups=available,
            unavailable_groups=unavailable,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SetupCog(bot))
