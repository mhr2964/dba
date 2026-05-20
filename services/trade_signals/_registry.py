from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

REGISTRY: list[tuple[str, Callable]] = []  # order matters for tie-break narrative


def register_signal(code: str):
    def decorator(fn: Callable) -> Callable:
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
