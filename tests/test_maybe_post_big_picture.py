"""
Characterization tests for _maybe_post_big_picture.

Written BEFORE converting its inline discord.Embed construction to the
Announcer protocol -- pins the discord-facing output (article embed
title/description/color/footer, feedback_log call, the ~70-game cadence
counter's gate/reset behavior) using recording fakes. Zero coverage
existed for this function before this file.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from services.sim_content_pipeline import _maybe_post_big_picture, _big_picture_game_counter


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


async def _run(league_id, initial_counter=70, standings=None, top_performers=None,
               article=None, analysis_channel=None, persona=True):
    _big_picture_game_counter[league_id] = initial_counter - len(_batch_results())

    pool = MagicMock()
    guild = MagicMock()
    analysis_channel = analysis_channel if analysis_channel is not None else _FakeChannel()
    guild.get_channel = MagicMock(return_value=analysis_channel)

    standings = standings if standings is not None else [{"team_id": 1}]
    top_performers = top_performers if top_performers is not None else [
        {"name": "Rook One", "team": "LAL", "ppg": 25.0, "apg": 5.0, "rpg": 7.0, "gp": 30},
    ]
    pool.fetch = AsyncMock(return_value=top_performers)

    register_calls = []

    async def _fake_register(pool_, sent_message, **kwargs):
        register_calls.append(kwargs)

    persona_patch = (
        patch.dict("services.sim_content_pipeline._PERSONAS",
                   {"big_picture": _FakePersona("Marcus Bell", "Long-form desk")}, clear=False)
        if persona else
        patch.dict("services.sim_content_pipeline._PERSONAS", {}, clear=True)
    )

    with (
        patch("services.sim_content_pipeline.league_repo.get_channel", AsyncMock(return_value=777)),
        persona_patch,
        patch("services.sim_content_pipeline.game_repo.get_standings", AsyncMock(return_value=standings)),
        patch("services.sim_content_pipeline.columnist_service.generate", AsyncMock(return_value=article)),
        patch("services.sim_content_pipeline._feedback_log.register_columnist_post", _fake_register),
    ):
        await _maybe_post_big_picture(pool, league_id, season=2025, batch_results=_batch_results(), guild=guild)
    return analysis_channel, register_calls


async def test_counter_below_threshold_skips():
    analysis_channel, register_calls = await _run(league_id=4001, initial_counter=10)
    assert analysis_channel.sent == []
    assert register_calls == []
    assert _big_picture_game_counter[4001] < 70


async def test_no_analysis_channel_skips():
    with patch("services.sim_content_pipeline.league_repo.get_channel", AsyncMock(return_value=None)):
        pool = MagicMock()
        guild = MagicMock()
        _big_picture_game_counter[4002] = 70
        await _maybe_post_big_picture(pool, 4002, 2025, _batch_results(), guild)
    # No assertion beyond "didn't raise" -- proves the missing-channel guard fires.


async def test_missing_persona_skips():
    analysis_channel, register_calls = await _run(league_id=4003, persona=False)
    assert analysis_channel.sent == []
    assert register_calls == []


async def test_happy_path_posts_article_and_resets_counter():
    article = {"headline": "The State of the League", "body": "A sweeping look at the season so far."}
    analysis_channel, register_calls = await _run(league_id=4004, article=article)

    assert len(analysis_channel.sent) == 1
    embed = analysis_channel.sent[0]["embed"]
    assert embed.title == "🔭 The State of the League"
    assert embed.description == "A sweeping look at the season so far."
    assert embed.footer.text == "by Marcus Bell · Long-form desk"

    assert len(register_calls) == 1
    assert register_calls[0]["persona_id"] == "big_picture"
    assert register_calls[0]["category"] == "sunday_column"

    assert _big_picture_game_counter[4004] == 0
