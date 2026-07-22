"""Seam between business logic and Discord posting.

Services build plain data (this module's `EmbedData`) and hand it to an
`Announcer`; they never import `discord` or touch a channel object directly.
Concrete implementations (`cpu_trade_announcer.py`, `sim_channel_announcer.py`,
added in Phase 2) resolve `channel_key` to a real channel via
`league_repo.get_channel(pool, league.id, channel_key)` and do the actual
`discord.Embed(...)` / `channel.send(...)` calls.

`channel_key` matches the string already passed to `league_repo.get_channel`
today (e.g. `"trade-block"`, `"league-news"`) — this protocol formalizes an
existing convention rather than inventing a new one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class EmbedField:
    name: str
    value: str
    inline: bool = False


@dataclass
class EmbedData:
    title: str
    description: str = ""
    color: int | None = None
    fields: list[EmbedField] = field(default_factory=list)
    footer: str | None = None
    thumbnail_url: str | None = None
    image_url: str | None = None


@runtime_checkable
class Announcer(Protocol):
    """Implemented by presentation-layer adapters, consumed by services."""

    async def post_embed(self, channel_key: str, embed_data: EmbedData) -> None:
        """Post one embed to the channel resolved from `channel_key`.

        No-ops (and logs a warning) if the channel isn't configured or the
        bot can't see it — mirrors the existing `if not ch: return` guard
        used at every direct `channel.send` call site today.
        """
        ...

    async def post_text(self, channel_key: str, content: str) -> None:
        """Post a plain-text message to the channel resolved from `channel_key`."""
        ...
