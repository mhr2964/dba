from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger(__name__)

REGISTRY: dict[str, Callable] = {}


def register_philosophy(name: str):
    def decorator(fn: Callable) -> Callable:
        if name in REGISTRY:
            log.warning(
                "philosophies: overwriting existing philosophy %r — "
                "this is expected during test reloads but not in production", name
            )
        REGISTRY[name] = fn
        return fn
    return decorator
