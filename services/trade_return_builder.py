"""Builds the speculative return package offered back to a trade-block seller.

Ranks counterparties for a surplus player, then assembles a value-matched
return (players + picks) from a target team's roster. Extracted from
cpu_trade_proposal_runner.py — DB-read-heavy, no discord.
"""
from __future__ import annotations

from core.logging import get_logger
from data.repositories import league_repo, player_repo, team_repo, trade_repo
from services import trade_value_math
from services.cpu_trade_posture import _player_age, is_cornerstone
from services.trade_proposal_scoring import _fill_to_value

log = get_logger(__name__)


async def _score_counterparty_for_target(
    pool,
    league_id: int,
    season: int,
    target_player: player_repo.Player,
    target_contract: object | None,
    counterparty_team_id: int,
    counterparty_plan: dict | None,
    counterparty_context: dict,
    salary_cap: int,
    offerer_asset_targets: list[str],
    cp_r1_count: int,
) -> float:
    """Score how attractive a counterparty is for acquiring `target_player`.

    Returns a score (higher = better counterparty to call first).

    Composed of:
    - Base: team-specific value of target to the counterparty
    - Plan-fit multiplier: how well the player's age/OVR fits the counterparty's goal
    - Asset-availability bonus: does the counterparty have assets the offerer wants?

    Returns 0.0 if the counterparty's plan marks the target as clearly unwanted
    (e.g., tank team being offered a star who would win too many games).
    """
    target_age = _player_age(target_player) or 27
    target_ovr = target_player.overall
    cp_goal = (counterparty_plan or {}).get("goal", "transition")

    # Build a minimal contract dict if the object is a Namespace/row.
    if target_contract is not None:
        _salary = getattr(target_contract, "salary", 0) or 0
        _yrs = getattr(target_contract, "years_remaining", 1) or 1
    else:
        _salary = 0
        _yrs = 1
    contract_dict = {"salary": _salary, "years_remaining": _yrs}
    player_dict = {
        "overall": target_ovr,
        "age": target_age,
        "position": target_player.position,
    }

    # Base: what the player is worth to the receiving team specifically.
    base = trade_value_math.player_team_specific_value(
        player_dict,
        contract_dict,
        counterparty_context,
        salary_cap,
    )

    # Plan-fit multiplier — applied to base score.
    fit_mult = 1.0
    if cp_goal == "win_now":
        if 25 <= target_age <= 32 and target_ovr >= 80:
            fit_mult = 1.20   # proven veteran in the window
        elif target_age <= 22:
            fit_mult = 0.85   # young player doesn't help win now
    elif cp_goal == "rebuild":
        if target_age >= 29:
            fit_mult = 0.60   # vet doesn't fit timeline
        elif target_age <= 23:
            fit_mult = 1.25   # young asset, perfect fit
    elif cp_goal == "tank":
        if target_ovr >= 86:
            return 0.0        # star sabotages the tank — hard disqualification
        if target_age <= 22:
            fit_mult = 1.30   # young upside, future star
    # transition → fit_mult stays 1.0 (no strong preference)

    score = base * fit_mult

    # Asset-availability bonus: does this counterparty have what WE want?
    if "picks_r1" in offerer_asset_targets and cp_r1_count >= 3:
        score *= 1.15

    return max(0.0, round(score, 3))


async def _league_scan_counterparties(
    pool,
    league_id: int,
    season: int,
    surplus_player: player_repo.Player,
    surplus_contract: object | None,
    offerer_team_id: int,
    offerer_asset_targets: list[str],
    cpu_teams: list[team_repo.Team],
    cp_plans: dict[int, dict | None],
    cp_contexts: dict[int, dict],
    cp_r1_counts: dict[int, int],
    salary_cap: int,
) -> list[tuple[team_repo.Team, float, str]]:
    """Rank counterparties for a specific surplus player using _score_counterparty_for_target.

    Returns up to 3 (team, score, reason_str) tuples sorted descending by score.
    Falls back to the full unranked list on any error so the caller can degrade gracefully.
    """
    results: list[tuple[team_repo.Team, float]] = []
    for cp_team in cpu_teams:
        if cp_team.id == offerer_team_id:
            continue
        try:
            score = await _score_counterparty_for_target(
                pool=pool,
                league_id=league_id,
                season=season,
                target_player=surplus_player,
                target_contract=surplus_contract,
                counterparty_team_id=cp_team.id,
                counterparty_plan=cp_plans.get(cp_team.id),
                counterparty_context=cp_contexts.get(cp_team.id, {
                    "team_id": cp_team.id, "mode": "developing",
                    "current_payroll": 0, "position_counts": {},
                }),
                salary_cap=salary_cap,
                offerer_asset_targets=offerer_asset_targets,
                cp_r1_count=cp_r1_counts.get(cp_team.id, 0),
            )
            results.append((cp_team, score))
        except Exception as exc:
            log.debug(
                "_score_counterparty_for_target failed for team %d player %d: %s",
                cp_team.id, surplus_player.id, exc,
            )

    results.sort(key=lambda x: x[1], reverse=True)
    top3 = results[:3]

    # Build readable reason strings for headless output.
    annotated: list[tuple[team_repo.Team, float, str]] = []
    for cp_team, score in top3:
        cp_goal = (cp_plans.get(cp_team.id) or {}).get("goal", "transition")
        annotated.append((cp_team, score, f"{cp_team.nba_team_code} {cp_goal} {score:.2f}"))

    return annotated


async def _derive_return_from_b(
    pool,
    league,
    team_b,
    outgoing_player,
    asset_targets_a: list[str],
    taken_player_ids: set[int],
    recently_signed_ids: set[int],
    plan_b: dict | None,
    posture_b: str,
    cp_contexts: dict,
    cp_r1_counts: dict,
) -> tuple[list[int], list[int], float, dict] | None:
    """Derive what team B would plausibly send in exchange for outgoing_player.

    Logical inverse of _build_return_package: given that B is the receiver, what
    would B send?  Same surplus-first ordering, same 25% tolerance.  Target value
    is the team-specific value of outgoing_player to B (how much B values the player).

    Returns (player_ids, pick_ids, total_value, contracts_map) or None if B can't
    build a package.  contracts_map maps each returned player_id to its active
    Contract object (or None) so callers can pass real contracts to scoring.

    asset_targets_a: A's asset targets — biases B's selection toward what A wants.
    """
    # Target value: what the outgoing player is worth to team B.
    outgoing_contract = await player_repo.get_active_contract(pool, outgoing_player.id)
    _ctx_b = cp_contexts.get(team_b.id, {
        "team_id": team_b.id, "mode": "developing",
        "current_payroll": 0, "position_counts": {},
    })
    target_value = trade_value_math.player_team_specific_value(
        {
            "overall": outgoing_player.overall,
            "age": _player_age(outgoing_player) or 27,
            "position": outgoing_player.position,
        },
        {
            "salary": outgoing_contract.salary if outgoing_contract else 0,
            "years_remaining": outgoing_contract.years_remaining if outgoing_contract else 1,
        },
        _ctx_b,
        league.salary_cap,
    )
    if target_value <= 0:
        return None

    tolerance = target_value * 0.25

    # Build B's outgoing pool from their plan (surplus + flex, no core).
    _plan_b_core: set[int] = set((plan_b or {}).get("core_player_ids") or [])
    _plan_b_surplus: set[int] = set((plan_b or {}).get("surplus_player_ids") or [])
    _plan_b_flex: set[int] = set((plan_b or {}).get("flex_player_ids") or [])

    b_roster = await player_repo.get_roster(pool, league.id, team_b.id)
    full_roster_b = b_roster  # for is_cornerstone check

    scored_players: list[tuple[int, float, int]] = []
    # contracts_by_id: retain fetched contracts so _score_outgoing_pair can use
    # real salary/years_remaining instead of the synthetic {salary:0, years:1} shim.
    _contracts_by_id: dict[int, player_repo.Contract | None] = {}
    for p in b_roster:
        if p.id in taken_player_ids or p.id in recently_signed_ids:
            continue
        if p.team_id != team_b.id:
            continue
        # Never send core players.
        if p.id in _plan_b_core:
            continue
        # Cornerstone backstop.
        if is_cornerstone(team_b, p, full_roster_b):
            continue

        contract = await player_repo.get_active_contract(pool, p.id)
        _contracts_by_id[p.id] = contract
        v = trade_value_math.player_trade_value(
            {"overall": p.overall, "age": _player_age(p)},
            {
                "salary": contract.salary if contract else 0,
                "years_remaining": contract.years_remaining if contract else 1,
            },
            league.salary_cap,
        )

        # Bucket: surplus first, flex second, unlisted last.
        if p.id in _plan_b_surplus:
            bucket = 0
        elif p.id in _plan_b_flex:
            bucket = 1
        else:
            bucket = 2

        # Bias toward what A's asset_targets want.
        _age_p = _player_age(p)
        bias_boost = 1.0
        if asset_targets_a:
            if "role_players" in asset_targets_a and 78 <= p.overall <= 85:
                bias_boost += 0.10
            if "veterans" in asset_targets_a and _age_p is not None and _age_p >= 28 and p.overall >= 78:
                bias_boost += 0.10
            if "young_u23" in asset_targets_a and _age_p is not None and _age_p < 23:
                bias_boost += 0.10
        # Apply bias as a value boost (affects sort order only, not bucket).
        scored_players.append((bucket, v * bias_boost, p.id))

    # Sort: surplus before flex before unlisted; highest value first within bucket.
    scored_players.sort(key=lambda x: (x[0], -x[1]))

    # B's picks.
    all_picks_b = await trade_repo.get_team_picks(pool, league.id, team_b.id)
    r2_picks = sorted([pk for pk in all_picks_b if pk["round"] == 2], key=lambda pk: pk["season"])
    r1_picks = sorted([pk for pk in all_picks_b if pk["round"] == 1], key=lambda pk: pk["season"])

    # B's pick willingness (mirrors _build_return_package logic).
    mode_b = team_b.cpu_mode or "default"
    if mode_b == "rebuilding":
        max_picks = 1
        sorted_picks = r2_picks + r1_picks
    elif mode_b == "soft_rebuild":
        max_picks = 2
        sorted_picks = r2_picks + r1_picks
    elif mode_b == "contending":
        max_picks = 3
        sorted_picks = r2_picks + r1_picks
    else:
        max_picks = 2
        sorted_picks = r2_picks + r1_picks

    # Bias toward picks when A wants picks.
    if asset_targets_a:
        if "picks_r1" in asset_targets_a:
            sorted_picks = r1_picks + r2_picks  # prefer r1 for A's desire
            max_picks = max(max_picks, 2)
        elif "picks_any" in asset_targets_a:
            max_picks = max(max_picks, 2)

    # Plan goal override for B.
    if plan_b is not None:
        _plan_goal_b = plan_b.get("goal", "")
        if _plan_goal_b == "win_now":
            max_picks = max(max_picks, 3)
        elif _plan_goal_b in ("tank", "rebuild"):
            max_picks = min(max_picks, 1)
            sorted_picks = r2_picks + r1_picks

    # Pre-fetch win% for pick valuation.
    orig_team_ids_b = list({pk["original_team_id"] for pk in sorted_picks if pk.get("original_team_id")})
    _orig_win_pct_b: dict[int, float | None] = {}
    if orig_team_ids_b:
        _wp_rows_b = await pool.fetch(
            """SELECT team_id,
                      CASE WHEN (wins + losses) > 0
                           THEN wins::float / (wins + losses)
                           ELSE NULL END AS win_pct
               FROM standings_cache
               WHERE league_id = $1 AND season = $2 AND team_id = ANY($3)""",
            league.id, league.current_season, orig_team_ids_b,
        )
        _orig_win_pct_b = {r["team_id"]: r["win_pct"] for r in _wp_rows_b}

    # Fill.
    player_ids, pick_ids, achieved_value = _fill_to_value(
        scored_players=scored_players,
        sorted_picks=sorted_picks,
        target_value=target_value,
        max_picks=max_picks,
        tolerance=tolerance,
        orig_win_pct=_orig_win_pct_b,
        current_season=league.current_season,
        team_id=team_b.id,
        mode=mode_b,
        counterparty_mode="developing",  # A's mode not needed here; gap-filling only
    )

    if not player_ids and not pick_ids:
        return None

    # Build contracts_map for only the player IDs actually selected.
    _ret_contracts: dict[int, player_repo.Contract | None] = {
        pid: _contracts_by_id.get(pid)
        for pid in player_ids
    }
    return player_ids, pick_ids, achieved_value, _ret_contracts


async def _build_return_package(
    pool,
    league: league_repo.League,
    team_a: team_repo.Team,
    block_player_ids: list[int],
    target_value: float,
    taken_player_ids: set[int],
    recently_signed_ids: set[int] | None = None,
    counterparty_mode: str = "default",
    plan_a: dict | None = None,
) -> tuple[list[int], list[int], float]:
    """
    Build a return package from team A's trade block players and picks that
    roughly matches target_value (within 25%).

    Returns (player_ids, pick_ids, total_value).
    Prefer picks over players when possible to keep rosters intact.
    taken_player_ids prevents double-offering players committed to another offer
    in the same round.
    recently_signed_ids enforces the 60-day sign-and-trade restriction.

    plan_a (Phase 2): when provided, player ordering is surplus-first, then flex,
    and core players are never included even if mathematically needed.  The trade
    falls short rather than violating the franchise plan.  Falls back to pure
    value-descending sort when plan_a is absent.
    """
    if recently_signed_ids is None:
        recently_signed_ids = set()

    tolerance = target_value * 0.25

    # Load full roster once so is_cornerstone can compare within-team OVR rank.
    full_roster = await player_repo.get_roster(pool, league.id, team_a.id)

    # Plan-driven ID sets (empty when plan unavailable — falls back to cornerstone-only).
    _plan_core_set: set[int] = set((plan_a or {}).get("core_player_ids") or [])
    _plan_surplus_set: set[int] = set((plan_a or {}).get("surplus_player_ids") or [])
    _plan_flex_set: set[int] = set((plan_a or {}).get("flex_player_ids") or [])

    # Load and score A's own trade block players.
    # Each entry: (bucket_rank, value_desc, pid)
    #   bucket_rank: 0 = surplus (offer first), 1 = flex (offer second), 2 = other
    scored_players: list[tuple[int, float, int]] = []
    for pid in block_player_ids:
        # Skip players already committed to another offer this round.
        if pid in taken_player_ids:
            continue
        # Skip recently-signed players (60-day restriction).
        if pid in recently_signed_ids:
            continue
        p = await player_repo.get_by_id(pool, pid)
        if not p or p.team_id != team_a.id:
            continue  # player was already traded away or belongs to another team

        # Plan core protection — never include, plan is primary.
        if plan_a is not None and pid in _plan_core_set:
            log.info(
                "[CPU] plan-core skipped from return package: "
                "%s %s OVR %d (team %d, goal=%s)",
                p.first_name, p.last_name, p.overall, team_a.id,
                plan_a.get("goal", "?"),
            )
            continue

        # Cornerstone protection — backstop when no plan or OVR 92+.
        if is_cornerstone(team_a, p, full_roster):
            log.info(
                f"[CPU] cornerstone skipped from return package: "
                f"{p.first_name} {p.last_name} OVR {p.overall} "
                f"(team {team_a.id}, mode={team_a.cpu_mode})"
            )
            continue

        contract = await player_repo.get_active_contract(pool, p.id)
        v = trade_value_math.player_trade_value(
            {"overall": p.overall, "age": _player_age(p)},
            {
                "salary": contract.salary if contract else 0,
                "years_remaining": contract.years_remaining if contract else 1,
            },
            league.salary_cap,
        )
        # Plan bucket ordering: surplus first, flex second, uncategorised last.
        if plan_a is not None:
            if pid in _plan_surplus_set:
                bucket = 0
            elif pid in _plan_flex_set:
                bucket = 1
            else:
                bucket = 2
        else:
            bucket = 2  # no plan — treat everything as uncategorised

        scored_players.append((bucket, v, pid))

    # Sort: surplus before flex before uncategorised; within bucket highest value first.
    scored_players.sort(key=lambda x: (x[0], -x[1]))

    # Load A's available picks: round 2 first, then round 1 (protect higher picks).
    all_picks = await trade_repo.get_team_picks(pool, league.id, team_a.id)
    r2_picks = [p for p in all_picks if p["round"] == 2]
    r1_picks = [p for p in all_picks if p["round"] == 1]
    # Nearest season picks first (most concrete value).
    r2_picks.sort(key=lambda p: p["season"])
    r1_picks.sort(key=lambda p: p["season"])

    # Hard cap of 3 picks per trade (1st + 2nd combined).
    # posture mode governs willingness to include picks:
    #   rebuilding   — picks are their future; offer at most 1, prefer 2nds over 1sts.
    #   soft_rebuild — transitional; offer up to 2 but protect near-term 1sts.
    #   contending   — willing to deal picks for immediate help; up to 3.
    #   play_in_fringe — capped at 2; balanced approach.
    #   developing / default — balanced; up to 2 picks.
    # Note: counterparty_mode drives strategic pick inclusion for rebuilder targets.
    # Phase 2: plan goal can override mode-based pick willingness:
    #   win_now plan → willing to ship picks (matches contending behaviour)
    #   tank/rebuild plan → hoard picks (cap at 1, 2nds preferred)
    # Use team_a's own posture for pick willingness (not counterparty mode).
    # counterparty_mode is only for strategic pick inclusion logic below.
    mode = team_a.cpu_mode or "default"
    if mode == "rebuilding":
        max_picks = 1
        # Offer 2nd-rounders first; only expose 1sts as a last resort.
        sorted_picks = r2_picks + r1_picks
    elif mode == "soft_rebuild":
        max_picks = 2
        sorted_picks = r2_picks + r1_picks
    elif mode == "contending":
        max_picks = 3
        sorted_picks = r2_picks + r1_picks
    elif mode == "play_in_fringe":
        max_picks = 2
        sorted_picks = r2_picks + r1_picks
    else:
        # developing / default
        max_picks = 2
        sorted_picks = r2_picks + r1_picks

    # Phase 2 plan override: franchise goal takes precedence over cpu_mode for
    # pick willingness.  win_now teams pursue titles now and can send picks;
    # tank/rebuild teams are building for the future and hoard picks.
    if plan_a is not None:
        _plan_goal = plan_a.get("goal", "")
        if _plan_goal == "win_now":
            # Override upward: willing to ship picks like a contending team.
            max_picks = max(max_picks, 3)
        elif _plan_goal in ("tank", "rebuild"):
            # Override downward: protect picks, offer at most 1 and prefer 2nds.
            max_picks = min(max_picks, 1)
            sorted_picks = r2_picks + r1_picks  # 2nds first regardless of prior sort

    # Pre-fetch win% for all original team IDs so pick_trade_value can discount by record.
    orig_team_ids = list({p["original_team_id"] for p in sorted_picks if p.get("original_team_id")})
    _orig_win_pct: dict[int, float | None] = {}
    if orig_team_ids:
        _wp_rows = await pool.fetch(
            """SELECT team_id,
                      CASE WHEN (wins + losses) > 0
                           THEN wins::float / (wins + losses)
                           ELSE NULL END AS win_pct
               FROM standings_cache
               WHERE league_id = $1 AND season = $2 AND team_id = ANY($3)""",
            league.id, league.current_season, orig_team_ids,
        )
        _orig_win_pct = {r["team_id"]: r["win_pct"] for r in _wp_rows}

    offer_player_ids, offer_pick_ids, accumulated = _fill_to_value(
        scored_players=scored_players,
        sorted_picks=sorted_picks,
        target_value=target_value,
        max_picks=max_picks,
        tolerance=tolerance,
        orig_win_pct=_orig_win_pct,
        current_season=league.current_season,
        team_id=team_a.id,
        mode=mode,
        counterparty_mode=counterparty_mode,
    )

    return offer_player_ids, offer_pick_ids, accumulated
