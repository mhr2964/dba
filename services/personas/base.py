from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Persona:
    id: str
    display_name: str
    byline: str
    avatar_emoji: str
    voice_notes: str
    categories: tuple[str, ...]
