from __future__ import annotations

import pytest

from bot.cogs.stats_cog import compute_power_rankings, count_h2h_wins
from bot.embeds.sim_embeds import _championship_odds
from core.errors import PhaseError
from phase.helpers import PHASE_SUGGESTIONS
from phase.states import Phase
from services.trade_evaluator import grade_trade


# ---------------------------------------------------------------------------
# 1. Phase error includes a suggestion
# ---------------------------------------------------------------------------

async def test_phase_error_includes_suggestion() -> None:
    """require_phase must append a PHASE_SUGGESTIONS hint when one exists."""
    from unittest.mock import MagicMock
    from phase.helpers import require_phase

    league = MagicMock()
    league.current_phase = Phase.SETUP.value

    with pytest.raises(PhaseError) as exc_info:
        await require_phase(league, "sim_rivalry")

    error_msg = exc_info.value.message
    assert "Available in:" in error_msg
    expected_hint = PHASE_SUGGESTIONS["SETUP"]
    assert expected_hint in error_msg, (
        f"Expected hint '{expected_hint}' not found in: {error_msg!r}"
    )


# ---------------------------------------------------------------------------
# 2. Power rankings formula — 60-20 beats 40-40
# ---------------------------------------------------------------------------

def test_power_rankings_formula() -> None:
    """A 60-20 team must rank above a 40-40 team regardless of last-10."""
    standings = [
        {
            "team_id": 1,
            "conference": "East",
            "wins": 60,
            "losses": 20,
            "last_10_wins": 7,
            "last_10_losses": 3,
        },
        {
            "team_id": 2,
            "conference": "East",
            "wins": 40,
            "losses": 40,
            "last_10_wins": 5,
            "last_10_losses": 5,
        },
    ]

    class _FakeTeam:
        def __init__(self, id_: int) -> None:
            self.id = id_
            self.nba_team_code = f"T{id_}"

    teams_by_id = {1: _FakeTeam(1), 2: _FakeTeam(2)}
    ranked = compute_power_rankings(standings, teams_by_id)

    assert ranked[0]["team_code"] == "T1", "60-20 team should be ranked #1"
    assert ranked[1]["team_code"] == "T2"


# ---------------------------------------------------------------------------
# 3. Trade grade — lopsided trade gives the winner a better grade
# ---------------------------------------------------------------------------

def test_trade_grade_winner_gets_higher_grade() -> None:
    """When one side gives up far more value, they should receive a worse grade."""
    # Team A gives assets worth 100, team B gives assets worth 10 — massive imbalance
    grade_a, grade_b = grade_trade(score_a=100.0, score_b=10.0)

    # score_a is what team_a GIVES; score_b is what team_b GIVES.
    # team_b wins because it receives score_a (100) and only gives score_b (10).
    # grade_b should be better than grade_a.
    grade_rank = {"A": 0, "B+": 1, "B": 2, "B-": 3, "C": 4, "D": 5}
    assert grade_rank[grade_b] < grade_rank[grade_a], (
        f"Expected grade_b ({grade_b}) to be better than grade_a ({grade_a}) "
        "when team_b receives far more value"
    )


def test_trade_grade_even_trade_gives_a_to_both() -> None:
    """An even trade (< 5% differential) gives both sides an A."""
    grade_a, grade_b = grade_trade(score_a=50.0, score_b=51.0)
    assert grade_a == "A"
    assert grade_b == "A"


# ---------------------------------------------------------------------------
# 4. Championship odds sum to ~100%
# ---------------------------------------------------------------------------

def test_championship_odds_sum_to_100() -> None:
    """Odds across all 30 teams must sum to approximately 100%."""
    standings: list[dict] = []
    for i in range(15):
        standings.append({
            "team_id": i + 1,
            "conference": "East",
            "wins": 40 - i,
            "losses": 20 + i,
        })
    for i in range(15):
        standings.append({
            "team_id": i + 16,
            "conference": "West",
            "wins": 38 - i,
            "losses": 22 + i,
        })

    odds = _championship_odds(standings)
    total = sum(odds.values())
    assert abs(total - 100.0) < 1.0, f"Expected odds to sum to ~100%, got {total:.2f}%"


def test_championship_odds_top_seed_has_highest_in_conf() -> None:
    """Each conference's top seed should have the highest odds in that conference."""
    standings: list[dict] = []
    for i in range(8):
        standings.append({
            "team_id": i + 1,
            "conference": "East",
            "wins": 50 - i * 3,
            "losses": 10 + i * 3,
        })
    for i in range(8):
        standings.append({
            "team_id": i + 9,
            "conference": "West",
            "wins": 50 - i * 3,
            "losses": 10 + i * 3,
        })

    odds = _championship_odds(standings)

    east_ids = list(range(1, 9))
    west_ids = list(range(9, 17))
    east_top = max(east_ids, key=lambda tid: odds.get(tid, 0))
    west_top = max(west_ids, key=lambda tid: odds.get(tid, 0))

    assert east_top == 1, f"East seed 1 (id=1) should have highest odds, got id={east_top}"
    assert west_top == 9, f"West seed 1 (id=9) should have highest odds, got id={west_top}"


# ---------------------------------------------------------------------------
# 5. H2H win counting
# ---------------------------------------------------------------------------

def test_h2h_counts_wins_correctly() -> None:
    """count_h2h_wins must correctly tally wins from a list of game dicts."""
    games = [
        {"winner_team_id": 1, "home_team_id": 1, "away_team_id": 2},
        {"winner_team_id": 2, "home_team_id": 2, "away_team_id": 1},
        {"winner_team_id": 1, "home_team_id": 2, "away_team_id": 1},
        {"winner_team_id": 1, "home_team_id": 1, "away_team_id": 2},
    ]
    wins_a, wins_b = count_h2h_wins(games, team_a_id=1, team_b_id=2)
    assert wins_a == 3
    assert wins_b == 1


def test_h2h_counts_no_games() -> None:
    """With no games played, both win counts are 0."""
    wins_a, wins_b = count_h2h_wins([], team_a_id=1, team_b_id=2)
    assert wins_a == 0
    assert wins_b == 0
