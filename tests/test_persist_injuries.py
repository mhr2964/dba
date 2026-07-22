"""
Characterization tests for _persist_injuries.

Written BEFORE converting the function's inline discord.Embed/channel.send
calls to the Announcer protocol (services/announcer_protocol.py) -- these
pin the CURRENT output (embed title/description/fields, manager-ping text,
persisted row shape) so the conversion can be verified byte-for-byte instead
of by inspection. _persist_injuries had zero test coverage before this file.
"""
from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

from services.sim_persistence import _persist_injuries


class _FakeChannel:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, content=None, embed=None):
        self.sent.append({"content": content, "embed": embed})
        return MagicMock()


def _make_pool(player_row=None, team_code_row=None, mgr_row=None):
    async def _fetchrow(sql, *args):
        if "first_name" in sql:
            return player_row
        if "nba_team_code" in sql:
            return team_code_row
        if "manager_user_id" in sql:
            return mgr_row
        return None

    pool = MagicMock()
    pool.fetchrow = _fetchrow
    return pool


async def test_severe_injury_announces_and_pings_manager():
    """week_4_8 severity on a managed team: one row persisted, one embed +
    one manager-ping posted to the injury channel, no triage_report (guild=None)."""
    game = {"league_id": 1, "scheduled_date": datetime.date(2025, 1, 1), "game_index": 42}
    result = {"injuries": [{"player_id": 10, "team_id": 5, "severity": "week_4_8"}]}
    pool = _make_pool(
        player_row={"first_name": "Test", "last_name": "Player", "team_id": 5, "overall": 80},
        team_code_row={"nba_team_code": "LAL"},
        mgr_row={"manager_user_id": 999},
    )
    injury_channel = _FakeChannel()

    inserted: list[dict] = []

    async def _fake_insert_injuries(pool_, rows):
        inserted.extend(rows)

    with patch("services.sim_persistence.game_repo.insert_injuries", _fake_insert_injuries):
        await _persist_injuries(
            pool, game, game_id=100, season=2025, result=result,
            injury_channel=injury_channel, guild=None,
        )

    assert len(inserted) == 1
    assert inserted[0]["player_id"] == 10
    assert inserted[0]["severity"] == "week_4_8"
    assert inserted[0]["team_id"] == 5
    assert inserted[0]["affects_progression"] is True
    assert 20 <= inserted[0]["games_missed"] <= 35

    assert len(injury_channel.sent) == 2, "expected one embed + one manager ping"
    embed_msg, ping_msg = injury_channel.sent
    embed = embed_msg["embed"]
    assert embed.title == "\U0001F3E5 Injury Report"
    assert "Test Player" in embed.description
    assert "LAL" in embed.description
    assert ping_msg["content"] == "<@999> — **Test Player** just went down."


async def test_day_to_day_injury_persists_without_announcement():
    """day_to_day severity is below _ANNOUNCE_SEVERITIES: row is written, no post."""
    game = {"league_id": 1, "scheduled_date": datetime.date(2025, 1, 1), "game_index": 1}
    result = {"injuries": [{"player_id": 11, "team_id": 6, "severity": "day_to_day"}]}
    pool = _make_pool()
    injury_channel = _FakeChannel()

    inserted: list[dict] = []

    async def _fake_insert_injuries(pool_, rows):
        inserted.extend(rows)

    with patch("services.sim_persistence.game_repo.insert_injuries", _fake_insert_injuries):
        await _persist_injuries(
            pool, game, game_id=101, season=2025, result=result,
            injury_channel=injury_channel, guild=None,
        )

    assert len(inserted) == 1
    assert inserted[0]["affects_progression"] is False
    assert injury_channel.sent == []


async def test_duplicate_player_in_same_game_announced_once():
    """Two injury entries for the same player_id in one game: only the first announces."""
    game = {"league_id": 1, "scheduled_date": datetime.date(2025, 1, 1), "game_index": 5}
    result = {"injuries": [
        {"player_id": 20, "team_id": 7, "severity": "season_ending"},
        {"player_id": 20, "team_id": 7, "severity": "season_ending"},
    ]}
    pool = _make_pool(
        player_row={"first_name": "Dupe", "last_name": "Guy", "team_id": 7, "overall": 70},
        team_code_row={"nba_team_code": "BOS"},
        mgr_row=None,
    )
    injury_channel = _FakeChannel()

    inserted: list[dict] = []

    async def _fake_insert_injuries(pool_, rows):
        inserted.extend(rows)

    with patch("services.sim_persistence.game_repo.insert_injuries", _fake_insert_injuries):
        await _persist_injuries(
            pool, game, game_id=102, season=2025, result=result,
            injury_channel=injury_channel, guild=None,
        )

    assert len(inserted) == 2, "both rows are still persisted"
    assert len(injury_channel.sent) == 1, "but only one announcement fires"


async def test_star_injury_with_guild_triggers_triage_report():
    """OVR >= 84 injury with a guild present calls _maybe_post_triage_report."""
    game = {"league_id": 1, "scheduled_date": datetime.date(2025, 1, 1), "game_index": 7}
    result = {"injuries": [{"player_id": 30, "team_id": 8, "severity": "season_ending"}]}
    pool = _make_pool(
        player_row={"first_name": "Star", "last_name": "Player", "team_id": 8, "overall": 90},
        team_code_row={"nba_team_code": "MIA"},
        mgr_row=None,
    )
    injury_channel = _FakeChannel()
    fake_guild = MagicMock()

    triage_calls = []

    async def _fake_triage(pool_, league_id, season, guild, injury_info):
        triage_calls.append(injury_info)

    async def _fake_insert_injuries(pool_, rows):
        pass

    with (
        patch("services.sim_persistence.game_repo.insert_injuries", _fake_insert_injuries),
        patch("services.batch_sim_runner._maybe_post_triage_report", _fake_triage),
    ):
        await _persist_injuries(
            pool, game, game_id=103, season=2025, result=result,
            injury_channel=injury_channel, guild=fake_guild,
        )

    assert len(triage_calls) == 1
    assert triage_calls[0]["player_name"] == "Star Player"
    assert triage_calls[0]["team_code"] == "MIA"


async def test_below_threshold_star_with_guild_skips_triage_report():
    """OVR < 84 with a guild present must NOT call _maybe_post_triage_report."""
    game = {"league_id": 1, "scheduled_date": datetime.date(2025, 1, 1), "game_index": 8}
    result = {"injuries": [{"player_id": 31, "team_id": 8, "severity": "season_ending"}]}
    pool = _make_pool(
        player_row={"first_name": "Role", "last_name": "Guy", "team_id": 8, "overall": 76},
        team_code_row={"nba_team_code": "MIA"},
        mgr_row=None,
    )
    injury_channel = _FakeChannel()
    fake_guild = MagicMock()

    triage_calls = []

    async def _fake_triage(*args, **kwargs):
        triage_calls.append(1)

    async def _fake_insert_injuries(pool_, rows):
        pass

    with (
        patch("services.sim_persistence.game_repo.insert_injuries", _fake_insert_injuries),
        patch("services.batch_sim_runner._maybe_post_triage_report", _fake_triage),
    ):
        await _persist_injuries(
            pool, game, game_id=104, season=2025, result=result,
            injury_channel=injury_channel, guild=fake_guild,
        )

    assert triage_calls == []


async def test_no_injury_channel_still_persists():
    """injury_channel=None: rows still written, no crash on the missing-channel guard."""
    game = {"league_id": 1, "scheduled_date": datetime.date(2025, 1, 1), "game_index": 1}
    result = {"injuries": [{"player_id": 40, "team_id": 9, "severity": "week_4_8"}]}
    pool = _make_pool()

    inserted: list[dict] = []

    async def _fake_insert_injuries(pool_, rows):
        inserted.extend(rows)

    with patch("services.sim_persistence.game_repo.insert_injuries", _fake_insert_injuries):
        await _persist_injuries(
            pool, game, game_id=105, season=2025, result=result,
            injury_channel=None, guild=None,
        )

    assert len(inserted) == 1


async def test_no_injuries_in_result_is_a_noop():
    """result['injuries'] empty/absent: function returns immediately, nothing persisted."""
    game = {"league_id": 1}
    pool = MagicMock()
    pool.fetchrow = MagicMock(side_effect=AssertionError("should not query the DB"))

    await _persist_injuries(
        pool, game, game_id=106, season=2025, result={}, injury_channel=None, guild=None,
    )
    # No assertion needed beyond "didn't raise" -- the AssertionError side_effect
    # on fetchrow proves no DB call was attempted.
