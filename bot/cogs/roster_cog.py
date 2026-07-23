from __future__ import annotations

import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.errors import DBAError, safe_defer, safe_respond
from core.logging import get_logger
from data.db import get_pool
from data.repositories import player_repo
from services import roster_service
from bot.embeds import roster_embeds
from bot.embeds.info_embeds import injury_report_embed

log = get_logger(__name__)


async def _resolve_team(pool, guild_id: int, team_code: Optional[str], user_id: int):
    """Return (league_id, team_row) or raise DBAError."""
    league_row = await pool.fetchrow(
        "SELECT id FROM leagues WHERE discord_guild_id = $1", guild_id
    )
    if league_row is None:
        raise DBAError("No league found for this server. Use `/league create` to get started.")

    league_id: int = league_row["id"]

    if team_code:
        team_row = await pool.fetchrow(
            "SELECT * FROM teams WHERE league_id = $1 AND nba_team_code = $2",
            league_id,
            team_code.upper(),
        )
        if team_row is None:
            raise DBAError(f"Team `{team_code.upper()}` not found in this league.")
    else:
        team_row = await pool.fetchrow(
            "SELECT * FROM teams WHERE league_id = $1 AND manager_user_id = $2",
            league_id,
            user_id,
        )

    return league_id, team_row


_LINEUP_DEP_WARNED: set[int] = set()


async def _send_lineup_dep_warning(
    interaction: discord.Interaction,
    old: str,
    new: str,
) -> None:
    uid = interaction.user.id
    if uid in _LINEUP_DEP_WARNED:
        return
    if len(_LINEUP_DEP_WARNED) > 1000:
        _LINEUP_DEP_WARNED.clear()
    _LINEUP_DEP_WARNED.add(uid)
    try:
        await interaction.followup.send(
            f"**Heads up:** `{old}` has moved to `{new}`. "
            "The old path will be removed after the next season rollover.",
            ephemeral=True,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Internal helpers for lineup modification (shared by group + legacy aliases)
# ---------------------------------------------------------------------------

async def _do_lineup_start(interaction: discord.Interaction, player: str) -> None:
    """Core logic for promoting a player to starter."""
    await safe_defer(interaction)
    pool = await get_pool()

    league_row = await pool.fetchrow(
        "SELECT id, commissioner_user_id FROM leagues WHERE discord_guild_id = $1 AND archived_at IS NULL",
        interaction.guild_id,
    )
    if not league_row:
        await safe_respond(interaction, content="No active league found.", ephemeral=True)
        return
    league_id: int = league_row["id"]

    team_row = await pool.fetchrow(
        "SELECT * FROM teams WHERE league_id = $1 AND manager_user_id = $2",
        league_id,
        interaction.user.id,
    )
    is_commissioner = interaction.user.id == league_row["commissioner_user_id"]
    if not team_row and not is_commissioner:
        await safe_respond(interaction, content="You are not a team manager in this league.", ephemeral=True)
        return
    if not team_row:
        await safe_respond(
            interaction,
            content="Commissioners must use `/lineup` commands on behalf of a team — specify which team first.",
            ephemeral=True,
        )
        return

    team_id: int = team_row["id"]

    rows = await pool.fetch(
        """
        SELECT l.slot, l.is_starter, p.id AS player_id, p.first_name, p.last_name, p.overall
        FROM lineups l
        JOIN players p ON p.id = l.player_id
        WHERE l.league_id = $1
          AND l.team_id = $2
          AND LOWER(p.first_name || ' ' || p.last_name) LIKE LOWER($3)
        """,
        league_id, team_id, f"%{player}%",
    )
    if not rows:
        await safe_respond(
            interaction,
            content=f"No player matching '{player}' found in your lineup.",
            ephemeral=True,
        )
        return
    if len(rows) > 1:
        names = ", ".join(f"{r['first_name']} {r['last_name']}" for r in rows)
        await safe_respond(
            interaction,
            content=f"Multiple matches for '{player}': {names}. Be more specific.",
            ephemeral=True,
        )
        return

    target = rows[0]
    target_slot: int = target["slot"]
    target_pid: int = target["player_id"]

    if target["is_starter"]:
        await safe_respond(
            interaction,
            content=f"**{target['first_name']} {target['last_name']}** is already a starter.",
            ephemeral=True,
        )
        return

    await pool.execute(
        "UPDATE lineups SET is_starter = TRUE WHERE league_id = $1 AND team_id = $2 AND slot = $3",
        league_id, team_id, target_slot,
    )

    starter_rows = await pool.fetch(
        """
        SELECT l.slot, p.overall
        FROM lineups l
        JOIN players p ON p.id = l.player_id
        WHERE l.league_id = $1 AND l.team_id = $2 AND l.is_starter = TRUE AND l.player_id != $3
        ORDER BY p.overall ASC
        """,
        league_id, team_id, target_pid,
    )
    if len(starter_rows) >= 5:
        demote_slot = starter_rows[0]["slot"]
        await pool.execute(
            "UPDATE lineups SET is_starter = FALSE WHERE league_id = $1 AND team_id = $2 AND slot = $3",
            league_id, team_id, demote_slot,
        )

    lineup_rows = await roster_service.get_lineup(league_id, team_id)
    embed = roster_embeds.lineup_embed(team_row, lineup_rows)
    embed.title = (
        f"Lineup Updated — {target['first_name']} {target['last_name']} promoted to starter"
    )
    await safe_respond(interaction, embed=embed)


async def _do_lineup_bench(interaction: discord.Interaction, player: str) -> None:
    """Core logic for moving a player to the bench."""
    await safe_defer(interaction)
    pool = await get_pool()

    league_row = await pool.fetchrow(
        "SELECT id, commissioner_user_id FROM leagues WHERE discord_guild_id = $1 AND archived_at IS NULL",
        interaction.guild_id,
    )
    if not league_row:
        await safe_respond(interaction, content="No active league found.", ephemeral=True)
        return
    league_id: int = league_row["id"]

    team_row = await pool.fetchrow(
        "SELECT * FROM teams WHERE league_id = $1 AND manager_user_id = $2",
        league_id,
        interaction.user.id,
    )
    is_commissioner = interaction.user.id == league_row["commissioner_user_id"]
    if not team_row and not is_commissioner:
        await safe_respond(interaction, content="You are not a team manager in this league.", ephemeral=True)
        return
    if not team_row:
        await safe_respond(
            interaction,
            content="Commissioners must use `/lineup` commands on behalf of a team — specify which team first.",
            ephemeral=True,
        )
        return

    team_id: int = team_row["id"]

    rows = await pool.fetch(
        """
        SELECT l.slot, l.is_starter, p.id AS player_id, p.first_name, p.last_name, p.overall
        FROM lineups l
        JOIN players p ON p.id = l.player_id
        WHERE l.league_id = $1
          AND l.team_id = $2
          AND LOWER(p.first_name || ' ' || p.last_name) LIKE LOWER($3)
        """,
        league_id, team_id, f"%{player}%",
    )
    if not rows:
        await safe_respond(
            interaction,
            content=f"No player matching '{player}' found in your lineup.",
            ephemeral=True,
        )
        return
    if len(rows) > 1:
        names = ", ".join(f"{r['first_name']} {r['last_name']}" for r in rows)
        await safe_respond(
            interaction,
            content=f"Multiple matches for '{player}': {names}. Be more specific.",
            ephemeral=True,
        )
        return

    target = rows[0]
    target_slot: int = target["slot"]
    target_pid: int = target["player_id"]

    if not target["is_starter"]:
        await safe_respond(
            interaction,
            content=f"**{target['first_name']} {target['last_name']}** is already on the bench.",
            ephemeral=True,
        )
        return

    await pool.execute(
        "UPDATE lineups SET is_starter = FALSE WHERE league_id = $1 AND team_id = $2 AND slot = $3",
        league_id, team_id, target_slot,
    )

    remaining_starters = await pool.fetchval(
        "SELECT COUNT(*) FROM lineups WHERE league_id = $1 AND team_id = $2 AND is_starter = TRUE",
        league_id, team_id,
    )
    if remaining_starters < 5:
        best_bench = await pool.fetchrow(
            """
            SELECT l.slot, p.overall
            FROM lineups l
            JOIN players p ON p.id = l.player_id
            WHERE l.league_id = $1 AND l.team_id = $2 AND l.is_starter = FALSE AND l.player_id != $3
            ORDER BY p.overall DESC
            LIMIT 1
            """,
            league_id, team_id, target_pid,
        )
        if best_bench:
            await pool.execute(
                "UPDATE lineups SET is_starter = TRUE WHERE league_id = $1 AND team_id = $2 AND slot = $3",
                league_id, team_id, best_bench["slot"],
            )

    lineup_rows = await roster_service.get_lineup(league_id, team_id)
    embed = roster_embeds.lineup_embed(team_row, lineup_rows)
    embed.title = (
        f"Lineup Updated — {target['first_name']} {target['last_name']} moved to bench"
    )
    await safe_respond(interaction, embed=embed)


# ---------------------------------------------------------------------------
# /lineup group — show / start / bench
# ---------------------------------------------------------------------------

class LineupGroup(app_commands.Group, name="lineup", description="Lineup management"):

    @app_commands.command(name="show", description="View a team's current starting lineup.")
    @app_commands.describe(team="Team code (e.g. LAL). Defaults to your team.")
    async def show(
        self,
        interaction: discord.Interaction,
        team: Optional[str] = None,
    ) -> None:
        await safe_defer(interaction)
        pool = await get_pool()

        league_id, team_row = await _resolve_team(
            pool, interaction.guild_id, team, interaction.user.id
        )

        if team_row is None:
            await safe_respond(
                interaction,
                content="You don't manage a team. Use `/lineup show LAL` to view any team.",
                ephemeral=True,
            )
            return

        team_id: int = team_row["id"]
        lineup_rows = await roster_service.get_lineup(league_id, team_id)

        if not lineup_rows:
            await safe_respond(
                interaction,
                content=(
                    f"**{team_row['city']} {team_row['name']}** has no lineup set yet. "
                    "Run `python scripts/import_players.py` to populate rosters and auto-generate lineups."
                ),
                ephemeral=True,
            )
            return

        embed = roster_embeds.lineup_embed(team_row, lineup_rows)
        await safe_respond(interaction, embed=embed)

    @app_commands.command(name="start", description="Move a player on your team to the starting five")
    @app_commands.describe(player="Part of the player's name (e.g. 'LeBron')")
    async def start(self, interaction: discord.Interaction, player: str) -> None:
        await _do_lineup_start(interaction, player)

    @app_commands.command(name="bench", description="Move a player on your team to the bench")
    @app_commands.describe(player="Part of the player's name (e.g. 'LeBron')")
    async def bench(self, interaction: discord.Interaction, player: str) -> None:
        await _do_lineup_bench(interaction, player)


class RosterCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.bot.tree.add_command(LineupGroup())

    @app_commands.command(name="roster", description="View a team's full roster and cap sheet.")
    @app_commands.describe(team="Team code (e.g. LAL). Defaults to your team.")
    async def roster(
        self,
        interaction: discord.Interaction,
        team: Optional[str] = None,
    ) -> None:
        await safe_defer(interaction)
        pool = await get_pool()

        league_id, team_row = await _resolve_team(
            pool, interaction.guild_id, team, interaction.user.id
        )

        if team_row is None:
            await safe_respond(
                interaction,
                content="You don't manage a team. Use `/roster LAL` to view any team.",
                ephemeral=True,
            )
            return

        team_id: int = team_row["id"]
        players = await roster_service.get_roster(league_id, team_id)

        if not players:
            await safe_respond(
                interaction,
                content=(
                    f"**{team_row['city']} {team_row['name']}** has no players yet. "
                    "Run `python scripts/import_players.py` to populate the roster."
                ),
                ephemeral=True,
            )
            return

        contracts_by_player_id = {}
        for p in players:
            c = await player_repo.get_active_contract(pool, p.id)
            if c:
                contracts_by_player_id[p.id] = c

        cap_summary = await roster_service.get_cap_summary(league_id, team_id)
        embed = roster_embeds.roster_embed(team_row, players, contracts_by_player_id, cap_summary)
        await safe_respond(interaction, embed=embed)

    # /lineup (standalone view) is now /lineup show — this is kept only as a
    # compatibility shim so the old /lineup <team> path still works.
    # Remove after the next season rollover.
    # NOTE: This command cannot coexist with the LineupGroup if they share the
    # name "lineup" — the group is registered on the bot tree directly, so this
    # method is intentionally left WITHOUT an @app_commands.command decorator to
    # avoid the name collision.  Users who type /lineup will hit the LineupGroup.
    async def _lineup_view_shim(
        self,
        interaction: discord.Interaction,
        team: Optional[str] = None,
    ) -> None:
        await safe_defer(interaction)
        pool = await get_pool()

        league_id, team_row = await _resolve_team(
            pool, interaction.guild_id, team, interaction.user.id
        )

        if team_row is None:
            await safe_respond(
                interaction,
                content="You don't manage a team. Use `/lineup show LAL` to view any team.",
                ephemeral=True,
            )
            return

        team_id: int = team_row["id"]
        lineup_rows = await roster_service.get_lineup(league_id, team_id)

        if not lineup_rows:
            await safe_respond(
                interaction,
                content=(
                    f"**{team_row['city']} {team_row['name']}** has no lineup set yet. "
                    "Run `python scripts/import_players.py` to populate rosters and auto-generate lineups."
                ),
                ephemeral=True,
            )
            return

        embed = roster_embeds.lineup_embed(team_row, lineup_rows)
        await safe_respond(interaction, embed=embed)

    @app_commands.command(
        name="lineup-start",
        description="[MOVED] Use /lineup start instead",
    )
    @app_commands.describe(player="Part of the player's name (e.g. 'LeBron')")
    async def lineup_start(
        self,
        interaction: discord.Interaction,
        player: str,
    ) -> None:
        await safe_defer(interaction)
        await _send_lineup_dep_warning(
            interaction, old="/lineup-start", new="/lineup start"
        )
        await _do_lineup_start(interaction, player)

    @app_commands.command(
        name="lineup-bench",
        description="[MOVED] Use /lineup bench instead",
    )
    @app_commands.describe(player="Part of the player's name (e.g. 'LeBron')")
    async def lineup_bench(
        self,
        interaction: discord.Interaction,
        player: str,
    ) -> None:
        await safe_defer(interaction)
        await _send_lineup_dep_warning(
            interaction, old="/lineup-bench", new="/lineup bench"
        )
        await _do_lineup_bench(interaction, player)

    @app_commands.command(name="injury-report", description="Show all active injuries in the league")
    async def injury_report(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction)
        pool = await get_pool()

        league_row = await pool.fetchrow(
            "SELECT id, current_season FROM leagues WHERE discord_guild_id = $1 AND archived_at IS NULL",
            interaction.guild_id,
        )
        if not league_row:
            await safe_respond(
                interaction,
                content="No active league in this server.",
                ephemeral=True,
            )
            return

        league_id: int = league_row["id"]
        current_season: int = league_row["current_season"]
        today = datetime.date.today()

        rows = await pool.fetch(
            """
            SELECT i.*, p.first_name, p.last_name, p.team_id AS player_team_id
            FROM injuries i
            JOIN players p ON p.id = i.player_id
            WHERE i.league_id = $1
              AND i.season = $2
              AND (i.return_date IS NULL OR i.return_date > $3)
            ORDER BY i.severity, p.last_name
            """,
            league_id,
            current_season,
            today,
        )

        injuries = [dict(r) for r in rows]

        team_ids = {r.get("team_id") or r.get("player_team_id") for r in injuries if r.get("team_id") or r.get("player_team_id")}

        players_by_id: dict = {}
        for row in injuries:
            players_by_id[row["player_id"]] = {
                "full_name": f"{row['first_name']} {row['last_name']}"
            }

        teams_by_id: dict = {}
        if team_ids:
            team_rows = await pool.fetch(
                "SELECT id, nba_team_code FROM teams WHERE id = ANY($1::int[])",
                list(team_ids),
            )
            for tr in team_rows:
                teams_by_id[tr["id"]] = {"code": tr["nba_team_code"]}

        # Normalize team_id field for embed: prefer explicit team_id column, fall back to player's team
        normalized: list[dict] = []
        for r in injuries:
            d = dict(r)
            d["team_id"] = d.get("team_id") or d.get("player_team_id")
            normalized.append(d)

        embed = injury_report_embed(normalized, players_by_id, teams_by_id)
        await safe_respond(interaction, embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RosterCog(bot))
