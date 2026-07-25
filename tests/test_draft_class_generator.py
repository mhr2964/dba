"""
Unit tests for services.draft_class_generator (pure, no DB).

D2 (docs/design/draft-logic-rules.md): the old formula
`potential = overall + randint(0, 15)` made potential < overall structurally
impossible, so no prospect could ever turn out to be a true bust (realized
ceiling below what their draft slot suggested). These tests regression-guard
the slot-weighted Gaussian delta that replaced it.

Statistical tests seed the RNG up front (random.seed) for determinism, per
the P3 lesson documented in tests/test_progression.py's
test_high_potential_grows_more — don't rely on iteration count alone to paper
over an unseeded test.
"""
from __future__ import annotations

import random

from services.draft_class_generator import (
    _ALL_ATTRS,
    _slot_tier_bust_boom_mu,
    generate_draft_class,
    POSITIONS,
)

_MARKET_PREFS = {"big_market", "neutral", "indifferent"}


def test_potential_can_be_below_overall():
    """D2 regression: across a large generated class, a meaningful fraction
    of prospects should have potential < overall (a real bust) — impossible
    under the old `overall + randint(0, 15)` formula."""
    random.seed(1234)
    prospects = generate_draft_class(2031, num_players=600)

    busts = [p for p in prospects if p["potential"] < p["overall"]]
    bust_fraction = len(busts) / len(prospects)

    assert bust_fraction >= 0.10, (
        f"Expected a meaningful bust fraction (>=10%) with the Gaussian delta; "
        f"got {bust_fraction:.1%} ({len(busts)}/{len(prospects)})"
    )
    # Sanity: also confirm booms (potential clearly above overall) still happen —
    # this isn't just "busts exist," the distribution should be genuinely two-sided.
    booms = [p for p in prospects if p["potential"] > p["overall"] + 5]
    assert booms, "Expected some prospects with a clearly higher potential than overall"


def test_potential_variance_by_slot():
    """D2 regression: early picks (slot 1-5) should have a higher mean
    potential-delta than late/second-round picks (slot 31-60) across many
    generated classes — but not strictly monotonic per player, since sigma=10
    means any individual pick can bust or boom regardless of slot.
    """
    random.seed(5678)

    early_deltas: list[int] = []
    late_deltas: list[int] = []
    for _ in range(40):
        prospects = generate_draft_class(2032, num_players=60)
        for p in prospects:
            slot = p["_mock_rank"]
            delta = p["potential"] - p["overall"]
            if slot <= 5:
                early_deltas.append(delta)
            elif slot > 30:
                late_deltas.append(delta)

    early_mean = sum(early_deltas) / len(early_deltas)
    late_mean = sum(late_deltas) / len(late_deltas)

    assert early_mean > late_mean, (
        f"Expected early-pick mean potential-delta ({early_mean:.2f}) to exceed "
        f"late-pick mean ({late_mean:.2f}) across {len(early_deltas)}/{len(late_deltas)} samples"
    )
    # Not strictly monotonic per player — individual busts/booms at any slot are fine,
    # just confirm both buckets contain at least one below-average-slot-mu outcome.
    assert any(d < 0 for d in early_deltas), "Even early picks should occasionally bust"
    assert any(d > 0 for d in late_deltas), "Even late picks should occasionally boom"


def test_slot_tier_bust_boom_mu_shape():
    """The mean delta should trend downward as slot gets later, per D2's rule
    (picks 1-5: +6, 6-14: +3, 15-30: 0, second round: -2)."""
    assert _slot_tier_bust_boom_mu(1) == 6
    assert _slot_tier_bust_boom_mu(5) == 6
    assert _slot_tier_bust_boom_mu(6) == 3
    assert _slot_tier_bust_boom_mu(14) == 3
    assert _slot_tier_bust_boom_mu(15) == 0
    assert _slot_tier_bust_boom_mu(30) == 0
    assert _slot_tier_bust_boom_mu(31) == -2
    assert _slot_tier_bust_boom_mu(60) == -2


def test_position_distribution_weights():
    """Position distribution should roughly track _POSITION_WEIGHTS
    (PG/SG/SF all more common than PF, PF more common than C) across a
    large sample."""
    random.seed(2468)
    prospects = generate_draft_class(2033, num_players=3000)

    counts = {pos: 0 for pos in POSITIONS}
    for p in prospects:
        counts[p["position"]] += 1

    total = len(prospects)
    fractions = {pos: counts[pos] / total for pos in POSITIONS}

    # Expected fractions from _POSITION_WEIGHTS = [22, 22, 24, 18, 14] (sum 100).
    expected = {"PG": 0.22, "SG": 0.22, "SF": 0.24, "PF": 0.18, "C": 0.14}
    for pos, exp_frac in expected.items():
        assert abs(fractions[pos] - exp_frac) < 0.04, (
            f"{pos}: expected ~{exp_frac:.0%}, got {fractions[pos]:.1%}"
        )

    # C should be clearly the least common position — matches weight ordering.
    # (PG/SG/SF are close enough in weight (22/22/24) that sampling noise can
    # flip their exact rank, so only assert the position with a clear gap.)
    assert counts["C"] == min(counts.values())
    assert counts["C"] < counts["PF"] < counts["PG"]
    assert counts["C"] < counts["PF"] < counts["SG"]
    assert counts["C"] < counts["PF"] < counts["SF"]


def test_all_prospects_have_valid_attribute_ranges():
    """Every generated prospect should have attributes within their documented
    clamp ranges, regardless of the widened D2 tier bands."""
    random.seed(999)
    prospects = generate_draft_class(2034, num_players=600)

    assert len(prospects) == 600
    for p in prospects:
        assert 40 <= p["overall"] <= 99, p
        assert 50 <= p["potential"] <= 99, p
        assert p["position"] in POSITIONS
        for attr in _ALL_ATTRS:
            assert 40 <= p[attr] <= 99, f"{attr}={p[attr]} out of range for {p}"
        assert 24 <= p["peak_age_start"] <= 28
        assert p["peak_age_end"] > p["peak_age_start"]
        assert 0 <= p["loyalty"] <= 100
        assert 0 <= p["money_drive"] <= 100
        assert 0 <= p["win_drive"] <= 100
        assert p["market_pref"] in _MARKET_PREFS
        assert 0 <= p["star_leverage"] <= 99
        assert p["roster_status"] == "prospect"
        assert p["is_rookie"] is True
        assert p["team_id"] is None
