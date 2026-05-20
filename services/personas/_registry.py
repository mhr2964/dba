from __future__ import annotations

from services.personas.base import Persona

REGISTRY: dict[str, Persona] = {}


def register_persona(persona: Persona) -> Persona:
    """Register a Persona instance into the REGISTRY by its id.

    Call at module-level in each persona file after constructing the instance.
    Returns the persona unchanged so it can still be assigned to a module attribute.
    """
    if persona.id in REGISTRY:
        raise ValueError(f"Persona '{persona.id}' already registered")
    REGISTRY[persona.id] = persona
    return persona
