"""Captures user replies to tracked bot messages and exposes export/list commands.

The on_message listener self-gates: it looks up the replied-to message in
`bot_message_log` and silently skips if the post wasn't registered. This means
adding the cog is safe even when no bot-post sites are wired yet — the listener
just does nothing until tracked posts exist.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core.logging import get_logger
from data.db import get_pool
from data.repositories import bot_message_repo, feedback_repo
from services import feedback_log

log = get_logger(__name__)

# 💬 — the emoji posted on a captured reply so the user can tell at a glance
# which of their replies the bot accepted.
_CAPTURE_REACTION = "\U0001f4ac"


class FeedbackCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.reference is None or message.reference.message_id is None:
            return

        pool = await get_pool()
        bot_row = await bot_message_repo.get_by_message_id(
            pool, message.reference.message_id
        )
        if bot_row is None:
            # Reply was to a non-tracked message — silent skip. This is the
            # listener's self-gate: cog is safe to register even if no sites
            # are wired yet.
            return

        try:
            reply_id = await feedback_log.record_reply(pool, bot_row, message)
        except Exception as exc:
            log.warning(
                f"feedback_cog: record_reply failed for reply {message.id}: {exc}",
                exc_info=True,
            )
            return

        if reply_id is None:
            # Already recorded (Discord redelivery) — don't react again.
            return

        try:
            await message.add_reaction(_CAPTURE_REACTION)
        except discord.HTTPException:
            # Missing permission, channel locked down, etc. — capture still
            # succeeded, the user just won't see the confirmation glyph.
            pass

    feedback_group = app_commands.Group(
        name="feedback",
        description="Inspect and export captured feedback replies",
    )

    @feedback_group.command(
        name="export",
        description="Attach this session's feedback JSONL to a private response",
    )
    async def export(self, interaction: discord.Interaction) -> None:
        pool = await get_pool()
        path, line_count = await feedback_log.export_session(pool)
        if line_count == 0 or not path.exists():
            await interaction.response.send_message(
                "No feedback captured this session yet — reply to a tracked bot post first.",
                ephemeral=True,
            )
            return
        try:
            await interaction.response.send_message(
                content=(
                    f"`{path.name}` — {line_count} reply line(s). "
                    "Attach this when feeding the session back."
                ),
                file=discord.File(str(path)),
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            log.warning(f"/feedback export attachment failed: {exc}")
            await interaction.response.send_message(
                f"Couldn't attach the file — check `{path}` on the host. ({exc})",
                ephemeral=True,
            )

    @feedback_group.command(
        name="list",
        description="Show the 10 most recent captured feedback replies",
    )
    async def list_replies(self, interaction: discord.Interaction) -> None:
        pool = await get_pool()
        rows = await feedback_repo.list_recent(pool, limit=10)
        if not rows:
            await interaction.response.send_message(
                "No feedback captured yet.", ephemeral=True
            )
            return
        embed = discord.Embed(
            title="Recent feedback (last 10)",
            color=discord.Color.from_rgb(80, 160, 200),
        )
        for r in rows:
            anchor_bits = []
            if r.get("sim_date"):
                anchor_bits.append(f"sim {r['sim_date']}")
            elif r.get("game_index") is not None:
                anchor_bits.append(f"game #{r['game_index']}")
            anchor_bits.append(r.get("kind") or "?")
            anchor = " · ".join(anchor_bits)
            preview = (r.get("content_preview") or "")[:120]
            reply_text = (r.get("reply_text") or "")[:200]
            embed.add_field(
                name=f"{r['author_name']} — {anchor}",
                value=f"> {reply_text}\n_re: {preview}_",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FeedbackCog(bot))
