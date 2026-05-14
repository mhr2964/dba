from __future__ import annotations

import discord


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
