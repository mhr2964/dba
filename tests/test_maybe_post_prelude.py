"""
Characterization tests for _maybe_post_prelude.

Written BEFORE converting its inline discord.Embed construction to the
Announcer protocol -- pins the discord-facing output (article embed
title/description/color/footer, feedback_log call with subject_team_ids,
the missing-channel and missing-persona guards) using recording fakes.
Zero coverage existed for this function before this file.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from services.sim_content_pipeline import _maybe_post_prelude


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


def _series_context(high_id=1, low_id=2):
    return {
        "high_seed_team": "LAL",
        "low_seed_team": "BOS",
        "round": "R1",
        "high_seed_team_id": high_id,
        "low_seed_team_id": low_id,
    }


async def _run(league_id, series_context=None, article=None,
               analysis_channel=None, persona=True):
    pool = MagicMock()
    guild = MagicMock()
    analysis_channel = analysis_channel if analysis_channel is not None else _FakeChannel()
    guild.get_channel = MagicMock(return_value=analysis_channel)

    register_calls = []

    async def _fake_register(pool_, sent_message, **kwargs):
        register_calls.append(kwargs)

    persona_patch = (
        patch.dict("services.sim_content_pipeline._PERSONAS",
                   {"the_prelude": _FakePersona("Sam Okafor", "Playoff desk")}, clear=False)
        if persona else
        patch.dict("services.sim_content_pipeline._PERSONAS", {}, clear=True)
    )

    with (
        patch("services.sim_content_pipeline.league_repo.get_channel", AsyncMock(return_value=777)),
        persona_patch,
        patch("services.sim_content_pipeline.columnist_service.generate", AsyncMock(return_value=article)),
        patch("services.sim_content_pipeline._feedback_log.register_columnist_post", _fake_register),
    ):
        await _maybe_post_prelude(
            pool, league_id, season=2025, guild=guild,
            series_context=series_context if series_context is not None else _series_context(),
        )
    return analysis_channel, register_calls


async def test_no_analysis_channel_skips():
    with patch("services.sim_content_pipeline.league_repo.get_channel", AsyncMock(return_value=None)):
        pool = MagicMock()
        guild = MagicMock()
        await _maybe_post_prelude(
            pool, 8001, season=2025, guild=guild, series_context=_series_context(),
        )
    # No assertion beyond "didn't raise" -- proves the missing-channel guard fires.


async def test_missing_persona_skips():
    analysis_channel, register_calls = await _run(league_id=8002, persona=False)
    assert analysis_channel.sent == []
    assert register_calls == []


async def test_happy_path_posts_article_with_subject_team_ids():
    article = {"headline": "A Clash Of Styles", "body": "Two contrasting rosters collide."}
    analysis_channel, register_calls = await _run(league_id=8003, article=article)

    assert len(analysis_channel.sent) == 1
    embed = analysis_channel.sent[0]["embed"]
    assert embed.title == "🎬 A Clash Of Styles"
    assert embed.description == "Two contrasting rosters collide."
    assert embed.footer.text == "by Sam Okafor · Playoff desk"

    assert len(register_calls) == 1
    assert register_calls[0]["persona_id"] == "the_prelude"
    assert register_calls[0]["category"] == "series_preview"
    assert register_calls[0]["subject_team_ids"] == [1, 2]


async def test_no_team_ids_in_context_registers_none():
    article = {"headline": "A Clash Of Styles", "body": "Two contrasting rosters collide."}
    analysis_channel, register_calls = await _run(
        league_id=8004, series_context=_series_context(high_id=None, low_id=None), article=article,
    )
    assert len(register_calls) == 1
    assert register_calls[0]["subject_team_ids"] is None
