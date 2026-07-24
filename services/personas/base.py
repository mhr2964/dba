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
    # Controls which renderer _assemble_article uses to format the Discord body.
    # Valid string-body values (dispatched via _RENDERERS): "analytics",
    # "hot_take", "potm", "trade_report", "passthrough", "tank_watch", "default".
    # "default" reproduces the previous template-stamp format as a safe fallback.
    # "moment", "verdict", "index", "tactical", "recap" exist as renderers in
    # columnist_assembly.py but return EmbedData, not str, and are NOT in
    # _RENDERERS (see that module's docstring) -- do not assign one of these
    # here without also adding explicit EmbedData dispatch in columnist_service.
    format_style: str = "default"
    # Per-category overrides: maps a category string to a format_style string.
    # When the article category matches a key, that style is used instead of
    # the persona's base format_style.  E.g. {"trade_report": "trade_report"}.
    category_overrides: dict[str, str] = field(default_factory=dict)
    # When set, replaces the global output_shape_rule injected by columnist_service.
    # Use this for personas that require a non-standard JSON response shape.
    output_shape_override: str = ""
    # Per-category shape overrides: maps category name → output_shape_override string.
    # When the article category matches a key, that shape is used instead of
    # output_shape_override.  Prevents two conflicting format instructions reaching
    # the LLM when a persona uses different renderers for different categories.
    category_shape_overrides: dict[str, str] = field(default_factory=dict)
