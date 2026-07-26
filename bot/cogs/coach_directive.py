"""Discord slash commands for player coaching directives and team philosophy.

Extracted from coach_cog.py (Phase 3 opportunistic cog split, see
HANDOFF.md) -- DirectiveSubGroup and PhilosophyGroup are added as
subcommands of CoachGroup (still defined in coach_cog.py). Neither is a
separate bot.client.py extension; they're imported into coach_cog.py and
registered from there, same as before the split -- this is a pure
code-organization split, not a new command-tree shape.
"""
from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands

from bot.embeds import intel_embeds
from core.errors import DBAError, safe_defer, safe_respond
from core.logging import get_logger
from data.db import get_pool
from data.repositories import player_repo
from services import league_service, team_intel

log = get_logger(__name__)

# Valid directive values per category (mirrored from the old directive_cog)
_VALID_VALUES: dict[str, frozenset[str]] = {
    "shot_diet": frozenset({"auto", "force_3s", "attack_rim", "post_heavy", "midrange"}),
    "usage":     frozenset({"feature", "normal", "conserve"}),
    "defense":   frozenset({"lockdown", "standard", "off"}),
    "role":      frozenset({"creator", "scorer", "spot_up"}),
    "clutch":    frozenset({"hero", "normal", "hide"}),
}


_CATEGORY_COLUMN: dict[str, str] = {
    "shot_diet": "shot_diet",
    "usage":     "usage_mode",
    "defense":   "defense_mode",
    "role":      "role_mode",
    "clutch":    "clutch_mode",
}


_CATEGORY_LABELS: dict[str, str] = {
    "shot_diet":    "Shot Diet",
    "usage_mode":   "Usage",
    "defense_mode": "Defense",
    "role_mode":    "Role",
    "clutch_mode":  "Clutch",
}


async def _get_league_id(pool, guild_id: int) -> int:
    row = await pool.fetchrow(
        "SELECT id FROM leagues WHERE discord_guild_id = $1 AND archived_at IS NULL",
        guild_id,
    )
    if not row:
        raise DBAError("No active league in this server.")
    return row["id"]


async def _require_manager_or_commissioner_directive(
    pool, league_id: int, user_id: int, player_team_id: int
) -> None:
    """Raise DBAError if the user is neither manager of player's team nor commissioner."""
    league_row = await pool.fetchrow(
        "SELECT commissioner_user_id FROM leagues WHERE id = $1", league_id
    )
    if league_row and user_id == league_row["commissioner_user_id"]:
        return
    team_row = await pool.fetchrow(
        "SELECT id FROM teams WHERE league_id = $1 AND manager_user_id = $2 AND id = $3",
        league_id, user_id, player_team_id,
    )
    if not team_row:
        raise DBAError("You can only set directives for players on your own team.")


async def _resolve_team_for_directive_view(
    pool, league_id: int, team_code: Optional[str], user_id: int
) -> dict:
    if team_code:
        row = await pool.fetchrow(
            "SELECT * FROM teams WHERE league_id = $1 AND UPPER(nba_team_code) = UPPER($2)",
            league_id, team_code,
        )
        if not row:
            raise DBAError(f"Team `{team_code.upper()}` not found.")
        return dict(row)
    row = await pool.fetchrow(
        "SELECT * FROM teams WHERE league_id = $1 AND manager_user_id = $2",
        league_id, user_id,
    )
    if not row:
        raise DBAError(
            "You don't manage a team. Provide a team code to view another team's directives."
        )
    return dict(row)


class DirectiveSubGroup(app_commands.Group, name="directive", description="Player coaching directives"):

    @app_commands.command(name="set", description="Set a coaching directive for a player")
    @app_commands.describe(
        player="Player name (e.g. LeBron James)",
        category="Category: shot_diet / usage / defense / role / clutch",
        value="New value for that category",
    )
    async def set_directive(
        self,
        interaction: discord.Interaction,
        player: str,
        category: str,
        value: str,
    ) -> None:
        await safe_defer(interaction, ephemeral=True)

        category = category.lower().strip()
        value = value.lower().strip()

        if category not in _VALID_VALUES:
            valid_cats = ", ".join(sorted(_VALID_VALUES))
            await safe_respond(
                interaction,
                content=f"Unknown category `{category}`. Valid categories: {valid_cats}",
                ephemeral=True,
            )
            return

        if value not in _VALID_VALUES[category]:
            valid_vals = ", ".join(sorted(_VALID_VALUES[category]))
            await safe_respond(
                interaction,
                content=f"Invalid value `{value}` for `{category}`. Valid values: {valid_vals}",
                ephemeral=True,
            )
            return

        pool = await get_pool()
        league_id = await _get_league_id(pool, interaction.guild_id)

        matches = await player_repo.search_by_name(pool, league_id, player)
        if not matches:
            await safe_respond(
                interaction,
                content=f"No player found matching '{player}'.",
                ephemeral=True,
            )
            return
        if len(matches) > 1:
            names = ", ".join(f"{p.first_name} {p.last_name}" for p in matches[:5])
            await safe_respond(
                interaction,
                content=f"Multiple players match '{player}': {names}. Be more specific.",
                ephemeral=True,
            )
            return

        found_player = matches[0]
        if found_player.team_id is None:
            await safe_respond(interaction, content="That player is not on a team.", ephemeral=True)
            return

        try:
            await _require_manager_or_commissioner_directive(
                pool, league_id, interaction.user.id, found_player.team_id
            )
        except DBAError as exc:
            await safe_respond(interaction, content=str(exc), ephemeral=True)
            return

        column = _CATEGORY_COLUMN[category]
        await pool.execute(
            f"""
            INSERT INTO player_directives (league_id, player_id, {column}, set_by, updated_at)
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (league_id, player_id) DO UPDATE
                SET {column} = EXCLUDED.{column},
                    set_by = EXCLUDED.set_by,
                    updated_at = EXCLUDED.updated_at
            """,
            league_id, found_player.id, value, interaction.user.id,
        )

        log.info(
            f"directive set: league={league_id} player={found_player.id} "
            f"{column}={value} by user={interaction.user.id}"
        )

        embed = discord.Embed(title="Directive Updated", color=discord.Color.green())
        embed.add_field(name="Player", value=found_player.full_name, inline=True)
        embed.add_field(name="Category", value=_CATEGORY_LABELS.get(column, category), inline=True)
        embed.add_field(name="Value", value=f"`{value}`", inline=True)
        await safe_respond(interaction, embed=embed)

    @app_commands.command(name="view", description="View all directives for your team")
    @app_commands.describe(team="Team code (e.g. LAL), defaults to your team")
    async def view_directives(
        self,
        interaction: discord.Interaction,
        team: Optional[str] = None,
    ) -> None:
        await safe_defer(interaction)

        pool = await get_pool()
        league_id = await _get_league_id(pool, interaction.guild_id)
        try:
            team_row = await _resolve_team_for_directive_view(pool, league_id, team, interaction.user.id)
        except DBAError as exc:
            await safe_respond(interaction, content=str(exc), ephemeral=True)
            return

        rows = await pool.fetch(
            """
            SELECT p.id, p.first_name, p.last_name, p.position, p.overall,
                   pd.shot_diet, pd.usage_mode, pd.defense_mode, pd.role_mode, pd.clutch_mode
            FROM lineups l
            JOIN players p ON p.id = l.player_id
            LEFT JOIN player_directives pd ON pd.league_id = $1 AND pd.player_id = p.id
            WHERE l.league_id = $1 AND l.team_id = $2
            ORDER BY l.slot ASC
            """,
            league_id, team_row["id"],
        )

        team_name = f"{team_row.get('city', '')} {team_row.get('name', '')}".strip()
        embed = discord.Embed(
            title=f"{team_name} — Player Directives",
            color=discord.Color.blue(),
        )

        if not rows:
            embed.description = "No players in lineup."
            await safe_respond(interaction, embed=embed)
            return

        lines = []
        for r in rows:
            name = f"{r['first_name']} {r['last_name']}"
            pos = r["position"] or "?"
            shot = r["shot_diet"] or "auto"
            usage = r["usage_mode"] or "normal"
            defense = r["defense_mode"] or "standard"
            role = r["role_mode"] or "scorer"
            clutch = r["clutch_mode"] or "normal"
            lines.append(
                f"**{name}** ({pos}, OVR {r['overall']})\n"
                f"  Shot: `{shot}` | Usage: `{usage}` | Defense: `{defense}` "
                f"| Role: `{role}` | Clutch: `{clutch}`"
            )

        chunk: list[str] = []
        chunk_len = 0
        field_idx = 1
        for line in lines:
            if chunk_len + len(line) + 1 > 1020 and chunk:
                embed.add_field(
                    name=f"Players ({field_idx})",
                    value="\n".join(chunk),
                    inline=False,
                )
                chunk = []
                chunk_len = 0
                field_idx += 1
            chunk.append(line)
            chunk_len += len(line) + 1

        if chunk:
            label = "Players" if field_idx == 1 else f"Players ({field_idx})"
            embed.add_field(name=label, value="\n".join(chunk), inline=False)

        await safe_respond(interaction, embed=embed)

    @app_commands.command(name="reset", description="Reset all directives for a player to auto")
    @app_commands.describe(player="Player name (e.g. LeBron James)")
    async def reset_directive(
        self,
        interaction: discord.Interaction,
        player: str,
    ) -> None:
        await safe_defer(interaction, ephemeral=True)

        pool = await get_pool()
        league_id = await _get_league_id(pool, interaction.guild_id)

        matches = await player_repo.search_by_name(pool, league_id, player)
        if not matches:
            await safe_respond(
                interaction,
                content=f"No player found matching '{player}'.",
                ephemeral=True,
            )
            return
        if len(matches) > 1:
            names = ", ".join(f"{p.first_name} {p.last_name}" for p in matches[:5])
            await safe_respond(
                interaction,
                content=f"Multiple players match '{player}': {names}. Be more specific.",
                ephemeral=True,
            )
            return

        found_player = matches[0]
        if found_player.team_id is None:
            await safe_respond(interaction, content="That player is not on a team.", ephemeral=True)
            return

        try:
            await _require_manager_or_commissioner_directive(
                pool, league_id, interaction.user.id, found_player.team_id
            )
        except DBAError as exc:
            await safe_respond(interaction, content=str(exc), ephemeral=True)
            return

        await pool.execute(
            "DELETE FROM player_directives WHERE league_id = $1 AND player_id = $2",
            league_id, found_player.id,
        )

        log.info(
            f"directive reset: league={league_id} player={found_player.id} "
            f"by user={interaction.user.id}"
        )

        embed = discord.Embed(
            title="Directives Reset",
            description=(
                f"All directives for **{found_player.full_name}** have been cleared. "
                "The sim engine will use their natural tendencies."
            ),
            color=discord.Color.orange(),
        )
        await safe_respond(interaction, embed=embed)


class PhilosophyGroup(app_commands.Group, name="philosophy", description="Team coaching philosophy"):

    @app_commands.command(
        name="show",
        description="View a team's coach philosophy and its effect on role assignments",
    )
    @app_commands.describe(code="Team code (e.g. LAL). Defaults to your team.")
    async def show(
        self,
        interaction: discord.Interaction,
        code: str | None = None,
    ) -> None:
        await safe_defer(interaction)

        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await safe_respond(interaction, content="No active league in this server.", ephemeral=True)
            return

        pool = await get_pool()

        # Resolve the team the same way TeamGroup.philosophy does.
        if code:
            row = await pool.fetchrow(
                "SELECT * FROM teams WHERE league_id = $1 AND UPPER(nba_team_code) = UPPER($2)",
                league.id, code,
            )
        else:
            row = await pool.fetchrow(
                "SELECT * FROM teams WHERE league_id = $1 AND manager_user_id = $2",
                league.id, interaction.user.id,
            )

        if row is None:
            if code:
                await safe_respond(
                    interaction,
                    content=f"Team `{code.upper()}` not found in this league.",
                    ephemeral=True,
                )
            else:
                await safe_respond(
                    interaction,
                    content="You don't manage a team. Use `/coach philosophy show LAL` to view any team.",
                    ephemeral=True,
                )
            return

        team = dict(row)
        team_id: int = team["id"]

        try:
            philosophy = await team_intel.get_team_philosophy(pool, team_id)
            recent_role_changes = await team_intel.get_recent_role_changes(
                pool, league.id, league.current_season, team_id
            )
        except Exception as exc:
            log.error(
                f"coach philosophy show failed for team {team_id}: {exc}", exc_info=True
            )
            await safe_respond(
                interaction,
                content="Failed to retrieve philosophy data.",
                ephemeral=True,
            )
            return

        team_name = f"{team['city']} {team['name']}"
        embed = intel_embeds.philosophy_embed(
            team_name=team_name,
            philosophy=philosophy,
            recent_role_changes=recent_role_changes,
        )
        await safe_respond(interaction, embed=embed)
