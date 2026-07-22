"""Concrete `Announcer` for CPU trade events — the only place in the trade-proposal
pipeline allowed to import `discord`. cpu_trade_proposal_runner.py calls
`_post_trade_block_ads` instead of building embeds itself.

Extracted from cpu_trade_proposals.py (`_post_trade_block_ads`), restructured to
build `EmbedData` and post through `_DiscordAnnouncer` rather than constructing
`discord.Embed` directly, per the Announcer protocol in `announcer_protocol.py`.
"""
from __future__ import annotations

import discord

from core.logging import get_logger
from data.repositories import league_repo
from services.announcer_protocol import EmbedData

log = get_logger(__name__)


class _DiscordAnnouncer:
    """Resolves `channel_key` via `league_repo.get_channel`, posts via discord.py."""

    def __init__(self, pool, league_id: int, guild: discord.Guild):
        self._pool = pool
        self._league_id = league_id
        self._guild = guild

    async def _resolve_channel(self, channel_key: str):
        channel_id = await league_repo.get_channel(self._pool, self._league_id, channel_key)
        if not channel_id:
            return None
        return self._guild.get_channel(channel_id)

    async def post_embed(self, channel_key: str, embed_data: EmbedData) -> None:
        channel = await self._resolve_channel(channel_key)
        if not channel:
            return
        embed = discord.Embed(
            title=embed_data.title,
            description=embed_data.description,
            color=embed_data.color,
        )
        for field in embed_data.fields:
            embed.add_field(name=field.name, value=field.value, inline=field.inline)
        if embed_data.footer:
            embed.set_footer(text=embed_data.footer)
        if embed_data.thumbnail_url:
            embed.set_thumbnail(url=embed_data.thumbnail_url)
        if embed_data.image_url:
            embed.set_image(url=embed_data.image_url)
        await channel.send(embed=embed)

    async def post_text(self, channel_key: str, content: str) -> None:
        channel = await self._resolve_channel(channel_key)
        if not channel:
            return
        await channel.send(content)


_MODE_DESCRIPTIONS = {
    "rebuilding": "Rebuilding mode — looking for young assets (age ≤ 22) and future picks",
    "soft_rebuild": "Soft Rebuild mode — selling veterans, looking for picks and youth (age ≤ 24)",
    "contending": "Contending mode — looking for immediate impact role players (OVR 75+)",
    "play_in_fringe": "Play-In Fringe mode — looking for solid upgrades (OVR 77+)",
    "developing": "Developing mode — looking for veteran depth and expiring contracts",
}


async def _post_trade_block_ads(
    pool,
    league,
    trade,
    guild: discord.Guild,
) -> None:
    """
    After a CPU-to-CPU trade is approved, post one embed per involved team to
    #trade-block advertising what they're looking to acquire next.
    """
    announcer = _DiscordAnnouncer(pool, league.id, guild)

    team_rows = await pool.fetch(
        "SELECT nba_team_code, cpu_mode FROM teams WHERE id = ANY($1)",
        [trade.proposer_team_id, trade.counterparty_team_id],
    )

    for row in team_rows:
        team_code = row["nba_team_code"]
        cpu_mode = row["cpu_mode"] or "default"
        description = _MODE_DESCRIPTIONS.get(
            cpu_mode,
            "Open for business — looking to improve at the margins",
        )
        embed_data = EmbedData(
            title=f"\U0001F4CB {team_code} — Looking to Deal",
            description=description,
            color=discord.Color.blue().value,
            footer="CPU-managed team",
        )
        try:
            await announcer.post_embed("trade-block", embed_data)
        except Exception as exc:
            log.warning(f"Failed to post trade-block ad for {team_code}: {exc}")
