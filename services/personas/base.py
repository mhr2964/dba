from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Persona:
    id: str
    display_name: str
    byline: str
    avatar_emoji: str
    voice_notes: str
    categories: tuple[str, ...]
    # Which team_intel slices this persona consumes.  Drives prompt augmentation
    # in columnist_service — only declared slices are injected.  Empty tuple means
    # no intel block; existing personas behave exactly as before.
    context_keys: tuple[str, ...] = field(default_factory=tuple)
