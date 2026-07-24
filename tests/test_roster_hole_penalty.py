"""
Unit tests for trade_proposal_scoring._roster_hole_penalty (finding #4 / B9:
docs/design/trade-logic-rules.md's roster-hole follow-up check, previously
pure documentation with zero implementation).
"""
from __future__ import annotations

from services.trade_proposal_scoring import _roster_hole_penalty


class _FakePlayer:
    def __init__(self, player_id: int, position: str):
        self.id = player_id
        self.position = position


def _depth_roster(**extra_at_position: int) -> list[_FakePlayer]:
    """Build a roster with 2 players at each of the 5 core positions by
    default (a realistic depth chart, so only the position(s) under test
    are thin) — pass e.g. C=1 to override how many centers it starts with.
    """
    positions = ["PG", "SG", "SF", "PF", "C"]
    roster: list[_FakePlayer] = []
    next_id = 0
    for pos in positions:
        count = extra_at_position.get(pos, 2)
        for _ in range(count):
            roster.append(_FakePlayer(next_id, pos))
            next_id += 1
    return roster


def _ids_at(roster: list[_FakePlayer], position: str) -> list[int]:
    return [p.id for p in roster if p.position == position]


def test_no_penalty_when_position_still_has_two_after_trade():
    """3 centers pre-trade; trade away 1 — post-trade count stays at 2,
    which clears hole_floor(2), so no penalty fires."""
    roster = _depth_roster(C=3)
    outgoing = {_ids_at(roster, "C")[0]}
    incoming: list = []
    penalty, holes = _roster_hole_penalty(roster, outgoing, incoming, "developing")
    assert penalty == 1.0, f"Post-trade C count (2) should clear hole_floor; got holes={holes}"
    assert holes == []


def test_penalty_when_only_center_traded_away_with_no_backfill():
    roster = _depth_roster(C=1)
    outgoing = {_ids_at(roster, "C")[0]}  # the only center leaves
    incoming: list = []  # nothing comes back at C
    penalty, holes = _roster_hole_penalty(roster, outgoing, incoming, "developing")
    assert penalty == 0.75, f"Expected B9 downweight; got penalty={penalty}"
    assert holes == ["C"], f"Expected hole at C; got {holes}"


def test_no_penalty_when_incoming_player_backfills_the_position():
    """2 centers pre-trade; one leaves but an incoming center replaces it —
    post-trade count stays at 2 (clears hole_floor), so no penalty fires."""
    roster = _depth_roster(C=2)
    outgoing = {_ids_at(roster, "C")[0]}
    incoming = [_FakePlayer(99, "C")]  # incoming player replaces the departing center
    penalty, holes = _roster_hole_penalty(roster, outgoing, incoming, "developing")
    assert penalty == 1.0
    assert holes == []


def test_no_penalty_when_position_was_already_deep_surplus():
    """4 centers pre-trade (>= surplus_floor 3) — trading 3 away at once drops
    the post-trade count to 1 (< hole_floor 2), but the pre-trade surplus
    (4 >= 3) exempts this position from the hole check entirely."""
    roster = _depth_roster(C=4)
    outgoing = set(_ids_at(roster, "C")[:3])  # 3 of the 4 centers leave
    incoming: list = []
    penalty, holes = _roster_hole_penalty(roster, outgoing, incoming, "developing")
    assert penalty == 1.0, f"Pre-trade surplus (4 >= 3) should exempt this position; got holes={holes}"
    assert holes == []


def test_rebuilding_posture_skips_the_check_entirely():
    roster = _depth_roster(C=1)
    outgoing = {_ids_at(roster, "C")[0]}
    incoming: list = []
    penalty, holes = _roster_hole_penalty(roster, outgoing, incoming, "rebuilding")
    assert penalty == 1.0, "B4's own carve-out: holes don't matter when rebuilding"
    assert holes == []


def test_tanking_posture_skips_the_check_entirely():
    roster = _depth_roster(C=1)
    outgoing = {_ids_at(roster, "C")[0]}
    incoming: list = []
    penalty, holes = _roster_hole_penalty(roster, outgoing, incoming, "tanking")
    assert penalty == 1.0
    assert holes == []


def test_soft_rebuild_posture_is_not_exempted():
    """Only rebuilding/tanking are exempt — soft_rebuild still gets the check
    (it's a lighter rebuild, not the 'deep tank' the carve-out targets)."""
    roster = _depth_roster(C=1)
    outgoing = {_ids_at(roster, "C")[0]}
    incoming: list = []
    penalty, holes = _roster_hole_penalty(roster, outgoing, incoming, "soft_rebuild")
    assert penalty == 0.75
    assert holes == ["C"]


def test_multiple_holes_reported():
    """A trade that hollows out two positions at once reports both."""
    roster = _depth_roster(C=1, PG=1)
    outgoing = {_ids_at(roster, "C")[0], _ids_at(roster, "PG")[0]}
    incoming: list = []
    penalty, holes = _roster_hole_penalty(roster, outgoing, incoming, "developing")
    assert penalty == 0.75
    assert set(holes) == {"C", "PG"}


def test_contending_posture_is_not_exempted():
    """contending is nowhere near the rebuilding/tanking carve-out — a
    contender trading away its only backup center still gets flagged."""
    roster = _depth_roster(C=1)
    outgoing = {_ids_at(roster, "C")[0]}
    incoming: list = []
    penalty, holes = _roster_hole_penalty(roster, outgoing, incoming, "contending")
    assert penalty == 0.75
    assert holes == ["C"]
