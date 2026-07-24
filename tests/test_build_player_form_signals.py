"""
Unit tests for services.sim_content_pipeline._build_player_form_signals
(Finding #2 — grounded player-level "declining/washed" signal).

Reuses the trade system's compute_form_map so a "cold stretch"/"hot stretch"
claim about a player has a real number behind it, the same way team-level
streak claims are gated on real win_streak/loss_streak values.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from services.sim_content_pipeline import _build_player_form_signals


async def test_no_performers_returns_empty_without_querying():
    pool = MagicMock()
    pool.fetch = AsyncMock(side_effect=AssertionError("should not query with no player_ids"))
    result = await _build_player_form_signals(pool, league_id=1, season=2025, performers=[])
    assert result == {}


async def test_cold_stretch_player_labeled_correctly():
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[{"id": 101, "overall": 85}])
    performers = [{"player_id": 101, "name": "Jordan Cole"}]

    with patch(
        "services.trade_context_builder.compute_form_map",
        AsyncMock(return_value={101: (0.85, {"games_played": 15})}),
    ):
        result = await _build_player_form_signals(pool, league_id=1, season=2025, performers=performers)

    assert "Jordan Cole" in result
    assert result["Jordan Cole"]["form_modifier"] == 0.85
    assert result["Jordan Cole"]["games_played"] == 15
    assert "cold stretch" in result["Jordan Cole"]["read"]


async def test_hot_stretch_player_labeled_correctly():
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[{"id": 202, "overall": 80}])
    performers = [{"player_id": 202, "name": "Ray Solano"}]

    with patch(
        "services.trade_context_builder.compute_form_map",
        AsyncMock(return_value={202: (1.15, {"games_played": 20})}),
    ):
        result = await _build_player_form_signals(pool, league_id=1, season=2025, performers=performers)

    assert "hot stretch" in result["Ray Solano"]["read"]


async def test_insufficient_sample_excluded_from_signals():
    """A player with < 10 games sampled must not appear at all — no grounded
    claim should ever be possible off a noisy small sample."""
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[{"id": 303, "overall": 78}])
    performers = [{"player_id": 303, "name": "Rookie Guy"}]

    with patch(
        "services.trade_context_builder.compute_form_map",
        AsyncMock(return_value={303: (0.70, {"games_played": 4})}),
    ):
        result = await _build_player_form_signals(pool, league_id=1, season=2025, performers=performers)

    assert result == {}


async def test_compute_form_map_failure_returns_empty_gracefully():
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[{"id": 404, "overall": 80}])
    performers = [{"player_id": 404, "name": "Some Player"}]

    with patch(
        "services.trade_context_builder.compute_form_map",
        AsyncMock(side_effect=RuntimeError("db exploded")),
    ):
        result = await _build_player_form_signals(pool, league_id=1, season=2025, performers=performers)

    assert result == {}


async def test_normal_form_player_labeled_neutral():
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[{"id": 505, "overall": 82}])
    performers = [{"player_id": 505, "name": "Steady Eddie"}]

    with patch(
        "services.trade_context_builder.compute_form_map",
        AsyncMock(return_value={505: (1.0, {"games_played": 12})}),
    ):
        result = await _build_player_form_signals(pool, league_id=1, season=2025, performers=performers)

    assert "normal form" in result["Steady Eddie"]["read"]
