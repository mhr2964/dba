"""Shared deprecation-notice helper for coach_cog.py's legacy command aliases.

Split out of coach_cog.py so the role and directive command clusters can
each import it without depending on each other's module.
"""
from __future__ import annotations

import discord

from core.logging import get_logger

log = get_logger(__name__)

# Per-cog-instance set: track which users have already seen a deprecation
# notice this session so we don't spam them on every invocation.
_DEPRECATION_WARNED: set[int] = set()


async def _send_deprecation_warning(
    interaction: discord.Interaction,
    old: str,
    new: str,
) -> None:
    """Send a one-time ephemeral notice that a command path has moved."""
    uid = interaction.user.id
    if uid in _DEPRECATION_WARNED:
        return
    if len(_DEPRECATION_WARNED) > 1000:
        _DEPRECATION_WARNED.clear()
    _DEPRECATION_WARNED.add(uid)
    try:
        await interaction.followup.send(
            f"**Heads up:** `{old}` has moved to `{new}`. "
            "The old path will be removed after the next season rollover.",
            ephemeral=True,
        )
    except Exception:
        pass  # best-effort; don't block the real response
