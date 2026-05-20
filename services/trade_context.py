"""Context-signal aggregator for trade evaluation.

Each detector examines one aspect of an incoming player's fit with the receiving
team and returns a ContextSignal (delta, reason, code) or None when the signal
does not fire.

The aggregator ``compute_context_modifier`` sums all fired signals into a
single multiplicative modifier in [0.85, 1.15] that is applied to the
player's value contribution in ``cpu_should_accept`` and ``_attempt_one_offer``.

The same signal list that drives value math is exposed through the evaluation
dict so ride-along / ra_reasoning can render identical narrative bullets —
guaranteeing that what the user reads IS what moved the number.

Detectors live in ``services/trade_signals/``.  Adding a new detector is one
file: create ``services/trade_signals/<name>.py`` with ``@register_signal``.

Delta magnitudes (tunable, documented in each detector file):
  synergy_complementary       : +0.06
  synergy_overlap             : -0.08
  role_fit_match              : +0.05
  role_mismatch               : +0.04   (buy-low signal — miscast player elsewhere)
  window_fit_match            : +0.07
  window_fit_miss             : -0.10
  defense_elite               : +0.06
  defense_floor               : -0.03
  defense_elite_blocker       : +0.07
  defense_elite_steals        : +0.06
  defense_two_way_disruption  : +0.05
  efficiency_elite            : +0.06
  efficiency_3pt_specialist   : +0.05
  efficiency_inefficient_volume: -0.05
  overperforming              : -0.04   (regression risk on incoming)
  scheme_fit_match            : +0.05

Total clamped to ±0.15 so context nudges but never dominates raw value math.
"""
from __future__ import annotations

import inspect
from typing import NamedTuple

from core.logging import get_logger

log = get_logger(__name__)


class ContextSignal(NamedTuple):
    delta: float    # additive contribution to context modifier
    reason: str     # narrative text for display (same text shown in ride-along)
    code: str       # short ID for JSONL logging


# ---------------------------------------------------------------------------
# Internal helper: resolve the incoming player's role from player_roles
# ---------------------------------------------------------------------------

async def _get_incoming_role(pool, league_id: int, season: int, player_id: int) -> str | None:
    """Return the player's most recent role string, or None if unknown."""
    try:
        row = await pool.fetchrow(
            """
            SELECT pr.role
            FROM player_roles pr
            WHERE pr.league_id = $1 AND pr.season = $2 AND pr.player_id = $3
            ORDER BY pr.team_id DESC
            LIMIT 1
            """,
            league_id, season, player_id,
        )
        return row["role"] if row else None
    except Exception as exc:
        log.debug("_get_incoming_role failed pid=%d: %s", player_id, exc)
        return None


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

async def compute_context_modifier(
    pool,
    league_id: int,
    season: int,
    perspective_team_id: int,
    plan: dict,
    posture: dict,
    coach_philosophy: str | None,
    incoming_player: dict,
    form_mod: float,
    stats: dict | None = None,
) -> tuple[float, list[ContextSignal]]:
    """Aggregate all registered context detectors for one incoming player.

    Returns ``(modifier, signals)`` where:
    - ``modifier`` is in [0.85, 1.15] — multiply against the player's base value
      contribution to apply the context adjustment.
    - ``signals`` is the list of every ContextSignal that fired, in registration
      order.  Pass this to ra_reasoning so the narrative reads from the same data
      that moved the math.

    ``stats`` is the per-player season production dict (ppg, bpg, spg, fg_pct,
    fg3_pct, ft_pct, ts_pct, fg3a, gp, mpg, …).  Pass it through from the
    asset dict's ``season_stats`` field so shooting/defensive detectors fire.

    The modifier clamp is ±0.15 so context nudges but never dominates.
    """
    # Import here to trigger auto-discovery of all signal modules.
    from services.trade_signals import REGISTRY, SignalContext  # noqa: PLC0415

    signals: list[ContextSignal] = []
    delta = 0.0
    _stats = stats or {}

    incoming_role = await _get_incoming_role(
        pool, league_id, season, incoming_player.get("id", -1)
    )

    ctx = SignalContext(
        pool=pool,
        league_id=league_id,
        season=season,
        perspective_team_id=perspective_team_id,
        plan=plan,
        posture=posture,
        coach_philosophy=coach_philosophy,
        incoming_player=incoming_player,
        incoming_role=incoming_role,
        form_mod=form_mod,
        stats=_stats,
    )

    for _code, detector_fn in REGISTRY:
        try:
            if inspect.iscoroutinefunction(detector_fn):
                result = await detector_fn(ctx)
            else:
                result = detector_fn(ctx)
        except Exception as exc:
            log.debug("context detector %s failed: %s", _code, exc)
            result = None

        if result is not None:
            signals.append(result)
            delta += result.delta

    # Clamp total delta to ±0.15.
    delta = max(-0.15, min(0.15, delta))
    modifier = round(1.0 + delta, 4)
    return modifier, signals
