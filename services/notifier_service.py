from __future__ import annotations

import discord

from core.logging import get_logger
from data.db import get_pool
from data.repositories import league_repo

log = get_logger(__name__)


async def send_dm(
    bot: discord.Client,
    league_id: int,
    user_id: int,
    embed: discord.Embed,
    fallback_message: str,
    fallback_channel_role: str = "league-news",
) -> bool:
    """
    Try to DM the user. On failure (DM closed or user not found):
    - Log the failure
    - Post fallback_message as a ping in the fallback channel
    Returns True if DM succeeded, False if fell back to channel.
    """
    user = bot.get_user(user_id)
    if user is None:
        try:
            user = await bot.fetch_user(user_id)
        except discord.NotFound:
            log.warning(f"send_dm: user {user_id} not found — skipping DM, no fallback channel available")
            return False

    try:
        await user.send(embed=embed)
        log.info(f"send_dm: DM delivered to user {user_id}")
        return True
    except discord.Forbidden:
        log.info(f"send_dm: DM to user {user_id} blocked — falling back to channel {fallback_channel_role!r}")

    pool = await get_pool()
    channel_id = await league_repo.get_channel(pool, league_id, fallback_channel_role)
    if channel_id is None:
        log.warning(
            f"send_dm: fallback channel {fallback_channel_role!r} not configured for league {league_id}"
        )
        return False

    channel = bot.get_channel(channel_id)
    if channel is None:
        log.warning(
            f"send_dm: fallback channel {channel_id} not in cache for league {league_id}"
        )
        return False

    await channel.send(f"<@{user_id}> {fallback_message}")
    return False


async def notify_trade_proposed(
    bot: discord.Client,
    league_id: int,
    user_id: int,
    trade_embed: discord.Embed,
) -> None:
    """Notify a manager that a trade was proposed to their team."""
    await send_dm(
        bot,
        league_id,
        user_id,
        trade_embed,
        fallback_message="A trade has been proposed to your team. Check your DMs or the trade channel.",
        fallback_channel_role="transactions",
    )


async def notify_game_upcoming(
    bot: discord.Client,
    league_id: int,
    user_id: int,
    game_embed: discord.Embed,
) -> None:
    """Notify a manager their team has an upcoming user-vs-user game."""
    await send_dm(
        bot,
        league_id,
        user_id,
        game_embed,
        fallback_message="Your team has an upcoming matchup. Head to the sim channel to ready up.",
        fallback_channel_role="league-news",
    )


async def notify_fa_offer_response(
    bot: discord.Client,
    league_id: int,
    user_id: int,
    offer_result_embed: discord.Embed,
) -> None:
    """Notify a manager of their player's FA decision."""
    await send_dm(
        bot,
        league_id,
        user_id,
        offer_result_embed,
        fallback_message="A free agent has responded to your offer. Check the transactions channel for details.",
        fallback_channel_role="transactions",
    )
