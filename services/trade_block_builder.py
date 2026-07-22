"""Derives each CPU team's tradeable-player list ("trade block") from its franchise plan.

Extracted from cpu_trade_proposal_runner.py — DB-read-heavy, no discord.
"""
from __future__ import annotations

from core.logging import get_logger
from data.repositories import player_repo, team_repo
from services import franchise_plan_service
from services.cpu_trade_posture import _player_age

log = get_logger(__name__)


async def _get_franchise_plan(pool, league_id: int, team_id: int, season: int) -> dict | None:
    """Read the stored franchise plan for a CPU team.  Returns None if absent or on error.

    Deliberately reads only — does NOT derive or persist.  derive_and_persist_all()
    at batch-start should have populated plans for all CPU teams; if it didn't,
    the warning here is the signal, not a silent backfill write per trade-eval iteration.
    """
    try:
        plan = await franchise_plan_service.get_plan(pool, league_id, team_id, season)
        if plan is None:
            log.warning(
                "franchise_plan missing for team %d league %d season %d — "
                "derive_and_persist_all may not have run this batch",
                team_id, league_id, season,
            )
        return plan
    except Exception as exc:
        log.debug(
            "franchise_plan lookup failed for team %d league %d season %d — %s",
            team_id, league_id, season, exc,
        )
        return None


def _urgency_allows_flex(urgency: str) -> bool:
    """Return True when the team's urgency level permits including flex players in trade block."""
    return urgency in ("pushing", "desperate", "tanking")


async def _build_cpu_trade_block(
    pool,
    league_id: int,
    season: int,
    cpu_teams: list[team_repo.Team],
    recently_signed_ids: set[int] | None = None,
    postures: dict[int, dict] | None = None,
) -> dict[int, list[int]]:
    """
    For each CPU team, identify players that make sense to offer in a trade.
    Returns a map of team_id -> list of player_ids considered tradeable.

    CPU teams never manually populate the trade block, so this derives it from
    each team's franchise plan (Phase 2) with a fallback to cpu_mode heuristics.

    Plan-driven logic (primary):
    - surplus players are always tradeable
    - flex players are tradeable when urgency is pushing/desperate/tanking
    - core players are NEVER tradeable (plan primary, is_cornerstone is backstop)

    Mode-based heuristics (fallback when plan unavailable):
    - rebuilding: veterans age >= 32, or age >= 29 with OVR >= 65
    - contending: mid-tier players OVR 72–84 (trade bait, not franchise cornerstones)
    - developing: players age >= 30, or age >= 27 with OVR >= 78
    - default: players OVR 70–82

    Players whose contracts were signed within the last 60 sim games are
    excluded from the block (recently_signed_ids).
    """
    if recently_signed_ids is None:
        recently_signed_ids = set()
    if postures is None:
        postures = {}

    result: dict[int, list[int]] = {}

    for team in cpu_teams:
        players = await player_repo.get_roster(pool, league_id, team.id)
        tradeable: list[int] = []
        mode = team.cpu_mode or "default"
        urgency = postures.get(team.id, {}).get("urgency", "comfortable")

        # --- Phase 2: plan-driven filtering (primary) ---
        plan = await _get_franchise_plan(pool, league_id, team.id, season)
        if plan is not None:
            core_set = set(plan.get("core_player_ids") or [])
            flex_set = set(plan.get("flex_player_ids") or [])
            surplus_set = set(plan.get("surplus_player_ids") or [])
            allow_flex = _urgency_allows_flex(urgency)

            for p in players:
                if p.id in recently_signed_ids:
                    continue
                # Core players are never tradeable — plan is primary, cornerstone is backstop.
                if p.id in core_set:
                    continue
                # Surplus always tradeable.
                if p.id in surplus_set:
                    tradeable.append(p.id)
                    continue
                # Flex tradeable only under pressure.
                if p.id in flex_set and allow_flex:
                    tradeable.append(p.id)
        else:
            # --- Fallback: mode-based heuristics ---
            for p in players:
                if p.id in recently_signed_ids:
                    continue
                age = _player_age(p)  # may be None — age-based checks skip if None
                ovr = p.overall

                if mode == "rebuilding":
                    # True rebuild: shop every vet 27+ and every high-OVR non-cornerstone.
                    # Skip age check for players with missing birth_date (don't assume young).
                    if (age is not None and age >= 27) or (ovr >= 78 and age is not None and age >= 22):
                        tradeable.append(p.id)
                elif mode == "soft_rebuild":
                    # Sell aging vets: anyone 30+, or 28+ OVR 65+
                    if (age is not None and age >= 30) or (age is not None and age >= 28 and ovr >= 65):
                        tradeable.append(p.id)
                elif mode == "contending":
                    # Trade non-star bench pieces for better role players.
                    if 72 <= ovr <= 84:
                        tradeable.append(p.id)
                elif mode == "play_in_fringe":
                    # Like contending but slightly broader — any non-star role player
                    if 70 <= ovr <= 84:
                        tradeable.append(p.id)
                elif mode == "developing":
                    if (age is not None and age >= 30) or (ovr >= 78 and age is not None and age >= 27):
                        tradeable.append(p.id)
                else:
                    if 70 <= ovr <= 82:
                        tradeable.append(p.id)

        if tradeable:
            result[team.id] = tradeable

    return result
