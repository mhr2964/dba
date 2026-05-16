from __future__ import annotations

import discord

from bot.embeds import sim_embeds


def _build_playoff_box_embed(
    team_name: str,
    team_code: str,
    box: list[dict],
    game_number: int | str,
) -> discord.Embed:
    """Full monospace per-player box score for one team in a playoff game."""
    game_row = {
        "game_index": game_number,
        "away_code": "",
        "home_code": team_code,
        "away_score": 0,
        "home_score": 0,
    }
    return sim_embeds.box_score_team_embed(team_code, team_name, box, game_row)


class PlayoffBoxScoreView(discord.ui.View):
    """Select menu attached to playoff game recap embeds.

    Each option reveals one team's full per-player box score as an
    ephemeral response (only the selecting user sees it).
    """

    def __init__(
        self,
        home_team_name: str,
        home_team_code: str,
        away_team_name: str,
        away_team_code: str,
        home_box: list[dict],
        away_box: list[dict],
        game_number: int | str,
    ) -> None:
        super().__init__(timeout=300)
        self._home_embed = _build_playoff_box_embed(
            home_team_name, home_team_code, home_box, game_number
        )
        self._away_embed = _build_playoff_box_embed(
            away_team_name, away_team_code, away_box, game_number
        )

        options = [
            discord.SelectOption(label=f"{away_team_name} Box Score", value="away"),
            discord.SelectOption(label=f"{home_team_name} Box Score", value="home"),
        ]
        self.select_menu.options = options

    @discord.ui.select(
        placeholder="View player stats...",
        options=[
            discord.SelectOption(label="Away Box Score", value="away"),
            discord.SelectOption(label="Home Box Score", value="home"),
        ],
    )
    async def select_menu(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ) -> None:
        chosen = select.values[0]
        embed = self._home_embed if chosen == "home" else self._away_embed
        await interaction.response.send_message(embed=embed, ephemeral=True)


class BoxScoreView(discord.ui.View):
    """Select menu to flip between Summary, Away box score, and Home box score."""

    def __init__(
        self,
        summary_embed: discord.Embed,
        away_embed: discord.Embed,
        home_embed: discord.Embed,
        away_code: str,
        home_code: str,
    ) -> None:
        super().__init__(timeout=180)
        self._embeds = {
            "summary": summary_embed,
            "away": away_embed,
            "home": home_embed,
        }

        options = [
            discord.SelectOption(label="Summary", value="summary", default=True),
            discord.SelectOption(label=f"{away_code} Box Score", value="away"),
            discord.SelectOption(label=f"{home_code} Box Score", value="home"),
        ]
        self.select_menu.options = options

    @discord.ui.select(
        placeholder="Switch view...",
        options=[
            discord.SelectOption(label="Summary", value="summary"),
            discord.SelectOption(label="Away Box Score", value="away"),
            discord.SelectOption(label="Home Box Score", value="home"),
        ],
    )
    async def select_menu(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ) -> None:
        chosen = select.values[0]
        embed = self._embeds[chosen]
        for opt in self.select_menu.options:
            opt.default = opt.value == chosen
        await interaction.response.edit_message(embed=embed, view=self)
