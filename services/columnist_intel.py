"""Columnist intel builder — wraps team_intel for columnist prompt injection.

Single responsibility: given a persona and a set of subject team IDs, produce
the intel block that gets injected into the columnist prompt.

Design decisions
----------------
- Caps team coverage to max_teams (default 8) so prompts don't balloon.
  Subject teams are prioritised; if slots remain we fill from the caller's
  supplied rival list (not expanded here — callers pass what they know).
- Filters to only the slices declared in persona.context_keys so each persona
  only gets data it has been instructed to use.
- Returns an empty dict when persona.context_keys is empty — no DB round trip.
- Async because build_team_intel issues DB queries.

Intel provider registry
-----------------------
INTEL_PROVIDERS maps context_key strings to async callables.  Each provider
receives (pool, league, season, team_ids) and returns a partial intel dict
keyed by team_id.

Adding a new provider is one function with @register_intel_provider("key").
The key must match the string personas declare in their context_keys tuple.

Built-in providers (registered at module level below):
  "posture"              — cpu trade posture via team_intel.compute_posture (bulk path)
  "plan"                 — franchise plan via team_intel.get_team_plan
  "philosophy"           — coaching philosophy via team_intel.get_team_philosophy
  "recent_pivots"        — plan pivot history via team_intel.get_recent_pivots
  "recent_role_changes"  — role assignment changes via team_intel.get_recent_role_changes
  "season_history"       — champion/MVP/Finals MVP per completed season (history_repo)
  "hall_of_fame"         — league HOF inductees (hof_repo)
  "all_time_records"     — current league all-time records (all_time_records_repo)

"context_signals" is NOT a team-intel provider — it is sourced from the caller's
extra_context dict (trade signals computed per player) and handled upstream in
columnist_service.generate.

League-wide providers (Phase 1 fix D1)
---------------------------------------
"season_history", "hall_of_fame", and "all_time_records" are LEAGUE-wide, not
team-scoped — history_seasons/hall_of_fame/all_time_records carry no team_id
filter that would make sense here (a champion or a career record belongs to
the league's history, not to one of the current subject teams). To keep the
per-team merge shape in build_columnist_intel uniform, each provider attaches
the SAME league-wide list under every team_id in team_ids. All three are
None-safe: a league with no completed seasons yet (season 1, nothing
finished) gets an empty list back, never an error — a persona's voice_notes
can check "if season_history:" before referencing it (Phase 2 concern; this
module's job is just making the data reliably retrievable).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.logging import get_logger
from services.personas.base import Persona
from services import team_intel
from data.repositories import history_repo, hof_repo, all_time_records_repo

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Intel provider registry
# ---------------------------------------------------------------------------

INTEL_PROVIDERS: dict[str, Callable] = {}


def register_intel_provider(key: str):
    """Decorator that registers an async intel provider under *key*.

    The provider signature must be:
        async def provider(pool, league, season: int, team_ids: list[int]) -> dict[int, Any]

    The returned dict is keyed by team_id.  build_columnist_intel merges
    results from all declared providers into a single per-team dict.

    Usage::

        @register_intel_provider("my_key")
        async def _provide_my_key(pool, league, season, team_ids):
            result = {}
            for tid in team_ids:
                result[tid] = await some_query(pool, league.id, tid, season)
            return result
    """
    def decorator(fn: Callable) -> Callable:
        if key in INTEL_PROVIDERS:
            log.warning(
                "columnist_intel: overwriting existing provider for key %r", key
            )
        INTEL_PROVIDERS[key] = fn
        return fn
    return decorator


# ---------------------------------------------------------------------------
# Built-in providers — delegate to team_intel bulk path where possible
# ---------------------------------------------------------------------------

@register_intel_provider("posture")
async def _provide_posture(pool, league, season: int, team_ids: list[int]) -> dict[int, Any]:
    """Fetch trade posture for each team via the team_intel bulk path."""
    raw = await team_intel.build_team_intel(
        pool, league, season, team_ids, include=("posture",)
    )
    return {tid: data.get("posture") for tid, data in raw.items()}


@register_intel_provider("plan")
async def _provide_plan(pool, league, season: int, team_ids: list[int]) -> dict[int, Any]:
    """Fetch franchise plan for each team."""
    raw = await team_intel.build_team_intel(
        pool, league, season, team_ids, include=("plan",)
    )
    return {tid: data.get("plan") for tid, data in raw.items()}


@register_intel_provider("philosophy")
async def _provide_philosophy(pool, league, season: int, team_ids: list[int]) -> dict[int, Any]:
    """Fetch coaching philosophy for each team."""
    raw = await team_intel.build_team_intel(
        pool, league, season, team_ids, include=("philosophy",)
    )
    return {tid: data.get("philosophy") for tid, data in raw.items()}


@register_intel_provider("recent_pivots")
async def _provide_recent_pivots(pool, league, season: int, team_ids: list[int]) -> dict[int, Any]:
    """Fetch recent plan pivots for each team."""
    raw = await team_intel.build_team_intel(
        pool, league, season, team_ids, include=("recent_pivots",)
    )
    return {tid: data.get("recent_pivots") for tid, data in raw.items()}


@register_intel_provider("recent_role_changes")
async def _provide_recent_role_changes(pool, league, season: int, team_ids: list[int]) -> dict[int, Any]:
    """Fetch recent role assignment changes for each team."""
    raw = await team_intel.build_team_intel(
        pool, league, season, team_ids, include=("recent_role_changes",)
    )
    return {tid: data.get("recent_role_changes") for tid, data in raw.items()}


@register_intel_provider("season_history")
async def _provide_season_history(pool, league, season: int, team_ids: list[int]) -> dict[int, Any]:
    """League-wide season history — champion/MVP/Finals MVP per completed
    season (history_repo.get_all_seasons), newest first.

    Not team-scoped (see module docstring) — the same list is attached to
    every team_id. Returns [] per team when no season has completed yet.
    """
    seasons = await history_repo.get_all_seasons(pool, league.id)
    return {tid: seasons for tid in team_ids}


@register_intel_provider("hall_of_fame")
async def _provide_hall_of_fame(pool, league, season: int, team_ids: list[int]) -> dict[int, Any]:
    """League-wide Hall of Fame inductees (hof_repo.get_all), induction order.

    Not team-scoped (see module docstring) — the same list is attached to
    every team_id. Returns [] per team when nobody has been inducted yet.
    """
    inductees = await hof_repo.get_all(pool, league.id)
    return {tid: inductees for tid in team_ids}


@register_intel_provider("all_time_records")
async def _provide_all_time_records(pool, league, season: int, team_ids: list[int]) -> dict[int, Any]:
    """League-wide current all-time records (all_time_records_repo.get_all).

    Not team-scoped (see module docstring) — the same list is attached to
    every team_id. Returns [] per team when no all-time record has been set
    yet (early in season 1).
    """
    records = await all_time_records_repo.get_all(pool, league.id)
    return {tid: records for tid in team_ids}


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------

# Keys that map to team_intel slices (not handled as providers here because
# they are already covered by the five built-in providers above, but we still
# need to know which persona keys are team-intel sourced vs. caller-sourced).
_KNOWN_TEAM_INTEL_KEYS = frozenset(
    {"posture", "plan", "philosophy", "recent_pivots", "recent_role_changes"}
)


async def build_columnist_intel(
    pool,
    league,
    season: int,
    persona: Persona,
    subject_team_ids: list[int],
    *,
    max_teams: int = 8,
) -> dict:
    """Build the intel block for a columnist call.

    - If persona.context_keys is empty, returns {} immediately (no DB hit).
    - Caps team_ids to max_teams; subject teams take priority.
    - Dispatches to registered INTEL_PROVIDERS for each declared context_key.
      "context_signals" is skipped (caller-sourced, not team-intel).
    - Returns {"team_intel": {team_id: {...}}} shaped for prompt injection.
      The caller is responsible for merging this into the user message.

    When all declared keys map to the same underlying team_intel bulk call
    (the five built-in providers), a single consolidated bulk query is issued
    instead of one query per key, matching the behaviour of the previous
    implementation.
    """
    if not persona.context_keys:
        return {}

    if not subject_team_ids:
        return {}

    # Cap to max_teams, preserving subject order.
    capped_ids = list(dict.fromkeys(subject_team_ids))[:max_teams]

    # Separate keys that have registered providers from unknown ones.
    # "context_signals" is caller-sourced — skip it.
    provider_keys = [
        k for k in persona.context_keys
        if k != "context_signals" and k in INTEL_PROVIDERS
    ]
    if not provider_keys:
        return {}

    # Fast path: all declared keys are covered by the built-in team_intel
    # providers, which each issue one bulk query.  We can collapse them into a
    # single build_team_intel call to avoid redundant round trips.
    builtin_keys = [k for k in provider_keys if k in _KNOWN_TEAM_INTEL_KEYS]
    custom_keys = [k for k in provider_keys if k not in _KNOWN_TEAM_INTEL_KEYS]

    merged: dict[int, dict] = {tid: {} for tid in capped_ids}

    # Single consolidated query for all built-in keys.
    if builtin_keys:
        try:
            raw = await team_intel.build_team_intel(
                pool, league, season, capped_ids, include=tuple(builtin_keys)
            )
            for tid, slices in raw.items():
                for key in builtin_keys:
                    if key in slices:
                        merged.setdefault(tid, {})[key] = slices[key]
        except Exception as exc:
            log.warning(
                "build_columnist_intel: bulk team_intel fetch failed for persona %s: %s",
                persona.id, exc,
            )

    # Custom providers (non-team_intel sources) run individually.
    for key in custom_keys:
        provider = INTEL_PROVIDERS[key]
        try:
            result = await provider(pool, league, season, capped_ids)
            for tid, val in result.items():
                if val is not None:
                    merged.setdefault(tid, {})[key] = val
        except Exception as exc:
            log.warning(
                "build_columnist_intel: provider %r failed for persona %s: %s",
                key, persona.id, exc,
            )

    # Serialise asyncpg Record values to plain Python types for JSON safety.
    safe: dict[int, dict] = {}
    for tid, slices in merged.items():
        safe[tid] = {}
        for key, val in slices.items():
            if isinstance(val, list):
                safe[tid][key] = [
                    dict(item) if hasattr(item, "items") else item for item in val
                ]
            elif hasattr(val, "items"):
                safe[tid][key] = dict(val)
            else:
                safe[tid][key] = val

    # Drop teams with no intel (all providers failed or returned nothing).
    safe = {tid: data for tid, data in safe.items() if data}

    if not safe:
        return {}

    return {"team_intel": safe}
