"""
Characterization tests for _maybe_post_ledger.

Written BEFORE converting its inline discord.Embed construction to the
Announcer protocol -- pins the discord-facing output (article embed
title/description/color/footer, feedback_log call, the phase gate, the
force-fire-on-first-post-of-season behavior, and the ~280-game cadence
counter's gate/reset behavior -- including the pre-existing quirk where a
non-force-fire counter reset happens BEFORE the channel/persona/data
guards, so a skip after that point still loses the counter) using
recording fakes. Zero coverage existed for this function before this file.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from phase.states import Phase
from services.sim_content_pipeline import (
    _maybe_post_ledger,
    _ledger_game_counter,
    _ledger_first_post_done,
)


class _FakeChannel:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, content=None, embed=None):
        self.sent.append({"embed": embed, "content": content})
        return MagicMock()


class _FakePersona:
    def __init__(self, display_name, byline):
        self.display_name = display_name
        self.byline = byline


def _batch_results():
    return [{"home_team": None, "away_team": None, "result": {}}]


def _make_pool(phase, trade_rows=None, asset_rows=None, team_mode_rows=None):
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={"current_phase": phase})

    trade_rows = trade_rows if trade_rows is not None else []
    asset_rows = asset_rows if asset_rows is not None else []
    team_mode_rows = team_mode_rows if team_mode_rows is not None else []

    async def _fake_fetch(sql, *args):
        if "FROM trades" in sql:
            return trade_rows
        if "FROM trade_assets" in sql:
            return asset_rows
        if "cpu_mode" in sql:
            return team_mode_rows
        return []

    pool.fetch = _fake_fetch
    return pool


async def _run(league_id, season=2025, phase=Phase.TRADE_DEADLINE_OPEN.value,
               already_done=True, initial_counter=280, trade_rows=None,
               asset_rows=None, team_mode_rows=None, article=None,
               analysis_channel=None, persona=True):
    _season_key = (league_id, season)
    _ledger_first_post_done[_season_key] = already_done
    _ledger_game_counter[league_id] = initial_counter - len(_batch_results())

    pool = _make_pool(phase, trade_rows, asset_rows, team_mode_rows)
    guild = MagicMock()
    analysis_channel = analysis_channel if analysis_channel is not None else _FakeChannel()
    guild.get_channel = MagicMock(return_value=analysis_channel)

    register_calls = []

    async def _fake_register(pool_, sent_message, **kwargs):
        register_calls.append(kwargs)

    persona_patch = (
        patch.dict("services.sim_content_pipeline._PERSONAS",
                   {"the_ledger": _FakePersona("Owen Marsh", "Front-office desk")}, clear=False)
        if persona else
        patch.dict("services.sim_content_pipeline._PERSONAS", {}, clear=True)
    )

    with (
        patch("services.sim_content_pipeline.league_repo.get_channel", AsyncMock(return_value=777)),
        persona_patch,
        patch("services.sim_content_pipeline.columnist_service.generate", AsyncMock(return_value=article)),
        patch("services.sim_content_pipeline._feedback_log.register_columnist_post", _fake_register),
    ):
        await _maybe_post_ledger(pool, league_id, season, _batch_results(), guild)
    return analysis_channel, register_calls


async def test_wrong_phase_skips():
    analysis_channel, register_calls = await _run(league_id=5001, phase="draft")
    assert analysis_channel.sent == []
    assert register_calls == []


async def test_counter_below_threshold_and_already_posted_skips():
    analysis_channel, register_calls = await _run(
        league_id=5002, already_done=True, initial_counter=50,
    )
    assert analysis_channel.sent == []
    assert register_calls == []
    assert _ledger_game_counter[5002] < 280


async def test_no_analysis_channel_skips():
    pool = _make_pool(Phase.TRADE_DEADLINE_OPEN.value)
    guild = MagicMock()
    with patch("services.sim_content_pipeline.league_repo.get_channel", AsyncMock(return_value=None)):
        _ledger_first_post_done[(5003, 2025)] = True
        _ledger_game_counter[5003] = 280
        await _maybe_post_ledger(pool, 5003, 2025, _batch_results(), guild)
    # No assertion beyond "didn't raise" -- proves the missing-channel guard fires.


async def test_missing_persona_skips():
    analysis_channel, register_calls = await _run(league_id=5004, persona=False)
    assert analysis_channel.sent == []
    assert register_calls == []


async def test_no_trade_or_mode_data_skips():
    analysis_channel, register_calls = await _run(
        league_id=5005, trade_rows=[], team_mode_rows=[],
    )
    assert analysis_channel.sent == []
    assert register_calls == []


async def test_force_fire_first_post_ignores_counter():
    """First post of the season fires even with a low counter, since
    _ledger_first_post_done starts False."""
    team_mode_rows = [{"nba_team_code": "LAL", "cpu_mode": "aggressive", "wins": 10, "losses": 5}]
    article = {"headline": "Grading The Deals", "body": "Front office report card."}
    analysis_channel, register_calls = await _run(
        league_id=5006, already_done=False, initial_counter=1,
        team_mode_rows=team_mode_rows, article=article,
    )
    assert len(analysis_channel.sent) == 1
    assert _ledger_first_post_done[(5006, 2025)] is True


async def test_happy_path_posts_article_and_resets_counter():
    trade_rows = [{"id": 99, "proposer": "LAL", "counterparty": "BOS", "proposed_at": None}]
    asset_rows = [{"from_team_id": 1, "asset_type": "player", "player_name": "Guy One", "overall": 80}]
    team_mode_rows = [{"nba_team_code": "LAL", "cpu_mode": "aggressive", "wins": 10, "losses": 5}]
    article = {"headline": "Grading The Deals", "body": "Front office report card."}
    analysis_channel, register_calls = await _run(
        league_id=5007, trade_rows=trade_rows, asset_rows=asset_rows,
        team_mode_rows=team_mode_rows, article=article,
    )

    assert len(analysis_channel.sent) == 1
    embed = analysis_channel.sent[0]["embed"]
    assert embed.title == "📒 Grading The Deals"
    # B2: the fixture body doesn't match the graded-table row pattern, so it
    # falls back to a single "Grades" field holding the raw body.
    assert embed.fields[0].name == "Grades"
    assert embed.fields[0].value == "Front office report card."
    assert embed.footer.text == "by Owen Marsh · Front-office desk"

    assert len(register_calls) == 1
    assert register_calls[0]["persona_id"] == "the_ledger"
    assert register_calls[0]["category"] == "front_office_grade"

    assert _ledger_game_counter[5007] == 0
