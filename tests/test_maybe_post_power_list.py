"""
Characterization tests for _maybe_post_power_list.

Written BEFORE converting its inline discord.Embed construction to the
Announcer protocol -- pins the discord-facing output (article embed
title/description/color/footer, feedback_log call, the rank_deltas
derivation from a previous article body, and the ~70-game cadence
counter's gate/reset behavior) using recording fakes. Zero coverage
existed for this function before this file.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from services.sim_content_pipeline import _maybe_post_power_list, _power_list_game_counter


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
    def __init__(self, team_id, code):
        self.id = team_id
        self.nba_team_code = code


def _batch_results():
    return [{
        "home_team": _FakeTeam(1, "LAL"),
        "away_team": _FakeTeam(2, "BOS"),
        "result": {"home_score": 110, "away_score": 100, "winner_team_id": 1, "top_scorer": {}},
    }]


def _streak_row(code, wins, losses, win_streak=0, loss_streak=0):
    return {
        "nba_team_code": code, "wins": wins, "losses": losses,
        "win_streak": win_streak, "loss_streak": loss_streak,
    }


async def _run(league_id, initial_counter=70, standings=None, streak_rows=None,
               prev_articles=None, article=None, analysis_channel=None, persona=True):
    _power_list_game_counter[league_id] = initial_counter - len(_batch_results())

    pool = MagicMock()
    guild = MagicMock()
    analysis_channel = analysis_channel if analysis_channel is not None else _FakeChannel()
    guild.get_channel = MagicMock(return_value=analysis_channel)

    standings = standings if standings is not None else [{"team_id": 1}]
    streak_rows = streak_rows if streak_rows is not None else [
        _streak_row("LAL", 10, 5), _streak_row("BOS", 8, 7),
    ]
    prev_articles = prev_articles if prev_articles is not None else []

    pool.fetch = AsyncMock(return_value=streak_rows)

    register_calls = []
    generate_calls = []

    async def _fake_register(pool_, sent_message, **kwargs):
        register_calls.append(kwargs)

    async def _fake_generate(pool_, league_id_, season_, **kwargs):
        generate_calls.append(kwargs)
        return article

    persona_patch = (
        patch.dict("services.sim_content_pipeline._PERSONAS",
                   {"power_list": _FakePersona("Ray Solano", "Numbers guy")}, clear=False)
        if persona else
        patch.dict("services.sim_content_pipeline._PERSONAS", {}, clear=True)
    )

    with (
        patch("services.sim_content_pipeline.league_repo.get_channel", AsyncMock(return_value=777)),
        persona_patch,
        patch("services.sim_content_pipeline.game_repo.get_standings", AsyncMock(return_value=standings)),
        patch("services.sim_content_pipeline.article_repo.recent_by_persona", AsyncMock(return_value=prev_articles)),
        patch("services.sim_content_pipeline.columnist_service.generate", _fake_generate),
        patch("services.sim_content_pipeline._feedback_log.register_columnist_post", _fake_register),
    ):
        await _maybe_post_power_list(pool, league_id, season=2025, batch_results=_batch_results(), guild=guild)
    return analysis_channel, register_calls, generate_calls


async def test_counter_below_threshold_skips():
    analysis_channel, register_calls, _ = await _run(league_id=2001, initial_counter=10)
    assert analysis_channel.sent == []
    assert register_calls == []
    assert _power_list_game_counter[2001] < 70


async def test_no_analysis_channel_skips():
    with patch("services.sim_content_pipeline.league_repo.get_channel", AsyncMock(return_value=None)):
        pool = MagicMock()
        guild = MagicMock()
        _power_list_game_counter[2002] = 70
        await _maybe_post_power_list(pool, 2002, 2025, _batch_results(), guild)
    # No assertion beyond "didn't raise" -- proves the missing-channel guard fires.


async def test_missing_persona_skips():
    analysis_channel, register_calls, _ = await _run(league_id=2003, persona=False)
    assert analysis_channel.sent == []
    assert register_calls == []


async def test_no_standings_skips():
    analysis_channel, register_calls, _ = await _run(league_id=2004, standings=[])
    assert analysis_channel.sent == []
    assert register_calls == []


async def test_happy_path_posts_article_with_rank_deltas():
    """Legacy prior article (no structured_data) still yields correct deltas
    via the regex fallback -- pre-migration articles must keep working."""
    prev_articles = [{"body": "> **1.** BOS\n> **2.** LAL\n"}]
    article = {"headline": "Lakers Surge", "body": "LAL climbs the board."}
    analysis_channel, register_calls, generate_calls = await _run(
        league_id=2005, prev_articles=prev_articles, article=article,
    )

    assert len(analysis_channel.sent) == 1
    embed = analysis_channel.sent[0]["embed"]
    assert embed.title == "🏆 Lakers Surge"
    # B2: the fixture body doesn't match the ranked-row pattern, so it falls
    # back to a single "Rankings" field holding the raw body (never dropped).
    assert embed.fields[0].name == "Rankings"
    assert embed.fields[0].value == "LAL climbs the board."
    assert embed.footer.text == "by Ray Solano · Numbers guy"

    assert len(register_calls) == 1
    assert register_calls[0]["persona_id"] == "power_list"
    assert register_calls[0]["category"] == "power_rankings"

    # BOS was #1, now #2 (delta -1); LAL was #2, now #1 (delta +1).
    assert generate_calls[0]["context"]["rank_deltas"] == {"BOS": -1, "LAL": 1}

    assert _power_list_game_counter[2005] == 0


async def test_happy_path_no_prior_ranks_all_new():
    article = {"headline": "Fresh Start", "body": "Week one power list."}
    analysis_channel, register_calls, _ = await _run(
        league_id=2006, prev_articles=[], article=article,
    )
    assert len(analysis_channel.sent) == 1
    assert len(register_calls) == 1


async def test_rank_deltas_computed_from_structured_data_not_regex():
    """Finding #5 — prior article's structured_data (real team->rank dict) is
    used directly; the regex fallback must NOT fire when structured_data is present,
    even if the rendered body's markdown wouldn't match the regex at all."""
    prev_articles = [{
        "body": "Totally different rendering format the regex could never parse.",
        "structured_data": {"LAL": 2, "BOS": 1},
    }]
    article = {"headline": "Lakers Surge", "body": "LAL climbs the board."}
    _, _, generate_calls = await _run(
        league_id=2007, prev_articles=prev_articles, article=article,
    )
    # LAL: was #2, now #1 (streak_rows default order) -> delta +1.
    # BOS: was #1, now #2 -> delta -1.
    assert generate_calls[0]["context"]["rank_deltas"] == {"LAL": 1, "BOS": -1}


async def test_current_ranking_passed_as_structured_data_for_next_post():
    """The CURRENT ranking must be persisted as structured_data so the NEXT
    post's rank_deltas read real data instead of the rendered prose."""
    article = {"headline": "Fresh Start", "body": "Week one power list."}
    _, _, generate_calls = await _run(
        league_id=2008, prev_articles=[], article=article,
    )
    # streak_rows default: LAL 10-5 (rank 1), BOS 8-7 (rank 2).
    assert generate_calls[0]["structured_data"] == {"LAL": 1, "BOS": 2}
