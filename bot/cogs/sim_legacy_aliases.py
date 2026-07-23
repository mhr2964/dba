"""Deprecated top-level slash-command aliases forwarding to their new homes.

Extracted from sim_cog.py (Phase 3 opportunistic cog split, see
HANDOFF.md) -- these 5 commands (/ready, /forceready, /standings,
/schedule, /box-score) predate the /team, /admin, and /season command
groups and are kept for a one-cycle backward-compat window. None of
them touch SimGroup's simulation-advancing helpers, so this is a clean
zero-dependency extraction. Still registered individually (not as a
Group) by SimCog -- this file is a companion module, not its own
bot.client.py extension.
"""
from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands

from bot.embeds import sim_embeds
from bot.embeds.sim_embeds import box_score_summary_embed, box_score_team_embed
from bot.ui.box_score_views import BoxScoreView
from core.errors import safe_defer, safe_respond
from data.db import get_pool
from data.repositories import game_repo, league_repo, team_repo
from phase.helpers import get_league_or_error, require_phase


# ---------------------------------------------------------------------------
# Deprecation aliases for top-level commands moved to groups
# (remove after next season rollover)
# ---------------------------------------------------------------------------

_TOP_LEVEL_DEP_WARNED: set[int] = set()


async def _send_top_dep_warning(
    interaction: discord.Interaction,
    old: str,
    new: str,
) -> None:
    uid = interaction.user.id
    if uid in _TOP_LEVEL_DEP_WARNED:
        return
    if len(_TOP_LEVEL_DEP_WARNED) > 1000:
        _TOP_LEVEL_DEP_WARNED.clear()
    _TOP_LEVEL_DEP_WARNED.add(uid)
    try:
        await interaction.followup.send(
            f"**Heads up:** `{old}` has moved to `{new}`. "
            "The old path will be removed after the next season rollover.",
            ephemeral=True,
        )
    except Exception:
        pass


@app_commands.command(name="ready", description="[MOVED] Use /team ready instead")
async def ready_command(interaction: discord.Interaction) -> None:
    await safe_defer(interaction, ephemeral=True)
    await _send_top_dep_warning(interaction, old="/ready", new="/team ready")
    pool = await get_pool()
    league = await league_repo.get_by_guild(pool, interaction.guild_id)
    if not league:
        await safe_respond(interaction, content="No active league.", ephemeral=True)
        return

    await require_phase(league, "ready")

    team = await team_repo.get_by_manager(pool, league.id, interaction.user.id)
    if not team:
        await safe_respond(
            interaction,
            content="You don't manage a team in this league.",
            ephemeral=True,
        )
        return

    await game_repo.set_ready(pool, league.id, team.id, interaction.user.id, True)
    await safe_respond(interaction, content="Ready.", ephemeral=True)

    teams = await team_repo.get_all(pool, league.id)
    human_teams = [t for t in teams if t.manager_user_id is not None]
    ready_ids = set(await game_repo.get_ready_teams(pool, league.id))
    if human_teams and all(t.id in ready_ids for t in human_teams):
        await interaction.channel.send(
            "All managers are ready! Commissioner can run `/sim to-next-rival` to continue."
        )


@app_commands.command(name="forceready", description="[MOVED] Use /admin force-ready instead")
async def force_ready_command(interaction: discord.Interaction) -> None:
    await safe_defer(interaction, ephemeral=True)
    await _send_top_dep_warning(interaction, old="/forceready", new="/admin force-ready")
    pool = await get_pool()
    league = await league_repo.get_by_guild(pool, interaction.guild_id)
    if not league:
        await safe_respond(interaction, content="No active league.", ephemeral=True)
        return

    if interaction.user.id != league.commissioner_user_id:
        await safe_respond(interaction, content="Only the commissioner can force-ready.", ephemeral=True)
        return

    teams = await team_repo.get_all(pool, league.id)
    human_teams = [t for t in teams if t.manager_user_id is not None]

    if not human_teams:
        await safe_respond(interaction, content="No managers to ready up.", ephemeral=True)
        return

    for team in human_teams:
        await game_repo.set_ready(pool, league.id, team.id, team.manager_user_id, True)

    await safe_respond(
        interaction,
        content=f"Force-readied {len(human_teams)} manager(s). Now run `/sim to-next-rival` to advance.",
        ephemeral=True,
    )


@app_commands.command(name="standings", description="[MOVED] Use /season standings instead")
async def standings_command(interaction: discord.Interaction) -> None:
    await safe_defer(interaction, ephemeral=True)
    await _send_top_dep_warning(interaction, old="/standings", new="/season standings")
    pool = await get_pool()
    league = await league_repo.get_by_guild(pool, interaction.guild_id)
    if not league:
        await safe_respond(interaction, content="No active league.", ephemeral=True)
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
    await safe_respond(interaction, embed=embed)


@app_commands.command(name="schedule", description="[MOVED] Use /season schedule instead")
@app_commands.describe(team_code="NBA team code (e.g. LAL) — defaults to your team")
async def schedule_command(
    interaction: discord.Interaction,
    team_code: Optional[str] = None,
) -> None:
    await safe_defer(interaction, ephemeral=True)
    await _send_top_dep_warning(interaction, old="/schedule", new="/season schedule")
    pool = await get_pool()
    league = await league_repo.get_by_guild(pool, interaction.guild_id)
    if not league:
        await safe_respond(interaction, content="No active league.", ephemeral=True)
        return

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
        user_marker = " [RIVAL]" if r["is_user_matchup"] else ""
        lines.append(
            f"Game #{r['game_index']} — {r['scheduled_date']}  {venue} **{opp_code}**{user_marker}"
        )
    embed.description = "\n".join(lines)
    await safe_respond(interaction, embed=embed)


@app_commands.command(name="box-score", description="[MOVED] Use /season box-score instead")
@app_commands.describe(
    game="Game number from /season schedule or recap footers (regular season)",
    game_id="Game ID from playoff recap footer (use this for playoff games)",
)
async def box_score_command(
    interaction: discord.Interaction,
    game: Optional[int] = None,
    game_id: Optional[int] = None,
) -> None:
    await safe_defer(interaction, ephemeral=True)
    await _send_top_dep_warning(interaction, old="/box-score", new="/season box-score")

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
        game_row = await game_repo.get_game_by_index(pool, league.id, league.current_season, game)
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
