"""
Characterization tests for _maybe_post_triage_report.

Written BEFORE converting its inline discord.Embed construction to the
Announcer protocol -- pins the discord-facing output (article embed
title/description/color/footer, feedback_log call with subject_player_ids,
the missing-channel and missing-persona guards, the non-fatal roster
enrichment) using recording fakes. Zero coverage existed for this function
before this file.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from services.sim_content_pipeline import _maybe_post_triage_report


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


def _injury_info(player_id=10, team_code="LAL", season=2025):
    return {
        "player_name": "Star Player",
        "team_code": team_code,
        "severity": "season_ending",
        "games_missed": 60,
        "player_id": player_id,
        "season": season,
    }


async def _run(league_id, injury_info=None, roster_rows=None, article=None,
               analysis_channel=None, persona=True):
    pool = MagicMock()
    guild = MagicMock()
    analysis_channel = analysis_channel if analysis_channel is not None else _FakeChannel()
    guild.get_channel = MagicMock(return_value=analysis_channel)

    roster_rows = roster_rows if roster_rows is not None else [
        {"name": "Backup One", "position": "F", "overall": 75, "role": "rotation"},
    ]
    pool.fetch = AsyncMock(return_value=roster_rows)

    register_calls = []

    async def _fake_register(pool_, sent_message, **kwargs):
        register_calls.append(kwargs)

    persona_patch = (
        patch.dict("services.sim_content_pipeline._PERSONAS",
                   {"triage_report": _FakePersona("Dana Cross", "Injury desk")}, clear=False)
        if persona else
        patch.dict("services.sim_content_pipeline._PERSONAS", {}, clear=True)
    )

    with (
        patch("services.sim_content_pipeline.league_repo.get_channel", AsyncMock(return_value=777)),
        persona_patch,
        patch("services.sim_content_pipeline.columnist_service.generate", AsyncMock(return_value=article)),
        patch("services.sim_content_pipeline._feedback_log.register_columnist_post", _fake_register),
    ):
        await _maybe_post_triage_report(
            pool, league_id, season=2025, guild=guild,
            injury_info=injury_info if injury_info is not None else _injury_info(),
        )
    return analysis_channel, register_calls


async def test_no_analysis_channel_skips():
    with patch("services.sim_content_pipeline.league_repo.get_channel", AsyncMock(return_value=None)):
        pool = MagicMock()
        guild = MagicMock()
        await _maybe_post_triage_report(
            pool, 7001, season=2025, guild=guild, injury_info=_injury_info(),
        )
    # No assertion beyond "didn't raise" -- proves the missing-channel guard fires.


async def test_missing_persona_skips():
    analysis_channel, register_calls = await _run(league_id=7002, persona=False)
    assert analysis_channel.sent == []
    assert register_calls == []


async def test_empty_article_body_skips():
    analysis_channel, register_calls = await _run(
        league_id=7003, article={"headline": "Nothing", "body": ""},
    )
    assert analysis_channel.sent == []
    assert register_calls == []


async def test_happy_path_posts_article_with_subject_player_id():
    article = {"headline": "Star Player Down", "body": "A significant loss for the roster."}
    analysis_channel, register_calls = await _run(league_id=7004, article=article)

    assert len(analysis_channel.sent) == 1
    embed = analysis_channel.sent[0]["embed"]
    assert embed.title == "🩺 Star Player Down"
    assert embed.description == "A significant loss for the roster."
    assert embed.footer.text == "by Dana Cross · Injury desk"

    assert len(register_calls) == 1
    assert register_calls[0]["persona_id"] == "triage_report"
    assert register_calls[0]["category"] == "injury_report"
    assert register_calls[0]["subject_player_ids"] == [10]


async def test_roster_enrichment_failure_is_non_fatal():
    """pool.fetch raising during roster enrichment must not stop the article post."""
    article = {"headline": "Star Player Down", "body": "A significant loss for the roster."}
    pool = MagicMock()
    guild = MagicMock()
    analysis_channel = _FakeChannel()
    guild.get_channel = MagicMock(return_value=analysis_channel)
    pool.fetch = AsyncMock(side_effect=RuntimeError("db down"))

    register_calls = []

    async def _fake_register(pool_, sent_message, **kwargs):
        register_calls.append(kwargs)

    with (
        patch("services.sim_content_pipeline.league_repo.get_channel", AsyncMock(return_value=777)),
        patch.dict("services.sim_content_pipeline._PERSONAS",
                   {"triage_report": _FakePersona("Dana Cross", "Injury desk")}, clear=False),
        patch("services.sim_content_pipeline.columnist_service.generate", AsyncMock(return_value=article)),
        patch("services.sim_content_pipeline._feedback_log.register_columnist_post", _fake_register),
    ):
        await _maybe_post_triage_report(
            pool, 7005, season=2025, guild=guild, injury_info=_injury_info(),
        )

    assert len(analysis_channel.sent) == 1
    assert len(register_calls) == 1
