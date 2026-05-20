from __future__ import annotations

from typing import Callable

REGISTRY: dict[str, Callable] = {}


def register_philosophy(name: str):
    def decorator(fn: Callable) -> Callable:
        if name in REGISTRY:
            raise ValueError(f"Philosophy '{name}' already registered")
        REGISTRY[name] = fn
        return fn
    return decorator
