"""
Live-DB smoke tests for the unified `/trade propose` command (bot/cogs/trade_cog.py).

Covers the propose/propose-cpu unification: `/trade propose` now accepts EITHER
`counterparty` (a Discord member) OR `team_code` (a CPU team's code), and both
paths converge on the same downstream trade_service.propose(...) call and the
same response-handling block (embed, CPU auto-response branches, human DM
branch, transactions-channel announcement).

Follows this project's established convention (see test_trade_service_position_
floor.py, test_setup_cog.py) of driving real command callbacks against a real
seeded Postgres test DB rather than mocking the DB layer. The one thing that IS
patched is cpu_trade_evaluation._cpu_evaluate -- the actual CPU accept/decline/
counter heuristics are covered exhaustively elsewhere (test_apply_final_trade_
gates.py, test_cpu_trade_acceptance.py, etc.); here we only need to prove the
COMMAND correctly reaches and handles each of the three possible CPU outcomes,
so the outcome is forced deterministically (same technique test_apply_final_
trade_gates.py test 4/5 uses to patch _apply_final_trade_gates).

`bot.cogs.trade_cog.get_pool` is NOT in conftest.py's autouse patch list (only
services/other cogs are), so each test patches it locally to point at db_pool.
"""
from __future__ import annotations

import datetime as _dt
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot.cogs.trade_cog import TradeGroup
from data.repositories import league_repo, team_repo, trade_repo
from services import cpu_trade_evaluation

pytestmark = pytest.mark.usefixtures("patch_get_pool")


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_league(pool, guild_id: int, commissioner_id: int) -> league_repo.League:
    row = await pool.fetchrow(
        """
        INSERT INTO leagues (discord_guild_id, name, start_season_year, current_season,
                              commissioner_user_id, current_phase)
        VALUES ($1, 'Propose Unification Test League', 2025, 2025, $2, 'REGULAR_SEASON_ACTIVE')
        RETURNING *
        """,
        guild_id, commissioner_id,
    )
    return league_repo._league_from_record(row)


async def _seed_team(pool, league_id: int, code: str, manager_user_id) -> team_repo.Team:
    row = await pool.fetchrow(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division, manager_user_id)
        VALUES ($1, $2, $2, $2, 'East', 'Atlantic', $3)
        RETURNING *
        """,
        league_id, code, manager_user_id,
    )
    return team_repo._team_from_record(row)


async def _insert_player(pool, league_id: int, team_id: int, position: str, overall: int = 75) -> int:
    row = await pool.fetchrow(
        """
        INSERT INTO players (
            league_id, first_name, last_name, position, team_id,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq, potential,
            peak_age_start, peak_age_end, loyalty, money_drive, win_drive
        ) VALUES (
            $1, 'Trade', 'Guy', $3, $2,
            $4, 75, 75, 70, 70,
            75, 70, 75, 70, 75, 85,
            24, 30, 50, 50, 60
        )
        RETURNING id
        """,
        league_id, team_id, position, overall,
    )
    return row["id"]


async def _seed_full_roster(pool, league_id: int, team_id: int) -> dict[str, int]:
    """Seed one player at each core position; returns {position: player_id}."""
    ids: dict[str, int] = {}
    for pos in ("PG", "SG", "SF", "PF", "C"):
        ids[pos] = await _insert_player(pool, league_id, team_id, pos)
    return ids


def _fired_response(mock_interaction):
    """Return the mock (with .call_args) for whichever response path safe_respond used."""
    candidates = [
        mock_interaction.edit_original_response,
        mock_interaction.followup.send,
        mock_interaction.response.send_message,
    ]
    fired = [m for m in candidates if m.await_count > 0]
    assert fired, "command never sent any response"
    return fired[-1]


def _fired_call(mock_interaction):
    """Return the call_args for whichever response path safe_respond used."""
    return _fired_response(mock_interaction).call_args


# ---------------------------------------------------------------------------
# Fake _cpu_evaluate implementations -- force a deterministic outcome while
# leaving trade_service.propose's own validation/creation logic real.
# ---------------------------------------------------------------------------


def _make_fake_accept():
    async def _fake(pool, trade, league, proposer_team, counterparty_team,
                     proposer_players, proposer_picks, counterparty_players, counterparty_picks):
        await trade_repo.update_status(pool, trade.id, "pending_commissioner")
        return await trade_repo.get_trade(pool, trade.id)
    return _fake


def _make_fake_decline(reason: str):
    async def _fake(pool, trade, league, proposer_team, counterparty_team,
                     proposer_players, proposer_picks, counterparty_players, counterparty_picks):
        await trade_repo.set_cpu_evaluation(pool, trade.id, 0.0, reason)
        await trade_repo.update_status(pool, trade.id, "rejected")
        return await trade_repo.get_trade(pool, trade.id)
    return _fake


def _make_fake_counter():
    async def _fake(pool, trade, league, proposer_team, counterparty_team,
                     proposer_players, proposer_picks, counterparty_players, counterparty_picks):
        # Build a real counter-offer trade record (CPU proposing back to the human/original proposer),
        # mirroring what _attempt_counter_offer does in production.
        counter = await trade_repo.create_trade(
            pool,
            league_id=league.id,
            season=league.current_season,
            proposer_id=counterparty_team.id,
            counterparty_id=proposer_team.id,
        )
        if counterparty_players:
            await trade_repo.add_asset(
                pool, counter.id,
                from_team_id=counterparty_team.id,
                to_team_id=proposer_team.id,
                asset_type="player",
                player_id=counterparty_players[0].id,
            )
        await trade_repo.update_status(pool, counter.id, "counter_offered")
        await trade_repo.update_status(pool, trade.id, "superseded")
        return await trade_repo.get_trade(pool, counter.id)
    return _fake


# ---------------------------------------------------------------------------
# CPU-team path (team_code given, counterparty omitted)
# ---------------------------------------------------------------------------


async def test_propose_cpu_path_auto_accepts(db_pool, mock_interaction):
    """/trade propose with team_code set to a CPU team's code, counterparty omitted:
    CPU auto-accept path reaches pending_commissioner and responds correctly,
    with no attempt to DM anyone (there's no Discord member on the CPU side)."""
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)

    with patch("bot.cogs.trade_cog.get_pool", AsyncMock(return_value=db_pool)):
        league = await _seed_league(db_pool, mock_interaction.guild.id, mock_interaction.user.id)
        proposer_team = await _seed_team(db_pool, league.id, "HUM", mock_interaction.user.id)
        cpu_team = await _seed_team(db_pool, league.id, "CPU", None)

        proposer_positions = await _seed_full_roster(db_pool, league.id, proposer_team.id)
        cpu_positions = await _seed_full_roster(db_pool, league.id, cpu_team.id)

        group = TradeGroup()
        with patch.object(cpu_trade_evaluation, "_cpu_evaluate", _make_fake_accept()):
            await group.propose.callback(
                group, mock_interaction,
                give_players=str(proposer_positions["C"]),
                receive_players=str(cpu_positions["C"]),
                counterparty=None,
                team_code="cpu",  # lowercase on purpose -- command must .upper() it
            )

    call = _fired_call(mock_interaction)
    content = call.kwargs.get("content") or ""
    assert "accepted" in content.lower()
    assert "pending commissioner" in content.lower()

    row = await db_pool.fetchrow("SELECT status FROM trades WHERE league_id = $1", league.id)
    assert row["status"] == "pending_commissioner"


async def test_propose_cpu_path_declines(db_pool, mock_interaction):
    """CPU-team path: a hard decline surfaces the rejection reason and does not crash."""
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)

    with patch("bot.cogs.trade_cog.get_pool", AsyncMock(return_value=db_pool)):
        league = await _seed_league(db_pool, mock_interaction.guild.id, mock_interaction.user.id)
        proposer_team = await _seed_team(db_pool, league.id, "HUM", mock_interaction.user.id)
        cpu_team = await _seed_team(db_pool, league.id, "CPU", None)

        proposer_positions = await _seed_full_roster(db_pool, league.id, proposer_team.id)
        cpu_positions = await _seed_full_roster(db_pool, league.id, cpu_team.id)

        group = TradeGroup()
        with patch.object(cpu_trade_evaluation, "_cpu_evaluate", _make_fake_decline("test rejection reason")):
            await group.propose.callback(
                group, mock_interaction,
                give_players=str(proposer_positions["C"]),
                receive_players=str(cpu_positions["C"]),
                counterparty=None,
                team_code="CPU",
            )

    call = _fired_call(mock_interaction)
    content = call.kwargs.get("content") or ""
    assert "declined" in content.lower()
    assert "test rejection reason" in content

    row = await db_pool.fetchrow("SELECT status FROM trades WHERE league_id = $1", league.id)
    assert row["status"] == "rejected"


async def test_propose_cpu_path_counter_offer(db_pool, mock_interaction):
    """CPU-team path: a counter-offer surfaces the new trade ID and instructions to accept/decline it."""
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)

    with patch("bot.cogs.trade_cog.get_pool", AsyncMock(return_value=db_pool)):
        league = await _seed_league(db_pool, mock_interaction.guild.id, mock_interaction.user.id)
        proposer_team = await _seed_team(db_pool, league.id, "HUM", mock_interaction.user.id)
        cpu_team = await _seed_team(db_pool, league.id, "CPU", None)

        proposer_positions = await _seed_full_roster(db_pool, league.id, proposer_team.id)
        cpu_positions = await _seed_full_roster(db_pool, league.id, cpu_team.id)

        group = TradeGroup()
        with patch.object(cpu_trade_evaluation, "_cpu_evaluate", _make_fake_counter()):
            await group.propose.callback(
                group, mock_interaction,
                give_players=str(proposer_positions["C"]),
                receive_players=str(cpu_positions["C"]),
                counterparty=None,
                team_code="CPU",
            )

    call = _fired_call(mock_interaction)
    content = call.kwargs.get("content") or ""
    assert "counter-offer" in content.lower()

    rows = await db_pool.fetch("SELECT status FROM trades WHERE league_id = $1 ORDER BY id", league.id)
    statuses = [r["status"] for r in rows]
    assert "superseded" in statuses
    assert "counter_offered" in statuses


async def test_propose_rejects_cpu_team_that_is_human_managed(db_pool, mock_interaction):
    """team_code pointing at a human-managed team must be rejected with guidance
    to use `counterparty` instead -- not silently routed through as if it were CPU."""
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)

    with patch("bot.cogs.trade_cog.get_pool", AsyncMock(return_value=db_pool)):
        league = await _seed_league(db_pool, mock_interaction.guild.id, mock_interaction.user.id)
        await _seed_team(db_pool, league.id, "HUM", mock_interaction.user.id)
        await _seed_team(db_pool, league.id, "OTH", 555555)

        group = TradeGroup()
        await group.propose.callback(
            group, mock_interaction,
            give_players="1",
            receive_players="2",
            counterparty=None,
            team_code="OTH",
        )

    call = _fired_call(mock_interaction)
    content = call.kwargs.get("content") or ""
    assert "managed by a human" in content.lower()
    assert "counterparty" in content.lower()


# ---------------------------------------------------------------------------
# Human-counterparty path (counterparty given, team_code omitted) -- must be
# unchanged from pre-unification behavior.
# ---------------------------------------------------------------------------


async def test_propose_human_path_sends_dm(db_pool, mock_interaction, mock_manager):
    """/trade propose with counterparty set to a human member, team_code omitted:
    existing human-trade behavior (DM with accept/decline view) is unchanged."""
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    mock_manager.send = AsyncMock()

    with patch("bot.cogs.trade_cog.get_pool", AsyncMock(return_value=db_pool)):
        league = await _seed_league(db_pool, mock_interaction.guild.id, mock_interaction.user.id)
        proposer_team = await _seed_team(db_pool, league.id, "HUM", mock_interaction.user.id)
        cp_team = await _seed_team(db_pool, league.id, "OPP", mock_manager.id)

        proposer_positions = await _seed_full_roster(db_pool, league.id, proposer_team.id)
        cp_positions = await _seed_full_roster(db_pool, league.id, cp_team.id)

        group = TradeGroup()
        await group.propose.callback(
            group, mock_interaction,
            give_players=str(proposer_positions["C"]),
            receive_players=str(cp_positions["C"]),
            counterparty=mock_manager,
            team_code=None,
        )

    mock_manager.send.assert_awaited_once()
    call = _fired_call(mock_interaction)
    content = call.kwargs.get("content") or ""
    assert "notified via dm" in content.lower()

    row = await db_pool.fetchrow("SELECT status FROM trades WHERE league_id = $1", league.id)
    assert row["status"] == "pending_counterparty"


async def test_propose_human_path_dm_forbidden_fallback(db_pool, mock_interaction, mock_manager):
    """When the DM can't be delivered (DMs closed), the command must not crash --
    it falls back to telling the proposer to have the counterparty use /trade accept."""
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)
    fake_response = MagicMock(status=403, reason="Forbidden")
    mock_manager.send = AsyncMock(side_effect=discord.Forbidden(fake_response, "Cannot send messages to this user"))

    with patch("bot.cogs.trade_cog.get_pool", AsyncMock(return_value=db_pool)):
        league = await _seed_league(db_pool, mock_interaction.guild.id, mock_interaction.user.id)
        proposer_team = await _seed_team(db_pool, league.id, "HUM", mock_interaction.user.id)
        cp_team = await _seed_team(db_pool, league.id, "OPP", mock_manager.id)

        proposer_positions = await _seed_full_roster(db_pool, league.id, proposer_team.id)
        cp_positions = await _seed_full_roster(db_pool, league.id, cp_team.id)

        group = TradeGroup()
        await group.propose.callback(
            group, mock_interaction,
            give_players=str(proposer_positions["C"]),
            receive_players=str(cp_positions["C"]),
            counterparty=mock_manager,
            team_code=None,
        )

    mock_manager.send.assert_awaited_once()
    call = _fired_call(mock_interaction)
    content = call.kwargs.get("content") or ""
    assert "couldn't dm" in content.lower()


async def test_propose_rejects_human_counterparty_without_a_team(db_pool, mock_interaction, mock_manager):
    """counterparty given but that member doesn't manage a team -- rejected with
    guidance to use team_code for CPU teams, not silently treated as a CPU trade."""
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)

    with patch("bot.cogs.trade_cog.get_pool", AsyncMock(return_value=db_pool)):
        league = await _seed_league(db_pool, mock_interaction.guild.id, mock_interaction.user.id)
        await _seed_team(db_pool, league.id, "HUM", mock_interaction.user.id)

        group = TradeGroup()
        await group.propose.callback(
            group, mock_interaction,
            give_players="1",
            receive_players="2",
            counterparty=mock_manager,
            team_code=None,
        )

    call = _fired_call(mock_interaction)
    content = call.kwargs.get("content") or ""
    assert "does not manage a team" in content.lower()
    assert "team_code" in content.lower()


# ---------------------------------------------------------------------------
# Either/or enforcement -- both given, or neither given.
# ---------------------------------------------------------------------------


async def test_propose_rejects_when_both_counterparty_and_team_code_given(mock_interaction, mock_manager):
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)

    group = TradeGroup()
    await group.propose.callback(
        group, mock_interaction,
        give_players="1",
        receive_players="2",
        counterparty=mock_manager,
        team_code="LAL",
    )

    call = _fired_call(mock_interaction)
    content = call.kwargs.get("content") or ""
    assert "not both" in content.lower()
    assert "not neither" in content.lower()


async def test_propose_rejects_when_neither_counterparty_nor_team_code_given(mock_interaction):
    mock_interaction.created_at = _dt.datetime.now(_dt.timezone.utc)

    group = TradeGroup()
    await group.propose.callback(
        group, mock_interaction,
        give_players="1",
        receive_players="2",
        counterparty=None,
        team_code=None,
    )

    call = _fired_call(mock_interaction)
    content = call.kwargs.get("content") or ""
    assert "not both" in content.lower()
    assert "not neither" in content.lower()
