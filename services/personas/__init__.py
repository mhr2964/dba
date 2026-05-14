from __future__ import annotations

from services.personas.base import Persona
from services.personas.maya_chen import maya_chen
from services.personas.marcus_brooks import marcus_brooks
from services.personas.ren_takahashi import ren_takahashi

# Registry keyed by persona id — used by columnist_service for lookups.
PERSONAS: dict[str, Persona] = {
    maya_chen.id: maya_chen,
    marcus_brooks.id: marcus_brooks,
    ren_takahashi.id: ren_takahashi,
}

__all__ = ["Persona", "PERSONAS", "maya_chen", "marcus_brooks", "ren_takahashi"]
