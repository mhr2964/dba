"""
Characterization tests for _run_cpu_trades_inner / _maybe_run_cpu_trades.

Written BEFORE converting the function's inline discord.Embed/channel.send/
thread-creation calls to the Announcer protocol -- pins the discord-facing
output (trade-executed/pending embed, thread content, Marcus Cole blockbuster
embed) using recording fakes. Deep business logic (context-signal computation,
columnist prompt construction) is mocked out rather than re-verified here --
this file is about the presentation boundary, not trade-evaluation correctness.
Zero test coverage existed for this function before this file.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from services.cpu_trade_round_trigger import _maybe_run_cpu_trades, _run_cpu_trades_inner


class _FakeThread:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, content):
        self.sent.append(content)


class _FakeTradeMessage:
    def __init__(self):
        self.thread: _FakeThread | None = None
        self.thread_name: str | None = None

    async def create_thread(self, name, auto_archive_duration):
        self.thread_name = name
        self.thread = _FakeThread()
        return self.thread


class _FakeChannel:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, embed=None, content=None):
        msg = _FakeTradeMessage()
        self.sent.append({"embed": embed, "content": content, "msg": msg})
        return msg


def _make_pool(new_trades, team_rows, player_rows=None, pick_rows=None,
                teammate_rows=None, league_row=None):
    """Dispatches pool.fetch/fetchrow by inspecting the SQL text."""
    player_rows = player_rows or []
    pick_rows = pick_rows or []
    teammate_rows = teammate_rows or []

    async def _fetch(sql, *args):
        if "FROM trades" in sql:
            return new_trades
        if "FROM teams WHERE id = ANY" in sql:
            return team_rows
        if "FROM players WHERE id = ANY" in sql:
            return player_rows
        if "FROM draft_picks dp" in sql:
            return pick_rows
        if "FROM players" in sql and "team_id = $2" in sql:
            return teammate_rows
        return []

    async def _fetchrow(sql, *args):
        if "FROM leagues WHERE id" in sql:
            return league_row
        return None

    pool = MagicMock()
    pool.fetch = _fetch
    pool.fetchrow = _fetchrow
    return pool


def _trade_row(trade_id, proposer_id, counterparty_id, status):
    return {
        "id": trade_id,
        "proposer_team_id": proposer_id,
        "counterparty_team_id": counterparty_id,
        "status": status,
    }


async def _run(new_trades, team_rows, player_rows=None, pick_rows=None,
               teammate_rows=None, league_row=None):
    pool = _make_pool(new_trades, team_rows, player_rows, pick_rows, teammate_rows, league_row)
    guild = MagicMock()
    transactions_channel = _FakeChannel()
    analysis_channel = _FakeChannel()

    async def _fake_get_channel(pool_, league_id, key):
        return {"analysis": 999}.get(key)

    def _fake_get_channel_obj(channel_id):
        return analysis_channel if channel_id == 999 else None

    guild.get_channel = _fake_get_channel_obj

    trade_calls = []
    columnist_calls = []

    async def _fake_register_trade(pool_, sent_message, **kwargs):
        trade_calls.append(kwargs)

    async def _fake_register_columnist(pool_, sent_message, **kwargs):
        columnist_calls.append(kwargs)

    async def _fake_generate(pool_, league_id, season, **kwargs):
        columnist_calls.append(("generate", kwargs))
        return {"headline": "Blockbuster Deal!", "body": "Marcus Cole breaks it down."}

    with (
        patch("services.cpu_trade_round_trigger.cpu_trade_service.maybe_initiate_round", AsyncMock(return_value=True)),
        patch("services.cpu_trade_round_trigger._get_transactions_channel", AsyncMock(return_value=transactions_channel)),
        patch("services.cpu_trade_round_trigger.league_repo.get_channel", _fake_get_channel),
        patch("services.cpu_trade_round_trigger._feedback_log.register_trade_announcement", _fake_register_trade),
        patch("services.cpu_trade_round_trigger._feedback_log.register_columnist_post", _fake_register_columnist),
        patch("services.cpu_trade_round_trigger.columnist_service.generate", _fake_generate),
        patch("services.cpu_trade_round_trigger._columnist_ride_along.is_enabled", MagicMock(return_value=False)),
        patch("services.cpu_trade_round_trigger.team_repo.get_all", AsyncMock(return_value=[])),
    ):
        await _run_cpu_trades_inner(
            pool, league_id=1, season=2025, current_game_index=100,
            total_regular_games=1230, deadline_game_index=1000, guild=guild,
        )
    return transactions_channel, analysis_channel, trade_calls, columnist_calls


async def test_approved_non_blockbuster_trade_posts_executed_embed_and_thread():
    """Two low-OVR players, status=approved: green 'Trade Executed' embed, thread
    created with matching content, feedback_log.register_trade_announcement called,
    no Marcus Cole article (not a blockbuster)."""
    new_trades = [_trade_row(1, 10, 20, "approved")]
    team_rows = [{"id": 10, "nba_team_code": "LAL"}, {"id": 20, "nba_team_code": "BOS"}]
    player_rows = [
        {"id": 100, "first_name": "Role", "last_name": "PlayerA", "overall": 72, "position": "SG", "age": 26},
        {"id": 200, "first_name": "Role", "last_name": "PlayerB", "overall": 70, "position": "PF", "age": 28},
    ]
    # trade_repo.get_assets is patched separately below since it's not a pool call.
    class _Asset:
        def __init__(self, asset_type, from_team_id, to_team_id, player_id=None, pick_id=None):
            self.asset_type = asset_type
            self.from_team_id = from_team_id
            self.to_team_id = to_team_id
            self.player_id = player_id
            self.pick_id = pick_id

    assets = [
        _Asset("player", 10, 20, player_id=100),
        _Asset("player", 20, 10, player_id=200),
    ]

    with patch("services.cpu_trade_round_trigger.trade_repo.get_assets", AsyncMock(return_value=assets)):
        transactions_channel, analysis_channel, trade_calls, columnist_calls = await _run(
            new_trades, team_rows, player_rows=player_rows,
        )

    assert len(transactions_channel.sent) == 1
    embed = transactions_channel.sent[0]["embed"]
    assert embed.title == "✅ Trade Executed"
    assert len(embed.fields) == 2
    assert embed.fields[0].name == "BOS receives"
    assert "Role PlayerA" in embed.fields[0].value
    assert embed.fields[1].name == "LAL receives"
    assert "Role PlayerB" in embed.fields[1].value
    assert embed.footer.text == "CPU-initiated · Trade #1"

    msg = transactions_channel.sent[0]["msg"]
    assert msg.thread_name == "Trade #1 — LAL / BOS"
    assert len(msg.thread.sent) == 1
    assert "Executed" in msg.thread.sent[0]

    assert len(trade_calls) == 1
    assert trade_calls[0]["status"] == "approved"
    assert trade_calls[0]["trade_id"] == 1

    assert analysis_channel.sent == []
    assert columnist_calls == []


async def test_pending_commissioner_trade_posts_pending_embed_no_columnist():
    """status=pending_commissioner: orange 'Trade Pending Review' embed with an
    extra 'Action required' field. Marcus Cole is skipped even for a star player
    because the gate checks status == 'approved', not blockbuster-ness alone."""
    new_trades = [_trade_row(2, 10, 20, "pending_commissioner")]
    team_rows = [{"id": 10, "nba_team_code": "LAL"}, {"id": 20, "nba_team_code": "BOS"}]
    player_rows = [
        {"id": 300, "first_name": "Star", "last_name": "Guy", "overall": 90, "position": "C", "age": 25},
    ]

    class _Asset:
        def __init__(self, asset_type, from_team_id, to_team_id, player_id=None):
            self.asset_type = asset_type
            self.from_team_id = from_team_id
            self.to_team_id = to_team_id
            self.player_id = player_id

    assets = [_Asset("player", 10, 20, player_id=300)]

    with patch("services.cpu_trade_round_trigger.trade_repo.get_assets", AsyncMock(return_value=assets)):
        transactions_channel, analysis_channel, trade_calls, columnist_calls = await _run(
            new_trades, team_rows, player_rows=player_rows,
        )

    embed = transactions_channel.sent[0]["embed"]
    assert embed.title == "⏳ Trade Pending Review"
    assert len(embed.fields) == 3
    assert embed.fields[2].name == "Action required"

    assert analysis_channel.sent == []
    assert columnist_calls == []


async def test_blockbuster_approved_trade_posts_marcus_cole_article():
    """A star (OVR>=80) player in an approved trade triggers the blockbuster path:
    columnist_service.generate is called, and its result posts as a second embed
    to #analysis with feedback_log.register_columnist_post recording it."""
    new_trades = [_trade_row(3, 10, 20, "approved")]
    team_rows = [{"id": 10, "nba_team_code": "LAL"}, {"id": 20, "nba_team_code": "BOS"}]
    player_rows = [
        {"id": 400, "first_name": "Star", "last_name": "Man", "overall": 88, "position": "PF", "age": 27},
    ]

    class _Asset:
        def __init__(self, asset_type, from_team_id, to_team_id, player_id=None):
            self.asset_type = asset_type
            self.from_team_id = from_team_id
            self.to_team_id = to_team_id
            self.player_id = player_id

    assets = [_Asset("player", 10, 20, player_id=400)]

    with patch("services.cpu_trade_round_trigger.trade_repo.get_assets", AsyncMock(return_value=assets)):
        transactions_channel, analysis_channel, trade_calls, columnist_calls = await _run(
            new_trades, team_rows, player_rows=player_rows, league_row=None,
        )

    assert len(analysis_channel.sent) == 1
    embed = analysis_channel.sent[0]["embed"]
    assert embed.title == "\U0001F4E1 Blockbuster Deal!"
    assert embed.description == "Marcus Cole breaks it down."

    generate_calls = [c for c in columnist_calls if isinstance(c, tuple) and c[0] == "generate"]
    assert len(generate_calls) == 1
    assert generate_calls[0][1]["persona_id"] == "marcus_cole"
    assert generate_calls[0][1]["category"] == "trade_report"

    register_calls = [c for c in columnist_calls if isinstance(c, dict)]
    assert len(register_calls) == 1
    assert register_calls[0]["persona_id"] == "marcus_cole"
    assert register_calls[0]["subject_trade_id"] == 3


async def test_blockbuster_trade_wires_trade_magnitude_into_context(): # D2
    """When the league has a real salary_cap, trade_magnitude ranking (league +
    both teams) is computed and attached to the context dict Marcus Cole's
    columnist_service.generate call receives, under the key 'trade_magnitude'."""
    new_trades = [_trade_row(4, 10, 20, "approved")]
    team_rows = [{"id": 10, "nba_team_code": "LAL"}, {"id": 20, "nba_team_code": "BOS"}]
    player_rows = [
        {"id": 500, "first_name": "Star", "last_name": "Guy", "overall": 90, "position": "C", "age": 26},
    ]

    class _Asset:
        def __init__(self, asset_type, from_team_id, to_team_id, player_id=None):
            self.asset_type = asset_type
            self.from_team_id = from_team_id
            self.to_team_id = to_team_id
            self.player_id = player_id

    assets = [_Asset("player", 10, 20, player_id=500)]

    league_rank = {"rank": 1, "total_trades": 12, "magnitude": 55.5, "is_biggest": True}
    lal_rank = {"rank": 1, "total_trades": 3, "magnitude": 55.5, "is_biggest": True}
    bos_rank = {"rank": 2, "total_trades": 5, "magnitude": 55.5, "is_biggest": False}

    async def _fake_league_rank(pool_, league_id_, trade_id_, salary_cap, season_):
        return league_rank

    async def _fake_team_rank(pool_, league_id_, team_id_, trade_id_, salary_cap, season_):
        return lal_rank if team_id_ == 10 else bos_rank

    with (
        patch("services.cpu_trade_round_trigger.trade_repo.get_assets", AsyncMock(return_value=assets)),
        patch(
            "services.cpu_trade_round_trigger._trade_magnitude_service.rank_trade_in_league_history",
            _fake_league_rank,
        ),
        patch(
            "services.cpu_trade_round_trigger._trade_magnitude_service.rank_trade_in_team_history",
            _fake_team_rank,
        ),
    ):
        _transactions_channel, _analysis_channel, _trade_calls, columnist_calls = await _run(
            new_trades, team_rows, player_rows=player_rows,
            league_row={"salary_cap": 140_000_000},
        )

    generate_calls = [c for c in columnist_calls if isinstance(c, tuple) and c[0] == "generate"]
    assert len(generate_calls) == 1
    trade_magnitude = generate_calls[0][1]["context"]["trade_magnitude"]
    assert trade_magnitude == {
        "league": league_rank,
        "team_ranks": {"LAL": lal_rank, "BOS": bos_rank},
    }


async def test_blockbuster_trade_without_salary_cap_leaves_trade_magnitude_none():
    """No usable league row (salary_cap missing/None) -- trade_magnitude stays
    None rather than crashing the post."""
    new_trades = [_trade_row(5, 10, 20, "approved")]
    team_rows = [{"id": 10, "nba_team_code": "LAL"}, {"id": 20, "nba_team_code": "BOS"}]
    player_rows = [
        {"id": 600, "first_name": "Star", "last_name": "Two", "overall": 85, "position": "SF", "age": 24},
    ]

    class _Asset:
        def __init__(self, asset_type, from_team_id, to_team_id, player_id=None):
            self.asset_type = asset_type
            self.from_team_id = from_team_id
            self.to_team_id = to_team_id
            self.player_id = player_id

    assets = [_Asset("player", 10, 20, player_id=600)]

    with patch("services.cpu_trade_round_trigger.trade_repo.get_assets", AsyncMock(return_value=assets)):
        _transactions_channel, _analysis_channel, _trade_calls, columnist_calls = await _run(
            new_trades, team_rows, player_rows=player_rows, league_row=None,
        )

    generate_calls = [c for c in columnist_calls if isinstance(c, tuple) and c[0] == "generate"]
    assert len(generate_calls) == 1
    assert generate_calls[0][1]["context"]["trade_magnitude"] is None


async def test_no_transactions_channel_is_noop():
    """transactions_channel resolves to None: function returns early, no crash."""
    pool = _make_pool([], [])
    guild = MagicMock()

    with (
        patch("services.cpu_trade_round_trigger.cpu_trade_service.maybe_initiate_round", AsyncMock(return_value=True)),
        patch("services.cpu_trade_round_trigger._get_transactions_channel", AsyncMock(return_value=None)),
    ):
        await _run_cpu_trades_inner(
            pool, league_id=1, season=2025, current_game_index=100,
            total_regular_games=1230, deadline_game_index=1000, guild=guild,
        )
    # No assertion beyond "didn't raise" -- proves the early-return guard fires.


async def test_no_trades_proposed_is_noop():
    """cpu_trade_service.maybe_initiate_round returns falsy: early return, no channel lookup."""
    pool = _make_pool([], [])
    guild = MagicMock()

    with (
        patch("services.cpu_trade_round_trigger.cpu_trade_service.maybe_initiate_round", AsyncMock(return_value=0)),
        patch("services.cpu_trade_round_trigger._get_transactions_channel",
              AsyncMock(side_effect=AssertionError("should not resolve a channel when nothing was proposed"))),
    ):
        await _run_cpu_trades_inner(
            pool, league_id=1, season=2025, current_game_index=100,
            total_regular_games=1230, deadline_game_index=1000, guild=guild,
        )


async def test_no_deadline_game_index_is_noop():
    """deadline_game_index falsy: returns immediately, never calls maybe_initiate_round."""
    pool = _make_pool([], [])
    guild = MagicMock()

    with patch(
        "services.cpu_trade_round_trigger.cpu_trade_service.maybe_initiate_round",
        AsyncMock(side_effect=AssertionError("should not be called without a deadline")),
    ):
        await _run_cpu_trades_inner(
            pool, league_id=1, season=2025, current_game_index=100,
            total_regular_games=1230, deadline_game_index=None, guild=guild,
        )


async def test_maybe_run_cpu_trades_swallows_exceptions():
    """The public wrapper must not propagate exceptions from _run_cpu_trades_inner."""
    pool = MagicMock()
    guild = MagicMock()

    with patch(
        "services.cpu_trade_round_trigger._run_cpu_trades_inner",
        AsyncMock(side_effect=RuntimeError("boom")),
    ):
        await _maybe_run_cpu_trades(
            pool, league_id=1, season=2025, current_game_index=100,
            total_regular_games=1230, deadline_game_index=1000, guild=guild,
        )
    # No assertion beyond "didn't raise" -- proves the try/except wrapper works.
