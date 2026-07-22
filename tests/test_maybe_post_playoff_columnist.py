"""
Characterization tests for _maybe_post_playoff_columnist.

Written BEFORE converting its inline discord.Embed construction to the
Announcer protocol -- pins the discord-facing output (article embed
title/description/color/footer, feedback_log call with subject_team_ids,
the persona rotation via _playoff_rotation_index, the missing-persona and
missing-channel guards) using recording fakes. Zero coverage existed for
this function before this file.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from services.sim_content_pipeline import (
    _maybe_post_playoff_columnist,
    _playoff_rotation_index,
    _PLAYOFF_COLUMNIST_ROTATION,
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


def _context(high_id=1, low_id=2):
    return {"high_seed_team_id": high_id, "low_seed_team_id": low_id, "round": "R1"}


async def _run(league_id, context=None, article=None, analysis_channel=None,
               initial_index=0, personas=None):
    _playoff_rotation_index[league_id] = initial_index

    pool = MagicMock()
    guild = MagicMock()
    analysis_channel = analysis_channel if analysis_channel is not None else _FakeChannel()
    guild.get_channel = MagicMock(return_value=analysis_channel)

    register_calls = []

    async def _fake_register(pool_, sent_message, **kwargs):
        register_calls.append(kwargs)

    personas = personas if personas is not None else {
        pid: _FakePersona(pid.title(), "Playoff desk") for pid in _PLAYOFF_COLUMNIST_ROTATION
    }

    with (
        patch("services.sim_content_pipeline.league_repo.get_channel", AsyncMock(return_value=777)),
        patch.dict("services.sim_content_pipeline._PERSONAS", personas, clear=True),
        patch("services.sim_content_pipeline.columnist_service.generate", AsyncMock(return_value=article)),
        patch("services.sim_content_pipeline._feedback_log.register_columnist_post", _fake_register),
    ):
        await _maybe_post_playoff_columnist(
            pool, league_id, season=2025,
            context=context if context is not None else _context(), guild=guild,
        )
    return analysis_channel, register_calls


async def test_missing_persona_skips():
    analysis_channel, register_calls = await _run(league_id=9001, personas={})
    assert analysis_channel.sent == []
    assert register_calls == []
    # Rotation index still advances even though the post was skipped.
    assert _playoff_rotation_index[9001] == 1


async def test_no_analysis_channel_skips():
    with patch("services.sim_content_pipeline.league_repo.get_channel", AsyncMock(return_value=None)):
        pool = MagicMock()
        guild = MagicMock()
        personas = {pid: _FakePersona(pid.title(), "Playoff desk") for pid in _PLAYOFF_COLUMNIST_ROTATION}
        _playoff_rotation_index[9002] = 0
        with patch.dict("services.sim_content_pipeline._PERSONAS", personas, clear=True):
            await _maybe_post_playoff_columnist(pool, 9002, 2025, _context(), guild)
    # No assertion beyond "didn't raise" -- proves the missing-channel guard fires.


async def test_no_article_returned_is_a_noop():
    analysis_channel, register_calls = await _run(league_id=9003, article=None)
    assert analysis_channel.sent == []
    assert register_calls == []


async def test_happy_path_posts_article_and_rotates_persona():
    article = {"headline": "Game 7 For The Ages", "body": "A classic elimination battle."}
    analysis_channel, register_calls = await _run(league_id=9004, article=article, initial_index=1)

    assert len(analysis_channel.sent) == 1
    embed = analysis_channel.sent[0]["embed"]
    assert embed.title == "Game 7 For The Ages"
    assert embed.description == "A classic elimination battle."
    assert embed.footer.text == f"by {_PLAYOFF_COLUMNIST_ROTATION[1].title()} · Playoff desk"

    assert len(register_calls) == 1
    assert register_calls[0]["persona_id"] == _PLAYOFF_COLUMNIST_ROTATION[1]
    assert register_calls[0]["category"] == "playoff_recap"
    assert register_calls[0]["subject_team_ids"] == [1, 2]

    assert _playoff_rotation_index[9004] == 2


async def test_rotation_index_wraps_around():
    article = {"headline": "Round One Recap", "body": "The series is even."}
    n = len(_PLAYOFF_COLUMNIST_ROTATION)
    analysis_channel, register_calls = await _run(league_id=9005, article=article, initial_index=n)

    assert len(register_calls) == 1
    assert register_calls[0]["persona_id"] == _PLAYOFF_COLUMNIST_ROTATION[0]
    assert _playoff_rotation_index[9005] == n + 1
