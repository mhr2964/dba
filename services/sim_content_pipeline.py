"""Columnist/report content posted during batch sim: awards races, POTM, coach
beat, power rankings, and the rest of the `_maybe_post_*` family.

Extracted from batch_sim_runner.py one function at a time (see HANDOFF.md for
progress) -- each conversion is characterization-tests-first: pin the current
inline-discord.Embed output with a recording fake, confirm against the
original, convert to EmbedData + an announcer, confirm again unchanged, then
move here. Functions that only call an existing bot/embeds/ builder (which
already returns an opaque discord.Embed) and forward it to channel.send are
moved as-is without conversion -- same precedent as
sim_persistence.py's use of bot/embeds/sim_embeds.season_record_embed.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from bot.embeds import awards_embeds
from core.logging import get_logger
from services import awards_service

if TYPE_CHECKING:
    import discord

log = get_logger(__name__)


async def _maybe_post_awards_races(
    pool,
    league_id: int,
    season: int,
    news_channel: discord.TextChannel | None,
    current_game_index: int = 0,
    prefetched_leaders: dict | None = None,
) -> None:
    """Post award race odds. Called from _maybe_post_potm so it fires once per simulated month."""
    try:
        if not news_channel:
            return
        odds = await awards_service.generate_awards_race_odds(
            pool, league_id, season, prefetched_leaders=prefetched_leaders
        )
        if not odds:
            return
        embed = awards_embeds.awards_race_embed(odds, game_index=current_game_index)
        if embed:
            await news_channel.send(embed=embed)
    except Exception as exc:
        log.warning(f"_maybe_post_awards_races failed: {exc}", exc_info=True)
