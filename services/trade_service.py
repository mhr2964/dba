"""User-facing trade lifecycle: propose, accept, decline, approve, veto,
and the pending-queue listing. Validates assets, creates trade records,
and (for CPU counterparties) delegates to cpu_trade_evaluation for the
accept/counter/reject decision.

CPU-side evaluation logic lives in cpu_trade_evaluation.py (Phase 3
opportunistic split, see HANDOFF.md) -- this module is now the
user-facing orchestration layer only.
"""
from __future__ import annotations

import datetime

from core.errors import DBAError
from core.logging import get_logger
from data.db import get_pool
from data.repositories import league_repo, player_repo, team_repo, trade_block_repo, trade_repo
from services import cpu_trade_evaluation, trade_grading

log = get_logger(__name__)


async def _rebalance_starters(conn, league_id: int, team_id: int) -> None:
    """
    After a trade inserts a player into a team's lineup with is_starter=FALSE,
    recompute the starting five using position-aware selection (same algorithm as
    import_service._generate_lineup).  Runs inside an existing transaction.
    """
    rows = await conn.fetch(
        """
        SELECT l.player_id, p.position, p.overall
        FROM lineups l
        JOIN players p ON p.id = l.player_id
        WHERE l.league_id = $1 AND l.team_id = $2
        """,
        league_id,
        team_id,
    )
    if not rows:
        return

    roster = [{"id": r["player_id"], "position": r["position"], "overall": r["overall"]} for r in rows]

    starter_positions = ["PG", "SG", "SF", "PF", "C"]
    starters: list[int] = []
    used_ids: set[int] = set()

    for pos in starter_positions:
        best = max(
            (p for p in roster if p["position"] == pos and p["id"] not in used_ids),
            key=lambda x: x["overall"],
            default=None,
        )
        if best is None:
            best = max(
                (p for p in roster if p["id"] not in used_ids),
                key=lambda x: x["overall"],
                default=None,
            )
        if best:
            starters.append(best["id"])
            used_ids.add(best["id"])

    bench = [p["id"] for p in roster if p["id"] not in set(starters)]

    if starters:
        await conn.execute(
            "UPDATE lineups SET is_starter = TRUE WHERE league_id = $1 AND team_id = $2 AND player_id = ANY($3)",
            league_id,
            team_id,
            starters,
        )
    if bench:
        await conn.execute(
            "UPDATE lineups SET is_starter = FALSE WHERE league_id = $1 AND team_id = $2 AND player_id = ANY($3)",
            league_id,
            team_id,
            bench,
        )


async def propose(
    league: league_repo.League,
    proposer_team: team_repo.Team,
    counterparty_team: team_repo.Team,
    proposer_player_ids: list[int],
    proposer_pick_ids: list[int],
    counterparty_player_ids: list[int],
    counterparty_pick_ids: list[int],
) -> trade_repo.Trade:
    """
    Validate assets belong to correct teams, create trade record and assets.
    If counterparty is CPU: immediately evaluate and auto-accept/decline.
      - If accepted: status -> 'pending_commissioner'
      - If declined: status -> 'declined', return immediately
    If counterparty is human: status -> 'pending_counterparty'
    """
    if not proposer_player_ids and not proposer_pick_ids:
        raise DBAError("You must include at least one asset in your side of the trade.")
    if not counterparty_player_ids and not counterparty_pick_ids:
        raise DBAError("You must request at least one asset from the other team.")

    pool = await get_pool()

    proposer_players = await _fetch_and_validate_players(
        pool, proposer_player_ids, proposer_team.id, league.id, "your"
    )
    counterparty_players = await _fetch_and_validate_players(
        pool, counterparty_player_ids, counterparty_team.id, league.id, "the other team's"
    )

    proposer_picks = await _fetch_and_validate_picks(
        pool, proposer_pick_ids, proposer_team.id, league.id, "your"
    )
    counterparty_picks = await _fetch_and_validate_picks(
        pool, counterparty_pick_ids, counterparty_team.id, league.id, "the other team's"
    )

    # ── #3: Hard positional-floor check — reject before the trade is finalized ──
    # Nothing previously blocked trading away a team's only center (etc.);
    # _rebalance_starters would just silently fall back to best-remaining-player-
    # regardless-of-position. Checked here (propose time) rather than at approve
    # time so a doomed trade is never even created.
    proposer_roster = await player_repo.get_roster(pool, league.id, proposer_team.id)
    counterparty_roster = await player_repo.get_roster(pool, league.id, counterparty_team.id)
    _proposer_outgoing_ids = {p.id for p in proposer_players}
    _counterparty_outgoing_ids = {p.id for p in counterparty_players}
    _proposer_empty = _empty_positions_after_trade(
        proposer_roster, _proposer_outgoing_ids, counterparty_players
    )
    _counterparty_empty = _empty_positions_after_trade(
        counterparty_roster, _counterparty_outgoing_ids, proposer_players
    )
    if _proposer_empty or _counterparty_empty:
        _violations: list[str] = []
        if _proposer_empty:
            _violations.append(f"{proposer_team.nba_team_code}: {'/'.join(_proposer_empty)}")
        if _counterparty_empty:
            _violations.append(f"{counterparty_team.nba_team_code}: {'/'.join(_counterparty_empty)}")
        raise DBAError(
            "Trade rejected — would leave a team with zero rostered players at a "
            "core position (" + "; ".join(_violations) + ")."
        )
    # ── End positional-floor check ───────────────────────────────────────────────

    trade = await trade_repo.create_trade(
        pool,
        league_id=league.id,
        season=league.current_season,
        proposer_id=proposer_team.id,
        counterparty_id=counterparty_team.id,
    )

    for player in proposer_players:
        await trade_repo.add_asset(
            pool, trade.id,
            from_team_id=proposer_team.id,
            to_team_id=counterparty_team.id,
            asset_type="player",
            player_id=player.id,
        )

    for pick in proposer_picks:
        await trade_repo.add_asset(
            pool, trade.id,
            from_team_id=proposer_team.id,
            to_team_id=counterparty_team.id,
            asset_type="pick",
            pick_id=pick["id"],
        )

    for player in counterparty_players:
        await trade_repo.add_asset(
            pool, trade.id,
            from_team_id=counterparty_team.id,
            to_team_id=proposer_team.id,
            asset_type="player",
            player_id=player.id,
        )

    for pick in counterparty_picks:
        await trade_repo.add_asset(
            pool, trade.id,
            from_team_id=counterparty_team.id,
            to_team_id=proposer_team.id,
            asset_type="pick",
            pick_id=pick["id"],
        )

    if counterparty_team.manager_user_id is None:
        trade = await cpu_trade_evaluation._cpu_evaluate(
            pool, trade, league, proposer_team, counterparty_team,
            proposer_players, proposer_picks, counterparty_players, counterparty_picks,
        )
        return trade

    return trade


async def create_trade_record(
    pool,
    league: league_repo.League,
    proposer_team,
    counterparty_team,
    proposer_player_ids: list[int],
    proposer_pick_ids: list[int],
    counterparty_player_ids: list[int],
    counterparty_pick_ids: list[int],
    initial_status: str = "pending_counterparty",
) -> trade_repo.Trade:
    """
    Create a trade record and its assets without running any validation or CPU evaluation.
    Used internally (e.g. counter-offer generation).  Callers are responsible for ensuring
    all asset ownership is correct before calling this.
    """
    trade = await trade_repo.create_trade(
        pool,
        league_id=league.id,
        season=league.current_season,
        proposer_id=proposer_team.id,
        counterparty_id=counterparty_team.id,
    )
    if initial_status != "pending_counterparty":
        await trade_repo.update_status(pool, trade.id, initial_status)

    for pid in proposer_player_ids:
        await trade_repo.add_asset(
            pool, trade.id,
            from_team_id=proposer_team.id,
            to_team_id=counterparty_team.id,
            asset_type="player",
            player_id=pid,
        )
    for pick_id in proposer_pick_ids:
        await trade_repo.add_asset(
            pool, trade.id,
            from_team_id=proposer_team.id,
            to_team_id=counterparty_team.id,
            asset_type="pick",
            pick_id=pick_id,
        )
    for pid in counterparty_player_ids:
        await trade_repo.add_asset(
            pool, trade.id,
            from_team_id=counterparty_team.id,
            to_team_id=proposer_team.id,
            asset_type="player",
            player_id=pid,
        )
    for pick_id in counterparty_pick_ids:
        await trade_repo.add_asset(
            pool, trade.id,
            from_team_id=counterparty_team.id,
            to_team_id=proposer_team.id,
            asset_type="pick",
            pick_id=pick_id,
        )

    return await trade_repo.get_trade(pool, trade.id)


async def accept(pool, trade_id: int, user_team_id: int) -> trade_repo.Trade:
    """Human counterparty accepts -> status becomes 'pending_commissioner'.
    Also handles counter_offered trades (CPU proposed a counter the human can accept).
    """
    trade = await trade_repo.get_trade(pool, trade_id)
    if not trade:
        raise DBAError(f"Trade #{trade_id} not found.")
    if trade.status not in ("pending_counterparty", "counter_offered"):
        raise DBAError(f"Trade #{trade_id} is not awaiting your response (status: {trade.status}).")
    if trade.counterparty_team_id != user_team_id:
        raise DBAError("You are not the counterparty on this trade.")

    await trade_repo.update_status(pool, trade_id, "pending_commissioner")
    log.info(f"Trade {trade_id} accepted by team {user_team_id}, awaiting commissioner review")
    return await trade_repo.get_trade(pool, trade_id)


async def decline(pool, trade_id: int, user_team_id: int) -> trade_repo.Trade:
    """Human counterparty declines -> status becomes 'declined'.
    Also handles counter_offered trades.
    """
    trade = await trade_repo.get_trade(pool, trade_id)
    if not trade:
        raise DBAError(f"Trade #{trade_id} not found.")
    if trade.status not in ("pending_counterparty", "counter_offered"):
        raise DBAError(f"Trade #{trade_id} is not awaiting your response (status: {trade.status}).")
    if trade.counterparty_team_id != user_team_id:
        raise DBAError("You are not the counterparty on this trade.")

    await trade_repo.update_status(pool, trade_id, "declined")
    log.info(f"Trade {trade_id} declined by team {user_team_id}")
    return await trade_repo.get_trade(pool, trade_id)


async def approve(
    pool,
    trade_id: int,
    commissioner_id: int,
) -> tuple[trade_repo.Trade, dict]:
    """
    Commissioner approves. Execute the trade:
    - Move players between teams (UPDATE players SET team_id)
    - Move pick ownership (UPDATE draft_picks SET current_team_id)
    - Update contracts' team_id
    - Mark trade as 'approved', set resolved_at

    Returns (trade, grade_info) where grade_info has keys:
        grade_a, grade_b, score_a, score_b, rationale
    grade_a applies to the proposer team; grade_b to the counterparty team.
    """
    trade = await trade_repo.get_trade(pool, trade_id)
    if not trade:
        raise DBAError(f"Trade #{trade_id} not found.")
    if trade.status != "pending_commissioner":
        raise DBAError(f"Trade #{trade_id} is not pending commissioner review (status: {trade.status}).")

    assets = await trade_repo.get_assets(pool, trade_id)

    league_row = await pool.fetchrow("SELECT salary_cap, current_season FROM leagues WHERE id = $1", trade.league_id)
    salary_cap: int = league_row["salary_cap"] if league_row else 140_000_000
    current_season: int = league_row["current_season"] if league_row else trade.season

    proposer_player_dicts: list[dict] = []
    proposer_pick_dicts: list[dict] = []
    counterparty_player_dicts: list[dict] = []
    counterparty_pick_dicts: list[dict] = []

    for asset in assets:
        if asset.asset_type == "player" and asset.player_id:
            p_row = await pool.fetchrow("SELECT * FROM players WHERE id = $1", asset.player_id)
            c_row = await pool.fetchrow(
                "SELECT salary, years_remaining FROM contracts WHERE player_id = $1 AND is_active = TRUE LIMIT 1",
                asset.player_id,
            )
            if p_row:
                birth = p_row["birth_date"]
                if birth:
                    today = datetime.date.today()
                    age = today.year - birth.year
                    if (today.month, today.day) < (birth.month, birth.day):
                        age -= 1
                else:
                    age = 28
                player_d = {
                    "player": {
                        "full_name": f"{p_row['first_name']} {p_row['last_name']}",
                        "position": p_row["position"],
                        "overall": p_row["overall"],
                        "age": age,
                    },
                    "contract": {
                        "salary": c_row["salary"] if c_row else 0,
                        "years_remaining": c_row["years_remaining"] if c_row else 1,
                    },
                }
                if asset.from_team_id == trade.proposer_team_id:
                    proposer_player_dicts.append(player_d)
                else:
                    counterparty_player_dicts.append(player_d)
        elif asset.asset_type == "pick" and asset.pick_id:
            pk_row = await pool.fetchrow("SELECT season, round FROM draft_picks WHERE id = $1", asset.pick_id)
            if pk_row:
                pick_d = {"season": pk_row["season"], "round": pk_row["round"]}
                if asset.from_team_id == trade.proposer_team_id:
                    proposer_pick_dicts.append(pick_d)
                else:
                    counterparty_pick_dicts.append(pick_d)

    evaluation = trade_grading.evaluate_trade(
        side_a_players=[{"player": d["player"], "contract": d["contract"]} for d in proposer_player_dicts],
        side_a_picks=proposer_pick_dicts,
        side_b_players=[{"player": d["player"], "contract": d["contract"]} for d in counterparty_player_dicts],
        side_b_picks=counterparty_pick_dicts,
        salary_cap=salary_cap,
        current_season=current_season,
    )

    grade_a, grade_b = trade_grading.grade_trade(evaluation["score_a"], evaluation["score_b"])
    grade_info = {
        "grade_a": grade_a,
        "grade_b": grade_b,
        "score_a": evaluation["score_a"],
        "score_b": evaluation["score_b"],
        "rationale": evaluation["rationale"],
        # Enriched player lists for AI reasoning assembly in the cog.
        # Each entry: {full_name, position, overall, age}
        "players_a": [d["player"] for d in proposer_player_dicts],
        "players_b": [d["player"] for d in counterparty_player_dicts],
    }

    sim_date = await pool.fetchval(
        "SELECT MAX(scheduled_date) FROM games WHERE league_id = $1 AND status = 'simmed'",
        trade.league_id,
    )
    if sim_date is None:
        sim_date = datetime.date.today()

    # Collect all teams that had player movement so we can re-derive roles after.
    affected_team_ids: set[int] = {
        tid
        for a in assets
        if a.asset_type == "player" and a.player_id
        for tid in (a.from_team_id, a.to_team_id)
    }

    async with pool.acquire() as conn:
        async with conn.transaction():
            receiving_team_ids: set[int] = set()
            for asset in assets:
                if asset.asset_type == "player" and asset.player_id:
                    await conn.execute(
                        "UPDATE players SET team_id = $1, last_traded_at = $2 WHERE id = $3",
                        asset.to_team_id,
                        sim_date,
                        asset.player_id,
                    )
                    await conn.execute(
                        "UPDATE contracts SET team_id = $1 WHERE player_id = $2 AND is_active = TRUE",
                        asset.to_team_id,
                        asset.player_id,
                    )
                    # Remove from old team's lineup so the sim engine doesn't keep
                    # playing them for the wrong team after the trade clears.
                    await conn.execute(
                        "DELETE FROM lineups WHERE league_id = $1 AND player_id = $2",
                        trade.league_id,
                        asset.player_id,
                    )
                    # Insert into new team's lineup at the next open slot.
                    next_slot = await conn.fetchval(
                        "SELECT COALESCE(MAX(slot), 0) + 1 FROM lineups "
                        "WHERE league_id = $1 AND team_id = $2",
                        trade.league_id,
                        asset.to_team_id,
                    )
                    await conn.execute(
                        """INSERT INTO lineups
                               (league_id, team_id, is_starter, slot, player_id, set_by)
                           VALUES ($1, $2, FALSE, $3, $4, NULL)
                           ON CONFLICT (league_id, team_id, slot) DO NOTHING""",
                        trade.league_id,
                        asset.to_team_id,
                        next_slot,
                        asset.player_id,
                    )
                    receiving_team_ids.add(asset.to_team_id)
                elif asset.asset_type == "pick" and asset.pick_id:
                    await conn.execute(
                        "UPDATE draft_picks SET current_team_id = $1 WHERE id = $2",
                        asset.to_team_id,
                        asset.pick_id,
                    )

            # Rebalance starters for every team that received a player so stars
            # don't get stuck on the bench after a trade.
            for tid in receiving_team_ids:
                await _rebalance_starters(conn, trade.league_id, tid)

            await conn.execute(
                """
                UPDATE trades
                SET status = 'approved',
                    commissioner_action_by = $1,
                    resolved_at = NOW()
                WHERE id = $2
                """,
                commissioner_id,
                trade_id,
            )

    traded_player_ids = [
        a.player_id for a in assets if a.asset_type == "player" and a.player_id
    ]
    if traded_player_ids:
        await trade_block_repo.remove_players_from_block(pool, trade.league_id, traded_player_ids)

    # Invalidate role cache and re-derive for every team that gained or lost a player.
    # Must run after the transaction commits so the new lineups are visible to derive_roles.
    if affected_team_ids:
        from services import role_service
        from services.sim_persistence import invalidate_role_cache
        async with pool.acquire() as _conn:
            for tid in affected_team_ids:
                invalidate_role_cache(trade.league_id, tid, current_season)
                await role_service.derive_and_persist_all_for_team(
                    _conn, trade.league_id, tid, current_season,
                    silent_emit=True,
                )
        log.debug(
            "Trade %d: re-derived roles for teams %s (season %d)",
            trade_id, sorted(affected_team_ids), current_season,
        )

    log.info(f"Trade {trade_id} approved by commissioner {commissioner_id}")
    return await trade_repo.get_trade(pool, trade_id), grade_info


async def veto(pool, trade_id: int, commissioner_id: int, reason: str) -> trade_repo.Trade:
    """Commissioner vetoes. Status -> 'vetoed'. No asset movement."""
    trade = await trade_repo.get_trade(pool, trade_id)
    if not trade:
        raise DBAError(f"Trade #{trade_id} not found.")
    if trade.status != "pending_commissioner":
        raise DBAError(f"Trade #{trade_id} is not pending commissioner review (status: {trade.status}).")

    await trade_repo.update_status(pool, trade_id, "vetoed", commissioner_by=commissioner_id, reason=reason)
    log.info(f"Trade {trade_id} vetoed by commissioner {commissioner_id}: {reason}")
    return await trade_repo.get_trade(pool, trade_id)


async def get_pending_queue(pool, league_id: int) -> list[dict]:
    """Returns list of {trade, assets, proposer_team, counterparty_team} dicts."""
    trades = await trade_repo.get_pending_for_league(pool, league_id)
    result = []
    for trade in trades:
        assets = await trade_repo.get_assets(pool, trade.id)
        proposer_team = await team_repo.get_by_id(pool, trade.proposer_team_id)
        counterparty_team = await team_repo.get_by_id(pool, trade.counterparty_team_id)
        result.append({
            "trade": trade,
            "assets": assets,
            "proposer_team": proposer_team,
            "counterparty_team": counterparty_team,
        })
    return result


_CORE_POSITIONS: tuple[str, ...] = ("PG", "SG", "SF", "PF", "C")


def _empty_positions_after_trade(
    current_roster: list[player_repo.Player],
    outgoing_player_ids: set[int],
    incoming_players: list[player_repo.Player],
) -> list[str]:
    """#3: Return the core positions (PG/SG/SF/PF/C) a team would have ZERO
    rostered players at after this trade — current roster minus outgoing plus
    incoming. Nothing previously enforced a roster-shape floor; trade_service
    (and _rebalance_starters downstream of it) would silently fall back to
    "best remaining player regardless of position" rather than the trade being
    blocked. Empty list means the trade is safe for this team.
    """
    counts: dict[str, int] = dict.fromkeys(_CORE_POSITIONS, 0)
    for p in current_roster:
        if p.id in outgoing_player_ids:
            continue
        if p.position in counts:
            counts[p.position] += 1
    for p in incoming_players:
        if p.position in counts:
            counts[p.position] += 1
    return [pos for pos in _CORE_POSITIONS if counts[pos] == 0]


async def _fetch_and_validate_players(
    pool,
    player_ids: list[int],
    expected_team_id: int,
    league_id: int,
    label: str,
) -> list[player_repo.Player]:
    players = []
    for pid in player_ids:
        player = await player_repo.get_by_id(pool, pid)
        if not player:
            raise DBAError(f"Player ID {pid} not found.")
        if player.league_id != league_id:
            raise DBAError(f"Player ID {pid} does not belong to this league.")
        if player.team_id != expected_team_id:
            raise DBAError(f"Player ID {pid} is not on {label} roster.")
        players.append(player)
    return players


async def _fetch_and_validate_picks(
    pool,
    pick_ids: list[int],
    expected_team_id: int,
    league_id: int,
    label: str,
) -> list[dict]:
    picks = []
    for pick_id in pick_ids:
        row = await pool.fetchrow("SELECT * FROM draft_picks WHERE id = $1", pick_id)
        if not row:
            raise DBAError(f"Pick ID {pick_id} not found.")
        if row["league_id"] != league_id:
            raise DBAError(f"Pick ID {pick_id} does not belong to this league.")
        if row["current_team_id"] != expected_team_id:
            raise DBAError(f"Pick ID {pick_id} is not currently owned by {label} team.")
        if row["used_for_player_id"] is not None:
            raise DBAError(f"Pick ID {pick_id} has already been used.")
        picks.append(dict(row))
    return picks
