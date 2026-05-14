from __future__ import annotations

import datetime

from core.errors import DBAError
from core.logging import get_logger
from data.db import get_pool
from data.repositories import league_repo, player_repo, team_repo, trade_block_repo, trade_repo
from services import trade_evaluator

log = get_logger(__name__)


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
        trade = await _cpu_evaluate(
            pool, trade, league, proposer_team, counterparty_team,
            proposer_players, proposer_picks, counterparty_players, counterparty_picks,
        )
        return trade

    return trade


async def _cpu_evaluate(
    pool,
    trade: trade_repo.Trade,
    league: league_repo.League,
    proposer_team: team_repo.Team,
    counterparty_team: team_repo.Team,
    proposer_players,
    proposer_picks: list[dict],
    counterparty_players,
    counterparty_picks: list[dict],
) -> trade_repo.Trade:
    """Run the CPU evaluator and update trade status accordingly."""
    def _player_age(player: player_repo.Player) -> int:
        if player.birth_date:
            today = datetime.date.today()
            age = today.year - player.birth_date.year
            if (today.month, today.day) < (player.birth_date.month, player.birth_date.day):
                age -= 1
            return age
        return 28

    async def _build_player_dicts(players, team_id: int) -> list[dict]:
        result = []
        for p in players:
            contract = await player_repo.get_active_contract(pool, p.id)
            result.append({
                "asset_type": "player",
                "player": {
                    "overall": p.overall,
                    "age": _player_age(p),
                },
                "contract": {
                    "salary": contract.salary if contract else 0,
                    "years_remaining": contract.years_remaining if contract else 1,
                },
            })
        return result

    def _build_pick_dicts(picks: list[dict]) -> list[dict]:
        return [
            {
                "asset_type": "pick",
                "pick": {"round": p["round"]},
                "season": p["season"],
                "round": p["round"],
            }
            for p in picks
        ]

    proposer_asset_dicts = await _build_player_dicts(proposer_players, proposer_team.id)
    proposer_pick_dicts = _build_pick_dicts(proposer_picks)
    counterparty_asset_dicts = await _build_player_dicts(counterparty_players, counterparty_team.id)
    counterparty_pick_dicts = _build_pick_dicts(counterparty_picks)

    # From CPU perspective: side_a = what CPU receives (proposer gives), side_b = what CPU gives
    evaluation = trade_evaluator.evaluate_trade(
        side_a_players=[{"player": d["player"], "contract": d["contract"]} for d in proposer_asset_dicts],
        side_a_picks=proposer_pick_dicts,
        side_b_players=[{"player": d["player"], "contract": d["contract"]} for d in counterparty_asset_dicts],
        side_b_picks=counterparty_pick_dicts,
        salary_cap=league.salary_cap,
        current_season=league.current_season,
    )

    cpu_cap_used = await player_repo.get_team_cap_usage(pool, league.id, counterparty_team.id)

    assets_receiving = proposer_asset_dicts + proposer_pick_dicts
    assets_giving = counterparty_asset_dicts + counterparty_pick_dicts

    accept, reason = trade_evaluator.cpu_should_accept(
        cpu_team_mode=counterparty_team.cpu_mode,
        assets_receiving=assets_receiving,
        assets_giving=assets_giving,
        evaluation=evaluation,
        salary_cap=league.salary_cap,
        current_cap_used=cpu_cap_used,
    )

    cpu_score = evaluation["score_a"] - evaluation["score_b"]
    await trade_repo.set_cpu_evaluation(pool, trade.id, cpu_score, reason)

    if accept:
        await trade_repo.update_status(pool, trade.id, "pending_commissioner")
        log.info(f"CPU auto-accepted trade {trade.id}: {reason}")
    else:
        await trade_repo.update_status(pool, trade.id, "declined")
        log.info(f"CPU auto-declined trade {trade.id}: {reason}")

    updated = await trade_repo.get_trade(pool, trade.id)
    return updated


async def accept(pool, trade_id: int, user_team_id: int) -> trade_repo.Trade:
    """Human counterparty accepts -> status becomes 'pending_commissioner'."""
    trade = await trade_repo.get_trade(pool, trade_id)
    if not trade:
        raise DBAError(f"Trade #{trade_id} not found.")
    if trade.status != "pending_counterparty":
        raise DBAError(f"Trade #{trade_id} is not awaiting your response (status: {trade.status}).")
    if trade.counterparty_team_id != user_team_id:
        raise DBAError("You are not the counterparty on this trade.")

    await trade_repo.update_status(pool, trade_id, "pending_commissioner")
    log.info(f"Trade {trade_id} accepted by team {user_team_id}, awaiting commissioner review")
    return await trade_repo.get_trade(pool, trade_id)


async def decline(pool, trade_id: int, user_team_id: int) -> trade_repo.Trade:
    """Human counterparty declines -> status becomes 'declined'."""
    trade = await trade_repo.get_trade(pool, trade_id)
    if not trade:
        raise DBAError(f"Trade #{trade_id} not found.")
    if trade.status != "pending_counterparty":
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

    evaluation = trade_evaluator.evaluate_trade(
        side_a_players=[{"player": d["player"], "contract": d["contract"]} for d in proposer_player_dicts],
        side_a_picks=proposer_pick_dicts,
        side_b_players=[{"player": d["player"], "contract": d["contract"]} for d in counterparty_player_dicts],
        side_b_picks=counterparty_pick_dicts,
        salary_cap=salary_cap,
        current_season=current_season,
    )

    grade_a, grade_b = trade_evaluator.grade_trade(evaluation["score_a"], evaluation["score_b"])
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

    async with pool.acquire() as conn:
        async with conn.transaction():
            for asset in assets:
                if asset.asset_type == "player" and asset.player_id:
                    await conn.execute(
                        "UPDATE players SET team_id = $1 WHERE id = $2",
                        asset.to_team_id,
                        asset.player_id,
                    )
                    await conn.execute(
                        "UPDATE contracts SET team_id = $1 WHERE player_id = $2 AND is_active = TRUE",
                        asset.to_team_id,
                        asset.player_id,
                    )
                elif asset.asset_type == "pick" and asset.pick_id:
                    await conn.execute(
                        "UPDATE draft_picks SET current_team_id = $1 WHERE id = $2",
                        asset.to_team_id,
                        asset.pick_id,
                    )

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
