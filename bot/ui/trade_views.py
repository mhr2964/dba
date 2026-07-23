from __future__ import annotations

import discord

from bot.embeds import trade_embeds
from core.errors import safe_defer
from core.logging import get_logger
from data.db import get_pool
from services import trade_service

log = get_logger(__name__)


class TradeAcceptView(discord.ui.View):
    """Sent via DM to the counterparty so they can accept or decline."""

    def __init__(self, trade_id: int, counterparty_user_id: int, counterparty_team_id: int) -> None:
        super().__init__(timeout=86400)  # 24h — trades expire server-side separately
        self.trade_id = trade_id
        self.counterparty_user_id = counterparty_user_id
        self.counterparty_team_id = counterparty_team_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.counterparty_user_id:
            await interaction.response.send_message(
                "Only the counterparty manager can respond to this trade.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.green)
    async def accept_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await safe_defer(interaction)
        pool = await get_pool()
        trade = await trade_service.accept(pool, self.trade_id, self.counterparty_team_id)
        result_embed = trade_embeds.trade_result(trade, "accepted")
        await interaction.edit_original_response(content="Trade accepted and sent to commissioner.", embed=result_embed, view=None)
        self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.red)
    async def decline_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await safe_defer(interaction)
        pool = await get_pool()
        trade = await trade_service.decline(pool, self.trade_id, self.counterparty_team_id)
        result_embed = trade_embeds.trade_result(trade, "declined")
        await interaction.edit_original_response(content="Trade declined.", embed=result_embed, view=None)
        self.stop()
