from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.embeds import trade_embeds
from bot.embeds import intel_embeds
from bot.ui.trade_views import TradeAcceptView
from core.errors import DBAError, post_to_channel_or_respond, safe_defer, safe_respond
from core.logging import get_logger
from data.db import get_pool
from data.repositories import league_repo, player_repo, team_repo, trade_repo
from phase.helpers import require_commissioner, require_phase
import os

from services import feedback_log, league_service, trade_service
from services.trade_ai_reasoning import get_ai_reasoning

log = get_logger(__name__)


def _parse_ids(raw: str) -> list[int]:
    """Parse a space-separated string of integers. Raises DBAError on bad input."""
    parts = raw.strip().split()
    try:
        return [int(p) for p in parts if p]
    except ValueError:
        raise DBAError(f"Could not parse IDs from '{raw}'. Use space-separated integers (e.g. '12 34 56').")


async def _build_asset_lookup(
    pool,
    assets: list[trade_repo.TradeAsset],
) -> tuple[dict, dict]:
    """Return (players_by_id, picks_by_id) dicts for embed rendering."""
    players_by_id: dict = {}
    picks_by_id: dict = {}
    for asset in assets:
        if asset.asset_type == "player" and asset.player_id and asset.player_id not in players_by_id:
            player = await player_repo.get_by_id(pool, asset.player_id)
            if player:
                players_by_id[player.id] = {
                    "full_name": player.full_name,
                    "overall": player.overall,
                }
        elif asset.asset_type == "pick" and asset.pick_id and asset.pick_id not in picks_by_id:
            row = await pool.fetchrow("SELECT * FROM draft_picks WHERE id = $1", asset.pick_id)
            if row:
                picks_by_id[row["id"]] = dict(row)
    return players_by_id, picks_by_id


class TradeGroup(app_commands.Group, name="trade", description="Trade management"):

    @app_commands.command(name="propose", description="Propose a trade with another team")
    @app_commands.describe(
        give_players="Space-separated player IDs you are giving up",
        receive_players="Space-separated player IDs you want to receive",
        counterparty="The Discord member managing the other team (use this OR team_code, not both)",
        team_code="NBA team code of a CPU-managed team, e.g. LAL (use this OR counterparty, not both)",
        give_picks="Space-separated pick IDs you are giving up (optional)",
        receive_picks="Space-separated pick IDs you want to receive (optional)",
    )
    async def propose(
        self,
        interaction: discord.Interaction,
        give_players: str,
        receive_players: str,
        counterparty: Optional[discord.Member] = None,
        team_code: Optional[str] = None,
        give_picks: str = "",
        receive_picks: str = "",
    ) -> None:
        await safe_defer(interaction)

        team_code = (team_code or "").strip() or None

        if (counterparty is None) == (team_code is None):
            await safe_respond(
                interaction,
                content=(
                    "Provide either `counterparty` (a Discord member) or `team_code` "
                    "(a CPU team's code) — not both, not neither."
                ),
                ephemeral=True,
            )
            return

        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await safe_respond(interaction, content="No active league in this server.", ephemeral=True)
            return

        await require_phase(league, "trade_propose")

        pool = await get_pool()

        proposer_team = await team_repo.get_by_manager(pool, league.id, interaction.user.id)
        if not proposer_team:
            await safe_respond(interaction, content="You don't manage a team in this league.", ephemeral=True)
            return

        if team_code is not None:
            counterparty_team = await team_repo.get_by_code(pool, league.id, team_code.upper())
            if not counterparty_team:
                await safe_respond(
                    interaction,
                    content=f"No team found with code **{team_code.upper()}**.",
                    ephemeral=True,
                )
                return

            if counterparty_team.manager_user_id is not None:
                await safe_respond(
                    interaction,
                    content=(
                        f"**{counterparty_team.full_name}** is managed by a human. "
                        "Use `/trade propose` with `counterparty` set to them instead."
                    ),
                    ephemeral=True,
                )
                return

            if proposer_team.id == counterparty_team.id:
                await safe_respond(interaction, content="You cannot trade with yourself.", ephemeral=True)
                return
        else:
            counterparty_team = await team_repo.get_by_manager(pool, league.id, counterparty.id)
            if not counterparty_team:
                await safe_respond(
                    interaction,
                    content=(
                        f"{counterparty.mention} does not manage a team in this league. "
                        "To trade with a CPU team, use `/trade propose` with `team_code` instead."
                    ),
                    ephemeral=True,
                )
                return

            if proposer_team.id == counterparty_team.id:
                await safe_respond(interaction, content="You cannot trade with yourself.", ephemeral=True)
                return

        give_player_ids = _parse_ids(give_players) if give_players.strip() else []
        receive_player_ids = _parse_ids(receive_players) if receive_players.strip() else []
        give_pick_ids = _parse_ids(give_picks) if give_picks.strip() else []
        receive_pick_ids = _parse_ids(receive_picks) if receive_picks.strip() else []

        trade = await trade_service.propose(
            league=league,
            proposer_team=proposer_team,
            counterparty_team=counterparty_team,
            proposer_player_ids=give_player_ids,
            proposer_pick_ids=give_pick_ids,
            counterparty_player_ids=receive_player_ids,
            counterparty_pick_ids=receive_pick_ids,
        )

        assets = await trade_repo.get_assets(pool, trade.id)
        players_by_id, picks_by_id = await _build_asset_lookup(pool, assets)
        card = trade_embeds.trade_card(trade, assets, proposer_team, counterparty_team, players_by_id, picks_by_id)

        if trade.status in ("declined", "rejected"):
            await safe_respond(
                interaction,
                content=(
                    f"The CPU **{counterparty_team.full_name}** declined the trade.\n"
                    f"Reason: {trade.cpu_rationale or 'No reason given.'}"
                ),
                ephemeral=True,
            )
            return

        if trade.status == "counter_offered":
            # CPU sent back a counter-offer — load its assets and display them.
            counter_assets = await trade_repo.get_assets(pool, trade.id)
            counter_players_by_id, counter_picks_by_id = await _build_asset_lookup(pool, counter_assets)
            counter_card = trade_embeds.trade_card(
                trade, counter_assets, counterparty_team, proposer_team,
                counter_players_by_id, counter_picks_by_id,
            )
            await safe_respond(
                interaction,
                content=(
                    f"The CPU **{counterparty_team.full_name}** sent back a counter-offer (Trade #{trade.id}). "
                    f"Use `/trade accept {trade.id}` or `/trade decline {trade.id}` to respond."
                ),
                embed=counter_card,
            )
            return

        transactions_channel_id = await league_repo.get_channel(pool, league.id, "transactions")

        if trade.status == "pending_commissioner":
            # CPU auto-accepted
            announced = trade_embeds.trade_proposed(trade, proposer_team, counterparty_team)
            await safe_respond(
                interaction,
                content=f"The CPU **{counterparty_team.full_name}** accepted! Trade is now pending commissioner approval.",
                embed=card,
            )
            if transactions_channel_id:
                tx_channel = interaction.guild.get_channel(transactions_channel_id)
                if tx_channel and tx_channel.id != interaction.channel_id:
                    _sent = await tx_channel.send(embed=announced)
                    await feedback_log.register_trade_announcement(
                        pool, _sent,
                        league_id=league.id, season=league.current_season,
                        trade_id=trade.id,
                        proposer_team_id=proposer_team.id,
                        counterparty_team_id=counterparty_team.id,
                        status=trade.status,
                        headline=f"{proposer_team.full_name} / {counterparty_team.full_name} (Trade #{trade.id})",
                    )
            return

        # Human counterparty — send DM with accept/decline view
        view = TradeAcceptView(trade.id, counterparty.id, counterparty_team.id)
        try:
            await counterparty.send(
                f"**{proposer_team.full_name}** has proposed a trade with you (Trade #{trade.id}):",
                embed=card,
                view=view,
            )
        except discord.Forbidden:
            log.warning(f"Could not DM counterparty {counterparty.id} for trade {trade.id}")
            await safe_respond(
                interaction,
                content=(
                    f"Trade #{trade.id} proposed, but I couldn't DM {counterparty.mention} — their DMs may be closed. "
                    "They can still use `/trade accept {trade.id}` or `/trade decline {trade.id}`."
                ),
                embed=card,
            )
        else:
            await safe_respond(
                interaction,
                content=f"Trade #{trade.id} proposed. {counterparty.mention} has been notified via DM.",
                embed=card,
            )

        if transactions_channel_id:
            tx_channel = interaction.guild.get_channel(transactions_channel_id)
            if tx_channel and tx_channel.id != interaction.channel_id:
                announced = trade_embeds.trade_proposed(trade, proposer_team, counterparty_team)
                _sent = await tx_channel.send(embed=announced)
                await feedback_log.register_trade_announcement(
                    pool, _sent,
                    league_id=league.id, season=league.current_season,
                    trade_id=trade.id,
                    proposer_team_id=proposer_team.id,
                    counterparty_team_id=counterparty_team.id,
                    status=trade.status,
                    headline=f"{proposer_team.full_name} / {counterparty_team.full_name} (Trade #{trade.id})",
                )

    @app_commands.command(name="accept", description="Accept an incoming trade offer")
    @app_commands.describe(trade_id="The trade ID to accept")
    async def accept(self, interaction: discord.Interaction, trade_id: int) -> None:
        await safe_defer(interaction, ephemeral=True)

        pool = await get_pool()
        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await safe_respond(interaction, content="No active league in this server.", ephemeral=True)
            return

        user_team = await team_repo.get_by_manager(pool, league.id, interaction.user.id)
        if not user_team:
            await safe_respond(interaction, content="You don't manage a team in this league.", ephemeral=True)
            return

        trade = await trade_service.accept(pool, trade_id, user_team.id)

        assets = await trade_repo.get_assets(pool, trade.id)
        proposer_team = await team_repo.get_by_id(pool, trade.proposer_team_id)
        players_by_id, picks_by_id = await _build_asset_lookup(pool, assets)
        card = trade_embeds.trade_card(trade, assets, proposer_team, user_team, players_by_id, picks_by_id)

        await safe_respond(
            interaction,
            content=f"Trade #{trade_id} accepted and sent to commissioner for review.",
            embed=card,
        )

        transactions_channel_id = await league_repo.get_channel(pool, league.id, "transactions")
        if transactions_channel_id:
            tx_channel = interaction.guild.get_channel(transactions_channel_id)
            if tx_channel and tx_channel.id != interaction.channel_id:
                _sent = await tx_channel.send(embed=trade_embeds.trade_proposed(trade, proposer_team, user_team))
                await feedback_log.register_trade_announcement(
                    pool, _sent,
                    league_id=league.id, season=league.current_season,
                    trade_id=trade.id,
                    proposer_team_id=proposer_team.id,
                    counterparty_team_id=user_team.id,
                    status=trade.status,
                    headline=f"{proposer_team.full_name} / {user_team.full_name} (Trade #{trade.id})",
                )

    @app_commands.command(name="decline", description="Decline an incoming trade offer")
    @app_commands.describe(trade_id="The trade ID to decline")
    async def decline(self, interaction: discord.Interaction, trade_id: int) -> None:
        await safe_defer(interaction, ephemeral=True)

        pool = await get_pool()
        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await safe_respond(interaction, content="No active league in this server.", ephemeral=True)
            return

        user_team = await team_repo.get_by_manager(pool, league.id, interaction.user.id)
        if not user_team:
            await safe_respond(interaction, content="You don't manage a team in this league.", ephemeral=True)
            return

        trade = await trade_service.decline(pool, trade_id, user_team.id)
        result_embed = trade_embeds.trade_result(trade, "declined")
        await safe_respond(interaction, content=f"Trade #{trade_id} declined.", embed=result_embed)

    @app_commands.command(name="approve", description="Commissioner: approve a pending trade")
    @app_commands.describe(trade_id="The trade ID to approve")
    async def approve(self, interaction: discord.Interaction, trade_id: int) -> None:
        await safe_defer(interaction)

        pool = await get_pool()
        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await safe_respond(interaction, content="No active league in this server.", ephemeral=True)
            return

        await require_phase(league, "trade_approve")
        await require_commissioner(interaction, league)

        trade, grade_info = await trade_service.approve(pool, trade_id, interaction.user.id)
        result_embed = trade_embeds.trade_result(trade, "approved")
        await safe_respond(interaction, content="Trade approved.")

        transactions_channel_id = await league_repo.get_channel(pool, league.id, "transactions")
        if transactions_channel_id:
            tx_channel = interaction.guild.get_channel(transactions_channel_id)
            if tx_channel and tx_channel.id != interaction.channel_id:
                _sent = await tx_channel.send(embed=result_embed)

                proposer_team = await team_repo.get_by_id(pool, trade.proposer_team_id)
                counterparty_team = await team_repo.get_by_id(pool, trade.counterparty_team_id)
                if proposer_team and counterparty_team:
                    await feedback_log.register_trade_announcement(
                        pool, _sent,
                        league_id=league.id, season=league.current_season,
                        trade_id=trade.id,
                        proposer_team_id=proposer_team.id,
                        counterparty_team_id=counterparty_team.id,
                        status="approved",
                        headline=f"{proposer_team.full_name} / {counterparty_team.full_name} (Trade #{trade.id}) — approved",
                    )
                    # Query win-loss records for both teams to give AI context.
                    rec_a = await pool.fetchrow(
                        "SELECT wins, losses FROM standings_cache "
                        "WHERE league_id = $1 AND team_id = $2 "
                        "ORDER BY season DESC LIMIT 1",
                        league.id, proposer_team.id,
                    )
                    rec_b = await pool.fetchrow(
                        "SELECT wins, losses FROM standings_cache "
                        "WHERE league_id = $1 AND team_id = $2 "
                        "ORDER BY season DESC LIMIT 1",
                        league.id, counterparty_team.id,
                    )
                    record_a = f"{rec_a['wins']}-{rec_a['losses']}" if rec_a else "?"
                    record_b = f"{rec_b['wins']}-{rec_b['losses']}" if rec_b else "?"

                    trade_summary = {
                        "team_a_name": proposer_team.full_name,
                        "team_b_name": counterparty_team.full_name,
                        "team_a_record": record_a,
                        "team_b_record": record_b,
                        "team_a_players": grade_info.get("players_a", []),
                        "team_b_players": grade_info.get("players_b", []),
                        "grade_a": grade_info["grade_a"],
                        "grade_b": grade_info["grade_b"],
                    }
                    ai_reasoning = await get_ai_reasoning(
                        trade_summary, os.environ.get("ANTHROPIC_API_KEY")
                    )

                    grade_embed = trade_embeds.trade_grade_embed(
                        trade=trade,
                        grade_a=grade_info["grade_a"],
                        grade_b=grade_info["grade_b"],
                        team_a=proposer_team,
                        team_b=counterparty_team,
                        rationale=grade_info["rationale"],
                        ai_reasoning=ai_reasoning or None,
                    )
                    _grade_sent = await tx_channel.send(embed=grade_embed)
                    await feedback_log.register_trade_announcement(
                        pool, _grade_sent,
                        league_id=league.id, season=league.current_season,
                        trade_id=trade.id,
                        proposer_team_id=proposer_team.id,
                        counterparty_team_id=counterparty_team.id,
                        status="approved_grade",
                        headline=f"Grade: {proposer_team.full_name} {grade_info['grade_a']} / {counterparty_team.full_name} {grade_info['grade_b']} (Trade #{trade.id})",
                    )

    @app_commands.command(name="veto", description="Commissioner: veto a pending trade")
    @app_commands.describe(trade_id="The trade ID to veto", reason="Reason for veto")
    async def veto(
        self,
        interaction: discord.Interaction,
        trade_id: int,
        reason: str = "No reason provided.",
    ) -> None:
        await safe_defer(interaction)

        pool = await get_pool()
        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await safe_respond(interaction, content="No active league in this server.", ephemeral=True)
            return

        await require_phase(league, "trade_veto")
        await require_commissioner(interaction, league)

        trade = await trade_service.veto(pool, trade_id, interaction.user.id, reason)
        result_embed = trade_embeds.trade_result(trade, "vetoed")

        transactions_channel_id = await league_repo.get_channel(pool, league.id, "transactions")
        await post_to_channel_or_respond(
            interaction,
            transactions_channel_id,
            embed=result_embed,
            ephemeral_ack="Trade vetoed. Posted to #transactions.",
        )

    @app_commands.command(name="pending", description="Commissioner: view trades awaiting approval")
    async def pending(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction, ephemeral=True)

        pool = await get_pool()
        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await safe_respond(interaction, content="No active league in this server.", ephemeral=True)
            return

        if interaction.user.id != league.commissioner_user_id:
            await safe_respond(interaction, content="Only the commissioner can view the trade queue.", ephemeral=True)
            return

        queue = await trade_service.get_pending_queue(pool, league.id)
        embed = trade_embeds.pending_queue(queue)
        await safe_respond(interaction, embed=embed)

    @app_commands.command(name="list", description="View your team's active trades (incoming and outgoing)")
    async def list(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction, ephemeral=True)

        pool = await get_pool()
        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await safe_respond(interaction, content="No active league in this server.", ephemeral=True)
            return

        user_team = await team_repo.get_by_manager(pool, league.id, interaction.user.id)
        if not user_team:
            await safe_respond(interaction, content="You don't manage a team in this league.", ephemeral=True)
            return

        incoming = await trade_repo.get_pending_for_team(pool, user_team.id)
        outgoing = await trade_repo.get_active_outgoing(pool, user_team.id)

        embed = discord.Embed(
            title=f"{user_team.full_name} — Active Trades",
            color=discord.Color.blurple(),
        )

        if incoming:
            lines = [f"#{t.id} — from team {t.proposer_team_id} ({t.status})" for t in incoming]
            embed.add_field(name="Incoming (awaiting your response)", value="\n".join(lines), inline=False)

        if outgoing:
            lines = [f"#{t.id} — to team {t.counterparty_team_id} ({t.status})" for t in outgoing]
            embed.add_field(name="Outgoing", value="\n".join(lines), inline=False)

        if not incoming and not outgoing:
            embed.description = "No active trades."

        await safe_respond(interaction, embed=embed)

    @app_commands.command(name="history", description="View approved trade history for a team")
    @app_commands.describe(
        team="Team code (defaults to your team)",
        season="Filter by season (omit for all seasons)",
    )
    async def history(
        self,
        interaction: discord.Interaction,
        team: Optional[str] = None,
        season: Optional[int] = None,
    ) -> None:
        await safe_defer(interaction, ephemeral=True)

        pool = await get_pool()
        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await safe_respond(interaction, content="No active league in this server.", ephemeral=True)
            return

        if team:
            target_team = await team_repo.get_by_code(pool, league.id, team.upper())
            if not target_team:
                await safe_respond(
                    interaction,
                    content=f"Team `{team.upper()}` not found.",
                    ephemeral=True,
                )
                return
        else:
            target_team = await team_repo.get_by_manager(pool, league.id, interaction.user.id)
            if not target_team:
                await safe_respond(
                    interaction,
                    content="You don't manage a team. Provide a team code.",
                    ephemeral=True,
                )
                return

        trades = await trade_repo.get_team_trade_history(pool, league.id, target_team.id)

        if season is not None:
            trades = [e for e in trades if e["trade"].season == season]

        all_assets = [a for entry in trades for a in entry["assets"]]
        players_by_id: dict = {}
        for asset in all_assets:
            if (
                asset.asset_type == "player"
                and asset.player_id
                and asset.player_id not in players_by_id
            ):
                p = await player_repo.get_by_id(pool, asset.player_id)
                if p:
                    players_by_id[p.id] = {"full_name": p.full_name, "overall": p.overall}

        embed = trade_embeds.trade_history_embed(trades, target_team, players_by_id)
        await safe_respond(interaction, embed=embed)

    @app_commands.command(
        name="why",
        description="Show the CPU's reasoning signals for a past trade.",
    )
    @app_commands.describe(trade_id="The trade ID to inspect")
    async def trade_why(self, interaction: discord.Interaction, trade_id: int) -> None:
        await safe_defer(interaction, ephemeral=True)

        pool = await get_pool()
        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await safe_respond(interaction, content="No active league in this server.", ephemeral=True)
            return

        trade = await trade_repo.get_trade(pool, trade_id)
        if not trade or trade.league_id != league.id:
            await safe_respond(
                interaction,
                content=f"Trade #{trade_id} not found in this league.",
                ephemeral=True,
            )
            return

        context_signals = await trade_repo.get_context_signals(pool, trade_id)
        if not context_signals:
            await safe_respond(
                interaction,
                content=(
                    "This trade pre-dates context signal tracking — no reasoning available. "
                    "Signals tracking started 2026-05-20; trades before this date have no signal history."
                ),
                ephemeral=True,
            )
            return

        proposer_team = await team_repo.get_by_id(pool, trade.proposer_team_id)
        counterparty_team = await team_repo.get_by_id(pool, trade.counterparty_team_id)

        # Resolve player names for signal display (one query per player in the signals blob).
        per_player: dict = context_signals.get("per_player") or {}
        player_names: dict[int, str] = {}
        for pid_str in per_player:
            try:
                pid = int(pid_str)
            except ValueError:
                continue
            p = await player_repo.get_by_id(pool, pid)
            if p:
                player_names[pid] = p.full_name

        embed = trade_embeds.trade_why_embed(
            trade=trade,
            context_signals=context_signals,
            proposer_team=proposer_team,
            counterparty_team=counterparty_team,
            player_names=player_names,
        )
        await safe_respond(interaction, embed=embed)

    @app_commands.command(
        name="signals",
        description="Aggregate signal patterns for a team's recent trades.",
    )
    @app_commands.describe(
        team_code="NBA team code (e.g. CLE)",
        limit="Number of recent trades to scan (default 20, max 50)",
    )
    async def trade_signals(
        self,
        interaction: discord.Interaction,
        team_code: str,
        limit: int = 20,
    ) -> None:
        await safe_defer(interaction, ephemeral=True)

        pool = await get_pool()
        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await safe_respond(interaction, content="No active league in this server.", ephemeral=True)
            return

        target_team = await team_repo.get_by_code(pool, league.id, team_code.upper())
        if not target_team:
            await safe_respond(
                interaction,
                content=f"Team `{team_code.upper()}` not found.",
                ephemeral=True,
            )
            return

        # Clamp limit to [1, 50] so users can't request absurd result sets.
        clamped_limit = max(1, min(50, limit))

        trade_rows = await trade_repo.get_trades_with_signals_for_team(
            pool, league.id, target_team.id, clamped_limit
        )

        most_fired, top_positive, top_negative = trade_embeds._aggregate_signals(trade_rows)

        embed = trade_embeds.trade_signals_embed(
            team=target_team,
            trade_count=len(trade_rows),
            most_fired=most_fired,
            top_positive=top_positive,
            top_negative=top_negative,
        )
        await safe_respond(interaction, embed=embed)

    @app_commands.command(
        name="league",
        description="Show the last 20 trades across the whole league",
    )
    async def league_feed(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction)

        pool = await get_pool()
        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await safe_respond(interaction, content="No active league in this server.", ephemeral=True)
            return

        # Single query: last 20 trades across all statuses, ordered by most-recent
        # activity (resolved_at takes priority; falls back to proposed_at).
        trade_rows = await pool.fetch(
            """
            SELECT
                t.id,
                t.status,
                t.proposer_team_id,
                t.counterparty_team_id,
                COALESCE(t.resolved_at, t.proposed_at) AS updated_at,
                tp.nba_team_code  AS proposer_code,
                tc.nba_team_code  AS counterparty_code
            FROM trades t
            JOIN teams tp ON tp.id = t.proposer_team_id
            JOIN teams tc ON tc.id = t.counterparty_team_id
            WHERE t.league_id = $1
              AND t.status IN ('approved', 'pending_commissioner', 'pending_counterparty',
                               'declined', 'rejected', 'vetoed')
            ORDER BY COALESCE(t.resolved_at, t.proposed_at) DESC
            LIMIT 20
            """,
            league.id,
        )

        if not trade_rows:
            embed = intel_embeds.league_trades_embed([])
            await safe_respond(interaction, embed=embed)
            return

        # Bulk-fetch assets for all trade IDs in one query.
        trade_ids = [r["id"] for r in trade_rows]
        asset_rows = await pool.fetch(
            """
            SELECT
                ta.trade_id,
                ta.from_team_id,
                ta.asset_type,
                ta.player_id,
                ta.pick_id,
                p.first_name || ' ' || p.last_name AS player_name,
                dp.season AS pick_season,
                dp.round  AS pick_round
            FROM trade_assets ta
            LEFT JOIN players p ON p.id = ta.player_id
            LEFT JOIN draft_picks dp ON dp.id = ta.pick_id
            WHERE ta.trade_id = ANY($1::int[])
            """,
            trade_ids,
        )

        # Group assets by trade_id.
        assets_by_trade: dict[int, list] = {tid: [] for tid in trade_ids}
        for a in asset_rows:
            assets_by_trade[a["trade_id"]].append(dict(a))

        def _asset_label(a: dict) -> str:
            if a["asset_type"] == "player" and a.get("player_name"):
                return a["player_name"]
            if a["asset_type"] == "pick":
                season = a.get("pick_season", "?")
                rnd = a.get("pick_round", "?")
                return f"{season} R{rnd} pick"
            return "asset"

        trades_for_embed: list[dict] = []
        for row in trade_rows:
            tid = row["id"]
            all_assets = assets_by_trade.get(tid, [])
            proposer_assets = [
                _asset_label(a) for a in all_assets
                if a["from_team_id"] == row["proposer_team_id"]
            ]
            cp_assets = [
                _asset_label(a) for a in all_assets
                if a["from_team_id"] == row["counterparty_team_id"]
            ]
            trades_for_embed.append({
                "id": tid,
                "status": row["status"],
                "updated_at": row["updated_at"],
                "proposer_code": row["proposer_code"],
                "counterparty_code": row["counterparty_code"],
                "proposer_assets": proposer_assets,
                "counterparty_assets": cp_assets,
            })

        embed = intel_embeds.league_trades_embed(trades_for_embed)
        await safe_respond(interaction, embed=embed)


class TradeCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.bot.tree.add_command(TradeGroup())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TradeCog(bot))
