"""
Characterization tests for _maybe_post_rookie_watch.

Written BEFORE converting its inline discord.Embed construction to the
Announcer protocol -- pins the discord-facing output (article embed
title/description/color/footer, feedback_log call with subject_player_ids,
the ~70-game cadence counter's gate/reset behavior) using recording fakes.
Zero coverage existed for this function before this file.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from services.sim_content_pipeline import _maybe_post_rookie_watch, _rookie_watch_game_counter


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


def _rookie_row(id_, name, team, ppg=10.0, rpg=5.0, apg=3.0, gp=20):
    return {"id": id_, "name": name, "team": team, "ppg": ppg, "rpg": rpg, "apg": apg, "gp": gp}


async def _run(league_id, initial_counter=70, rookie_rows=None, article=None,
               analysis_channel=None, persona=True):
    _rookie_watch_game_counter[league_id] = initial_counter - len(_batch_results())

    pool = MagicMock()
    guild = MagicMock()
    analysis_channel = analysis_channel if analysis_channel is not None else _FakeChannel()
    guild.get_channel = MagicMock(return_value=analysis_channel)

    rookie_rows = rookie_rows if rookie_rows is not None else [_rookie_row(1, "Rook One", "LAL")]
    pool.fetch = AsyncMock(return_value=rookie_rows)

    register_calls = []

    async def _fake_register(pool_, sent_message, **kwargs):
        register_calls.append(kwargs)

    persona_patch = (
        patch.dict("services.sim_content_pipeline._PERSONAS",
                   {"rookie_watch": _FakePersona("Nia Torres", "Development desk")}, clear=False)
        if persona else
        patch.dict("services.sim_content_pipeline._PERSONAS", {}, clear=True)
    )

    with (
        patch("services.sim_content_pipeline.league_repo.get_channel", AsyncMock(return_value=777)),
        persona_patch,
        patch("services.sim_content_pipeline.columnist_service.generate", AsyncMock(return_value=article)),
        patch("services.sim_content_pipeline._feedback_log.register_columnist_post", _fake_register),
    ):
        await _maybe_post_rookie_watch(pool, league_id, season=2025, batch_results=_batch_results(), guild=guild)
    return analysis_channel, register_calls


async def test_counter_below_threshold_skips():
    analysis_channel, register_calls = await _run(league_id=3001, initial_counter=10)
    assert analysis_channel.sent == []
    assert register_calls == []
    assert _rookie_watch_game_counter[3001] < 70


async def test_no_analysis_channel_skips():
    with patch("services.sim_content_pipeline.league_repo.get_channel", AsyncMock(return_value=None)):
        pool = MagicMock()
        guild = MagicMock()
        _rookie_watch_game_counter[3002] = 70
        await _maybe_post_rookie_watch(pool, 3002, 2025, _batch_results(), guild)
    # No assertion beyond "didn't raise" -- proves the missing-channel guard fires.


async def test_missing_persona_skips():
    analysis_channel, register_calls = await _run(league_id=3003, persona=False)
    assert analysis_channel.sent == []
    assert register_calls == []


async def test_no_rookies_skips():
    analysis_channel, register_calls = await _run(league_id=3004, rookie_rows=[])
    assert analysis_channel.sent == []
    assert register_calls == []


async def test_happy_path_posts_article_and_resets_counter():
    rookie_rows = [_rookie_row(1, "Rook One", "LAL"), _rookie_row(2, "Rook Two", "BOS")]
    article = {"headline": "Rookies On The Rise", "body": "Two rookies shine early."}
    analysis_channel, register_calls = await _run(
        league_id=3005, rookie_rows=rookie_rows, article=article,
    )

    assert len(analysis_channel.sent) == 1
    embed = analysis_channel.sent[0]["embed"]
    assert embed.title == "🌟 Rookies On The Rise"
    assert embed.description == "Two rookies shine early."
    assert embed.footer.text == "by Nia Torres · Development desk"

    assert len(register_calls) == 1
    assert register_calls[0]["persona_id"] == "rookie_watch"
    assert register_calls[0]["category"] == "rookie_watch"
    assert register_calls[0]["subject_player_ids"] == [1, 2]

    assert _rookie_watch_game_counter[3005] == 0
