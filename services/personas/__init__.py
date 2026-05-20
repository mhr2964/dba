"""Persona registry — auto-discovers every sibling module.

Adding a new persona:
1. Create ``services/personas/<name>.py`` defining a Persona instance
   and calling ``register_persona(Persona(...))``
2. That's it — no edits to this file needed
"""
from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from services.personas.base import Persona
from services.personas._registry import REGISTRY, register_persona

_pkg_path = Path(__file__).parent
for _, modname, _ in pkgutil.iter_modules([str(_pkg_path)]):
    if modname.startswith("_"):
        continue
    importlib.import_module(f"{__name__}.{modname}")

# Public alias — preserves all existing ``from services.personas import PERSONAS`` callers.
PERSONAS: dict[str, Persona] = REGISTRY

__all__ = ["Persona", "PERSONAS", "register_persona"]
