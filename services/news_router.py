from __future__ import annotations

from typing import Optional

import discord

from core.logging import get_logger
from data.repositories import league_repo

log = get_logger(__name__)

# Maps a content category to the channel role it should post to.
# Any category not listed here falls back to "league-news".
_CATEGORY_TO_ROLE: dict[str, str] = {
    "headline":   "league-news",
    "analysis":   "analysis",
    "transaction": "transactions",
    "injury":     "injuries",
    "game_recap": "box-scores",
    "streak":     "league-news",
    "record":     "league-news",
}

_FALLBACK_ROLE = "league-news"


async def get_channel(
    pool,
    guild: discord.Guild,
    league_id: int,
    category: str,
) -> Optional[discord.TextChannel]:
    """Route a content category to its Discord channel.

    Looks up the channel role for *category*, fetches the registered Discord
    channel ID from the DB, and returns the live channel object.  Falls back
    to 'league-news' if:
      - the category is not in the mapping, or
      - the mapped channel's ID is not registered in the DB yet (league
        predates the channel-structure overhaul), or
      - the Discord channel object is no longer in the guild cache.

    Returns None only when even the league-news fallback cannot be resolved.
    """
    target_role = _CATEGORY_TO_ROLE.get(category, _FALLBACK_ROLE)

    channel_id = await league_repo.get_channel(pool, league_id, target_role)
    if channel_id:
        ch = guild.get_channel(channel_id)
        if ch is not None:
            return ch  # type: ignore[return-value]
        log.warning(
            f"news_router: channel id {channel_id} for role '{target_role}' "
            f"not found in guild {guild.id} — falling back to league-news"
        )

    # Primary role failed (not registered or not in cache) — try fallback.
    if target_role != _FALLBACK_ROLE:
        fallback_id = await league_repo.get_channel(pool, league_id, _FALLBACK_ROLE)
        if fallback_id:
            ch = guild.get_channel(fallback_id)
            if ch is not None:
                return ch  # type: ignore[return-value]

    return None
