"""
Characterization tests for _maybe_post_the_race.

Written BEFORE converting its inline discord.Embed construction to the
Announcer protocol -- pins the discord-facing output (article embed
title/description/color/footer, feedback_log call, the empty-race-data
and all-races-empty guards, the ~280-game cadence counter's gate/reset
behavior) using recording fakes. Zero coverage existed for this function
before this file.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from services.sim_content_pipeline import _maybe_post_the_race, _race_game_counter


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


async def _run(league_id, initial_counter=280, race_leaders=None, name_rows=None,
               article=None, analysis_channel=None, persona=True):
    _race_game_counter[league_id] = initial_counter - len(_batch_results())

    pool = MagicMock()
    guild = MagicMock()
    analysis_channel = analysis_channel if analysis_channel is not None else _FakeChannel()
    guild.get_channel = MagicMock(return_value=analysis_channel)

    race_leaders = race_leaders if race_leaders is not None else {
        "mvp": [{"player_id": 1, "score": 99.0}],
    }
    name_rows = name_rows if name_rows is not None else [
        {"id": 1, "name": "Star One", "team": "LAL", "ppg": 28.0, "rpg": 8.0, "apg": 6.0, "gp": 40},
    ]
    pool.fetch = AsyncMock(return_value=name_rows)

    register_calls = []

    async def _fake_register(pool_, sent_message, **kwargs):
        register_calls.append(kwargs)

    persona_patch = (
        patch.dict("services.sim_content_pipeline._PERSONAS",
                   {"the_race": _FakePersona("Priya Nair", "Awards desk")}, clear=False)
        if persona else
        patch.dict("services.sim_content_pipeline._PERSONAS", {}, clear=True)
    )

    with (
        patch("services.sim_content_pipeline.league_repo.get_channel", AsyncMock(return_value=777)),
        persona_patch,
        patch("services.sim_content_pipeline.awards_service.get_race_leaders", AsyncMock(return_value=race_leaders)),
        patch("services.sim_content_pipeline.columnist_service.generate", AsyncMock(return_value=article)),
        patch("services.sim_content_pipeline._feedback_log.register_columnist_post", _fake_register),
    ):
        await _maybe_post_the_race(pool, league_id, season=2025, batch_results=_batch_results(), guild=guild)
    return analysis_channel, register_calls


async def test_counter_below_threshold_skips():
    analysis_channel, register_calls = await _run(league_id=6001, initial_counter=50)
    assert analysis_channel.sent == []
    assert register_calls == []
    assert _race_game_counter[6001] < 280


async def test_no_analysis_channel_skips():
    with patch("services.sim_content_pipeline.league_repo.get_channel", AsyncMock(return_value=None)):
        pool = MagicMock()
        guild = MagicMock()
        _race_game_counter[6002] = 280
        await _maybe_post_the_race(pool, 6002, 2025, _batch_results(), guild)
    # No assertion beyond "didn't raise" -- proves the missing-channel guard fires.


async def test_missing_persona_skips():
    analysis_channel, register_calls = await _run(league_id=6003, persona=False)
    assert analysis_channel.sent == []
    assert register_calls == []


async def test_no_race_data_skips():
    analysis_channel, register_calls = await _run(league_id=6004, race_leaders={})
    assert analysis_channel.sent == []
    assert register_calls == []


async def test_all_races_empty_skips():
    analysis_channel, register_calls = await _run(league_id=6005, race_leaders={"mvp": [], "dpoy": []})
    assert analysis_channel.sent == []
    assert register_calls == []


async def test_happy_path_posts_article_and_resets_counter():
    article = {"headline": "The MVP Race Tightens", "body": "Two stars separate from the pack."}
    analysis_channel, register_calls = await _run(league_id=6006, article=article)

    assert len(analysis_channel.sent) == 1
    embed = analysis_channel.sent[0]["embed"]
    assert embed.title == "🏅 The MVP Race Tightens"
    # B2: the fixture body doesn't match the medal-line pattern, so it falls
    # back to a single "Candidates" field holding the raw body.
    assert embed.fields[0].name == "Candidates"
    assert embed.fields[0].value == "Two stars separate from the pack."
    assert embed.footer.text == "by Priya Nair · Awards desk"

    assert len(register_calls) == 1
    assert register_calls[0]["persona_id"] == "the_race"
    assert register_calls[0]["category"] == "award_race"

    assert _race_game_counter[6006] == 0
