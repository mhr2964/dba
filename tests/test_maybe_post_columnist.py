"""
Characterization tests for _maybe_post_columnist.

Written BEFORE converting its THREE inline discord.Embed construction sites
(main rotation article, hot_take_hour debate format, Darius Cole tank-watch,
Marcus Brooks power-rankings -- four embeds total across three independent
posting paths) to the Announcer protocol. Pins: the interesting-batch gate
(blowout/streak/big-game/trade/fallback), the force-mode suppression gate,
the rotation index advancing even on a skipped/re-rolled slot, the
hot_take_hour DAVE/TONY bold-replacement, the Darius Cole ~30-game and
Marcus Brooks ~200-game independent counters, and feedback_log calls.
Not every business-logic branch (Pat Chen strategy enrichment, HTH season
narrative seeding, award-race/narrative-hook derivation) is asserted on --
those are exercised (so they don't crash) but only the discord-facing
output and gating/counter behavior are pinned, matching the level of
characterization used for the rest of this migration. Zero coverage
existed for this function before this file.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from services.sim_content_pipeline import (
    _maybe_post_columnist,
    _darius_game_counter,
    _marcus_game_counter,
    _columnist_rotation_index,
    _last_columnist_game_index,
    _HTH_NARRATIVES,
    _COLUMNIST_ROTATION,
    _COLUMNIST_FORCE_MIN_GAP,
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


class _FakeTeam:
    def __init__(self, team_id, code):
        self.id = team_id
        self.nba_team_code = code


def _game(home_id, home_code, away_id, away_code, home_score, away_score, top_pts=10):
    return {
        "home_team": _FakeTeam(home_id, home_code),
        "away_team": _FakeTeam(away_id, away_code),
        "result": {
            "home_score": home_score,
            "away_score": away_score,
            "winner_team_id": home_id if home_score > away_score else away_id,
            "home_box": [{"player_name": f"{home_code} Star", "points": top_pts}],
            "away_box": [],
        },
        "home_gameplan": {"rationale": "balanced", "strategy": {"offensive_scheme": "auto"}},
        "away_gameplan": {"rationale": "balanced", "strategy": {"offensive_scheme": "auto"}},
    }


def _blowout_batch():
    """Margin >= 20 -> triggers the interesting-batch gate via _has_blowout."""
    return [_game(1, "LAL", 2, "BOS", 120, 90)]


def _quiet_batch():
    """Margin < 20, no big scorer, no trades, no streaks -> not interesting."""
    return [_game(1, "LAL", 2, "BOS", 100, 95, top_pts=15)]


def _make_pool(streak_rows=None, trade_rows=None, asset_rows=None, race_name_rows=None,
               seed_rows=None, std_rows=None, strat_rows=None, all_standings=None):
    pool = MagicMock()

    async def _fake_fetch(sql, *args):
        if "win_streak" in sql and "loss_streak" in sql and "FROM standings_cache" in sql and "JOIN teams" not in sql:
            return streak_rows or []
        if "ORDER BY sc.wins ASC" in sql:
            return all_standings or []
        if "ORDER BY sc.wins DESC LIMIT 1" in sql:
            return std_rows or []
        if "t.conference" in sql:
            return seed_rows or []
        if "FROM trade_assets" in sql:
            return asset_rows or []
        if "FROM trades tr" in sql:
            return trade_rows or []
        if "first_name, last_name FROM players" in sql:
            return race_name_rows or []
        if "team_strategies" in sql:
            return strat_rows or []
        return []

    pool.fetch = _fake_fetch
    return pool


def _personas():
    return {
        pid: _FakePersona(pid.title(), "Desk")
        for pid in _COLUMNIST_ROTATION + ["marcus_brooks"]
    }


async def _run(league_id, batch_results, guild=None, analysis_channel=None,
               initial_rotation_index=0, initial_darius=0, initial_marcus=0,
               initial_last_columnist=0, force=False, article=None,
               dc_article=None, mb_article=None, standings=None, personas=None,
               **pool_kwargs):
    _columnist_rotation_index[league_id] = initial_rotation_index
    _darius_game_counter[league_id] = initial_darius
    _marcus_game_counter[league_id] = initial_marcus
    _last_columnist_game_index[league_id] = initial_last_columnist
    _HTH_NARRATIVES.pop(league_id, None)

    pool = _make_pool(**pool_kwargs)
    guild = guild if guild is not None else MagicMock()
    analysis_channel = analysis_channel if analysis_channel is not None else _FakeChannel()
    guild.get_channel = MagicMock(return_value=analysis_channel)

    standings = standings if standings is not None else [
        {"nba_team_code": "LAL", "conference": "East", "win_pct": 0.6, "wins": 30, "losses": 20},
        {"nba_team_code": "BOS", "conference": "West", "win_pct": 0.4, "wins": 20, "losses": 30},
    ]

    register_calls = []

    async def _fake_register(pool_, sent_message, **kwargs):
        register_calls.append(kwargs)

    # generate() is called for potentially 3 different personas in one run
    # (main rotation, darius_cole, marcus_brooks) -- dispatch by persona_id.
    async def _fake_generate(pool_, league_id_, season_, persona_id, category, context,
                              subject_team_ids=None, _capture_prompt=None):
        if persona_id == "darius_cole" and category == "tank_watch":
            return dc_article
        if persona_id == "marcus_brooks":
            return mb_article
        return article

    personas = personas if personas is not None else _personas()

    with (
        patch("services.sim_content_pipeline.league_repo.get_channel", AsyncMock(return_value=777)),
        patch.dict("services.sim_content_pipeline._PERSONAS", personas, clear=True),
        patch("services.sim_content_pipeline.game_repo.get_standings", AsyncMock(return_value=standings)),
        patch("services.sim_content_pipeline.columnist_service.generate", _fake_generate),
        patch("services.sim_content_pipeline._feedback_log.register_columnist_post", _fake_register),
        patch("services.sim_content_pipeline.awards_service.get_race_leaders", AsyncMock(return_value={})),
        patch("services.sim_content_pipeline.strategy_service.get_team_archetype_label", MagicMock(return_value=None)),
        patch("services.sim_content_pipeline._player_style_context", MagicMock(return_value=None)),
        patch("services.sim_content_pipeline._columnist_ride_along.is_enabled", MagicMock(return_value=False)),
    ):
        await _maybe_post_columnist(
            pool, league_id, season=2025, batch_results=batch_results, guild=guild,
            batch_start_index=0, batch_end_index=100, total_regular_games=1000,
            force=force, prefetched_race_leaders={},
        )
    return analysis_channel, register_calls


async def test_empty_batch_results_is_noop():
    pool = MagicMock()
    pool.fetch = AsyncMock(side_effect=AssertionError("should not query the DB"))
    guild = MagicMock()
    await _maybe_post_columnist(pool, 10001, season=2025, batch_results=[], guild=guild)
    # No assertion beyond "didn't raise" -- proves the empty-batch guard fires first.


async def test_no_analysis_channel_skips_all_posting():
    with patch("services.sim_content_pipeline.league_repo.get_channel", AsyncMock(return_value=None)):
        pool = _make_pool()
        guild = MagicMock()
        await _maybe_post_columnist(pool, 10002, 2025, _blowout_batch(), guild)
    # No assertion beyond "didn't raise" -- proves the missing-channel guard fires
    # (counters were already incremented above it, which is intentional per the
    # comment in the original code -- not independently verifiable without the
    # channel present, so not asserted here).


async def test_uninteresting_batch_skips_main_article_but_rotation_advances():
    article = {"headline": "Should Not Post", "body": "..."}
    analysis_channel, register_calls = await _run(
        league_id=10003, batch_results=_quiet_batch(), article=article,
        initial_rotation_index=0, initial_last_columnist=99,  # gap of 1, well under fallback (50)
    )
    assert analysis_channel.sent == []
    assert register_calls == []
    # Rotation index still advances past the (skipped) main-article slot.
    assert _columnist_rotation_index[10003] == 1


async def test_happy_path_regular_persona_posts_article():
    """Rotation index 0 -> jordan_rivera (first slot), a blowout makes the
    batch interesting, article posts with jordan_rivera's color/footer."""
    article = {"headline": "Lakers Blow Out Celtics", "body": "A dominant win."}
    analysis_channel, register_calls = await _run(
        league_id=10004, batch_results=_blowout_batch(), article=article,
        initial_rotation_index=0,
    )
    assert len(analysis_channel.sent) == 1
    embed = analysis_channel.sent[0]["embed"]
    assert embed.title == "Lakers Blow Out Celtics"
    assert embed.description == "A dominant win."
    assert embed.footer.text == "by Jordan_Rivera · Desk"

    assert len(register_calls) == 1
    assert register_calls[0]["persona_id"] == "jordan_rivera"
    assert register_calls[0]["category"] == "game_recap"

    # last_columnist_game_index advances to batch_end_index (100) on a real post.
    assert _last_columnist_game_index[10004] == 100
    assert _columnist_rotation_index[10004] == 1


async def test_hot_take_hour_bolds_speakers_and_uses_red():
    """Rotation index 2 lands on hot_take_hour (3rd slot in _COLUMNIST_ROTATION)."""
    idx = _COLUMNIST_ROTATION.index("hot_take_hour")
    article = {
        "headline": "Debate Night",
        "body": "DAVE: I love this team.\n\nTONY: You're wrong, Dave.",
    }
    analysis_channel, register_calls = await _run(
        league_id=10005, batch_results=_blowout_batch(), article=article,
        initial_rotation_index=idx,
    )
    assert len(analysis_channel.sent) == 1
    embed = analysis_channel.sent[0]["embed"]
    assert embed.title == "🔥 Debate Night"
    assert "**Dave:**" in embed.description
    assert "**Tony:**" in embed.description
    assert "DAVE:" not in embed.description
    assert embed.footer.text == "Dave Collier & Tony Reyes · DBA Sports Debate"

    assert register_calls[0]["persona_id"] == "hot_take_hour"


async def test_darius_cole_rotation_slot_rerolls_to_next_persona():
    """Rotation index landing on darius_cole's regular slot re-rolls forward
    (he only fires via his own independent counter, never the main rotation)."""
    idx = _COLUMNIST_ROTATION.index("darius_cole")
    article = {"headline": "Rerolled", "body": "Landed on the next persona."}
    analysis_channel, register_calls = await _run(
        league_id=10006, batch_results=_blowout_batch(), article=article,
        initial_rotation_index=idx,
    )
    assert len(register_calls) == 1
    assert register_calls[0]["persona_id"] != "darius_cole"
    # Two slots consumed: the skipped darius_cole slot + the re-rolled persona.
    assert _columnist_rotation_index[10006] == idx + 2


async def test_force_mode_suppresses_recent_article():
    """force=True with games_since_last < _COLUMNIST_FORCE_MIN_GAP suppresses
    the main article even though the batch is interesting."""
    article = {"headline": "Should Be Suppressed", "body": "..."}
    analysis_channel, register_calls = await _run(
        league_id=10007, batch_results=_blowout_batch(), article=article,
        force=True, initial_last_columnist=100 - (_COLUMNIST_FORCE_MIN_GAP - 1),
    )
    assert analysis_channel.sent == []
    assert register_calls == []


async def test_darius_cole_independent_counter_fires_at_threshold():
    dc_article = {"headline": "Tank Watch: The Bottom Five", "body": "Lottery odds update."}
    all_standings = [
        {"team_id": 9, "nba_team_code": "DET", "wins": 5, "losses": 40},
    ]
    analysis_channel, register_calls = await _run(
        league_id=10008, batch_results=_quiet_batch(), dc_article=dc_article,
        initial_darius=29, all_standings=all_standings,
    )
    assert len(analysis_channel.sent) == 1
    embed = analysis_channel.sent[0]["embed"]
    assert embed.title == "📋 Tank Watch: The Bottom Five"
    assert embed.description == "Lottery odds update."

    assert len(register_calls) == 1
    assert register_calls[0]["persona_id"] == "darius_cole"
    assert register_calls[0]["category"] == "tank_watch"
    assert register_calls[0]["subject_team_ids"] == [9]

    assert _darius_game_counter[10008] == 0


async def test_marcus_brooks_independent_counter_fires_at_threshold():
    mb_article = {"headline": "Power Rankings Check-In", "body": "Who's rising, who's falling."}
    analysis_channel, register_calls = await _run(
        league_id=10009, batch_results=_quiet_batch(), mb_article=mb_article,
        initial_marcus=199,
    )
    assert len(analysis_channel.sent) == 1
    embed = analysis_channel.sent[0]["embed"]
    assert embed.title == "Power Rankings Check-In"
    assert embed.description == "Who's rising, who's falling."

    assert len(register_calls) == 1
    assert register_calls[0]["persona_id"] == "marcus_brooks"
    assert register_calls[0]["category"] == "power_rankings"

    assert _marcus_game_counter[10009] == 0
