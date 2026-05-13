from __future__ import annotations

from typing import Optional

import discord
from discord.ext import commands

from core.errors import DBAError
from core.logging import get_logger
from data.db import get_pool
from data.repositories import team_repo

log = get_logger(__name__)


class DraftPickView(discord.ui.View):
    """
    Presented when a human team is on the clock.
    Select menu lists top 20 available prospects.
    Only the on-clock team's manager may interact.
    timeout=None so the view survives bot restarts (paired with a stable custom_id).
    """

    def __init__(
        self,
        league_id: int,
        season: int,
        team_id: int,
        prospects: list[dict],
        bot: commands.Bot,
    ) -> None:
        super().__init__(timeout=None)
        self.league_id = league_id
        self.season = season
        self.team_id = team_id
        self.bot = bot

        select = _build_select(league_id, season, prospects)
        self.add_item(select)


def _build_select(league_id: int, season: int, prospects: list[dict]) -> discord.ui.Select:
    options = [
        discord.SelectOption(
            label=f"{p['first_name']} {p['last_name']}",
            description=f"{p['position']} | OVR {p['overall']}",
            value=str(p["id"]),
        )
        for p in prospects[:25]  # Discord max is 25 options
    ]

    select = _ProspectSelect(
        league_id=league_id,
        season=season,
        options=options,
        custom_id=f"draft_pick_{league_id}_{season}",
        placeholder="Select a player to draft...",
    )
    return select


class _ProspectSelect(discord.ui.Select):
    def __init__(
        self,
        league_id: int,
        season: int,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.league_id = league_id
        self.season = season

    async def callback(self, interaction: discord.Interaction) -> None:
        from services import draft_service

        pool = await get_pool()

        # Resolve which team the interacting user manages.
        user_team = await team_repo.get_by_manager(pool, self.league_id, interaction.user.id)
        if user_team is None:
            await interaction.response.send_message(
                "You don't manage a team in this league.", ephemeral=True
            )
            return

        # Verify it's actually this team's pick.
        draft = await pool.fetchrow(
            "SELECT current_pick_number, status FROM drafts WHERE league_id = $1 AND season = $2",
            self.league_id,
            self.season,
        )
        if not draft or draft["status"] != "in_progress":
            await interaction.response.send_message("The draft is not in progress.", ephemeral=True)
            return

        from data.repositories import draft_repo as dr
        draft_row = await dr.get_draft(pool, self.league_id, self.season)
        on_clock_team_id = await dr.get_on_the_clock_team(pool, draft_row.id, draft_row.current_pick_number)

        if on_clock_team_id != user_team.id:
            await interaction.response.send_message(
                "It's not your team's pick right now.", ephemeral=True
            )
            return

        await interaction.response.defer()

        player_id = int(self.values[0])

        try:
            result = await draft_service.make_pick(
                self.league_id,
                self.season,
                user_team.id,
                player_id,
            )
        except DBAError as exc:
            await interaction.followup.send(exc.message, ephemeral=True)
            return

        # Disable this view so the select can't be reused.
        for child in self.view.children:
            child.disabled = True
        await interaction.message.edit(view=self.view)

        # Announce the pick and advance to next.
        from bot.embeds.draft_embeds import pick_announcement
        player = result["player"] or {}
        team_obj = await team_repo.get_by_id(pool, user_team.id)
        embed = pick_announcement(result["pick_number"], team_obj, player)
        await interaction.followup.send(embed=embed)

        # Continue draft (handles CPU auto-picks until next human team).
        guild = interaction.guild
        next_state = await draft_service.advance_pick(self.league_id, self.season, guild)

        if next_state["status"] == "human_on_clock":
            from bot.embeds.draft_embeds import on_the_clock_embed
            next_team = next_state["team"]
            next_view = DraftPickView(
                league_id=self.league_id,
                season=self.season,
                team_id=next_team.id,
                prospects=next_state["prospects"],
                bot=self.view.bot,
            )
            next_embed = on_the_clock_embed(next_team, next_state["pick_number"], next_state["prospects"])

            news_channel_id = await pool.fetchval(
                "SELECT discord_channel_id FROM league_channels WHERE league_id = $1 AND channel_role = 'league-news'",
                self.league_id,
            )
            if news_channel_id:
                channel = guild.get_channel(news_channel_id)
                if channel:
                    await channel.send(embed=next_embed, view=next_view)

        elif next_state["status"] == "complete":
            from bot.embeds.draft_embeds import draft_complete_embed
            from data.repositories import draft_repo as dr2
            final_draft = await dr2.get_draft(pool, self.league_id, self.season)
            selections = await dr2.get_selections(pool, final_draft.id)
            summary = await _build_summary(pool, selections)
            embed = draft_complete_embed(summary)
            await interaction.followup.send(embed=embed)


async def _build_summary(pool, selections) -> list[dict]:
    result = []
    for sel in selections:
        player_row = await pool.fetchrow(
            "SELECT first_name, last_name, position, overall FROM players WHERE id = $1",
            sel.player_id,
        )
        team_row = await pool.fetchrow(
            "SELECT name, city FROM teams WHERE id = $1",
            sel.team_id,
        )
        result.append({
            "pick_number": sel.pick_number,
            "round": sel.round,
            "team_name": f"{team_row['city']} {team_row['name']}" if team_row else "Unknown",
            "player_name": f"{player_row['first_name']} {player_row['last_name']}" if player_row else "Unknown",
            "position": player_row["position"] if player_row else "?",
            "overall": player_row["overall"] if player_row else 0,
        })
    return result
