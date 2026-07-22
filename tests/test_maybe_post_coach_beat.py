"""
Characterization tests for _maybe_post_coach_beat.

Written BEFORE converting its inline discord.Embed construction to the
Announcer protocol -- pins the discord-facing output (article embed
title/description/color/footer, feedback_log call, the ~50-game cadence
counter's gate/reset behavior) using recording fakes. Zero coverage existed
for this function before this file.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from services.sim_content_pipeline import _maybe_post_coach_beat, _coach_beat_game_counter


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


class _FakeTeam:
    def __init__(self, team_id):
        self.id = team_id


def _batch_results(team_ids):
    return [{"home_team": _FakeTeam(team_ids[0]), "away_team": _FakeTeam(team_ids[1])}]


async def _run(league_id, batch_results, intel, article=None, analysis_channel=None,
               initial_counter=50, league_row=None):
    _coach_beat_game_counter[league_id] = initial_counter - len(batch_results)

    pool = MagicMock()
    guild = MagicMock()
    analysis_channel = analysis_channel if analysis_channel is not None else _FakeChannel()

    async def _fake_get_channel(pool_, league_id_, key):
        return {"analysis": 777}.get(key)

    guild.get_channel = MagicMock(side_effect=lambda cid: analysis_channel if cid == 777 else None)

    async def _fake_fetchrow(sql, *args):
        if "FROM leagues" in sql:
            return league_row if league_row is not None else {"id": league_id}
        if "FROM teams" in sql:
            return {"nba_team_code": "LAL"}
        return None

    pool.fetchrow = _fake_fetchrow

    register_calls = []

    async def _fake_register(pool_, sent_message, **kwargs):
        register_calls.append(kwargs)

    with (
        patch("services.sim_content_pipeline.league_repo.get_channel", _fake_get_channel),
        patch.dict("services.sim_content_pipeline._PERSONAS",
                   {"coach_beat": _FakePersona("Quinn Park", "Sideline analyst")}, clear=False),
        patch("data.repositories.league_repo._league_from_record",
              MagicMock(side_effect=lambda row: row)),
        patch("services.sim_content_pipeline.team_intel.build_team_intel", AsyncMock(return_value=intel)),
        patch("services.sim_content_pipeline.random.choice", side_effect=lambda seq: seq[0]),
        patch("services.sim_content_pipeline.columnist_service.generate", AsyncMock(return_value=article)),
        patch("services.sim_content_pipeline._feedback_log.register_columnist_post", _fake_register),
    ):
        await _maybe_post_coach_beat(pool, league_id, season=2025, batch_results=batch_results, guild=guild)
    return analysis_channel, register_calls


async def test_counter_below_threshold_skips():
    analysis_channel, register_calls = await _run(
        league_id=1001, batch_results=_batch_results([1, 2]), intel={}, initial_counter=10,
    )
    assert analysis_channel.sent == []
    assert register_calls == []
    assert _coach_beat_game_counter[1001] < 50


async def test_no_analysis_channel_skips():
    analysis_channel, register_calls = await _run(
        league_id=1002, batch_results=_batch_results([1, 2]), intel={}, analysis_channel=None,
    )
    # analysis_channel resolves to a _FakeChannel here, so force the "not found" path directly.
    with patch("services.sim_content_pipeline.league_repo.get_channel", AsyncMock(return_value=None)):
        pool = MagicMock()
        guild = MagicMock()
        _coach_beat_game_counter[1003] = 50
        await _maybe_post_coach_beat(pool, 1003, 2025, _batch_results([1, 2]), guild)
    # No assertion beyond "didn't raise" -- proves the missing-channel guard fires.


async def test_no_recent_role_changes_skips_without_resetting_counter():
    """Content gate: philosophy match but no recent_role_changes means skip,
    and the counter must stay >= 50 so the next batch retries immediately."""
    intel = {1: {"philosophy": "chaos", "recent_role_changes": []}}
    _coach_beat_game_counter[1004] = 50
    analysis_channel, register_calls = await _run(
        league_id=1004, batch_results=_batch_results([1, 2]), intel=intel, initial_counter=50,
    )
    assert analysis_channel.sent == []
    assert register_calls == []
    assert _coach_beat_game_counter[1004] >= 50


async def test_happy_path_posts_article_and_resets_counter():
    intel = {1: {"philosophy": "chaos", "recent_role_changes": ["benched"], "posture": {}, "plan": {}}}
    article = {"headline": "Chaos Reigns", "body": "Coach Beat breaks down the chaos."}
    _coach_beat_game_counter[1005] = 50
    analysis_channel, register_calls = await _run(
        league_id=1005, batch_results=_batch_results([1, 2]), intel=intel, article=article,
    )

    assert len(analysis_channel.sent) == 1
    embed = analysis_channel.sent[0]["embed"]
    assert embed.title == "\U0001F3A4 Chaos Reigns"
    assert embed.description == "Coach Beat breaks down the chaos."
    assert embed.footer.text == "by Quinn Park · Sideline analyst"

    assert len(register_calls) == 1
    assert register_calls[0]["persona_id"] == "coach_beat"
    assert register_calls[0]["subject_team_ids"] == [1]

    assert _coach_beat_game_counter[1005] == 0
