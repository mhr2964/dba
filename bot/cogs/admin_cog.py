from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.errors import DBAError
from core.logging import get_logger
from data.db import get_pool
from data.repositories import admin_repo, player_repo, team_repo
from phase.helpers import get_league_or_error, require_commissioner
from services import league_service, roster_seed_service

log = get_logger(__name__)

# Allowlist for player attribute edits — validated before any SQL use.
_PLAYER_FIELD_ALLOWLIST: frozenset[str] = frozenset({
    "ovr",
    "speed",
    "shooting_2pt",
    "shooting_3pt",
    "shooting_mid",
    "finishing",
    "playmaking",
    "defense",
    "rebounding",
    "iq",
    "position",
})

# Map the command-facing field name to the DB column name.
_FIELD_TO_COLUMN: dict[str, str] = {
    "ovr": "overall",
    "speed": "speed",
    "shooting_2pt": "shooting_2pt",
    "shooting_3pt": "shooting_3pt",
    "shooting_mid": "shooting_mid",
    "finishing": "finishing",
    "playmaking": "playmaking",
    "defense": "defense",
    "rebounding": "rebounding",
    "iq": "iq",
    "position": "position",
}

_STRING_FIELDS: frozenset[str] = frozenset({"position"})


class AdminGroup(app_commands.Group, name="admin", description="Commissioner admin tools"):

    @app_commands.command(name="edit-player", description="Commissioner: edit a player attribute")
    @app_commands.describe(
        player="Player name to edit (e.g. LeBron James)",
        field="Attribute to change (ovr, speed, shooting_2pt, shooting_3pt, shooting_mid, finishing, playmaking, defense, rebounding, iq, position)",
        value="New value (integer for stats, string for position)",
    )
    async def edit_player(
        self,
        interaction: discord.Interaction,
        player: str,
        field: str,
        value: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        league = await get_league_or_error(interaction.guild_id)
        await require_commissioner(interaction, league)

        if field not in _PLAYER_FIELD_ALLOWLIST:
            await interaction.followup.send(
                f"Invalid field `{field}`. Allowed fields: {', '.join(sorted(_PLAYER_FIELD_ALLOWLIST))}",
                ephemeral=True,
            )
            return

        pool = await get_pool()
        matches = await player_repo.search_by_name(pool, league.id, player)
        if not matches:
            await interaction.followup.send(f"No player found matching '{player}'.", ephemeral=True)
            return
        if len(matches) > 1:
            names = ", ".join(p.full_name for p in matches)
            await interaction.followup.send(f"Multiple matches for '{player}': {names}. Be more specific.", ephemeral=True)
            return
        found_player = matches[0]
        player_id = found_player.id

        column = _FIELD_TO_COLUMN[field]
        old_value = getattr(found_player, column if column != "overall" else "overall", None)

        if field in _STRING_FIELDS:
            typed_value: str | int = value
        else:
            try:
                typed_value = int(value)
            except ValueError:
                await interaction.followup.send(
                    f"Field `{field}` requires an integer value.", ephemeral=True
                )
                return

        # column is validated against the allowlist — safe to interpolate
        await pool.execute(
            f"UPDATE players SET {column} = $1 WHERE id = $2",
            typed_value,
            player_id,
        )

        await admin_repo.log_commissioner_action(
            pool,
            league_id=league.id,
            user_id=interaction.user.id,
            action_type="edit_player",
            target_ref=str(player_id),
            detail=f"{field}: {old_value} → {typed_value}",
        )

        await interaction.followup.send(
            f"Updated **{found_player.full_name}** `{field}`: {old_value} → {typed_value}",
            ephemeral=True,
        )

    @app_commands.command(name="rollback-game", description="Commissioner: reset a game to scheduled and reverse standings")
    @app_commands.describe(game_id="Game ID to roll back")
    async def rollback_game(self, interaction: discord.Interaction, game_id: int) -> None:
        await interaction.response.defer(ephemeral=True)

        league = await get_league_or_error(interaction.guild_id)
        await require_commissioner(interaction, league)

        pool = await get_pool()

        game_row = await pool.fetchrow(
            "SELECT * FROM games WHERE id = $1 AND league_id = $2",
            game_id,
            league.id,
        )
        if not game_row:
            await interaction.followup.send(
                f"Game #{game_id} not found in this league.", ephemeral=True
            )
            return
        if game_row["status"] != "simmed":
            await interaction.followup.send(
                f"Game #{game_id} is not simmed (status: {game_row['status']}).", ephemeral=True
            )
            return

        home_team_id: int = game_row["home_team_id"]
        away_team_id: int = game_row["away_team_id"]
        winner_team_id: Optional[int] = game_row["winner_team_id"]
        season: int = game_row["season"]

        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE games
                    SET status = 'scheduled', home_score = NULL, away_score = NULL,
                        winner_team_id = NULL, simmed_at = NULL
                    WHERE id = $1
                    """,
                    game_id,
                )
                await conn.execute(
                    "DELETE FROM game_box_scores WHERE game_id = $1", game_id
                )
                await conn.execute(
                    "DELETE FROM team_game_stats WHERE game_id = $1", game_id
                )

                if winner_team_id == home_team_id:
                    loser_team_id = away_team_id
                else:
                    loser_team_id = home_team_id

                if winner_team_id:
                    await conn.execute(
                        """
                        UPDATE standings_cache
                        SET wins = GREATEST(0, wins - 1),
                            win_streak = GREATEST(0, win_streak - 1),
                            loss_streak = 0
                        WHERE league_id = $1 AND season = $2 AND team_id = $3
                        """,
                        league.id, season, winner_team_id,
                    )
                    await conn.execute(
                        """
                        UPDATE standings_cache
                        SET losses = GREATEST(0, losses - 1),
                            loss_streak = GREATEST(0, loss_streak - 1),
                            win_streak = 0
                        WHERE league_id = $1 AND season = $2 AND team_id = $3
                        """,
                        league.id, season, loser_team_id,
                    )

        await admin_repo.log_commissioner_action(
            pool,
            league_id=league.id,
            user_id=interaction.user.id,
            action_type="rollback_game",
            target_ref=str(game_id),
            detail=f"Game #{game_id} rolled back to scheduled; standings reversed.",
        )

        await interaction.followup.send(
            f"Game #{game_id} has been reset to scheduled and standings reversed.",
            ephemeral=True,
        )

    @app_commands.command(
        name="recalculate-standings",
        description="Commissioner: recount standings from scratch from simmed games",
    )
    async def recalculate_standings(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        league = await get_league_or_error(interaction.guild_id)
        await require_commissioner(interaction, league)

        pool = await get_pool()
        season = league.current_season

        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM standings_cache WHERE league_id = $1 AND season = $2",
                    league.id,
                    season,
                )

                games = await conn.fetch(
                    """
                    SELECT g.*, ht.conference AS home_conf, ht.division AS home_div,
                           at.conference AS away_conf, at.division AS away_div
                    FROM games g
                    JOIN teams ht ON ht.id = g.home_team_id
                    JOIN teams at ON at.id = g.away_team_id
                    WHERE g.league_id = $1 AND g.season = $2 AND g.status = 'simmed'
                    ORDER BY g.game_index ASC
                    """,
                    league.id,
                    season,
                )

                for game in games:
                    home_id = game["home_team_id"]
                    away_id = game["away_team_id"]
                    winner_id = game["winner_team_id"]

                    for team_id, conf, div in [
                        (home_id, game["home_conf"], game["home_div"]),
                        (away_id, game["away_conf"], game["away_div"]),
                    ]:
                        await conn.execute(
                            """
                            INSERT INTO standings_cache (league_id, season, team_id, conference, division, wins, losses)
                            VALUES ($1, $2, $3, $4, $5, 0, 0)
                            ON CONFLICT (league_id, season, team_id) DO NOTHING
                            """,
                            league.id, season, team_id, conf, div,
                        )

                    if winner_id == home_id:
                        await conn.execute(
                            """
                            UPDATE standings_cache SET wins = wins + 1
                            WHERE league_id = $1 AND season = $2 AND team_id = $3
                            """,
                            league.id, season, home_id,
                        )
                        await conn.execute(
                            """
                            UPDATE standings_cache SET losses = losses + 1
                            WHERE league_id = $1 AND season = $2 AND team_id = $3
                            """,
                            league.id, season, away_id,
                        )
                    else:
                        await conn.execute(
                            """
                            UPDATE standings_cache SET wins = wins + 1
                            WHERE league_id = $1 AND season = $2 AND team_id = $3
                            """,
                            league.id, season, away_id,
                        )
                        await conn.execute(
                            """
                            UPDATE standings_cache SET losses = losses + 1
                            WHERE league_id = $1 AND season = $2 AND team_id = $3
                            """,
                            league.id, season, home_id,
                        )

        n = len(games)
        await admin_repo.log_commissioner_action(
            pool,
            league_id=league.id,
            user_id=interaction.user.id,
            action_type="recalculate_standings",
            target_ref=None,
            detail=f"Recalculated from {n} simmed games for season {season}.",
        )

        await interaction.followup.send(
            f"Standings recalculated from {n} games.", ephemeral=True
        )

    @app_commands.command(
        name="force-fa-sign",
        description="Commissioner: emergency sign a free agent directly to a team",
    )
    @app_commands.describe(
        player="Player name to sign (e.g. LeBron James)",
        team_code="Team code (e.g. LAL)",
        salary="Annual salary in dollars",
        years="Contract length in years",
    )
    async def force_fa_sign(
        self,
        interaction: discord.Interaction,
        player: str,
        team_code: str,
        salary: int,
        years: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        league = await get_league_or_error(interaction.guild_id)
        await require_commissioner(interaction, league)

        pool = await get_pool()

        matches = await player_repo.search_by_name(pool, league.id, player)
        if not matches:
            await interaction.followup.send(f"No player found matching '{player}'.", ephemeral=True)
            return
        if len(matches) > 1:
            names = ", ".join(p.full_name for p in matches)
            await interaction.followup.send(f"Multiple matches for '{player}': {names}. Be more specific.", ephemeral=True)
            return
        found_player = matches[0]
        player_id = found_player.id

        if found_player.roster_status not in ("free_agent", "waived"):
            await interaction.followup.send(
                f"**{found_player.full_name}** is not a free agent or waived (status: {found_player.roster_status}).",
                ephemeral=True,
            )
            return

        team = await team_repo.get_by_code(pool, league.id, team_code.upper())
        if not team:
            await interaction.followup.send(
                f"No team found with code **{team_code.upper()}**.", ephemeral=True
            )
            return

        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE players
                    SET team_id = $1, roster_status = 'active'
                    WHERE id = $2
                    """,
                    team.id,
                    player_id,
                )
                await conn.execute(
                    """
                    UPDATE contracts SET is_active = FALSE
                    WHERE player_id = $1 AND is_active = TRUE
                    """,
                    player_id,
                )
                await conn.execute(
                    """
                    INSERT INTO contracts
                        (league_id, player_id, team_id, salary, years_remaining, total_years,
                         contract_type, signed_in_season, is_active)
                    VALUES ($1, $2, $3, $4, $5, $5, 'standard', $6, TRUE)
                    """,
                    league.id,
                    player_id,
                    team.id,
                    salary,
                    years,
                    league.current_season,
                )

        await admin_repo.log_commissioner_action(
            pool,
            league_id=league.id,
            user_id=interaction.user.id,
            action_type="force_fa_sign",
            target_ref=str(player_id),
            detail=f"Signed {found_player.full_name} to {team.full_name} — ${salary:,}/yr x {years}yr.",
        )

        await interaction.followup.send(
            f"**{found_player.full_name}** signed to **{team.full_name}** — ${salary:,}/yr x {years} year(s).",
            ephemeral=True,
        )


    @app_commands.command(
        name="reseed-rosters",
        description="Commissioner: reconcile league rosters against a season seed file",
    )
    @app_commands.describe(
        year="Season year to reconcile against (e.g. 2024)",
        mode="dry-run previews changes without writing; apply commits them",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="dry-run", value="dry-run"),
            app_commands.Choice(name="apply", value="apply"),
        ]
    )
    async def reseed_rosters(
        self,
        interaction: discord.Interaction,
        year: int,
        mode: app_commands.Choice[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        league = await get_league_or_error(interaction.guild_id)
        await require_commissioner(interaction, league)

        mode_value = mode.value if mode is not None else "dry-run"
        pool = await get_pool()

        # Validate seed file exists before doing any work.
        try:
            roster_seed_service.load_seed(year)
        except FileNotFoundError:
            available = roster_seed_service.list_available_seed_years()
            supported = ", ".join(str(y) for y in available) if available else "none"
            await interaction.followup.send(
                f"No roster seed found for **{year}**. Supported years: {supported}",
                ephemeral=True,
            )
            return

        if mode_value == "dry-run":
            diff = await roster_seed_service.diff_league_against_seed(
                pool, league.id, year
            )

            to_insert = diff["to_insert"]
            to_move = diff["to_move"]
            already_correct = diff["already_correct"]
            unknown_in_db = diff["unknown_in_db"]

            embed = discord.Embed(
                title=f"Roster Reseed Preview — {year}",
                color=discord.Color.blue(),
            )
            embed.add_field(name="To insert", value=str(len(to_insert)), inline=True)
            embed.add_field(name="To move", value=str(len(to_move)), inline=True)
            embed.add_field(name="Already correct", value=str(already_correct), inline=True)
            embed.add_field(
                name="Unknown in DB (not in seed)",
                value=str(len(unknown_in_db)),
                inline=True,
            )

            # First 10 pending changes as bullet lines.
            change_lines: list[str] = []
            for row in to_insert[:5]:
                name = row.get("full_name") or f"{row.get('first_name','')} {row.get('last_name','')}".strip()
                change_lines.append(f"+ Insert **{name}** → {row.get('nba_team_code')}")
            for entry in to_move[: max(0, 10 - len(change_lines))]:
                seed_row = entry["seed_row"]
                name = seed_row.get("full_name") or (
                    f"{seed_row.get('first_name','')} {seed_row.get('last_name','')}".strip()
                )
                change_lines.append(
                    f"~ Move **{name}**: {entry['from_team_code']} → {entry['to_team_code']}"
                )

            if change_lines:
                embed.add_field(
                    name="Sample changes (first 10)",
                    value="\n".join(change_lines),
                    inline=False,
                )

            embed.set_footer(text="Run with mode:apply to commit these changes.")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        else:  # apply
            result = await roster_seed_service.apply_reseed(
                pool, league.id, year, dry_run=False
            )

            embed = discord.Embed(
                title=f"Roster Reseed Applied — {year}",
                color=discord.Color.green() if not result["errors"] else discord.Color.orange(),
            )
            embed.add_field(name="Inserted", value=str(result["inserted"]), inline=True)
            embed.add_field(name="Moved", value=str(result["moved"]), inline=True)
            embed.add_field(name="Skipped", value=str(result["skipped"]), inline=True)

            if result["errors"]:
                # Show first 5 errors to avoid embed overflow.
                error_text = "\n".join(result["errors"][:5])
                if len(result["errors"]) > 5:
                    error_text += f"\n… and {len(result['errors']) - 5} more"
                embed.add_field(name="Errors", value=error_text, inline=False)

            await interaction.followup.send(embed=embed, ephemeral=True)

            await admin_repo.log_commissioner_action(
                pool,
                league_id=league.id,
                user_id=interaction.user.id,
                action_type="reseed_rosters",
                target_ref=str(year),
                detail=(
                    f"Reseed applied for season {year}: "
                    f"inserted={result['inserted']} moved={result['moved']} "
                    f"skipped={result['skipped']} errors={len(result['errors'])}"
                ),
            )

    @app_commands.command(
        name="purge-server",
        description="Remove all DBA categories, channels, and roles left over after a DB reset",
    )
    @app_commands.default_permissions(administrator=True)
    async def purge_server(self, interaction: discord.Interaction) -> None:
        """Scans the guild by name pattern and deletes DBA artifacts.
        Safe to run after a DB wipe when league_channels/league_roles tables are empty.
        """
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        deleted_categories: list[str] = []
        deleted_channels: int = 0
        deleted_roles: list[str] = []

        # Delete any category whose name starts with the basketball emoji we use.
        for category in list(guild.categories):
            if category.name.startswith("🏀"):
                for ch in list(category.channels):
                    try:
                        await ch.delete(reason="DBA purge-server")
                        deleted_channels += 1
                    except discord.HTTPException:
                        pass
                try:
                    await category.delete(reason="DBA purge-server")
                    deleted_categories.append(category.name)
                except discord.HTTPException:
                    pass

        # Load NBA team full names so we can match team roles exactly.
        import json
        from pathlib import Path
        _teams_path = Path(__file__).parent.parent.parent / "data" / "seeds" / "nba_teams.json"
        try:
            _nba_teams = json.loads(_teams_path.read_text())
            _team_full_names = {f"{t['city']} {t['name']}" for t in _nba_teams}
        except Exception:
            _team_full_names = set()

        # Delete DBA Commissioner role and any role matching a team's full name.
        for role in list(guild.roles):
            if role.name == "DBA Commissioner" or role.name in _team_full_names:
                try:
                    await role.delete(reason="DBA purge-server")
                    deleted_roles.append(role.name)
                except discord.HTTPException:
                    pass

        embed = discord.Embed(
            title="Server Purge Complete",
            color=discord.Color.red(),
        )
        embed.add_field(
            name="Categories deleted",
            value="\n".join(deleted_categories) or "none",
            inline=False,
        )
        embed.add_field(name="Channels deleted", value=str(deleted_channels), inline=True)
        embed.add_field(name="Roles deleted", value=str(len(deleted_roles)), inline=True)
        embed.set_footer(text="Server is clean. Use /league create to start fresh.")
        await interaction.followup.send(embed=embed, ephemeral=True)


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.bot.tree.add_command(AdminGroup())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
