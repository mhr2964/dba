"""discord.ui.View for The Big Picture's "Read full column" expand affordance (B3).

Mirrors the swap-embed-in-place pattern already proven in
bot/ui/box_score_views.py / bot/ui/stats_views.py: the full embed is pre-built
at construction time and a single button swaps the message's embed to it by
editing in place -- no second LLM call, no extra DB round-trip. Unlike those
two (which use a select menu to flip between several equally-weighted views),
this is a one-way reveal: once expanded there's nothing to collapse back to,
so a single button that disables itself after use is the right control,
not a select menu.
"""
from __future__ import annotations

import discord

from services.announcer_protocol import EmbedData


def _build_embed(embed_data: EmbedData) -> discord.Embed:
    """Same construction as sim_channel_announcer._build_embed -- duplicated
    locally (not imported) because that helper is a private implementation
    detail of a different module; bot/ui is the presentation layer that's
    expected to own its own discord.Embed construction, matching how
    box_score_views.py/stats_views.py are handed pre-built discord.Embed
    objects rather than importing an embed-builder from a services module."""
    embed = discord.Embed(
        title=embed_data.title,
        description=embed_data.description,
        color=embed_data.color,
    )
    for f in embed_data.fields:
        embed.add_field(name=f.name, value=f.value, inline=f.inline)
    if embed_data.footer:
        embed.set_footer(text=embed_data.footer)
    if embed_data.thumbnail_url:
        embed.set_thumbnail(url=embed_data.thumbnail_url)
    if embed_data.image_url:
        embed.set_image(url=embed_data.image_url)
    return embed


class BigPictureExpandView(discord.ui.View):
    """One button: "Read full column" expands the posted teaser to the full
    column in place, then disables itself -- there's nothing to collapse back
    to, so unlike the select-menu views elsewhere in bot/ui this is a
    one-way reveal."""

    def __init__(self, full_embed_data: EmbedData) -> None:
        super().__init__(timeout=300)
        self._full_embed = _build_embed(full_embed_data)
        self.message: discord.Message | None = None

    @discord.ui.button(label="Read full column", style=discord.ButtonStyle.secondary, emoji="📖")
    async def expand(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        button.disabled = True
        button.label = "Full column"
        await interaction.response.edit_message(embed=self._full_embed, view=self)
        self.message = interaction.message

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass
