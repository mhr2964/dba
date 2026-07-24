"""Bridges sim batches to CPU trade rounds: triggers cpu_trade_service each
batch, then announces the results (trade-executed embed + thread, Marcus Cole
blockbuster analysis) via the Announcer protocol.

Extracted from sim_orchestrator.py. Builds EmbedData and posts through
_BoundChannelAnnouncer (services/sim_channel_announcer.py) instead of
constructing discord.Embed directly -- see tests/test_run_cpu_trades_inner.py
for the characterization tests written before this conversion (both had zero
coverage before this pass).
"""
from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Optional

from core.logging import get_logger
from data.repositories import league_repo, team_repo, trade_repo
from services import columnist_ride_along as _columnist_ride_along
from services import columnist_service, cpu_trade_service
from services import feedback_log as _feedback_log
from services import trade_magnitude as _trade_magnitude_service
from services.announcer_protocol import EmbedData, EmbedField
from services.personas import PERSONAS as _PERSONAS
from services.sim_channel_announcer import _BoundChannelAnnouncer, _get_transactions_channel

if TYPE_CHECKING:
    import discord

log = get_logger(__name__)

# Matches discord.Color.green()/.orange()/.from_rgb(255, 69, 0) -- hardcoded so
# this module never needs `import discord`.
_COLOR_GREEN = 0x2ECC71
_COLOR_ORANGE = 0xE67E22
_COLOR_ORANGE_RED = 0xFF4500


async def _maybe_run_cpu_trades(
    pool,
    league_id: int,
    season: int,
    current_game_index: int,
    total_regular_games: int,
    deadline_game_index: Optional[int],
    guild: discord.Guild,
    refresh_block: bool = True,
) -> None:
    """Wrapper that swallows exceptions so a trade-round failure doesn't abort the sim.

    refresh_block=False skips the CPU trade-block refresh (cpu_block_service.refresh_league)
    for mid-batch calls.  Pass True only on the final flush of each sim function so the
    block is rebuilt once per sim invocation rather than once per game-day.
    """
    try:
        await _run_cpu_trades_inner(
            pool, league_id, season, current_game_index,
            total_regular_games, deadline_game_index, guild,
            refresh_block=refresh_block,
        )
    except Exception as exc:
        log.warning(f"_maybe_run_cpu_trades failed silently: {exc}")


async def _run_cpu_trades_inner(
    pool,
    league_id: int,
    season: int,
    current_game_index: int,
    total_regular_games: int,
    deadline_game_index: Optional[int],
    guild: discord.Guild,
    refresh_block: bool = True,
) -> None:
    if not deadline_game_index:
        return

    snapshot_ts = datetime.datetime.now(datetime.timezone.utc)

    trades_proposed = await cpu_trade_service.maybe_initiate_round(
        pool, league_id, season,
        current_game_index, total_regular_games, deadline_game_index,
        guild,
        refresh_block=refresh_block,
    )
    if not trades_proposed:
        return

    transactions_channel = await _get_transactions_channel(guild, pool, league_id)
    if not transactions_channel:
        return

    # Fetch trades created in this call (by timestamp).
    new_trades = await pool.fetch(
        """
        SELECT id, proposer_team_id, counterparty_team_id, status
        FROM trades
        WHERE league_id = $1 AND proposed_at >= $2
        ORDER BY id
        """,
        league_id, snapshot_ts,
    )

    for trade_row in new_trades:
        trade_id = trade_row["id"]
        status = trade_row["status"]

        # Fetch assets.
        assets = await trade_repo.get_assets(pool, trade_id)

        proposer_id = trade_row["proposer_team_id"]
        counterparty_id = trade_row["counterparty_team_id"]

        team_rows = await pool.fetch(
            "SELECT id, nba_team_code FROM teams WHERE id = ANY($1)",
            [proposer_id, counterparty_id],
        )
        team_codes = {r["id"]: r["nba_team_code"] for r in team_rows}

        # Look up real player names and OVR so embeds, Marcus Cole context, and
        # the blockbuster-importance check all have accurate data.
        _player_ids = [a.player_id for a in assets if a.asset_type == "player" and a.player_id]
        if _player_ids:
            # players table has birth_date, not a precomputed age column —
            # derive age from birth_date.
            _name_rows = await pool.fetch(
                """SELECT id, first_name, last_name, overall, position,
                          EXTRACT(YEAR FROM AGE(birth_date))::int AS age
                   FROM players WHERE id = ANY($1)""",
                _player_ids,
            )
            _player_names = {r["id"]: f"{r['first_name']} {r['last_name']}" for r in _name_rows}
            _player_ovrs: dict[int, int] = {r["id"]: r["overall"] for r in _name_rows}
            _player_positions: dict[int, str] = {r["id"]: r["position"] for r in _name_rows}
            _player_ages: dict[int, int | None] = {r["id"]: r["age"] for r in _name_rows}
        else:
            _player_names = {}
            _player_ovrs = {}
            _player_positions = {}
            _player_ages = {}

        # Load pick metadata so embeds show "2026 LAL 1st Round Pick" instead of "Pick #ID".
        _pick_ids = [a.pick_id for a in assets if a.asset_type == "pick" and a.pick_id]
        if _pick_ids:
            _pick_rows = await pool.fetch(
                """SELECT dp.id, dp.season, dp.round, t.nba_team_code AS original_team
                   FROM draft_picks dp
                   JOIN teams t ON t.id = dp.original_team_id
                   WHERE dp.id = ANY($1)""",
                _pick_ids,
            )
            _pick_info: dict[int, dict] = {r["id"]: r for r in _pick_rows}
        else:
            _pick_info = {}

        def _format_pick(pick_id: int) -> str:
            r = _pick_info.get(pick_id)
            if r:
                round_label = "1st Round" if r["round"] == 1 else "2nd Round"
                return f"{r['season']} {r['original_team']} {round_label} Pick"
            return f"Pick #{pick_id}"

        def _asset_lines(from_team_id: int) -> list[str]:
            lines = []
            for a in assets:
                if a.from_team_id != from_team_id:
                    continue
                if a.asset_type == "player" and a.player_id:
                    lines.append(_player_names.get(a.player_id) or f"Player #{a.player_id}")
                elif a.asset_type == "pick" and a.pick_id:
                    lines.append(_format_pick(a.pick_id))
            return lines or ["(nothing)"]

        def _asset_items(to_team_id: int) -> list[dict]:
            """Build structured asset list for trade_report ctx (items moving TO a team)."""
            items = []
            for a in assets:
                if a.to_team_id != to_team_id:
                    continue
                if a.asset_type == "player" and a.player_id:
                    pid = a.player_id
                    item: dict = {
                        "type": "player",
                        "name": _player_names.get(pid) or f"Player #{pid}",
                    }
                    if pid in _player_positions and _player_positions[pid]:
                        item["position"] = _player_positions[pid]
                    age = _player_ages.get(pid)
                    if age is not None:
                        item["age"] = age
                    ovr = _player_ovrs.get(pid)
                    if ovr is not None:
                        item["ovr"] = ovr
                    items.append(item)
                elif a.asset_type == "pick" and a.pick_id:
                    r = _pick_info.get(a.pick_id)
                    pick_item: dict = {"type": "pick"}
                    if r:
                        round_label = "1st-round" if r["round"] == 1 else "2nd-round"
                        pick_item["name"] = f"{r['season']} {round_label} pick"
                        pick_item["via"] = r["original_team"]
                    else:
                        pick_item["name"] = f"Pick #{a.pick_id}"
                    items.append(pick_item)
                elif a.asset_type == "cash":
                    items.append({"type": "cash", "name": "Cash considerations"})
            return items

        proposer_code = team_codes.get(proposer_id, f"Team {proposer_id}")
        counterparty_code = team_codes.get(counterparty_id, f"Team {counterparty_id}")

        title = "✅ Trade Executed" if status == "approved" else "⏳ Trade Pending Review"
        color = _COLOR_GREEN if status == "approved" else _COLOR_ORANGE

        fields = [
            EmbedField(
                name=f"{counterparty_code} receives",
                value="\n".join(_asset_lines(proposer_id)),
                inline=True,
            ),
            EmbedField(
                name=f"{proposer_code} receives",
                value="\n".join(_asset_lines(counterparty_id)),
                inline=True,
            ),
        ]
        if status == "pending_commissioner":
            fields.append(EmbedField(
                name="Action required",
                value="Commissioner must review and approve or reject this trade.",
                inline=False,
            ))
        trade_embed_data = EmbedData(
            title=title,
            color=color,
            fields=fields,
            footer=f"CPU-initiated · Trade #{trade_id}",
        )
        transactions_announcer = _BoundChannelAnnouncer(transactions_channel)
        trade_msg = await transactions_announcer.post_embed_get_ref("transactions", trade_embed_data)
        await _feedback_log.register_trade_announcement(
            pool, trade_msg,
            league_id=league_id, season=season, trade_id=trade_id,
            proposer_team_id=proposer_id, counterparty_team_id=counterparty_id,
            status=status,
            headline=f"{proposer_code} / {counterparty_code} (Trade #{trade_id})",
        )

        # Open a thread on the lead message so all follow-up activity stays grouped.
        try:
            thread_name = f"Trade #{trade_id} — {proposer_code} / {counterparty_code}"
            status_label = "Executed" if status == "approved" else "Pending commissioner review"
            detail_lines = [
                f"**Trade #{trade_id}**",
                f"Status: {status_label}",
                "",
                f"**{counterparty_code} receives:** {', '.join(_asset_lines(proposer_id))}",
                f"**{proposer_code} receives:** {', '.join(_asset_lines(counterparty_id))}",
            ]
            await transactions_announcer.create_thread_and_send(
                trade_msg, thread_name, "\n".join(detail_lines)
            )
        except Exception as _thread_exc:
            log.warning(f"Failed to create trade thread for trade #{trade_id}: {_thread_exc}")

        # Marcus Cole — insider trade report to #analysis.
        # Gate: only fire when trade is fully executed (status == "approved" in DB).
        # Trades involving a human-managed team land as "pending_commissioner" and
        # must not trigger a columnist article — the deal hasn't happened yet.
        mc_article = None
        _mc_ra_capture: dict | None = None  # populated inside the elif when ride-along is active
        if status != "approved":
            log.info(
                f"Marcus Cole: skipping trade #{trade_id} — not executed "
                f"(status={status!r})"
            )
        elif _is_blockbuster_trade(assets, _player_ovrs):
            # Build roster-fit context: for each traded player, look up who they'll
            # play alongside on their new team and what that team's build mode is.
            roster_fits: list[str] = []
            try:
                all_teams = await team_repo.get_all(pool, league_id)
                _team_by_id = {t.id: t for t in all_teams}
                for _asset in assets:
                    if _asset.asset_type != "player" or not _asset.player_id:
                        continue
                    p_name = _player_names.get(_asset.player_id, f"Player #{_asset.player_id}")
                    new_team_code = team_codes.get(_asset.to_team_id) or (
                        _team_by_id[_asset.to_team_id].nba_team_code
                        if _asset.to_team_id in _team_by_id else "???"
                    )
                    teammates = await pool.fetch(
                        """SELECT first_name || ' ' || last_name AS name, overall, position
                           FROM players
                           WHERE league_id = $1 AND team_id = $2 AND id != $3
                           ORDER BY overall DESC LIMIT 3""",
                        league_id, _asset.to_team_id, _asset.player_id,
                    )
                    teammate_str = ", ".join(
                        f"{r['name']} ({r['position']}, {r['overall']} OVR)" for r in teammates
                    ) or "no teammates found"
                    new_team_obj = _team_by_id.get(_asset.to_team_id)
                    team_mode = (getattr(new_team_obj, "cpu_mode", None) or "default") if new_team_obj else "default"
                    roster_fits.append(
                        f"{p_name} → {new_team_code} (top teammates: {teammate_str}; team mode: {team_mode})"
                    )
            except Exception as _rf_exc:
                log.warning(f"Marcus Cole roster-fit enrichment failed: {_rf_exc}")

            # Compute context signals for each player arriving at their new team.
            # These are the same signals that drove the CPU's accept/reject math;
            # Marcus Cole's voice_notes instruct him to lean on them in analysis.
            # Signals are computed fresh here (not persisted — Phase 5 adds that).
            context_signals_per_player: dict[int, list[dict]] = {}
            try:
                from services.trade_context import compute_context_modifier
                from services import team_intel as _ti
                _league_row = await pool.fetchrow(
                    "SELECT * FROM leagues WHERE id = $1", league_id
                )
                if _league_row:
                    from data.repositories import league_repo as _lr2
                    _league_obj = _lr2._league_from_record(_league_row)
                    # Fetch plan + posture for each receiving team in one bulk call.
                    _receiving_team_ids = list({
                        a.to_team_id for a in assets
                        if a.asset_type == "player" and a.player_id
                    })
                    if _receiving_team_ids:
                        _ti_data = await _ti.build_team_intel(
                            pool, _league_obj, season,
                            _receiving_team_ids,
                            include=("posture", "plan", "philosophy"),
                        )
                    else:
                        _ti_data = {}

                    for _asset in assets:
                        if _asset.asset_type != "player" or not _asset.player_id:
                            continue
                        _pid = _asset.player_id
                        _recv_tid = _asset.to_team_id
                        _intel = _ti_data.get(_recv_tid, {})
                        _plan = _intel.get("plan") or {}
                        _posture = _intel.get("posture") or {}
                        _phil = _intel.get("philosophy")
                        # Fetch minimal player dict for the detector.
                        _p_row = await pool.fetchrow(
                            """SELECT id, overall, position,
                                      scoring_tendency, playmaking_tendency,
                                      defense_tendency, rebounding_tendency
                               FROM players WHERE id = $1""",
                            _pid,
                        )
                        if not _p_row:
                            continue
                        _player_dict = dict(_p_row)
                        _modifier, _signals = await compute_context_modifier(
                            pool=pool,
                            league_id=league_id,
                            season=season,
                            perspective_team_id=_recv_tid,
                            plan=_plan,
                            posture=_posture,
                            coach_philosophy=_phil,
                            incoming_player=_player_dict,
                            form_mod=1.0,
                        )
                        if _signals:
                            context_signals_per_player[_pid] = [
                                {"code": s.code, "delta": s.delta, "reason": s.reason}
                                for s in _signals
                            ]
            except Exception as _sig_exc:
                log.warning(f"Marcus Cole signal enrichment failed: {_sig_exc}")

            # D2: how big is this trade against team/league history, so Marcus
            # Cole can truthfully call it "the biggest trade this franchise has
            # made" -- only wired here (context), never invented by the LLM.
            trade_magnitude: dict | None = None
            try:
                _league_row_tm = await pool.fetchrow(
                    "SELECT salary_cap FROM leagues WHERE id = $1", league_id
                )
                if _league_row_tm and _league_row_tm["salary_cap"]:
                    _salary_cap = _league_row_tm["salary_cap"]
                    _league_rank = await _trade_magnitude_service.rank_trade_in_league_history(
                        pool, league_id, trade_id, _salary_cap, season,
                    )
                    _proposer_rank = await _trade_magnitude_service.rank_trade_in_team_history(
                        pool, league_id, proposer_id, trade_id, _salary_cap, season,
                    )
                    _counterparty_rank = await _trade_magnitude_service.rank_trade_in_team_history(
                        pool, league_id, counterparty_id, trade_id, _salary_cap, season,
                    )
                    trade_magnitude = {
                        "league": _league_rank,
                        "team_ranks": {
                            proposer_code: _proposer_rank,
                            counterparty_code: _counterparty_rank,
                        },
                    }
            except Exception as _tm_exc:
                log.warning(f"Marcus Cole trade magnitude computation failed: {_tm_exc}")

            trade_context = {
                "proposer_team": proposer_code,
                "counterparty_team": counterparty_code,
                "proposer_sends": _asset_lines(proposer_id),
                "counterparty_sends": _asset_lines(counterparty_id),
                "trade_status": status,
                "roster_fits": roster_fits,
                "context_signals_per_player": {
                    int(pid): sigs
                    for pid, sigs in context_signals_per_player.items()
                },
                # Structured swap data consumed by the trade_report renderer.
                # Each entry: {name, gets: [{type, name, position?, age?, ovr?, via?}]}
                "teams": [
                    {
                        "name": counterparty_code,
                        "gets": _asset_items(counterparty_id),
                    },
                    {
                        "name": proposer_code,
                        "gets": _asset_items(proposer_id),
                    },
                ],
                # {"league": {rank, total_trades, magnitude, is_biggest} | None,
                #  "team_ranks": {team_code: {...} | None}} -- see
                # services/trade_magnitude.py. None/absent when the league has
                # no salary_cap row or the ranking lookup failed.
                "trade_magnitude": trade_magnitude,
            }
            _mc_ra_capture: dict | None = (
                {} if (
                    _columnist_ride_along.is_enabled()
                    and "marcus_cole" == _columnist_ride_along.target_persona_id()
                ) else None
            )
            mc_article = await columnist_service.generate(
                pool, league_id, season,
                persona_id="marcus_cole",
                category="trade_report",
                context=trade_context,
                subject_team_ids=[proposer_id, counterparty_id],
                _capture_prompt=_mc_ra_capture,
            )
        if mc_article:
            analysis_channel_id = await league_repo.get_channel(pool, league_id, "analysis")
            analysis_channel = guild.get_channel(analysis_channel_id) if analysis_channel_id else None
            if analysis_channel:
                mc_persona = _PERSONAS.get("marcus_cole")
                mc_embed_data = EmbedData(
                    title=f"\U0001F4E1 {mc_article['headline']}",
                    description=mc_article["body"][:2000],
                    color=_COLOR_ORANGE_RED,
                    footer=(
                        f"by {mc_persona.display_name} · {mc_persona.byline}"
                        if mc_persona else None
                    ),
                )
                _sent = await _BoundChannelAnnouncer(analysis_channel).post_embed_get_ref(
                    "analysis", mc_embed_data
                )
                await _feedback_log.register_columnist_post(
                    pool, _sent,
                    league_id=league_id, season=season,
                    persona_id="marcus_cole", category="trade_report",
                    headline=mc_article["headline"], body=mc_article["body"],
                    subject_team_ids=[proposer_id, counterparty_id],
                    subject_trade_id=trade_id,
                )
                # Ride-along: pause AFTER embed lands in Discord.
                if _mc_ra_capture is not None:
                    await _columnist_ride_along.request_pause({
                        "persona_id": "marcus_cole",
                        "persona_display_name": mc_persona.display_name if mc_persona else "Marcus Cole",
                        "league_id": league_id,
                        "season": season,
                        "game_index_at_post": 0,
                        "category": "trade_report",
                        "prompt": _mc_ra_capture,
                        "context_dict": trade_context,
                        "article": {
                            "headline": mc_article.get("headline", ""),
                            "body": mc_article.get("body", ""),
                            "raw_llm_response": _mc_ra_capture.get("raw_llm_response", ""),
                        },
                        "embed_preview": (
                            f"{mc_article.get('headline', '')}\n\n"
                            + mc_article.get("body", "")[:400]
                        ),
                    })


def _is_blockbuster_trade(assets: list, player_ovrs: dict[int, int]) -> bool:
    """Return True if the trade involves a star (OVR>=80) or a R1 pick."""
    for a in assets:
        if a.asset_type == "player" and a.player_id:
            if player_ovrs.get(a.player_id, 0) >= 80:
                return True
        if a.asset_type == "pick":
            # Any pick included in a trade is notable enough
            return True
    return False
