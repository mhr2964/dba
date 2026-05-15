from __future__ import annotations

from services.personas.base import Persona
from services.personas.maya_chen import maya_chen
from services.personas.marcus_brooks import marcus_brooks
from services.personas.marcus_cole import marcus_cole
from services.personas.ren_takahashi import ren_takahashi
from services.personas.jordan_rivera import jordan_rivera
from services.personas.keisha_williams import keisha_williams
from services.personas.hot_take_hour import hot_take_hour
from services.personas.pat_chen import pat_chen

# Registry keyed by persona id — used by columnist_service for lookups.
PERSONAS: dict[str, Persona] = {
    maya_chen.id: maya_chen,
    marcus_brooks.id: marcus_brooks,
    marcus_cole.id: marcus_cole,
    ren_takahashi.id: ren_takahashi,
    jordan_rivera.id: jordan_rivera,
    keisha_williams.id: keisha_williams,
    hot_take_hour.id: hot_take_hour,
    pat_chen.id: pat_chen,
}

__all__ = [
    "Persona",
    "PERSONAS",
    "maya_chen",
    "marcus_brooks",
    "marcus_cole",
    "ren_takahashi",
    "jordan_rivera",
    "keisha_williams",
    "hot_take_hour",
    "pat_chen",
]
