from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

REGISTRY: list[tuple[str, Callable]] = []  # order matters for tie-break narrative


def register_signal(code: str):
    def decorator(fn: Callable) -> Callable:
        if any(c == code for c, _ in REGISTRY):
            log.warning(
                "trade_signals: overwriting existing signal for code %r", code
            )
            # Replace in-place to preserve order; tests may legitimately reload.
            for i, (c, _) in enumerate(REGISTRY):
                if c == code:
                    REGISTRY[i] = (code, fn)
                    return fn
        REGISTRY.append((code, fn))
        return fn
    return decorator


@dataclass
class SignalContext:
    pool: Any                      # asyncpg pool
    league_id: int
    season: int
    perspective_team_id: int
    plan: dict
    posture: dict
    coach_philosophy: str | None
    incoming_player: dict
    incoming_role: str | None
    form_mod: float
    stats: dict
