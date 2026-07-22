"""Concrete Announcer for batch-sim game/injury/season announcements, plus the
channel-lookup helpers batch sim orchestration uses to resolve channels once
per batch.

Unlike cpu_trade_announcer.py (which resolves a channel_key fresh on every
post), sim orchestration resolves each channel ONCE per batch via the lookup
functions below, then threads the resolved channel object through several
function calls (_sim_single_game -> _persist_game_result -> _persist_injuries).
_BoundChannelAnnouncer adapts an already-resolved channel to the Announcer
protocol's post_embed/post_text shape so those functions build EmbedData
instead of discord.Embed directly, without changing that channel-threading
architecture. channel_key is accepted (for Announcer-protocol conformance)
but ignored -- the channel was already chosen by the caller.
"""
from __future__ import annotations

import discord

from core.logging import get_logger
from data.repositories import league_repo
from services.announcer_protocol import EmbedData

log = get_logger(__name__)


class _BoundChannelAnnouncer:
    def __init__(self, channel: discord.abc.Messageable | None):
        self._channel = channel

    async def post_embed(self, channel_key: str, embed_data: EmbedData) -> None:
        if not self._channel:
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
        await self._channel.send(embed=embed)

    async def post_text(self, channel_key: str, content: str) -> None:
        if not self._channel:
            return
        await self._channel.send(content)


async def _get_box_scores_channel(guild: discord.Guild, pool, league_id: int) -> "discord.TextChannel | None":
    channel_id = await league_repo.get_channel(pool, league_id, "box-scores")
    if not channel_id:
        return None
    return guild.get_channel(channel_id)


async def _get_standings_channel(guild: discord.Guild, pool, league_id: int) -> "discord.TextChannel | None":
    channel_id = await league_repo.get_channel(pool, league_id, "standings")
    if not channel_id:
        return None
    return guild.get_channel(channel_id)


async def _get_news_channel(guild: discord.Guild, pool, league_id: int) -> "discord.TextChannel | None":
    channel_id = await league_repo.get_channel(pool, league_id, "league-news")
    if not channel_id:
        return None
    return guild.get_channel(channel_id)


async def _get_injury_channel(guild: discord.Guild, pool, league_id: int) -> "discord.TextChannel | None":
    channel_id = await league_repo.get_channel(pool, league_id, "injuries")
    if not channel_id:
        return None
    return guild.get_channel(channel_id) or await _get_news_channel(guild, pool, league_id)


async def _get_transactions_channel(guild: discord.Guild, pool, league_id: int) -> "discord.TextChannel | None":
    channel_id = await league_repo.get_channel(pool, league_id, "transactions")
    if not channel_id:
        return None
    return guild.get_channel(channel_id)


async def _ensure_records_channel(guild: discord.Guild, pool, league_id: int) -> "discord.TextChannel | None":
    """Return the #records channel, creating it lazily if it doesn't exist yet.

    Existing leagues created before the records channel was added to CHANNEL_ROLES
    won't have a row in league_channels.  On first use we create the Discord channel
    under the same category as #league-news and store its ID — subsequent calls are
    a cheap DB lookup.  Falls back to #league-news if creation fails so records are
    never silently dropped.
    """
    channel_id = await league_repo.get_channel(pool, league_id, "records")
    if channel_id:
        ch = guild.get_channel(channel_id)
        if ch:
            return ch

    # Lazy-create: find the DBA category by looking at where #league-news lives.
    news_id = await league_repo.get_channel(pool, league_id, "league-news")
    category: "discord.CategoryChannel | None" = None
    if news_id:
        news_ch = guild.get_channel(news_id)
        if news_ch and news_ch.category:
            category = news_ch.category

    try:
        new_ch = await guild.create_text_channel("records", category=category)
        await league_repo.add_channel(pool, league_id, "records", new_ch.id)
        log.info(
            "sim_channel_announcer: lazily created #records channel (id=%d) for league %d",
            new_ch.id, league_id,
        )
        return new_ch
    except Exception as exc:
        log.warning(
            "sim_channel_announcer: could not create #records channel for league %d: %s — falling back to #league-news",
            league_id, exc,
        )
        # Fall back to league-news so the post isn't silently lost.
        return await _get_news_channel(guild, pool, league_id)
