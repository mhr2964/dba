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
from services import league_service

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
        player_id="Player ID to edit",
        field="Attribute to change (ovr, speed, shooting_2pt, shooting_3pt, shooting_mid, finishing, playmaking, defense, rebounding, iq, position)",
        value="New value (integer for stats, string for position)",
    )
    async def edit_player(
        self,
        interaction: discord.Interaction,
        player_id: int,
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
        player = await player_repo.get_by_id(pool, player_id)
        if not player:
            await interaction.followup.send(f"Player #{player_id} not found.", ephemeral=True)
            return
        if player.league_id != league.id:
            await interaction.followup.send(
                f"Player #{player_id} does not belong to this league.", ephemeral=True
            )
            return

        column = _FIELD_TO_COLUMN[field]
        old_value = getattr(player, column if column != "overall" else "overall", None)

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
            f"Updated **{player.full_name}** `{field}`: {old_value} → {typed_value}",
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
        player_id="Player ID to sign",
        team_code="Team code (e.g. LAL)",
        salary="Annual salary in dollars",
        years="Contract length in years",
    )
    async def force_fa_sign(
        self,
        interaction: discord.Interaction,
        player_id: int,
        team_code: str,
        salary: int,
        years: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        league = await get_league_or_error(interaction.guild_id)
        await require_commissioner(interaction, league)

        pool = await get_pool()

        player = await player_repo.get_by_id(pool, player_id)
        if not player:
            await interaction.followup.send(f"Player #{player_id} not found.", ephemeral=True)
            return
        if player.league_id != league.id:
            await interaction.followup.send(
                f"Player #{player_id} does not belong to this league.", ephemeral=True
            )
            return
        if player.roster_status not in ("free_agent", "waived"):
            await interaction.followup.send(
                f"**{player.full_name}** is not a free agent or waived (status: {player.roster_status}).",
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
            detail=f"Signed {player.full_name} to {team.full_name} — ${salary:,}/yr x {years}yr.",
        )

        await interaction.followup.send(
            f"**{player.full_name}** signed to **{team.full_name}** — ${salary:,}/yr x {years} year(s).",
            ephemeral=True,
        )


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.bot.tree.add_command(AdminGroup())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
