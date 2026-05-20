"""Context-signal detector registry — auto-discovers every sibling module.

Adding a new signal:
1. Create ``services/trade_signals/<name>.py`` with ``@register_signal("<code>")``
   on an ``async def detect(ctx: SignalContext)`` or ``def detect(ctx: SignalContext)``
2. ``compute_context_modifier`` in ``trade_context.py`` picks it up automatically
"""
from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from ._registry import REGISTRY, SignalContext, register_signal

_pkg_path = Path(__file__).parent
for _, modname, _ in pkgutil.iter_modules([str(_pkg_path)]):
    if modname.startswith("_"):
        continue
    importlib.import_module(f"{__name__}.{modname}")

__all__ = ["REGISTRY", "SignalContext", "register_signal"]
