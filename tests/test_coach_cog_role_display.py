"""
Tests for bot.cogs.coach_cog's /coach role show display renormalization.

Regression coverage for RA3 (Plan D role-assignment realism sweep): a
manually-locked role (via /coach role assign) stores the raw ROLE_REGISTRY
touch_share constant, and role_service.persist_roles' `WHERE locked = FALSE`
UPSERT guard means that locked row's stored touch_share is never renormalized
against the rest of the team again. Sim math is unaffected (sim_persistence's
_stamp_role_data renormalizes at stamp time regardless of lock state), but the
raw DB values displayed by _roles_embed could fail to sum to 100% -- these
tests confirm the display path renormalizes fresh at render time instead of
showing the stale stored fractions directly.
"""
from __future__ import annotations

import re

import pytest

from bot.cogs.coach_cog import _renormalized_touch_shares, _roles_embed


def _make_roles() -> list[dict]:
    """One locked manual override plus several unlocked auto-derived roles.

    Raw stored touch_share values intentionally sum to 0.80 (not 1.0) --
    mirroring a team where the locked role's raw registry constant (0.40)
    was never renormalized against the other three roles' last auto-derive
    pass (0.20 + 0.12 + 0.08). Chosen so that dividing every value by the
    0.80 total lands on clean round percentages (50/25/15/10) for an
    unambiguous assertion.
    """
    return [
        {
            "player_id": 1,
            "name": "Player One",
            "position": "PG",
            "role": "primary_scorer",
            "touch_share": 0.40,
            "locked": True,
            "assigned_by": "human:999",
        },
        {
            "player_id": 2,
            "name": "Player Two",
            "position": "SG",
            "role": "secondary_scorer",
            "touch_share": 0.20,
            "locked": False,
            "assigned_by": "cpu",
        },
        {
            "player_id": 3,
            "name": "Player Three",
            "position": "SF",
            "role": "glue_guy",
            "touch_share": 0.12,
            "locked": False,
            "assigned_by": "cpu",
        },
        {
            "player_id": 4,
            "name": "Player Four",
            "position": "C",
            "role": "end_of_bench",
            "touch_share": 0.08,
            "locked": False,
            "assigned_by": "cpu",
        },
    ]


def _percentages_from_embed(embed) -> list[int]:
    pct_strs = []
    for field in embed.fields:
        pct_strs.extend(re.findall(r"\((\d+)%\)", field.value))
    return [int(p) for p in pct_strs]


def test_renormalized_touch_shares_sums_to_one():
    """The raw stored values (0.40 + 0.20 + 0.12 + 0.08 = 0.80) don't sum to
    1.0 -- the display-time renormalization helper must fix that up."""
    roles = _make_roles()
    raw_total = sum(r["touch_share"] for r in roles)
    assert raw_total == pytest.approx(0.80)

    shares = _renormalized_touch_shares(roles)
    assert sum(shares) == pytest.approx(1.0)


def test_roles_embed_percentages_sum_to_100_with_locked_role():
    """/coach role show's rendered embed must display percentages that sum to
    100%, even though one role is locked with a raw non-renormalized value."""
    team = {"city": "Test", "name": "Team"}
    roles = _make_roles()

    embed = _roles_embed(team, roles, season=2025)

    pcts = _percentages_from_embed(embed)
    assert len(pcts) == len(roles)
    assert sum(pcts) == 100
    # Exact expected split given the 0.80 raw total (see _make_roles docstring).
    assert pcts == [50, 25, 15, 10]


def test_roles_embed_no_roles_still_renders():
    team = {"city": "Test", "name": "Team"}
    embed = _roles_embed(team, [], season=2025)
    assert embed.description == "No role assignments found for this team."


def test_renormalized_touch_shares_handles_zero_total():
    """All-zero touch_share (degenerate data) shouldn't raise a ZeroDivisionError."""
    roles = [{"touch_share": 0.0, "locked": False}, {"touch_share": 0.0, "locked": True}]
    shares = _renormalized_touch_shares(roles)
    assert shares == [0.0, 0.0]
