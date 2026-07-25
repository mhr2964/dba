"""Pure positional-need weighting for CPU free-agent targeting.

FA2 in docs/design/fa-logic-rules.md: CPU teams used to walk one global
OVR-sorted free-agent list (fa_repo.get_unsigned_players) regardless of their
own roster composition — a team with 4 centers and no point guards chased the
same names as a team starving at center. This is the structural cousin of
trade_proposal_scoring.py::_roster_hole_penalty (same core-position vocabulary,
same hole/surplus floors) but scores *toward* signing rather than penalizing a
trade — free agency has no "outgoing" side to net against, so a hole always
raises the score instead of discounting it.

No pool access, no async — a plain function over position counts, cheap to
unit test directly (see tests/test_fa_service.py's FA2 regression cases).
"""
from __future__ import annotations

# Core position groups tracked for need weighting — matches
# trade_proposal_scoring._CORE_POSITIONS so the two subsystems agree on
# what counts as a "position" for hole/surplus purposes.
CORE_POSITIONS: tuple[str, ...] = ("PG", "SG", "SF", "PF", "C")

_HOLE_FLOOR = 2      # fewer than this at a position, post-signing, is a hole
_SURPLUS_FLOOR = 3   # this many or more already rostered pre-signing is surplus
_HOLE_MULTIPLIER = 1.4
_SURPLUS_MULTIPLIER = 0.6
_NEUTRAL_MULTIPLIER = 1.0


def _position_need_multiplier(position_counts: dict[str, int], position: str) -> float:
    """Score multiplier for a free agent at `position` given the team's current
    active-roster position_counts (pre-signing counts, keyed by position code).

    Returns:
      1.4 — signing this FA would still leave the team with < _HOLE_FLOOR (2)
            players at `position` (a real hole even after adding this player).
      0.6 — the team already has >= _SURPLUS_FLOOR (3) players at `position`
            (already deep; a marginal add here is worth less than at a thin spot).
      1.0 — otherwise (adequately staffed, not yet surplus).

    position_counts.get(position, 0) defaults to 0 for positions the team has
    no active roster players at, which correctly reads as the deepest hole.
    """
    current = position_counts.get(position, 0)
    post_signing = current + 1

    if post_signing < _HOLE_FLOOR:
        return _HOLE_MULTIPLIER
    if current >= _SURPLUS_FLOOR:
        return _SURPLUS_MULTIPLIER
    return _NEUTRAL_MULTIPLIER
