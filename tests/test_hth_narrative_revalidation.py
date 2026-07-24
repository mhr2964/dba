"""
Unit tests for services.sim_content_pipeline._seed_or_revalidate_hth_narratives
(Finding #3 — Hot Take Hour season-long narratives never re-validated).

Previously sleeper_pick/fraud_call/rivalry were seeded once from early-season
data and re-injected verbatim for the rest of the season with no check for
whether the premise was still true. These tests pin: first-use seeding,
regeneration when a named player falls out of the current top-20-scorers
pool, regeneration when a different team takes over the #1-by-wins spot, and
a no-op re-validation when nothing has changed.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from services.sim_content_pipeline import _HTH_NARRATIVES, _seed_or_revalidate_hth_narratives


def _seed_row(pid, name, team, ppg):
    return {"id": pid, "player_name": name, "team_code": team, "conference": "East", "ppg": ppg}


def _pool(seed_rows, top_team):
    pool = MagicMock()

    async def _fetch(sql, *args):
        if "t.conference" in sql:
            return seed_rows
        if "ORDER BY sc.wins DESC LIMIT 1" in sql:
            return [{"nba_team_code": top_team}] if top_team else []
        return []

    pool.fetch = _fetch
    return pool


async def test_first_use_seeds_all_three_slots():
    _HTH_NARRATIVES.pop(9001, None)
    seed_rows = [
        _seed_row(1, "Sleeper Steve", "LAL", 28.0),
        _seed_row(2, "Rival Rick", "BOS", 24.0),
        _seed_row(3, "Rival Ray", "MIA", 23.5),
    ]
    pool = _pool(seed_rows, "LAL")

    await _seed_or_revalidate_hth_narratives(pool, 9001, season=2025)

    slots = _HTH_NARRATIVES[9001]
    assert slots["sleeper_pick"]["player_id"] == 1
    assert "Sleeper Steve" in slots["sleeper_pick"]["text"]
    assert slots["fraud_call"]["team_code"] == "LAL"
    assert slots["rivalry"]["player_a_id"] == 2
    assert slots["rivalry"]["player_b_id"] == 3


async def test_revalidation_is_noop_when_premises_still_hold():
    _HTH_NARRATIVES.pop(9002, None)
    seed_rows = [
        _seed_row(1, "Sleeper Steve", "LAL", 28.0),
        _seed_row(2, "Rival Rick", "BOS", 24.0),
        _seed_row(3, "Rival Ray", "MIA", 23.5),
    ]
    pool = _pool(seed_rows, "LAL")
    await _seed_or_revalidate_hth_narratives(pool, 9002, season=2025)
    first_pass = dict(_HTH_NARRATIVES[9002])

    # Fire again with identical current data — nothing should change.
    await _seed_or_revalidate_hth_narratives(pool, 9002, season=2025)
    assert _HTH_NARRATIVES[9002] == first_pass


async def test_sleeper_regenerates_when_player_falls_out_of_pool():
    """Player traded away / cooled off past the >=10-game qualifying pool —
    the frozen 'criminally underrated' claim about them is now unsupportable."""
    _HTH_NARRATIVES.pop(9003, None)
    seed_rows_initial = [
        _seed_row(1, "Sleeper Steve", "LAL", 28.0),
        _seed_row(2, "Rival Rick", "BOS", 24.0),
        _seed_row(3, "Rival Ray", "MIA", 23.5),
    ]
    pool = _pool(seed_rows_initial, "LAL")
    await _seed_or_revalidate_hth_narratives(pool, 9003, season=2025)
    assert _HTH_NARRATIVES[9003]["sleeper_pick"]["player_id"] == 1

    # Player 1 (Sleeper Steve) no longer appears — traded/cooled off.
    seed_rows_now = [
        _seed_row(4, "New Top Scorer", "DEN", 30.0),
        _seed_row(2, "Rival Rick", "BOS", 24.0),
        _seed_row(3, "Rival Ray", "MIA", 23.5),
    ]
    pool2 = _pool(seed_rows_now, "LAL")
    await _seed_or_revalidate_hth_narratives(pool2, 9003, season=2025)

    assert _HTH_NARRATIVES[9003]["sleeper_pick"]["player_id"] == 4
    assert "New Top Scorer" in _HTH_NARRATIVES[9003]["sleeper_pick"]["text"]


async def test_fraud_call_regenerates_when_top_team_changes():
    _HTH_NARRATIVES.pop(9004, None)
    seed_rows = [_seed_row(1, "Sleeper Steve", "LAL", 28.0)]
    pool = _pool(seed_rows, "LAL")
    await _seed_or_revalidate_hth_narratives(pool, 9004, season=2025)
    assert _HTH_NARRATIVES[9004]["fraud_call"]["team_code"] == "LAL"

    # A different team has since taken over the #1-by-wins spot.
    pool2 = _pool(seed_rows, "DEN")
    await _seed_or_revalidate_hth_narratives(pool2, 9004, season=2025)

    assert _HTH_NARRATIVES[9004]["fraud_call"]["team_code"] == "DEN"
    assert "DEN" in _HTH_NARRATIVES[9004]["fraud_call"]["text"]


async def test_rivalry_regenerates_when_either_player_falls_out_of_pool():
    _HTH_NARRATIVES.pop(9005, None)
    seed_rows_initial = [
        _seed_row(1, "Sleeper Steve", "LAL", 28.0),
        _seed_row(2, "Rival Rick", "BOS", 24.0),
        _seed_row(3, "Rival Ray", "MIA", 23.5),
    ]
    pool = _pool(seed_rows_initial, "LAL")
    await _seed_or_revalidate_hth_narratives(pool, 9005, season=2025)
    assert _HTH_NARRATIVES[9005]["rivalry"]["player_a_id"] == 2

    # Rival Rick (id=2) drops out of the pool — traded or injured.
    seed_rows_now = [
        _seed_row(1, "Sleeper Steve", "LAL", 28.0),
        _seed_row(3, "Rival Ray", "MIA", 23.5),
        _seed_row(5, "Fresh Face", "PHX", 22.0),
    ]
    pool2 = _pool(seed_rows_now, "LAL")
    await _seed_or_revalidate_hth_narratives(pool2, 9005, season=2025)

    rivalry = _HTH_NARRATIVES[9005]["rivalry"]
    assert rivalry["player_a_id"] == 3
    assert rivalry["player_b_id"] == 5


async def test_seed_fetch_failure_is_graceful():
    _HTH_NARRATIVES.pop(9006, None)
    pool = MagicMock()
    pool.fetch = AsyncMock(side_effect=RuntimeError("db exploded"))

    await _seed_or_revalidate_hth_narratives(pool, 9006, season=2025)

    assert _HTH_NARRATIVES[9006] == {}
