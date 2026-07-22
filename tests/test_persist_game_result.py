"""
Characterization tests for _persist_game_result.

Written BEFORE converting its inline discord.Embed/channel.send calls to the
Announcer protocol -- pins current output (streak embed, delegation to
_persist_injuries, all-time-record embeds) so the conversion is verifiable
byte-for-byte. _persist_game_result had zero test coverage before this file.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch


class _FakeChannel:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, content=None, embed=None):
        self.sent.append({"content": content, "embed": embed})
        return MagicMock()


class _FakeTeam:
    def __init__(self, team_id, conference="East", division="Atlantic", full_name=None):
        self.id = team_id
        self.conference = conference
        self.division = division
        self.full_name = full_name or f"Team {team_id}"


def _base_result(**overrides):
    result = {
        "home_score": 110,
        "away_score": 100,
        "winner_team_id": 1,
        "home_box": [{"points": 110}],
        "away_box": [{"points": 100}],
        "q1_home": 25, "q1_away": 20, "q2_home": 30, "q2_away": 25,
        "q3_home": 28, "q3_away": 27, "q4_home": 27, "q4_away": 28,
        "ot_home": None, "ot_away": None,
        "injuries": [],
    }
    result.update(overrides)
    return result


async def _run(result, standings_update, at_announcements=None, record_announcements=None):
    from services.sim_persistence import _persist_game_result

    game = {"id": 500, "league_id": 1, "scheduled_date": None, "game_index": 10}
    home_team = _FakeTeam(1, full_name="Home City Homers")
    away_team = _FakeTeam(2, full_name="Away City Awayers")
    news_channel = _FakeChannel()
    records_channel = _FakeChannel()
    pool = MagicMock()

    with (
        patch("services.sim_persistence.game_repo.mark_simmed", AsyncMock()),
        patch("services.sim_persistence.game_repo.insert_box_scores", AsyncMock()),
        patch("services.sim_persistence.game_repo.insert_team_game_stats", AsyncMock()),
        patch("services.sim_persistence.game_repo.update_standings", AsyncMock(return_value=standings_update)),
        patch("services.sim_persistence.game_repo.insert_injuries", AsyncMock()),
        patch("services.sim_persistence.team_repo.get_by_id", AsyncMock(return_value=home_team)),
        patch(
            "services.sim_persistence.records_service.check_and_update_records",
            AsyncMock(return_value=(record_announcements or [], at_announcements or [])),
        ),
    ):
        returned = await _persist_game_result(
            pool, game, result, home_team, away_team, season=2025,
            news_channel=news_channel, injury_channel=None, records_channel=records_channel,
            guild=None,
        )
    return returned, news_channel, records_channel


async def test_plain_game_no_streak_no_records():
    """No notable streak, no at_announcements: nothing posted to either channel."""
    returned, news_channel, records_channel = await _run(
        _base_result(), standings_update={"wins": 10, "losses": 5},
    )
    assert returned == {"wins": 10, "losses": 5}
    assert news_channel.sent == []
    assert records_channel.sent == []


async def test_notable_streak_posts_to_records_channel_over_news():
    """A notable_streak posts the gold win-streak embed to records_channel when both exist."""
    returned, news_channel, records_channel = await _run(
        _base_result(), standings_update={"notable_streak": (1, 8)},
    )
    assert news_channel.sent == []
    assert len(records_channel.sent) == 1
    embed = records_channel.sent[0]["embed"]
    assert embed.title == "\U0001F525 Win Streak"
    assert "Home City Homers" in embed.description
    assert "8" in embed.description


async def test_notable_streak_falls_back_to_news_channel():
    """When records_channel is unavailable, the streak embed goes to news_channel instead."""
    from services.sim_persistence import _persist_game_result

    game = {"id": 501, "league_id": 1, "scheduled_date": None, "game_index": 11}
    home_team = _FakeTeam(1, full_name="Home City Homers")
    away_team = _FakeTeam(2)
    news_channel = _FakeChannel()
    pool = MagicMock()

    with (
        patch("services.sim_persistence.game_repo.mark_simmed", AsyncMock()),
        patch("services.sim_persistence.game_repo.insert_box_scores", AsyncMock()),
        patch("services.sim_persistence.game_repo.insert_team_game_stats", AsyncMock()),
        patch("services.sim_persistence.game_repo.update_standings",
              AsyncMock(return_value={"notable_streak": (1, 5)})),
        patch("services.sim_persistence.game_repo.insert_injuries", AsyncMock()),
        patch("services.sim_persistence.team_repo.get_by_id", AsyncMock(return_value=home_team)),
        patch("services.sim_persistence.records_service.check_and_update_records",
              AsyncMock(return_value=([], []))),
    ):
        await _persist_game_result(
            pool, game, _base_result(), home_team, away_team, season=2025,
            news_channel=news_channel, injury_channel=None, records_channel=None, guild=None,
        )

    assert len(news_channel.sent) == 1
    assert news_channel.sent[0]["embed"].title == "\U0001F525 Win Streak"


async def test_at_announcements_post_season_record_embed():
    """Each all-time-record announcement string posts via sim_embeds.season_record_embed."""
    returned, news_channel, records_channel = await _run(
        _base_result(),
        standings_update={},
        at_announcements=["Most points in a game: 150"],
    )
    assert len(records_channel.sent) == 1
    embed = records_channel.sent[0]["embed"]
    assert "Most points in a game: 150" in embed.description


async def test_injuries_in_result_delegate_to_persist_injuries():
    """result['injuries'] non-empty: _persist_injuries is invoked with the same game/result."""
    from services.sim_persistence import _persist_game_result

    game = {"id": 502, "league_id": 1, "scheduled_date": None, "game_index": 12}
    home_team = _FakeTeam(1)
    away_team = _FakeTeam(2)
    news_channel = _FakeChannel()
    pool = MagicMock()

    calls = []

    async def _fake_persist_injuries(pool_, game_, game_id, season, result_, injury_channel, guild=None):
        calls.append((game_id, season))

    with (
        patch("services.sim_persistence.game_repo.mark_simmed", AsyncMock()),
        patch("services.sim_persistence.game_repo.insert_box_scores", AsyncMock()),
        patch("services.sim_persistence.game_repo.insert_team_game_stats", AsyncMock()),
        patch("services.sim_persistence.game_repo.update_standings", AsyncMock(return_value={})),
        patch("services.sim_persistence.team_repo.get_by_id", AsyncMock(return_value=home_team)),
        patch("services.sim_persistence.records_service.check_and_update_records",
              AsyncMock(return_value=([], []))),
        patch("services.sim_persistence._persist_injuries", _fake_persist_injuries),
    ):
        await _persist_game_result(
            pool, game, _base_result(injuries=[{"player_id": 1, "team_id": 1, "severity": "day_to_day"}]),
            home_team, away_team, season=2025,
            news_channel=news_channel, injury_channel=None, records_channel=None, guild=None,
        )

    assert calls == [(502, 2025)]
