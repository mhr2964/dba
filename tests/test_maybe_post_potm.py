"""
Characterization tests for _maybe_post_potm.

Written BEFORE converting its inline discord.Embed construction to the
Announcer protocol -- pins the discord-facing output (POTM article embed
title/description/color/footer, month-separator text, delegation to
_maybe_post_awards_races) using recording fakes. Zero coverage existed for
this function before this file.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from services.sim_content_pipeline import _maybe_post_potm


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


async def _run(league_id, current_game_date, awards, article=None, guild=None,
               news_channel=None, analysis_channel=None):
    from services.sim_content_pipeline import _potm_last_checked_month
    _potm_last_checked_month.pop(league_id, None)

    pool = MagicMock()
    guild = guild or MagicMock()
    news_channel = news_channel if news_channel is not None else _FakeChannel()
    analysis_channel = analysis_channel if analysis_channel is not None else _FakeChannel()

    async def _fake_get_news_channel(guild_, pool_, league_id_):
        return news_channel

    async def _fake_get_channel(pool_, league_id_, key):
        return {"analysis": 555}.get(key)

    guild.get_channel = MagicMock(side_effect=lambda cid: analysis_channel if cid == 555 else None)

    awards_calls = []

    async def _fake_awards_races(*args, **kwargs):
        awards_calls.append(kwargs)

    register_calls = []

    async def _fake_register(pool_, sent_message, **kwargs):
        register_calls.append(kwargs)

    with (
        patch("services.sim_content_pipeline.potm_service.check_and_get_potm_awards", AsyncMock(return_value=awards)),
        patch("services.sim_content_pipeline.potm_service.get_potm_context", MagicMock(return_value={})),
        patch("services.sim_content_pipeline._get_news_channel", _fake_get_news_channel),
        patch("services.sim_content_pipeline.league_repo.get_channel", _fake_get_channel),
        patch.dict("services.sim_content_pipeline._PERSONAS",
                   {"pat_chen": _FakePersona("Pat Chen", "Beat writer")}, clear=False),
        patch("services.sim_content_pipeline.columnist_service.generate", AsyncMock(return_value=article)),
        patch("services.sim_content_pipeline._feedback_log.register_columnist_post", _fake_register),
        patch("services.sim_content_pipeline._maybe_post_awards_races", _fake_awards_races),
    ):
        await _maybe_post_potm(
            pool, guild, league_id, season=2025,
            current_game_date=current_game_date, current_game_index=42,
        )
    return news_channel, analysis_channel, register_calls, awards_calls


async def test_no_current_game_date_skips():
    news_channel, analysis_channel, register_calls, awards_calls = await _run(
        league_id=901, current_game_date=None, awards=None,
    )
    assert news_channel.sent == []
    assert awards_calls == []


async def test_same_month_as_last_check_skips():
    from services.sim_content_pipeline import _potm_last_checked_month
    _potm_last_checked_month[902] = "2025-03"
    news_channel, analysis_channel, register_calls, awards_calls = await _run(
        league_id=902, current_game_date="2025-03-15", awards=None,
    )
    assert news_channel.sent == []
    assert awards_calls == []


async def test_no_awards_returned_is_noop():
    news_channel, analysis_channel, register_calls, awards_calls = await _run(
        league_id=903, current_game_date="2025-04-01", awards=None,
    )
    assert news_channel.sent == []
    assert awards_calls == []


async def test_new_month_posts_article_separator_and_triggers_awards_races():
    awards = [{"month_label": "April 2025", "player_id": 55}]
    article = {"headline": "Star Player Wins POTM", "body": "Great month for the star."}
    news_channel, analysis_channel, register_calls, awards_calls = await _run(
        league_id=904, current_game_date="2025-04-15", awards=awards, article=article,
    )

    assert len(analysis_channel.sent) == 1
    assert "April 2025" in analysis_channel.sent[0]["content"]

    assert len(news_channel.sent) == 1
    embed = news_channel.sent[0]["embed"]
    assert embed.title == "\U0001F3C6 Star Player Wins POTM"
    assert embed.description == "Great month for the star."
    assert embed.footer.text == "by Pat Chen · Beat writer"

    assert len(register_calls) == 1
    assert register_calls[0]["persona_id"] == "pat_chen"
    assert register_calls[0]["subject_player_ids"] == [55]

    assert len(awards_calls) == 1
    assert awards_calls[0]["current_game_index"] == 42
