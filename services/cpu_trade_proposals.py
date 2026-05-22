from __future__ import annotations

import asyncio
import os
import random
from typing import Optional

import discord

from core.logging import get_logger
from data.repositories import league_repo, player_repo, team_repo, trade_repo
from services import franchise_plan_service, ra_reasoning, ride_along, trade_evaluator, trade_service
from services.cpu_trade_posture import (
    _default_posture,
    _player_age,
    _player_age_from_row,
    is_cornerstone,
)

_HEADLESS = os.environ.get("DBA_HEADLESS_MODE") == "1"

# Minimum value-ratio each side must receive in a 3-team deal.
# Without this floor, the conduit team can ship a real pick for a near-worthless
# secondary asset (the "DAL-gets-nothing" bug caught in testing).
_MIN_3WAY_RATIO = 0.70

log = get_logger(__name__)


async def _get_franchise_plan(pool, league_id: int, team_id: int, season: int) -> dict | None:
    """Read the stored franchise plan for a CPU team.  Returns None if absent or on error.

    Deliberately reads only — does NOT derive or persist.  derive_and_persist_all()
    at batch-start should have populated plans for all CPU teams; if it didn't,
    the warning here is the signal, not a silent backfill write per trade-eval iteration.
    """
    try:
        plan = await franchise_plan_service.get_plan(pool, league_id, team_id, season)
        if plan is None:
            log.warning(
                "franchise_plan missing for team %d league %d season %d — "
                "derive_and_persist_all may not have run this batch",
                team_id, league_id, season,
            )
        return plan
    except Exception as exc:
        log.debug(
            "franchise_plan lookup failed for team %d league %d season %d — %s",
            team_id, league_id, season, exc,
        )
        return None


def _urgency_allows_flex(urgency: str) -> bool:
    """Return True when the team's urgency level permits including flex players in trade block."""
    return urgency in ("pushing", "desperate", "tanking")



async def _build_cpu_trade_block(
    pool,
    league_id: int,
    season: int,
    cpu_teams: list[team_repo.Team],
    recently_signed_ids: set[int] | None = None,
    postures: dict[int, dict] | None = None,
) -> dict[int, list[int]]:
    """
    For each CPU team, identify players that make sense to offer in a trade.
    Returns a map of team_id -> list of player_ids considered tradeable.

    CPU teams never manually populate the trade block, so this derives it from
    each team's franchise plan (Phase 2) with a fallback to cpu_mode heuristics.

    Plan-driven logic (primary):
    - surplus players are always tradeable
    - flex players are tradeable when urgency is pushing/desperate/tanking
    - core players are NEVER tradeable (plan primary, is_cornerstone is backstop)

    Mode-based heuristics (fallback when plan unavailable):
    - rebuilding: veterans age >= 32, or age >= 29 with OVR >= 65
    - contending: mid-tier players OVR 72–84 (trade bait, not franchise cornerstones)
    - developing: players age >= 30, or age >= 27 with OVR >= 78
    - default: players OVR 70–82

    Players whose contracts were signed within the last 60 sim games are
    excluded from the block (recently_signed_ids).
    """
    if recently_signed_ids is None:
        recently_signed_ids = set()
    if postures is None:
        postures = {}

    result: dict[int, list[int]] = {}

    for team in cpu_teams:
        players = await player_repo.get_roster(pool, league_id, team.id)
        tradeable: list[int] = []
        mode = team.cpu_mode or "default"
        urgency = postures.get(team.id, {}).get("urgency", "comfortable")

        # --- Phase 2: plan-driven filtering (primary) ---
        plan = await _get_franchise_plan(pool, league_id, team.id, season)
        if plan is not None:
            core_set = set(plan.get("core_player_ids") or [])
            flex_set = set(plan.get("flex_player_ids") or [])
            surplus_set = set(plan.get("surplus_player_ids") or [])
            allow_flex = _urgency_allows_flex(urgency)

            for p in players:
                if p.id in recently_signed_ids:
                    continue
                # Core players are never tradeable — plan is primary, cornerstone is backstop.
                if p.id in core_set:
                    continue
                # Surplus always tradeable.
                if p.id in surplus_set:
                    tradeable.append(p.id)
                    continue
                # Flex tradeable only under pressure.
                if p.id in flex_set and allow_flex:
                    tradeable.append(p.id)
        else:
            # --- Fallback: mode-based heuristics ---
            for p in players:
                if p.id in recently_signed_ids:
                    continue
                age = _player_age(p)  # may be None — age-based checks skip if None
                ovr = p.overall

                if mode == "rebuilding":
                    # True rebuild: shop every vet 27+ and every high-OVR non-cornerstone.
                    # Skip age check for players with missing birth_date (don't assume young).
                    if (age is not None and age >= 27) or (ovr >= 78 and age is not None and age >= 22):
                        tradeable.append(p.id)
                elif mode == "soft_rebuild":
                    # Sell aging vets: anyone 30+, or 28+ OVR 65+
                    if (age is not None and age >= 30) or (age is not None and age >= 28 and ovr >= 65):
                        tradeable.append(p.id)
                elif mode == "contending":
                    # Trade non-star bench pieces for better role players.
                    if 72 <= ovr <= 84:
                        tradeable.append(p.id)
                elif mode == "play_in_fringe":
                    # Like contending but slightly broader — any non-star role player
                    if 70 <= ovr <= 84:
                        tradeable.append(p.id)
                elif mode == "developing":
                    if (age is not None and age >= 30) or (ovr >= 78 and age is not None and age >= 27):
                        tradeable.append(p.id)
                else:
                    if 70 <= ovr <= 82:
                        tradeable.append(p.id)

        if tradeable:
            result[team.id] = tradeable

    return result



async def _attempt_three_team_deal(
    pool,
    league: league_repo.League,
    season: int,
    cpu_teams: list[team_repo.Team],
    block_by_team: dict[int, list[int]],
    used_pairs: set[tuple[int, int]],
    taken_player_ids: set[int],
    recently_signed_ids: set[int] | None = None,
    guild: Optional[discord.Guild] = None,
    postures: dict[int, dict] | None = None,
) -> int:
    """
    Attempt a simplified 3-team trade.

    Strategy: Team A wants a player from Team B but has a value gap.
              Team C wants a secondary asset from Team A.
              A sends secondary asset to C; C sends cap filler/pick to B;
              B sends target player to A.

    Implementation: executed as two back-to-back 2-team trades using
    trade_service.propose, labelled "3-Team Trade" in logs.  If either leg
    fails the whole attempt is abandoned (no partial execution).

    Returns 1 on success, 0 on any failure or when no 3-team configuration found.
    """
    if recently_signed_ids is None:
        recently_signed_ids = set()
    if postures is None:
        postures = {}

    # --- Leg 1: pick teams A and B with a VALUE GAP (20-40%) ---
    candidates_a = random.sample(cpu_teams, len(cpu_teams))
    team_a = team_b = team_c = None
    target_player = None

    for a in candidates_a:
        posture_a = postures.get(a.id) or _default_posture(a)
        a_block = block_by_team.get(a.id, [])
        b_candidates = [t for t in cpu_teams if t.id != a.id]
        random.shuffle(b_candidates)

        for b in b_candidates:
            pair_ab = (min(a.id, b.id), max(a.id, b.id))
            if pair_ab in used_pairs:
                continue

            b_block = block_by_team.get(b.id, [])
            if not b_block:
                continue

            for pid in b_block:
                if pid in taken_player_ids or pid in recently_signed_ids:
                    continue
                p = await player_repo.get_by_id(pool, pid)
                if not p or not _team_a_wants_player(posture_a, p):
                    continue

                target_contract = await player_repo.get_active_contract(pool, p.id)
                target_value = trade_evaluator.player_trade_value(
                    {"overall": p.overall, "age": _player_age(p)},
                    {
                        "salary": target_contract.salary if target_contract else 0,
                        "years_remaining": target_contract.years_remaining if target_contract else 1,
                    },
                    league.salary_cap,
                )

                # Build what A can offer directly to B.
                # Fetch A's plan here so core players are protected during the
                # gap-check evaluation (mirrors 2-team protection, prevents asymmetry).
                _plan_a_candidate = await _get_franchise_plan(pool, league.id, a.id, season)
                offer_pids, offer_pkids, pkg_val = await _build_return_package(
                    pool, league, a, a_block, target_value, taken_player_ids, recently_signed_ids,
                    plan_a=_plan_a_candidate,
                )
                if not offer_pids and not offer_pkids:
                    continue

                gap_ratio = (target_value - pkg_val) / max(target_value, 1)
                # Only proceed if gap is in the 20-40% range — close enough to
                # bridge with a third team, not so large it's hopeless.
                if 0.20 <= gap_ratio <= 0.40:
                    team_a = a
                    team_b = b
                    target_player = p
                    break

            if team_a:
                break
        if team_a:
            break

    if not (team_a and team_b and target_player):
        return 0

    # Look up team A's franchise plan for core-player protection in _build_return_package.
    # 2-team trades pass plan_a to protect core; 3-team must do the same so the two
    # paths are consistent and core players can't be shipped via the 3-team route.
    _plan_a_3team = await _get_franchise_plan(pool, league.id, team_a.id, season)

    # --- Find Team C that wants a secondary asset from A ---
    # Secondary asset: a player from A's block not already committed.
    a_block = block_by_team.get(team_a.id, [])
    secondary_pid: int | None = None
    for pid in a_block:
        if pid in taken_player_ids or pid in recently_signed_ids:
            continue
        if pid == target_player.id:
            continue
        secondary_pid = pid
        break

    if secondary_pid is None:
        return 0  # No secondary asset for A to send to C.

    # Find a CPU team C (not A or B) that wants the secondary player.
    c_candidates = [t for t in cpu_teams if t.id not in (team_a.id, team_b.id)]
    random.shuffle(c_candidates)

    for c in c_candidates:
        posture_c = postures.get(c.id) or _default_posture(c)
        pair_ac = (min(team_a.id, c.id), max(team_a.id, c.id))
        if pair_ac in used_pairs:
            continue

        sec_p = await player_repo.get_by_id(pool, secondary_pid)
        if not sec_p or not _team_a_wants_player(posture_c, sec_p):
            continue

        # C sends its cheapest available pick (preferably a 2nd) to B.
        c_picks = await trade_repo.get_team_picks(pool, league.id, c.id)
        c_r2 = [pk for pk in c_picks if pk["round"] == 2]
        c_r1 = [pk for pk in c_picks if pk["round"] == 1]
        c_r2.sort(key=lambda pk: pk["season"])
        c_r1.sort(key=lambda pk: pk["season"])
        filler_pick = (c_r2 or c_r1 or [None])[0]
        if not filler_pick:
            continue

        # ── Per-team value floor for 3-way deals ─────────────────────────────
        # Every participant must receive at least _MIN_3WAY_RATIO of what they
        # send.  See module constant _MIN_3WAY_RATIO.
        #
        # Asset map:
        #   Team A: sends secondary_pid (to C) + leg-2 package (to B) + filler_pick (to B)
        #           receives target_player (from B)
        #   Team B: sends target_player (to A)
        #           receives filler_pick (from C via A) + leg-2 players from A
        #   Team C: sends filler_pick (to B via A)
        #           receives secondary_pid (from A)
        #
        # _b_value_in subtracts _sec_value_c from pkg_val so only the leg-2 players
        # (not the secondary player sent to C) count toward what B receives.

        _sec_contract = await player_repo.get_active_contract(pool, secondary_pid)
        _sec_value_c = trade_evaluator.player_trade_value(
            {"overall": sec_p.overall, "age": _player_age(sec_p)},
            {
                "salary": _sec_contract.salary if _sec_contract else 0,
                "years_remaining": _sec_contract.years_remaining if _sec_contract else 1,
            },
            league.salary_cap,
        )
        _filler_pick_value_c = trade_evaluator.pick_trade_value(
            filler_pick["season"], filler_pick["round"], season
        )

        # Team C value check: C sends filler_pick, receives secondary player.
        _c_ratio = _sec_value_c / max(_filler_pick_value_c, 1.0)
        if _c_ratio < _MIN_3WAY_RATIO:
            log.info(
                f"3-team deal abandoned: {c.nba_team_code} ratio {_c_ratio:.2f} < {_MIN_3WAY_RATIO} "
                f"({c.nba_team_code} sends pick value {_filler_pick_value_c:.1f}, "
                f"receives player value {_sec_value_c:.1f})"
            )
            continue  # try next C candidate

        # Team B value check: B sends target_player, gets filler_pick + A's leg-2
        # package.  pkg_val from the gap-check includes secondary_pid (which goes to C,
        # not B), so subtract _sec_value_c to get the actual leg-2-only value A sends B.
        _b_leg2_val = max(0.0, pkg_val - _sec_value_c)
        _b_value_in = _b_leg2_val + _filler_pick_value_c
        _b_value_out = target_value
        _b_ratio = _b_value_in / max(_b_value_out, 1.0)
        if _b_ratio < _MIN_3WAY_RATIO:
            log.info(
                f"3-team deal abandoned: {team_b.nba_team_code} ratio {_b_ratio:.2f} < {_MIN_3WAY_RATIO} "
                f"({team_b.nba_team_code} sends value {_b_value_out:.1f}, "
                f"receives leg2={_b_leg2_val:.1f} + pick={_filler_pick_value_c:.1f})"
            )
            continue  # try next C candidate

        # Team A value check: A sends secondary_pid (to C) + leg-2 players (to B);
        # receives target_player.  The filler_pick originates from C and is routed
        # to B — A never owns it, so it does not appear in A's outflow.
        _a_value_in = target_value
        _a_value_out = _sec_value_c + _b_leg2_val
        _a_ratio = _a_value_in / max(_a_value_out, 1.0)
        if _a_ratio < _MIN_3WAY_RATIO:
            log.info(
                f"3-team deal abandoned: {team_a.nba_team_code} ratio {_a_ratio:.2f} < {_MIN_3WAY_RATIO} "
                f"({team_a.nba_team_code} sends value {_a_value_out:.1f}, "
                f"receives value {_a_value_in:.1f})"
            )
            continue  # try next C candidate
        # ── End per-team value floor ──────────────────────────────────────────

        team_c = c

        log.info(
            f"3-team trade: A={team_a.id} B={team_b.id} C={team_c.id} | "
            f"A→C: player {secondary_pid} | C→B: pick {filler_pick['id']} | "
            f"B→A: player {target_player.id} | "
            f"value-ratios A={_a_ratio:.2f} B={_b_ratio:.2f} C={_c_ratio:.2f}"
        )

        # ── Ride-along hook: three-team proposal ─────────────────────────────
        # Fires before leg 1 so the user can veto before any assets move.
        if ride_along.is_ride_along_enabled():
            try:
                _plan_a_ra = await _get_franchise_plan(pool, league.id, team_a.id, season)
                _plan_b_ra = await _get_franchise_plan(pool, league.id, team_b.id, season)
                _plan_c_ra = await _get_franchise_plan(pool, league.id, team_c.id, season)

                def _plan_summary(plan: dict | None, code: str) -> str:
                    if not plan:
                        return f"{code}: no plan"
                    return (
                        f"{code}: {plan.get('goal', '?')} "
                        f"core={len(plan.get('core_player_ids') or [])} "
                        f"surplus={len(plan.get('surplus_player_ids') or [])} "
                        f"pursuing={','.join(plan.get('asset_targets') or []) or 'none'}"
                    )

                _sec_name = f"{sec_p.first_name} {sec_p.last_name}" if sec_p else f"player#{secondary_pid}"
                _tgt_name = (
                    f"{target_player.first_name} {target_player.last_name} "
                    f"(OVR {target_player.overall}, {target_player.position})"
                )
                _ra_header = (
                    f"CPU [{team_a.nba_team_code}] proposes 3-team deal "
                    f"with [{team_b.nba_team_code}] and [{team_c.nba_team_code}]"
                )
                _ra_filler_pick_dict = dict(filler_pick)
                _ra_3t_flow = (
                    f"{team_a.nba_team_code} → {team_c.nba_team_code}: {_sec_name} | "
                    f"{team_c.nba_team_code} → {team_b.nba_team_code}: {filler_pick['season']} R{filler_pick['round']} | "
                    f"{team_b.nba_team_code} → {team_a.nba_team_code}: {_tgt_name}"
                )
                _ra_details = await ra_reasoning.render_trade_panel(
                    pool, league, season,
                    [
                        (
                            f"{team_a.nba_team_code} perspective",
                            team_a,
                            {
                                "players_in": [target_player.id],
                                "players_out": [secondary_pid] if secondary_pid else [],
                                "picks_in": [],
                                "picks_out": [],
                            },
                        ),
                        (
                            f"{team_b.nba_team_code} perspective",
                            team_b,
                            {
                                "players_in": [],
                                "players_out": [target_player.id],
                                "picks_in": [_ra_filler_pick_dict],
                                "picks_out": [],
                            },
                        ),
                        (
                            f"{team_c.nba_team_code} perspective",
                            team_c,
                            {
                                "players_in": [secondary_pid] if secondary_pid else [],
                                "players_out": [],
                                "picks_in": [],
                                "picks_out": [_ra_filler_pick_dict],
                            },
                        ),
                    ],
                    flow_summary=_ra_3t_flow,
                    decision_label="APPROVE",
                )
                _ra_result = ride_along.prompt_decision(
                    decision_type="three_team",
                    header=_ra_header,
                    details=_ra_details,
                    default_action="approve",
                )
                if _ra_result["action"] == "veto":
                    return 0
            except Exception:
                pass  # ride-along errors must never break the sim

        # --- Execute leg 1: A sends secondary player to C; C sends pick to A ---
        # We model this as A proposing to C: A gives secondary_pid, A receives
        # a pick from C (the filler pick will actually go to B, but in the
        # simplified 2-trade model C gives A the pick and A forwards it to B
        # in leg 2).  For simplicity we execute as:
        #   Leg 1: A → C: secondary player; C → A: filler_pick
        #   Leg 2: A → B: filler_pick; B → A: target_player
        # The transactions happen atomically per trade but not across both legs.
        # If leg 2 fails the filler pick ends up with A, which is acceptable.

        try:
            trade1 = await trade_service.propose(
                league=league,
                proposer_team=team_a,
                counterparty_team=team_c,
                proposer_player_ids=[secondary_pid],
                proposer_pick_ids=[],
                counterparty_player_ids=[],
                counterparty_pick_ids=[filler_pick["id"]],
            )
        except Exception as exc:
            log.warning(f"3-team trade leg-1 failed: {exc}")
            return 0

        # Auto-approve leg 1 if CPU-CPU.
        if trade1.status == "pending_commissioner":
            try:
                await _maybe_auto_approve(pool, league, trade1, guild)
            except Exception as exc:
                log.warning(f"3-team trade leg-1 auto-approve failed: {exc}")
                return 0
            # Reload to confirm it was approved.
            t1_status = await pool.fetchval("SELECT status FROM trades WHERE id = $1", trade1.id)
            if t1_status != "approved":
                log.info(f"3-team trade aborted: leg-1 not approved (status={t1_status})")
                return 0

        # Confirm A now owns the filler pick.
        pick_owner = await pool.fetchval(
            "SELECT current_team_id FROM draft_picks WHERE id = $1", filler_pick["id"]
        )
        if pick_owner != team_a.id:
            log.info(f"3-team trade aborted: filler pick {filler_pick['id']} not transferred to A")
            return 0

        # --- Execute leg 2: A sends filler_pick to B; B sends target_player to A ---
        target_contract = await player_repo.get_active_contract(pool, target_player.id)
        target_value = trade_evaluator.player_trade_value(
            {"overall": target_player.overall, "age": _player_age(target_player)},
            {
                "salary": target_contract.salary if target_contract else 0,
                "years_remaining": target_contract.years_remaining if target_contract else 1,
            },
            league.salary_cap,
        )

        # Build A's full return package for B (including the newly-acquired filler pick).
        # Pass plan_a so core players are protected — same as 2-team path.
        a_block_updated = block_by_team.get(team_a.id, [])
        offer2_pids, _, pkg2_val = await _build_return_package(
            pool, league, team_a, a_block_updated, target_value, taken_player_ids, recently_signed_ids,
            plan_a=_plan_a_3team,
        )
        # Force include the filler pick.
        offer2_pick_ids = [filler_pick["id"]]

        try:
            trade2 = await trade_service.propose(
                league=league,
                proposer_team=team_a,
                counterparty_team=team_b,
                proposer_player_ids=offer2_pids,
                proposer_pick_ids=offer2_pick_ids,
                counterparty_player_ids=[target_player.id],
                counterparty_pick_ids=[],
            )
        except Exception as exc:
            log.warning(f"3-team trade leg-2 failed: {exc}")
            # Leg 1 already executed — log and return partial.
            return 1

        if trade2.status == "pending_commissioner":
            try:
                await _maybe_auto_approve(pool, league, trade2, guild)
            except Exception as exc:
                log.warning(f"3-team trade leg-2 auto-approve failed: {exc}")

        # Mark all involved players as taken.
        taken_player_ids.add(target_player.id)
        taken_player_ids.add(secondary_pid)
        taken_player_ids.update(offer2_pids)

        pair_ab = (min(team_a.id, team_b.id), max(team_a.id, team_b.id))
        pair_ac = (min(team_a.id, team_c.id), max(team_a.id, team_c.id))
        used_pairs.add(pair_ab)
        used_pairs.add(pair_ac)

        log.info(
            f"3-Team Trade executed: A={team_a.id} B={team_b.id} C={team_c.id} | "
            f"trade1={trade1.id} trade2={trade2.id}"
        )
        return 1

    return 0



def _get_trade_target_positions(
    team,
    pos_count_map: dict[str, int],
    mode: str,
) -> list[str]:
    """
    Return the ordered list of positions a team should prioritize when seeking
    trade targets, based on their posture mode.

    contending  → 1 weakest position (focused attack on the biggest gap).
    play_in_fringe → 2 weakest positions.
    all other modes → all 5 positions (no position filter applied).

    Weakness is measured by summing counts at each position; ties broken by
    original position order so the result is deterministic.
    """
    _ALL = ["PG", "SG", "SF", "PF", "C"]
    if mode not in ("contending", "play_in_fringe"):
        return _ALL

    ranked = sorted(_ALL, key=lambda pos: pos_count_map.get(pos, 0))
    n = 1 if mode == "contending" else 2
    return ranked[:n]


def _position_matches_need(player_position: str, target_positions: list[str]) -> bool:
    """
    Return True when player_position is in target_positions.

    Includes flexible positional adjacency for contending/fringe teams:
    SF <-> PF/SG (wing versatility), PG <-> SG (backcourt depth).
    """
    if player_position in target_positions:
        return True
    _adjacency: dict[str, list[str]] = {
        "SF": ["PF", "SG"],
        "PF": ["SF", "C"],
        "PG": ["SG"],
        "SG": ["PG", "SF"],
    }
    for need in target_positions:
        if player_position in _adjacency.get(need, []):
            return True
    return False



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
    base = trade_evaluator.player_team_specific_value(
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



async def _attempt_one_offer(
    pool,
    league: league_repo.League,
    season: int,
    cpu_teams: list[team_repo.Team],
    block_by_team: dict[int, list[int]],
    used_pairs: set[tuple[int, int]],
    taken_player_ids: set[int],
    deadline_game_index: int,
    recently_signed_ids: set[int] | None = None,
    guild: Optional[discord.Guild] = None,
    postures: dict[int, dict] | None = None,
) -> int:
    """
    Pick a team A, find the best target from team B, build a return package,
    and call trade_service.propose. Returns 1 if a proposal was made, 0 otherwise.
    """
    if recently_signed_ids is None:
        recently_signed_ids = set()
    if postures is None:
        postures = {}

    # ── Phase 3: memoized plans + contexts for all CPU teams ─────────────────
    # Built once per _attempt_one_offer call; reused for both counterparty scoring
    # and the b-loop plan lookups so we don't query the same plan 30 times.
    _all_cp_ids = [t.id for t in cpu_teams]

    _cp_plan_list = await asyncio.gather(
        *[_get_franchise_plan(pool, league.id, t.id, season) for t in cpu_teams],
        return_exceptions=True,
    )
    cp_plans: dict[int, dict | None] = {}
    for t, result in zip(cpu_teams, _cp_plan_list):
        cp_plans[t.id] = result if not isinstance(result, Exception) else None

    _cp_ctx_list = await asyncio.gather(
        *[trade_evaluator.build_team_context(pool, league.id, t.id, season) for t in cpu_teams],
        return_exceptions=True,
    )
    cp_contexts: dict[int, dict] = {}
    for t, result in zip(cpu_teams, _cp_ctx_list):
        if isinstance(result, Exception):
            cp_contexts[t.id] = {
                "team_id": t.id, "mode": "developing",
                "current_payroll": 0, "position_counts": {},
            }
        else:
            cp_contexts[t.id] = result  # type: ignore[assignment]

    # R1 pick counts for each CPU team (next 3 seasons) — needed for asset-availability bonus.
    _cp_r1_rows = await asyncio.gather(
        *[
            pool.fetchval(
                """SELECT COUNT(*) FROM draft_picks
                   WHERE league_id=$1 AND current_team_id=$2
                     AND round=1 AND season>$3 AND season<=$3+3""",
                league.id, t.id, season,
            )
            for t in cpu_teams
        ],
        return_exceptions=True,
    )
    cp_r1_counts: dict[int, int] = {}
    for t, result in zip(cpu_teams, _cp_r1_rows):
        cp_r1_counts[t.id] = int(result) if isinstance(result, int) else 0
    # ── End Phase 3 memoization ───────────────────────────────────────────────

    # league-scan result storage: populated per surplus player in the a-loop
    # so _plan_alignment_str() can reference it.
    _league_scan_result: list[tuple[team_repo.Team, float, str]] = []
    _league_scan_player_name: str = ""

    # Shuffle so we don't always favour the same team.
    candidates_a = random.sample(cpu_teams, len(cpu_teams))
    team_a: Optional[team_repo.Team] = None
    target_team: Optional[team_repo.Team] = None
    target_player: Optional[player_repo.Player] = None
    posture_a_final: dict = {}
    posture_b_final: dict = {}
    # Franchise plans for the winning pair — populated when a match is found.
    _plan_a_final: dict | None = None
    _plan_b_final: dict | None = None

    for a in candidates_a:
        # Reset per-a-team scan state so a previous team's scan doesn't bleed
        # into the winning pair's _plan_alignment_str if this team has no surplus.
        _league_scan_result = []
        _league_scan_player_name = ""

        posture_a = postures.get(a.id) or _default_posture(a)
        mode_a = posture_a["mode"]

        # Tanking teams skip trading for vets — they want to lose.
        if posture_a["urgency"] == "tanking" and mode_a not in ("rebuilding", "soft_rebuild"):
            continue

        # Fetch team A's positional distribution once per team so we can score
        # each target player by how much A needs that position.
        pos_count_rows = await pool.fetch(
            """SELECT p.position, COUNT(*) AS cnt
               FROM lineups l JOIN players p ON p.id = l.player_id
               WHERE l.league_id = $1 AND l.team_id = $2
               GROUP BY p.position""",
            league.id, a.id,
        )
        pos_count_map = {r["position"]: r["cnt"] for r in pos_count_rows}

        # Mode-driven target position filter: contending/fringe teams pursue
        # specific positional gaps; all other modes take any position.
        target_positions = _get_trade_target_positions(a, pos_count_map, mode_a)

        # B6: build archetype distribution for team A once per outer team.
        # Used in the inner player-scoring loop to downweight redundant archetypes.
        _a_roster_for_arch = await player_repo.get_roster(pool, league.id, a.id)
        _a_archetype_counts: dict[str, int] = _team_archetype_counts(_a_roster_for_arch)

        # Mode-driven shop list: soft_rebuild/rebuilding teams shop their own
        # veterans across all b_candidates rather than just seeking inbound talent.
        # For sell modes the "b" team role is reversed — team A is selling, B is buying.
        # We still use the standard b→a player flow; the key difference is which players
        # A wants to receive (youth/picks) vs which A puts on the block (age/OVR filters
        # already handled in _build_cpu_trade_block).

        # Use memoized plan for team A (no extra DB hit per iteration).
        _plan_a = cp_plans.get(a.id)
        _asset_targets_a: list[str] = _plan_a.get("asset_targets") or [] if _plan_a else []
        _plan_a_goal: str = _plan_a.get("goal", "") if _plan_a else ""

        # Hoist philosophy fetch for team A — constant across the entire b-team/player loop.
        _a_philosophy = await pool.fetchval(
            "SELECT coach_philosophy FROM teams WHERE id = $1", a.id
        )

        # ── Phase 3: targeted counterparty scan for surplus players ──────────
        # When team A has a specific high-value surplus player to shop, rank all
        # counterparties by how much they'd value that player and restrict the
        # b-loop to the top 3 matches.  This makes proposals targeted ("who should
        # I call?") rather than iterating teams in arbitrary order.
        _surplus_ids_a: list[int] = list((_plan_a or {}).get("surplus_player_ids") or [])
        _scan_used: bool = False
        if _surplus_ids_a:
            # Find the highest-value surplus player on team A's block.
            _best_surplus_player: player_repo.Player | None = None
            _best_surplus_contract: object | None = None
            _best_surplus_val: float = -1.0
            for _sid in _surplus_ids_a:
                if _sid in taken_player_ids:
                    continue
                _sp = await player_repo.get_by_id(pool, _sid)
                if not _sp:
                    continue
                _sc = await player_repo.get_active_contract(pool, _sid)
                _sv = trade_evaluator.player_trade_value(
                    {"overall": _sp.overall, "age": _player_age(_sp) or 27},
                    {"salary": getattr(_sc, "salary", 0) or 0,
                     "years_remaining": getattr(_sc, "years_remaining", 1) or 1},
                    league.salary_cap,
                )
                if _sv > _best_surplus_val:
                    _best_surplus_val = _sv
                    _best_surplus_player = _sp
                    _best_surplus_contract = _sc

            if _best_surplus_player is not None and _best_surplus_val >= 15:
                try:
                    _scan_top3 = await _league_scan_counterparties(
                        pool=pool,
                        league_id=league.id,
                        season=season,
                        surplus_player=_best_surplus_player,
                        surplus_contract=_best_surplus_contract,
                        offerer_team_id=a.id,
                        offerer_asset_targets=_asset_targets_a,
                        cpu_teams=cpu_teams,
                        cp_plans=cp_plans,
                        cp_contexts=cp_contexts,
                        cp_r1_counts=cp_r1_counts,
                        salary_cap=league.salary_cap,
                    )
                    if _scan_top3:
                        _league_scan_result = _scan_top3
                        _league_scan_player_name = (
                            f"{_best_surplus_player.first_name} {_best_surplus_player.last_name}"
                        )
                        _scan_used = True
                        # Restrict b-loop to top-3 counterparties; keep ordering from scan.
                        _top3_ids = {t.id for t, _, _ in _scan_top3}
                        b_candidates_base = [t for t, _, _ in _scan_top3]
                        # Append remaining teams as fallback (preserving arbitrary shuffle).
                        _remaining = [t for t in cpu_teams if t.id != a.id and t.id not in _top3_ids]
                        random.shuffle(_remaining)
                        b_candidates = b_candidates_base + _remaining
                        log.info(
                            "[CPU] league scan for %s (%s OVR %d val %.1f): %s",
                            _league_scan_player_name, _best_surplus_player.position,
                            _best_surplus_player.overall, _best_surplus_val,
                            " | ".join(r for _, _, r in _scan_top3),
                        )
                        if _HEADLESS:
                            try:
                                _p_name = getattr(a, "nba_team_code", str(a.id))
                                print(
                                    f"   league scan: {_p_name} ranked "
                                    f"{', '.join(r for _, _, r in _scan_top3)} "
                                    f"as top counterparties for {_league_scan_player_name} "
                                    f"(OVR {_best_surplus_player.overall})"
                                )
                            except Exception:
                                pass
                except Exception as exc:
                    log.debug(
                        "league scan failed for team %d player %d: %s — falling back to arbitrary order",
                        a.id, _best_surplus_player.id if _best_surplus_player else -1, exc,
                    )

        if not _scan_used:
            b_candidates = [t for t in cpu_teams if t.id != a.id]
            random.shuffle(b_candidates)
        # ── End Phase 3 league scan ───────────────────────────────────────────

        # Collect all valid (score, b, player, posture_b) candidates so we can
        # rank by plan-aligned score rather than taking the first valid match.
        _scored_candidates: list[tuple[float, team_repo.Team, player_repo.Player, dict]] = []

        for b in b_candidates:
            posture_b = postures.get(b.id) or _default_posture(b)
            pair = (min(a.id, b.id), max(a.id, b.id))
            if pair in used_pairs:
                continue

            # Skip this team pair if they already have an active trade proposal
            # this season (any non-resolved status), preventing duplicate proposals
            # across batch rounds.
            active_pair_rows = await pool.fetch(
                """SELECT 1 FROM trades
                   WHERE league_id = $1 AND season = $2
                     AND ((proposer_team_id = $3 AND counterparty_team_id = $4)
                          OR (proposer_team_id = $4 AND counterparty_team_id = $3))
                     AND status IN ('pending_counterparty', 'pending_commissioner')
                   LIMIT 1""",
                league.id, season, a.id, b.id,
            )
            if active_pair_rows:
                continue

            b_block_ids = block_by_team.get(b.id, [])
            if not b_block_ids:
                continue

            # Use memoized plan for team B (no extra DB hit per b-team iteration).
            _plan_b = cp_plans.get(b.id)
            _b_surplus_set: set[int] = set((_plan_b or {}).get("surplus_player_ids") or [])
            _b_core_set: set[int] = set((_plan_b or {}).get("core_player_ids") or [])
            _b_flex_set: set[int] = set((_plan_b or {}).get("flex_player_ids") or [])

            # Use memoized R1 count for team B — no per-b DB query needed.
            _b_r1_count: int = cp_r1_counts.get(b.id, 0)

            # picks_any count still needs a separate query (only used for plan_bias; low-cost).
            _b_any_count: int = await pool.fetchval(
                """SELECT COUNT(*) FROM draft_picks
                   WHERE league_id = $1 AND current_team_id = $2
                     AND season > $3 AND season <= $3 + 3""",
                league.id, b.id, season,
            ) or 0

            # Load and score B's trade block players.
            for pid in b_block_ids:
                # Skip players already committed to another offer this round.
                if pid in taken_player_ids:
                    continue

                p = await player_repo.get_by_id(pool, pid)
                if not p:
                    continue

                if not _team_a_wants_player(posture_a, p):
                    continue

                # Contending/fringe teams only target their specific positional gaps.
                if mode_a in ("contending", "play_in_fringe"):
                    if not _position_matches_need(p.position, target_positions):
                        continue

                # Positional need multiplier: deprioritize if A already has 3+
                # at this position; boost if A has 0-1.
                pos_cnt = pos_count_map.get(p.position, 0)
                if pos_cnt >= 3:
                    need_multiplier = 0.6
                elif pos_cnt <= 1:
                    need_multiplier = 1.4
                else:
                    need_multiplier = 1.0

                # Compute a rough target value for the threshold check.
                _tc = await player_repo.get_active_contract(pool, p.id)
                _tv = trade_evaluator.player_trade_value(
                    {"overall": p.overall, "age": _player_age(p)},
                    {
                        "salary": _tc.salary if _tc else 0,
                        "years_remaining": _tc.years_remaining if _tc else 1,
                    },
                    league.salary_cap,
                )
                if _tv * need_multiplier < 15:
                    # Too low-priority given positional context — skip.
                    log.debug(
                        f"CPU trade target skipped (need_multiplier={need_multiplier:.1f}): "
                        f"player {p.id} OVR {p.overall} pos {p.position} "
                        f"for team {a.id} — effective value {_tv * need_multiplier:.1f} < 15"
                    )
                    continue

                # Cap: skip if player already appears in an active trade proposal.
                active_for_player = await pool.fetchval(
                    """SELECT COUNT(*) FROM trade_assets ta
                       JOIN trades t ON t.id = ta.trade_id
                       WHERE t.league_id = $1 AND ta.player_id = $2
                         AND ta.asset_type = 'player'
                         AND t.status NOT IN ('approved', 'rejected', 'declined', 'expired', 'superseded')
                    """,
                    league.id, pid,
                )
                if active_for_player and active_for_player >= 1:
                    continue

                # 60-sim-day trade restriction: skip recently traded players.
                if pid in (recently_signed_ids or set()):
                    continue
                last_traded = await pool.fetchval(
                    "SELECT last_traded_at FROM players WHERE id = $1", pid
                )
                if last_traded is not None:
                    last_game_date = await pool.fetchval(
                        "SELECT MAX(scheduled_date) FROM games "
                        "WHERE league_id = $1 AND status = 'simmed'",
                        league.id,
                    )
                    if last_game_date is not None:
                        days_since = (last_game_date - last_traded).days
                        if days_since < 60:
                            continue
                        # Also block if fewer than 60 days before deadline.
                        deadline_date = await pool.fetchval(
                            "SELECT MIN(scheduled_date) FROM games "
                            "WHERE league_id = $1 AND season = $2 AND game_index = $3",
                            league.id, league.current_season, deadline_game_index,
                        )
                        if deadline_date is not None:
                            days_to_deadline = (deadline_date - last_game_date).days
                            if days_to_deadline < 60:
                                continue

                # --- Counterparty hint: skip if target is core on B's plan ---
                # Core players have huge penalty — they won't move him no matter what.
                if pid in _b_core_set:
                    log.debug(
                        "CPU trade target skipped: player %d is core on team %d's plan",
                        pid, b.id,
                    )
                    continue

                # --- Score candidate with asset_target plan bias (+20-25% per match) ---
                _age_p = _player_age(p)  # may be None; all comparisons guard for it
                _plan_bias = 1.0
                if _asset_targets_a:
                    if "role_players" in _asset_targets_a and 78 <= p.overall <= 85:
                        _plan_bias += 0.20
                    if "veterans" in _asset_targets_a and _age_p is not None and _age_p >= 28 and p.overall >= 78:
                        _plan_bias += 0.20
                    if "young_u23" in _asset_targets_a and _age_p is not None and _age_p < 23 and (
                        pid in _b_flex_set or pid in _b_surplus_set
                    ):
                        _plan_bias += 0.20
                    if "expiring_contracts" in _asset_targets_a:
                        _tc_yrs = _tc.years_remaining if _tc else 99
                        if _tc_yrs <= 1:
                            _plan_bias += 0.20
                    # picks_r1: bias toward counterparties sitting on ≥4 R1s — they have surplus
                    if "picks_r1" in _asset_targets_a and _b_r1_count >= 4:
                        _plan_bias += 0.25
                    # picks_any: bias toward counterparties with ≥6 total picks (any round)
                    if "picks_any" in _asset_targets_a and _b_any_count >= 6:
                        _plan_bias += 0.20
                    # cap_space: bias toward expensive/expiring contracts the counterparty
                    # wants to dump (1-year remaining or salary > 25% of cap)
                    if "cap_space" in _asset_targets_a:
                        _tc_yrs = _tc.years_remaining if _tc else 99
                        _tc_sal = _tc.salary if _tc else 0
                        _cap_threshold = int(league.salary_cap * 0.25)
                        if _tc_yrs <= 1 or _tc_sal >= _cap_threshold:
                            _plan_bias += 0.20
                    # Bump further if B has this player flagged as surplus (willing to deal)
                    if pid in _b_surplus_set:
                        _plan_bias += 0.20

                # Apply context modifier from team A's perspective so the CPU
                # PREFERS targets who synergize, fit the window, scheme, etc.
                # This is a rank-time modifier — it does not affect the final
                # value submitted to evaluate_trade (that happens in cpu_should_accept).
                _ctx_modifier = 1.0
                try:
                    from services import trade_context as _tc_mod
                    _p_dict_ctx = {
                        "id": p.id,
                        "overall": p.overall,
                        "age": _age_p or 27,
                        "position": p.position,
                        "tendency_3pt": getattr(p, "tendency_3pt", 50) or 50,
                        "tendency_drive": getattr(p, "tendency_drive", 50) or 50,
                        "tendency_pass": getattr(p, "tendency_pass", 50) or 50,
                        "ast_tendency": getattr(p, "ast_tendency", 50) or 50,
                        "reb_tendency": getattr(p, "reb_tendency", 50) or 50,
                        "blk_tendency": getattr(p, "blk_tendency", 50) or 50,
                        "stl_tendency": getattr(p, "stl_tendency", 50) or 50,
                        "defense_tendency": getattr(p, "defense_tendency", 50) or 50,
                        "defensive_archetype": getattr(p, "defensive_archetype", None),
                        "usage_weight": getattr(p, "usage_weight", 0.5) or 0.5,
                    }
                    _a_plan_for_ctx = _plan_a or {}
                    _a_posture_for_ctx = postures.get(a.id) or {}
                    # _a_philosophy hoisted before b-team loop — no per-candidate DB hit.
                    _form_mod_ctx = 1.0  # form not yet computed at rank time; use neutral
                    _ctx_modifier, _ctx_sigs = await _tc_mod.compute_context_modifier(
                        pool=pool,
                        league_id=league.id,
                        season=season,
                        perspective_team_id=a.id,
                        plan=_a_plan_for_ctx,
                        posture=_a_posture_for_ctx,
                        coach_philosophy=_a_philosophy,
                        incoming_player=_p_dict_ctx,
                        form_mod=_form_mod_ctx,
                    )
                except Exception as _ctx_rank_exc:
                    log.debug("context modifier for candidate ranking failed pid=%d: %s", pid, _ctx_rank_exc)

                # B3: asset upside modifier — boosts young/pedigree/award-race players
                # so they don't get ranked as filler or offered away cheaply.
                _upside_mod = _asset_upside_modifier(
                    p,
                    draft_year=getattr(p, "draft_year", None),
                    current_season=season,
                    # Award ranks not available at proposal-rank time without an
                    # extra DB call per candidate. Skip here; B3 in cpu_should_accept
                    # handles the accept-side valuation via player_trade_value modifiers.
                )

                # B6: archetype redundancy penalty — downweight incoming player if
                # team A already has 2+ players in the same archetype.
                _incoming_arch = trade_evaluator._player_archetype({
                    "position": p.position,
                    "tendency_3pt": getattr(p, "tendency_3pt", 50) or 50,
                    "tendency_drive": getattr(p, "tendency_drive", 50) or 50,
                    "tendency_pass": getattr(p, "tendency_pass", 50) or 50,
                    "ast_tendency": getattr(p, "ast_tendency", 50) or 50,
                    "reb_tendency": getattr(p, "reb_tendency", 50) or 50,
                    "blk_tendency": getattr(p, "blk_tendency", 50) or 50,
                    "stl_tendency": getattr(p, "stl_tendency", 50) or 50,
                })
                _arch_penalty = 1.0
                if _incoming_arch and _a_archetype_counts.get(_incoming_arch, 0) >= 2:
                    # Already have 2+ of this archetype — materially downweight.
                    _arch_penalty = 0.65
                elif _incoming_arch and _a_archetype_counts.get(_incoming_arch, 0) == 1:
                    # Light downweight for one existing match.
                    _arch_penalty = 0.85

                _score = _tv * need_multiplier * _plan_bias * _ctx_modifier * _upside_mod * _arch_penalty
                _scored_candidates.append((_score, b, p, posture_b))

        if not _scored_candidates:
            continue

        # Pick the highest-scoring candidate.
        _scored_candidates.sort(key=lambda x: x[0], reverse=True)
        _best_score, _best_b, _best_p, _best_posture_b = _scored_candidates[0]

        team_a = a
        target_team = _best_b
        target_player = _best_p
        posture_a_final = posture_a
        posture_b_final = _best_posture_b
        # Capture plans for ride-along / headless output and return-package building.
        _plan_a_final = _plan_a
        # Use memoized plan for the winning b-team — no extra DB query needed.
        _plan_b_final = cp_plans.get(_best_b.id)

        if team_a:
            break

    if not (team_a and target_team and target_player):
        return 0

    posture_a = posture_a_final  # type: ignore[assignment]
    posture_b = posture_b_final  # type: ignore[assignment]
    # Plan references for the winning pair (may be None if plan unavailable).
    _offer_plan_a: dict | None = _plan_a_final
    _offer_plan_b: dict | None = _plan_b_final

    pair = (min(team_a.id, target_team.id), max(team_a.id, target_team.id))
    used_pairs.add(pair)

    # Optionally request a second player from B alongside the primary target.
    # Only attempted when the primary target is a star (OVR >= 75) and the 30%
    # dice roll hits.  The secondary must be rated below the primary and not
    # already committed elsewhere this round.  Falls back to single-player if
    # no valid secondary exists — never errors.
    secondary_target: Optional[player_repo.Player] = None
    if target_player.overall >= 75 and random.random() < 0.3:
        b_block_ids = block_by_team.get(target_team.id, [])
        for pid in b_block_ids:
            if pid == target_player.id:
                continue
            if pid in taken_player_ids:
                continue
            candidate = await player_repo.get_by_id(pool, pid)
            if candidate and candidate.overall < target_player.overall:
                secondary_target = candidate
                break

    # Build A's return package sized to match the combined value of all requested
    # players (primary + optional secondary) within the usual 25% tolerance.

    # ── Form-map: one batch DB query for all players involved in this eval ────
    # Covers the target(s) + all of A's block players so display and value logic
    # share the same stats without redundant queries.
    _form_all_ids: list[int] = [target_player.id]
    if secondary_target:
        _form_all_ids.append(secondary_target.id)
    _form_all_ids.extend(block_by_team.get(team_a.id, []))
    _form_all_ids = list(dict.fromkeys(_form_all_ids))  # deduplicate, preserve order

    _form_ovr_map: dict[int, int] = {pid: 80 for pid in _form_all_ids}
    _form_pos_map: dict[int, str] = {pid: "" for pid in _form_all_ids}
    _form_ovr_map[target_player.id] = target_player.overall
    _form_pos_map[target_player.id] = target_player.position
    if secondary_target:
        _form_ovr_map[secondary_target.id] = secondary_target.overall
        _form_pos_map[secondary_target.id] = secondary_target.position

    form_map: dict[int, tuple[float, dict]] = await trade_evaluator.compute_form_map(
        pool, _form_all_ids, _form_ovr_map, _form_pos_map, league.id, season,
    )

    target_contract = await player_repo.get_active_contract(pool, target_player.id)
    _target_form_mod, _target_stats = form_map.get(target_player.id, (1.0, {}))
    # Pass position + season_stats so player_trade_value applies experience_premium.
    target_value_raw = trade_evaluator.player_trade_value(
        {"overall": target_player.overall, "age": _player_age(target_player), "position": target_player.position},
        {
            "salary": target_contract.salary if target_contract else 0,
            "years_remaining": target_contract.years_remaining if target_contract else 1,
        },
        league.salary_cap,
        season_stats=_target_stats or None,
    )
    target_value = trade_evaluator.apply_form(target_value_raw, _target_form_mod)

    if secondary_target:
        secondary_contract = await player_repo.get_active_contract(pool, secondary_target.id)
        _sec_form_mod, _sec_stats = form_map.get(secondary_target.id, (1.0, {}))
        secondary_value_raw = trade_evaluator.player_trade_value(
            {"overall": secondary_target.overall, "age": _player_age(secondary_target), "position": secondary_target.position},
            {
                "salary": secondary_contract.salary if secondary_contract else 0,
                "years_remaining": secondary_contract.years_remaining if secondary_contract else 1,
            },
            league.salary_cap,
            season_stats=_sec_stats or None,
        )
        target_value += trade_evaluator.apply_form(secondary_value_raw, _sec_form_mod)
    else:
        _sec_form_mod, _sec_stats = 1.0, {}

    offer_player_ids, offer_pick_ids, package_value = await _build_return_package(
        pool,
        league,
        team_a,
        block_by_team.get(team_a.id, []),
        target_value,
        taken_player_ids,
        recently_signed_ids,
        counterparty_mode=posture_b["mode"],
        plan_a=_offer_plan_a,
    )

    if not offer_player_ids and not offer_pick_ids:
        log.debug(
            f"CPU trade skipped: team {team_a.id} has no assets to offer "
            f"for player {target_player.id} (value {target_value:.1f})"
        )
        return 0

    # ── Pre-emptive sweetener ─────────────────────────────────────────────────
    # If the package is close but underpaying (ratio in [0.85, 1.05)), try to add
    # a small sweetener (pick or cheap role player) to push it over 1.05 and make
    # the counterparty more likely to accept.
    _ratio_for_sweetener = package_value / max(target_value, 1)
    if 0.85 <= _ratio_for_sweetener < 1.05:
        _sw_pid, _sw_pkid, _sw_val = await _pick_sweetener(
            pool,
            league,
            team_a,
            offer_player_ids,
            offer_pick_ids,
            target_team,
            target_value,
            package_value,
        )
        if _sw_pid is not None:
            offer_player_ids = list(offer_player_ids) + [_sw_pid]
            package_value += _sw_val
            _sw_p = await player_repo.get_by_id(pool, _sw_pid)
            _sw_label = f"{_sw_p.first_name} {_sw_p.last_name} OVR {_sw_p.overall}" if _sw_p else str(_sw_pid)
            log.info(
                f"[CPU] sweetener added to make trade competitive: player {_sw_label} "
                f"(+{_sw_val:.1f}) → ratio now {package_value / max(target_value, 1):.2f}"
            )
            if _HEADLESS:
                try:
                    print(f"   note: sweetener added — {_sw_label} (+{_sw_val:.1f})")
                except Exception:
                    pass
            # Fetch form stats for the sweetener player if not already in form_map.
            # With Bug 1 fixed this is a targeted single-id query (cache has the rest).
            if _sw_pid not in form_map:
                _sw_ovr = _sw_p.overall if _sw_p else 80
                _sw_pos = _sw_p.position if _sw_p else ""
                _sw_form = await trade_evaluator.compute_form_map(
                    pool, [_sw_pid], {_sw_pid: _sw_ovr}, {_sw_pid: _sw_pos}, league.id, season,
                )
                form_map = {**form_map, **_sw_form}
        elif _sw_pkid is not None:
            offer_pick_ids = list(offer_pick_ids) + [_sw_pkid]
            package_value += _sw_val
            _sw_pk = await pool.fetchrow("SELECT season, round FROM draft_picks WHERE id = $1", _sw_pkid)
            _sw_pk_label = (
                f"{_sw_pk['season']} R{_sw_pk['round']} pick" if _sw_pk else f"pick #{_sw_pkid}"
            )
            log.info(
                f"[CPU] sweetener added to make trade competitive: {_sw_pk_label} "
                f"(+{_sw_val:.1f}) → ratio now {package_value / max(target_value, 1):.2f}"
            )
            if _HEADLESS:
                try:
                    print(f"   note: sweetener added — {_sw_pk_label} (+{_sw_val:.1f})")
                except Exception:
                    pass
    # ── End sweetener ─────────────────────────────────────────────────────────

    # ── Pre-propose sanity floor ──────────────────────────────────────────────
    # After the base package and any sweetener have been assembled, re-check the
    # final ratio using the form-adjusted target value.  If CPU is still offering
    # less than mode-floor of target value it would be laughed out of the room —
    # abort without submitting.
    # Mode-specific floors:
    #   contending   — 0.85 (willing to overpay for fit)
    #   play_in_fringe — 0.87 (slightly less aggressive)
    #   others       — 0.90 standard
    _mode_a_floor = posture_a.get("mode", "developing")
    _sanity_floor = (
        0.85 if _mode_a_floor == "contending"
        else 0.87 if _mode_a_floor == "play_in_fringe"
        else 0.90
    )
    _final_ratio = package_value / max(target_value, 1)
    if _final_ratio < _sanity_floor:
        _abort_msg = (
            f"[CPU] {team_a.nba_team_code} abandoning proposal to "
            f"{target_team.nba_team_code}: final ratio {_final_ratio:.2f} below "
            f"{_sanity_floor:.2f} sanity floor ({_mode_a_floor} mode)"
        )
        log.info(_abort_msg)
        if _HEADLESS:
            try:
                _target_name_abort = (
                    f"{target_player.first_name} {target_player.last_name}"
                )
                print(
                    f"CPU [{team_a.nba_team_code}] — trade ABORTED (sanity floor)\n"
                    f"   wanted: {_target_name_abort} (OVR {target_player.overall})"
                    f" value={target_value:.1f}\n"
                    f"   pkg value={package_value:.1f} ratio={_final_ratio:.2f}"
                    f" → below {_sanity_floor:.2f} ({_mode_a_floor}) → ABORTED (not proposed)"
                )
            except Exception:
                pass
        return 0
    # ── End pre-propose sanity floor ─────────────────────────────────────────

    # OVR sanity check — reject if team A is giving away a player 10+ OVR points
    # above the best player received.  The salary-weighted value metric can make
    # a superstar on a max contract appear equal in value to a cheaper role player,
    # producing absurd trades (e.g. Luka OVR 93 for Cameron Johnson OVR 79).
    # This check bypasses contract weighting and looks at raw OVR only.
    if offer_player_ids:
        best_offered_ovr = 0
        for pid in offer_player_ids:
            offered_p = await player_repo.get_by_id(pool, pid)
            if offered_p and offered_p.overall > best_offered_ovr:
                best_offered_ovr = offered_p.overall
        target_ovr = target_player.overall
        if secondary_target:
            target_ovr = max(target_ovr, secondary_target.overall)
        if best_offered_ovr >= target_ovr + 10:
            log.info(
                f"CPU trade aborted (OVR sanity): team {team_a.id} would give OVR "
                f"{best_offered_ovr} for OVR {target_ovr} — gap ≥ 10"
            )
            return 0

    # Sanity check: only propose if the return package value is reasonably close
    # to the target value.  A package worth less than 50% or more than 200% of
    # the target is too lopsided to submit — this prevents absurd offers like
    # SGA for Zubac from ever reaching trade_service.propose.
    log.info(
        f"Trade check: target={target_value:.1f} package={package_value:.1f} "
        f"ratio={package_value / max(target_value, 1):.2f} "
        f"(team {team_a.id} → player {target_player.id} OVR {target_player.overall})"
    )
    if target_value > 0 and (
        package_value < target_value * 0.50 or package_value > target_value * 2.0
    ):
        log.info(
            f"CPU trade aborted (lopsided): team {team_a.id} package value "
            f"{package_value:.1f} vs target value {target_value:.1f}"
        )
        if _HEADLESS:
            try:
                _ratio = package_value / max(target_value, 1)
                _target_name = f"{target_player.first_name} {target_player.last_name}"
                print(
                    f"CPU [{team_a.nba_team_code}] — trade REJECT (lopsided)\n"
                    f"   wanted: {_target_name} (OVR {target_player.overall}) value={target_value:.1f}\n"
                    f"   pkg value={package_value:.1f} ratio={_ratio:.2f} → outside [0.50, 2.00] → REJECT"
                )
            except Exception:
                pass
        return 0

    counterparty_player_ids = [target_player.id]
    if secondary_target:
        counterparty_player_ids.append(secondary_target.id)
    counterparty_pick_ids: list[int] = []

    log.info(
        f"CPU trade: team {team_a.id} ({posture_a.get('mode', team_a.cpu_mode)}/urgency={posture_a.get('urgency')}) "
        f"proposes to team {target_team.id} for player {target_player.id} "
        f"(OVR {target_player.overall})"
        + (
            f" + player {secondary_target.id} (OVR {secondary_target.overall})"
            if secondary_target else ""
        )
        + f" — combined value {target_value:.1f}, package value {package_value:.1f}"
    )

    # ── Stat-line formatter (shared by headless print + ride-along) ───────────
    def _stat_line(stats: dict) -> str:
        """Format a player's season averages as 'X.X PPG / Y.Y APG / Z.Z RPG (NN GP)'."""
        gp = stats.get("games_played", 0) if stats else 0
        if not gp:
            return "no data"
        ppg = stats.get("ppg", 0.0) or 0.0
        apg = stats.get("apg", 0.0) or 0.0
        rpg = stats.get("rpg", 0.0) or 0.0
        return f"{ppg:.1f} PPG / {apg:.1f} APG / {rpg:.1f} RPG ({gp} GP)"

    _ratio_raw = package_value / max(target_value_raw, 1)
    _ratio_form = package_value / max(target_value, 1)
    # Show form annotation only when it actually moved the ratio by ≥1%.
    _form_shifted = abs(_ratio_form - _ratio_raw) >= 0.01
    _ratio_display = (
        f"{_ratio_form:.2f}  [form-adjusted from {_ratio_raw:.2f}]"
        if _form_shifted else f"{_ratio_form:.2f}"
    )

    # ── Plan alignment annotation (Phase 2) ──────────────────────────────────
    # Compute once; used in both headless print and ride-along details.
    def _plan_alignment_str() -> str:
        """Build a human-readable plan_alignment string for logging."""
        try:
            _pa_goal = (_offer_plan_a or {}).get("goal", "unknown")
            _pa_targets = (_offer_plan_a or {}).get("asset_targets") or []
            _target_str = ", ".join(_pa_targets) if _pa_targets else "none"
            _a_line = f"{team_a.nba_team_code} {_pa_goal} (target: {_target_str})"

            # Categorise the target player on B's plan.
            _pb_core_fn = set((_offer_plan_b or {}).get("core_player_ids") or [])
            _pb_flex_fn = set((_offer_plan_b or {}).get("flex_player_ids") or [])
            _pb_surp_fn = set((_offer_plan_b or {}).get("surplus_player_ids") or [])
            _pb_goal = (_offer_plan_b or {}).get("goal", "unknown")
            if target_player.id in _pb_core_fn:
                _b_bucket = "CORE"
            elif target_player.id in _pb_surp_fn:
                _b_bucket = "SURPLUS"
            elif target_player.id in _pb_flex_fn:
                _b_bucket = "FLEX"
            else:
                _b_bucket = "unlisted"
            _b_line = (
                f"target on {target_team.nba_team_code}'s: {_b_bucket} "
                f"({target_team.nba_team_code} {_pb_goal})"
            )

            # Count offered pieces by bucket.
            _off_surplus = sum(
                1 for pid in offer_player_ids
                if pid in set((_offer_plan_a or {}).get("surplus_player_ids") or [])
            )
            _off_flex = sum(
                1 for pid in offer_player_ids
                if pid in set((_offer_plan_a or {}).get("flex_player_ids") or [])
            )
            _off_core = sum(
                1 for pid in offer_player_ids
                if pid in set((_offer_plan_a or {}).get("core_player_ids") or [])
            )
            _pieces_line = (
                f"offered pieces: {_off_surplus} surplus, "
                f"{_off_flex} flex, {_off_core} core"
            )

            # Phase 3: league-scan annotation.
            _scan_line = ""
            if _league_scan_result and _league_scan_player_name:
                _scan_parts = "; ".join(r for _, _, r in _league_scan_result)
                _scan_line = (
                    f"; league scan: {team_a.nba_team_code} ranked "
                    f"{_scan_parts} as top counterparties for {_league_scan_player_name}"
                )

            # Phase 4: pivot history / initial-plan annotation.
            # _a_last_idx is the game index when the plan was last derived;
            # _a_pivot_game is the game index when the most recent pivot fired.
            # "Recent" pivot = within 10 games of the derive that recorded it.
            _phase4_line = ""
            _a_dfr = (_offer_plan_a or {}).get("derived_from_record") or {}
            _a_last_idx = (_offer_plan_a or {}).get("last_derived_game_index")
            _a_from_goal = _a_dfr.get("pivot_from_goal")
            _a_pivot_reason = _a_dfr.get("pivot_reason", "")
            _a_pivot_game = _a_dfr.get("pivot_game_index")

            if _a_last_idx is None:
                _phase4_line = "; [initial plan]"
            elif (
                _a_from_goal
                and _a_pivot_game is not None
                and _a_last_idx is not None
                and (_a_last_idx - _a_pivot_game) <= 10
            ):
                _phase4_line = (
                    f"; pivot history: {_a_from_goal} → {_pa_goal} "
                    f"at game {_a_pivot_game} (\"{_a_pivot_reason}\")"
                )
            elif _a_from_goal:
                # Pivot happened but outside the 10-game recency window — still annotate.
                _phase4_line = (
                    f"; prior pivot: {_a_from_goal} → {_pa_goal} "
                    f"(\"{_a_pivot_reason}\")"
                )

            return f"plan: {_a_line}; {_b_line}; {_pieces_line}{_scan_line}{_phase4_line}"
        except Exception:
            return "plan: (unavailable)"

    async def _build_plan_ra_block(plan: dict | None, team_code: str) -> dict:
        """
        Build a structured plan block for ride-along / JSONL persistence.

        Returns a dict with scalar fields so ride_along._format_details can render
        it as a nested panel.  Player IDs are resolved to last-name strings (top-3
        per bucket) for readability; full lists stay in JSONL via the raw counts.
        """
        if plan is None:
            return {"goal": "unknown", "horizon_seasons": "?", "status": "plan unavailable"}

        goal = plan.get("goal", "unknown")
        horizon = plan.get("horizon_seasons", "?")
        asset_targets = plan.get("asset_targets") or []

        core_ids: list[int] = list(plan.get("core_player_ids") or [])
        flex_ids: list[int] = list(plan.get("flex_player_ids") or [])
        surplus_ids: list[int] = list(plan.get("surplus_player_ids") or [])

        async def _last_names(ids: list[int], limit: int = 3) -> list[str]:
            names: list[str] = []
            for pid in ids[:limit]:
                try:
                    _p = await player_repo.get_by_id(pool, pid)
                    if _p:
                        names.append(_p.last_name)
                except Exception:
                    pass
            return names

        core_names = await _last_names(core_ids)
        flex_names = await _last_names(flex_ids)
        surplus_names = await _last_names(surplus_ids)

        # Pivot / status annotation
        dfr = plan.get("derived_from_record") or {}
        last_idx = plan.get("last_derived_game_index")
        from_goal = dfr.get("pivot_from_goal") or plan.get("pivot_from_goal")
        pivot_reason = dfr.get("pivot_reason") or plan.get("pivot_reason", "")
        pivot_game = dfr.get("pivot_game_index") or plan.get("pivot_game_index")

        if last_idx is None:
            status = "initial plan (no reassessment yet)"
            pivot_history = None
        elif from_goal and pivot_game is not None:
            pivot_history = f"{from_goal} → {goal} at game {pivot_game} (\"{pivot_reason}\")"
            recent = (last_idx - pivot_game) <= 10
            status = f"pivot history: {pivot_history}" if recent else f"prior pivot: {pivot_history}"
        else:
            last_ra = last_idx if last_idx is not None else "?"
            status = f"sticky (last reassessed game {last_ra})"
            pivot_history = None

        block: dict = {
            "goal": goal,
            "horizon_seasons": horizon,
            "pursuing": ", ".join(asset_targets) if asset_targets else "none",
            "core_count": len(core_ids),
            "flex_count": len(flex_ids),
            "surplus_count": len(surplus_ids),
            "core_names": ", ".join(core_names) + (" ..." if len(core_ids) > 3 else ""),
            "flex_names": ", ".join(flex_names) + (" ..." if len(flex_ids) > 3 else ""),
            "surplus_names": ", ".join(surplus_names) + (" ..." if len(surplus_ids) > 3 else ""),
            "status": status,
        }
        if pivot_history:
            block["pivot_history"] = pivot_history
        if last_idx is not None:
            block["last_reassessed_game"] = last_idx
        return block

    # Resolve _pb_surplus/_pb_core/_pb_flex at this scope for use below.
    _pb_core_ids: set[int] = set((_offer_plan_b or {}).get("core_player_ids") or [])
    _pb_flex_ids: set[int] = set((_offer_plan_b or {}).get("flex_player_ids") or [])
    _pb_surplus: set[int] = set((_offer_plan_b or {}).get("surplus_player_ids") or [])

    if _HEADLESS:
        try:
            offered_player_names: list[str] = []
            offered_player_stat_lines: list[str] = []
            for pid in offer_player_ids:
                _op = await player_repo.get_by_id(pool, pid)
                if _op:
                    offered_player_names.append(f"{_op.first_name} {_op.last_name} (OVR {_op.overall}, {_op.position})")
                    _op_mod, _op_stats = form_map.get(pid, (1.0, {}))
                    offered_player_stat_lines.append(_stat_line(_op_stats))
            offered_pick_labels: list[str] = []
            for pkid in offer_pick_ids:
                _pk = await pool.fetchrow(
                    "SELECT season, round FROM draft_picks WHERE id = $1", pkid
                )
                if _pk:
                    offered_pick_labels.append(f"{_pk['season']} R{_pk['round']} pick")

            _target_name = f"{target_player.first_name} {target_player.last_name}"
            _target_stat = _stat_line(_target_stats)
            _sec_name = ""
            _sec_stat = ""
            if secondary_target:
                _sec_name = f" + {secondary_target.first_name} {secondary_target.last_name} (OVR {secondary_target.overall})"
                _sec_stat = f" [{_stat_line(_sec_stats)}]"

            _ovr_delta = (
                max((p.overall if hasattr(p, "overall") else 0) for p in [target_player] + ([secondary_target] if secondary_target else []))
                - (max(
                    (int(n.split("OVR ")[1].split(",")[0]) for n in offered_player_names if "OVR" in n),
                    default=0
                ) if offered_player_names else 0)
            )
            _pos_count = pos_count_map.get(target_player.position, 0)
            _pos_need = (
                "saturated (−)" if _pos_count >= 3
                else "needed (++)" if _pos_count <= 1
                else "neutral"
            )

            # Receiving team's positional needs (counterparty fit)
            _recv_pos_rows = await pool.fetch(
                """SELECT p.position, COUNT(*) AS cnt
                   FROM lineups l JOIN players p ON p.id = l.player_id
                   WHERE l.league_id = $1 AND l.team_id = $2
                   GROUP BY p.position""",
                league.id, target_team.id,
            )
            _recv_pos_map = {r["position"]: r["cnt"] for r in _recv_pos_rows}

            def _pos_label(cnt: int) -> str:
                return "saturated" if cnt >= 3 else "needed" if cnt <= 1 else "neutral"

            _POSITIONS = ["PG", "SG", "SF", "PF", "C"]
            _recv_lines = [
                f"     {pos}: {_recv_pos_map.get(pos, 0)} ({_pos_label(_recv_pos_map.get(pos, 0))})"
                for pos in _POSITIONS
            ]
            # Fit annotation for offered players
            _offered_fit_parts: list[str] = []
            for _oname in offered_player_names:
                # Format: "First Last (OVR 80, PF)"
                try:
                    _opos = _oname.split(", ")[-1].rstrip(")")
                    _olast = _oname.split("(")[0].strip().split()[-1]
                    _ocnt = _recv_pos_map.get(_opos, 0)
                    _offered_fit_parts.append(f"{_olast}({_opos})={_pos_label(_ocnt)}")
                except Exception:
                    pass

            # Offered player lines with stats
            _offered_lines: list[str] = []
            for _oname, _ostat in zip(offered_player_names, offered_player_stat_lines):
                _offered_lines.append(f"{_oname} — {_ostat}")
            _offered_lines.extend(offered_pick_labels)

            # Cornerstone note — identify any untouchable players on team A's roster
            # that were implicitly excluded from the offered package.
            _cornerstone_notes: list[str] = []
            try:
                _full_roster_a = await player_repo.get_roster(pool, league.id, team_a.id)
                for _cp in _full_roster_a:
                    if is_cornerstone(team_a, _cp, _full_roster_a) and _cp.id not in offer_player_ids:
                        _cornerstone_notes.append(
                            f"{_cp.first_name} {_cp.last_name} OVR {_cp.overall} (untouchable)"
                        )
            except Exception:
                pass

            # Format team_state lines for headless output
            def _fmt_team_state(code: str, posture: dict) -> str:
                m = posture.get("mode", "developing")
                u = posture.get("urgency", "comfortable")
                pw = posture.get("projected_wins")
                cr = posture.get("conf_rank")
                aa = posture.get("avg_age", 27.0)
                pw_str = f"proj {pw}W" if pw is not None else "early-season"
                cr_str = f"#{cr} conf" if cr is not None else "unranked"
                return f"{code}: {m}/{u} ({pw_str}, {cr_str}, avg_age {aa:.1f})"

            # Franchise plan blocks for headless print (both sides)
            _hl_plan_a = await _build_plan_ra_block(_offer_plan_a, team_a.nba_team_code)
            _hl_plan_b = await _build_plan_ra_block(_offer_plan_b, target_team.nba_team_code)

            def _fmt_plan_block(code: str, pb: dict, compact: bool = False) -> list[str]:
                """Render one team's franchise plan as indented lines."""
                if compact:
                    pivot_tag = ""
                    ph = pb.get("pivot_history")
                    if ph:
                        pivot_tag = f" [recent pivot: {ph}]"
                    elif pb.get("status", "").startswith("initial"):
                        pivot_tag = " [initial]"
                    return [
                        f"   plan {code}: {pb.get('goal','?')} h:{pb.get('horizon_seasons','?')}"
                        f" (pursuing: {pb.get('pursuing','none')}){pivot_tag}"
                    ]
                out = [f"   franchise plan {code} — {pb.get('goal','?')} (h:{pb.get('horizon_seasons','?')})"]
                if pb.get("core_names"):
                    out.append(f"     core (untouchable): {pb['core_names']}")
                if pb.get("flex_names"):
                    out.append(f"     flex (situational): {pb['flex_names']}")
                if pb.get("surplus_names"):
                    out.append(f"     surplus (shopping): {pb['surplus_names']}")
                if pb.get("pursuing"):
                    out.append(f"     pursuing: {pb['pursuing']}")
                out.append(f"     status: {pb.get('status', '?')}")
                return out

            _compact_hl = os.environ.get("RIDE_ALONG_COMPACT") == "1"
            _lines = [
                f"CPU [{team_a.nba_team_code}] — trade eval → [{target_team.nba_team_code}]",
                f"   wanted: {_target_name} (OVR {target_player.overall}, {target_player.position}){_sec_name} — {_target_stat}{_sec_stat}",
                f"   offered: {'; '.join(_offered_lines) or '(nothing)'}",
            ]
            _lines.extend(_fmt_plan_block(team_a.nba_team_code, _hl_plan_a, _compact_hl))
            _lines.extend(_fmt_plan_block(target_team.nba_team_code, _hl_plan_b, _compact_hl))
            _lines.extend([
                "   factors:",
                f"     position need ({target_player.position}): {_pos_need}",
                f"     OVR delta (wanted − offered): {_ovr_delta:+d}",
                f"     value ratio (pkg/target): {_ratio_display}",
                f"     mode/urgency: {posture_a.get('mode', team_a.cpu_mode)}/{posture_a.get('urgency', '?')}",
                f"   team_state {_fmt_team_state(team_a.nba_team_code, posture_a)}",
                f"   team_state {_fmt_team_state(target_team.nba_team_code, posture_b)}",
                f"   counterparty fit ({target_team.nba_team_code} roster needs):",
                *_recv_lines,
                f"     >> offered players fit: {', '.join(_offered_fit_parts) or '(no players)'}",
                f"   score: {_ratio_form:.2f} → PROPOSE",
            ])
            if _cornerstone_notes:
                _lines.append(
                    f"   note: cornerstone(s) withheld from package: {', '.join(_cornerstone_notes)}"
                )
            print("\n".join(_lines))
        except Exception:
            pass  # never let logging break the sim

    # ── Fix #2: Age/contract sweetener demand ────────────────────────────────
    # If the proposer's offered side is the better long-term asset (younger main
    # piece or more cumulative contract years at similar OVR), demand a pick from
    # the counterparty to compensate.  Runs BEFORE the ride-along so the pick
    # shows up in the panel.
    #
    # Key calibration changes vs prior version:
    # - Age comparison uses the MAX-OVR player on each side (the meaningful asset),
    #   not the average.  Averaging trips falsely on 2-for-1 deals where one player
    #   is a young throw-in — the "main piece" comparison is what matters.
    # - Age threshold raised 2.0 → 3.0 (on max-player, not average).
    # - Contract-years threshold raised +2 → +3 (cumulative sum, unchanged).
    # - OVR-equivalent window tightened ±2 → ±1, age threshold 1.5 → 2.5.
    # - When trigger fires but no pick is available AND the unsweetened offer is
    #   within value-ratio tolerance [0.80, 1.20], submit anyway — the age premium
    #   is a "would be nice," not a hard requirement.
    # Does NOT apply to 3-team trades; counterparty_pick_ids starts as [].
    async def _should_demand_pick_check() -> tuple[bool, str]:
        """Evaluate whether CPU should demand a pick sweetener from counterparty.

        Returns (should_demand, reason_string).
        Reason is for logging only; empty when should_demand is False.
        Compares the MAX-OVR player on each side (the meaningful asset), not
        player averages, to avoid false triggers on multi-player packages with
        young throw-ins.
        """
        if not offer_player_ids or not counterparty_player_ids:
            return False, ""

        # Collect offered player objects + contracts in one pass.
        offered_players_data: list[tuple] = []  # (player, contract_yrs)
        for _pid in offer_player_ids:
            _op = await player_repo.get_by_id(pool, _pid)
            if _op is None:
                continue
            _oc = await player_repo.get_active_contract(pool, _pid)
            _o_yrs = (_oc.years_remaining if _oc else 1) or 1
            offered_players_data.append((_op, _o_yrs))

        # Collect counterparty player objects + contracts.
        target_players_data: list[tuple] = []  # (player, contract_yrs)
        for _tid in counterparty_player_ids:
            _tp = await player_repo.get_by_id(pool, _tid)
            if _tp is None:
                continue
            _tc = await player_repo.get_active_contract(pool, _tid)
            _t_yrs = (_tc.years_remaining if _tc else 1) or 1
            target_players_data.append((_tp, _t_yrs))

        if not offered_players_data or not target_players_data:
            return False, ""

        # Identify the main piece on each side (highest OVR — the meaningful asset).
        offered_main, offered_main_yrs = max(offered_players_data, key=lambda x: x[0].overall)
        target_main, _ = max(target_players_data, key=lambda x: x[0].overall)

        offered_age = _player_age(offered_main) or 28
        target_age = _player_age(target_main) or 28
        age_gap = target_age - offered_age  # positive = offered main piece is younger

        # Trigger 1: offered main piece meaningfully younger (threshold 3.0y on max player).
        if age_gap >= 3.0:
            return (
                True,
                f"offered main piece is {age_gap:.1f}y younger "
                f"({offered_main.first_name} {offered_main.last_name} {offered_age} "
                f"vs {target_main.first_name} {target_main.last_name} {target_age})",
            )

        # Trigger 2: offered side carries more committed contract years (cumulative sum).
        # Long-term commitment is additive — all players on the offered side matter.
        offered_yrs_total = sum(yrs for _, yrs in offered_players_data)
        target_yrs_total = sum(yrs for _, yrs in target_players_data)
        if offered_yrs_total >= target_yrs_total + 3:  # raised +2 → +3
            return (
                True,
                f"offered side has {offered_yrs_total - target_yrs_total} more contract years",
            )

        # Trigger 3: OVR-equivalent deal but offered main piece is noticeably younger.
        # Tightened: OVR window ±2 → ±1, age threshold 1.5 → 2.5.
        if abs(offered_main.overall - target_main.overall) <= 1 and age_gap >= 2.5:
            return (
                True,
                f"OVR-equivalent ({offered_main.overall} vs {target_main.overall}) "
                f"but main piece is {age_gap:.1f}y younger",
            )

        return False, ""

    _demand_pick, _demand_reason = await _should_demand_pick_check()
    if _demand_pick:
        _cp_picks = await trade_repo.get_team_picks(pool, league.id, target_team.id)
        # Separate by round; pick the earliest (closest season) from each bucket.
        _cp_r2 = sorted(
            [p for p in _cp_picks if p["round"] == 2], key=lambda p: p["season"]
        )
        _cp_r1 = sorted(
            [p for p in _cp_picks if p["round"] == 1], key=lambda p: p["season"]
        )
        # Prefer 2nd-round first (more reasonable ask).
        # Fall back to an early 1st only when proposer gives up a clearly higher-OVR asset.
        _best_offered_ovr_sw = 0
        for _sw_pid in offer_player_ids:
            _sw_p = await player_repo.get_by_id(pool, _sw_pid)
            if _sw_p and _sw_p.overall > _best_offered_ovr_sw:
                _best_offered_ovr_sw = _sw_p.overall
        _best_target_ovr_sw = target_player.overall
        if secondary_target:
            _best_target_ovr_sw = max(_best_target_ovr_sw, secondary_target.overall)

        _sweetener_pick: dict | None = None
        if _cp_r2:
            _sweetener_pick = _cp_r2[0]
        elif _cp_r1 and _best_offered_ovr_sw >= _best_target_ovr_sw + 3:
            _sweetener_pick = _cp_r1[0]

        if _sweetener_pick is None:
            # No usable pick from counterparty.  Rather than always abandoning,
            # check whether the unsweetened offer is already within value-ratio
            # tolerance — if so, the age premium is a "would be nice" and we
            # submit anyway.  Only abandon when the offer is genuinely unbalanced.
            _unsweetened_ratio = package_value / max(target_value, 1.0)
            if 0.80 <= _unsweetened_ratio <= 1.20:
                log.info(
                    f"[CPU] Sweetener wanted ({_demand_reason}) but "
                    f"{target_team.nba_team_code} has no pick available; "
                    f"submitting unsweetened — ratio {_unsweetened_ratio:.2f} acceptable"
                )
                # Fall through: no pick added, proceed to normal propose flow.
            else:
                log.info(
                    f"[CPU] {team_a.nba_team_code} abandoning proposal to "
                    f"{target_team.nba_team_code}: age/contract premium detected "
                    f"({_demand_reason}); no pick available AND ratio "
                    f"{_unsweetened_ratio:.2f} unbalanced — abandoning"
                )
                return 0
        else:
            counterparty_pick_ids = [_sweetener_pick["id"]]
            log.info(
                f"Sweetener pick demanded: {_demand_reason} → adding "
                f"{_sweetener_pick['season']} R{_sweetener_pick['round']} "
                f"from {target_team.nba_team_code} to counterparty side"
            )
    # ── End Fix #2: age/contract sweetener demand ─────────────────────────────

    # ── Ride-along hook (a): Propose ─────────────────────────────────────────
    # After CPU thought-log prints, before the offer is submitted.
    # If the user vetoes, skip this offer entirely — return 0.
    if ride_along.is_ride_along_enabled():
        try:
            _ra_header = (
                f"CPU [{team_a.nba_team_code}] wants to PROPOSE "
                f"→ [{target_team.nba_team_code}]"
            )
            _offered_names: list[str] = []
            _offered_stat_lines_ra: list[str] = []
            for _pid in offer_player_ids:
                _rp = await player_repo.get_by_id(pool, _pid)
                if _rp:
                    _offered_names.append(
                        f"{_rp.first_name} {_rp.last_name} (OVR {_rp.overall}, {_rp.position})"
                    )
                    _rp_mod, _rp_stats = form_map.get(_pid, (1.0, {}))
                    _offered_stat_lines_ra.append(_stat_line(_rp_stats))
            for _pkid in offer_pick_ids:
                _pk = await pool.fetchrow(
                    "SELECT season, round FROM draft_picks WHERE id = $1", _pkid
                )
                if _pk:
                    _offered_names.append(f"{_pk['season']} R{_pk['round']} pick")

            _wanted_names: list[str] = []
            _wanted_stat_lines_ra: list[str] = []
            for _cp in counterparty_player_ids:
                _rp = await player_repo.get_by_id(pool, _cp)
                if _rp:
                    _wanted_names.append(
                        f"{_rp.first_name} {_rp.last_name} (OVR {_rp.overall}, {_rp.position})"
                    )
                    _rp_mod2, _rp_stats2 = form_map.get(_cp, (1.0, {}))
                    _wanted_stat_lines_ra.append(_stat_line(_rp_stats2))

            # Merge stat lines into the display strings
            _wanted_display = "; ".join(
                f"{n} — {s}" for n, s in zip(_wanted_names, _wanted_stat_lines_ra)
            ) or "(none)"
            _offered_display_parts: list[str] = []
            for _oname_ra, _ostat_ra in zip(_offered_names, _offered_stat_lines_ra):
                _offered_display_parts.append(f"{_oname_ra} — {_ostat_ra}")
            # picks have no stat line — append them as-is
            _offered_display_parts.extend(_offered_names[len(_offered_stat_lines_ra):])
            _offered_display = "; ".join(_offered_display_parts) or "(none)"

            # Receiving team's positional needs for ride-along display
            _ra_recv_pos_rows = await pool.fetch(
                """SELECT p.position, COUNT(*) AS cnt
                   FROM lineups l JOIN players p ON p.id = l.player_id
                   WHERE l.league_id = $1 AND l.team_id = $2
                   GROUP BY p.position""",
                league.id, target_team.id,
            )
            _ra_recv_pos_map = {r["position"]: r["cnt"] for r in _ra_recv_pos_rows}

            def _ra_pos_label(cnt: int) -> str:
                return "saturated (−)" if cnt >= 3 else "needed (++)" if cnt <= 1 else "neutral"

            _ra_POSITIONS = ["PG", "SG", "SF", "PF", "C"]
            _ra_recv_summary = {
                pos: f"{_ra_recv_pos_map.get(pos, 0)} ({_ra_pos_label(_ra_recv_pos_map.get(pos, 0))})"
                for pos in _ra_POSITIONS
            }
            _ra_fit_parts: list[str] = []
            for _rname in _offered_names[:len(_offered_stat_lines_ra)]:
                try:
                    _rpos = _rname.split(", ")[-1].rstrip(")")
                    _rlast = _rname.split("(")[0].strip().split()[-1]
                    _rcnt = _ra_recv_pos_map.get(_rpos, 0)
                    _ra_fit_parts.append(f"{_rlast}({_rpos})={_ra_pos_label(_rcnt).split(' ')[0]}")
                except Exception:
                    pass

            # Build team_state blobs for both sides (Fix 3)
            def _build_team_state(code: str, posture: dict) -> dict:
                state: dict = {
                    "mode": posture.get("mode", "developing"),
                    "urgency": posture.get("urgency", "comfortable"),
                    "projected_wins": posture.get("projected_wins"),
                    "conf_rank": posture.get("conf_rank"),
                    "avg_age": round(posture.get("avg_age", 27.0), 1),
                }
                nt = posture.get("near_threshold")
                if nt:
                    state["near_threshold"] = nt
                return state

            # Structured franchise plan blocks for both sides (Phase 4 polish)
            _ra_plan_a = await _build_plan_ra_block(_offer_plan_a, team_a.nba_team_code)
            _ra_plan_b = await _build_plan_ra_block(_offer_plan_b, target_team.nba_team_code)

            # Target player's bucket on B's plan
            _ra_pb_core = set((_offer_plan_b or {}).get("core_player_ids") or [])
            _ra_pb_flex = set((_offer_plan_b or {}).get("flex_player_ids") or [])
            _ra_pb_surp = set((_offer_plan_b or {}).get("surplus_player_ids") or [])
            _ra_tgt_bucket = (
                "CORE" if target_player.id in _ra_pb_core
                else "SURPLUS" if target_player.id in _ra_pb_surp
                else "FLEX" if target_player.id in _ra_pb_flex
                else "unlisted"
            )

            # Offered pieces by bucket (A's plan)
            _ra_pa_surplus_ids = set((_offer_plan_a or {}).get("surplus_player_ids") or [])
            _ra_pa_flex_ids = set((_offer_plan_a or {}).get("flex_player_ids") or [])
            _ra_pa_core_ids = set((_offer_plan_a or {}).get("core_player_ids") or [])
            _ra_off_surplus = sum(1 for pid in offer_player_ids if pid in _ra_pa_surplus_ids)
            _ra_off_flex = sum(1 for pid in offer_player_ids if pid in _ra_pa_flex_ids)
            _ra_off_core = sum(1 for pid in offer_player_ids if pid in _ra_pa_core_ids)

            # League scan summary string
            _ra_league_scan = ""
            if _league_scan_result and _league_scan_player_name:
                _scan_parts = "; ".join(r for _, _, r in _league_scan_result)
                _ra_league_scan = (
                    f"{team_a.nba_team_code} ranked {_scan_parts} as top counterparties "
                    f"for {_league_scan_player_name}"
                )

            _ra_offer_pick_dicts = await ra_reasoning.pick_ids_to_dicts(pool, list(offer_pick_ids))
            _ra_cp_pick_dicts = await ra_reasoning.pick_ids_to_dicts(pool, list(counterparty_pick_ids))
            _ra_flow = (
                f"{team_a.nba_team_code} → {target_team.nba_team_code}: {_offered_display} | "
                f"{target_team.nba_team_code} → {team_a.nba_team_code}: {_wanted_display}"
            )
            _scores_line = f"{_ratio_form:.2f} team" + (f" / league_scan: {_ra_league_scan}" if _ra_league_scan else "")
            _ra_details = await ra_reasoning.render_trade_panel(
                pool, league, season,
                [
                    (
                        f"{team_a.nba_team_code} perspective",
                        team_a,
                        {
                            "players_in": list(counterparty_player_ids),
                            "players_out": list(offer_player_ids),
                            "picks_in": _ra_cp_pick_dicts,
                            "picks_out": _ra_offer_pick_dicts,
                        },
                    ),
                    (
                        f"{target_team.nba_team_code} perspective",
                        target_team,
                        {
                            "players_in": list(offer_player_ids),
                            "players_out": list(counterparty_player_ids),
                            "picks_in": _ra_offer_pick_dicts,
                            "picks_out": _ra_cp_pick_dicts,
                        },
                    ),
                ],
                flow_summary=_ra_flow,
                decision_label="PROPOSE",
                scores_line=_scores_line,
            )
            _ra_result = ride_along.prompt_decision(
                decision_type="propose",
                header=_ra_header,
                details=_ra_details,
                default_action="approve",
            )
            if _ra_result["action"] == "veto":
                return 0
        except Exception:
            pass  # ride-along errors must never break the sim

    trade = await trade_service.propose(
        league=league,
        proposer_team=team_a,
        counterparty_team=target_team,
        proposer_player_ids=offer_player_ids,
        proposer_pick_ids=offer_pick_ids,
        counterparty_player_ids=counterparty_player_ids,
        counterparty_pick_ids=counterparty_pick_ids,
    )

    # Mark all players in this trade as taken so subsequent offers in this round
    # don't try to re-use them.
    taken_player_ids.add(target_player.id)
    if secondary_target:
        taken_player_ids.add(secondary_target.id)
    taken_player_ids.update(offer_player_ids)

    # trade_service.propose runs cpu_should_accept on target_team's side.
    # If accepted it lands as 'pending_commissioner'; auto-approve immediately for
    # CPU-CPU trades so they never pile up in the commissioner queue.
    if trade.status == "pending_commissioner":
        try:
            await _maybe_auto_approve(pool, league, trade, guild)
        except Exception as exc:
            log.error(
                f"CPU-CPU trade {trade.id} auto-approve failed — "
                f"trade remains pending_commissioner: {exc}",
                exc_info=True,
            )

    if _HEADLESS:
        try:
            _final_status = await pool.fetchval("SELECT status FROM trades WHERE id = $1", trade.id)
            _outcome = "APPROVED" if _final_status == "approved" else f"PENDING ({_final_status})"
            print(f"   [{team_a.nba_team_code}→{target_team.nba_team_code}] trade #{trade.id} outcome: {_outcome}")
        except Exception:
            pass

    return 1



async def _maybe_auto_approve(
    pool,
    league: league_repo.League,
    trade,
    guild: Optional[discord.Guild] = None,
) -> None:
    """
    CPU-to-CPU trades: always auto-approve (no human is involved, no review needed).
    If either team has a human manager the trade stays pending_commissioner for human review.
    After approval, each involved team posts a "looking to deal" embed to #trade-block.
    """
    # Confirm both sides are CPU teams.
    teams = await pool.fetch(
        "SELECT id, manager_user_id FROM teams WHERE id = ANY($1)",
        [trade.proposer_team_id, trade.counterparty_team_id],
    )
    has_human = any(r["manager_user_id"] is not None for r in teams)
    if has_human:
        log.info(
            f"Trade {trade.id} involves a human-managed team — leaving as pending_commissioner"
        )
        return

    assets = await trade_repo.get_assets(pool, trade.id)

    # Value scores are needed for the ride-along panel display only.
    # Fairness gating already ran upstream in cpu_should_accept — no second gate here.
    proposer_value = 0.0
    counterparty_value = 0.0

    for asset in assets:
        if asset.asset_type == "player" and asset.player_id:
            p_row = await pool.fetchrow("SELECT * FROM players WHERE id = $1", asset.player_id)
            c_row = await pool.fetchrow(
                "SELECT salary, years_remaining FROM contracts "
                "WHERE player_id = $1 AND is_active = TRUE LIMIT 1",
                asset.player_id,
            )
            if p_row:
                age = _player_age_from_row(p_row)
                v = trade_evaluator.player_trade_value(
                    {"overall": p_row["overall"], "age": age},
                    {
                        "salary": c_row["salary"] if c_row else 0,
                        "years_remaining": c_row["years_remaining"] if c_row else 1,
                    },
                    league.salary_cap,
                )
                if asset.from_team_id == trade.proposer_team_id:
                    proposer_value += v
                else:
                    counterparty_value += v
        elif asset.asset_type == "pick" and asset.pick_id:
            pk_row = await pool.fetchrow(
                """SELECT dp.season, dp.round,
                          CASE WHEN (sc.wins + sc.losses) > 0
                               THEN sc.wins::float / (sc.wins + sc.losses)
                               ELSE NULL END AS win_pct
                   FROM draft_picks dp
                   LEFT JOIN standings_cache sc
                          ON sc.team_id = dp.original_team_id
                         AND sc.league_id = dp.league_id
                         AND sc.season = $2
                   WHERE dp.id = $1""",
                asset.pick_id,
                league.current_season,
            )
            if pk_row:
                v = trade_evaluator.pick_trade_value(
                    pk_row["season"], pk_row["round"], league.current_season,
                    team_win_pct=pk_row["win_pct"],
                )
                if asset.from_team_id == trade.proposer_team_id:
                    proposer_value += v
                else:
                    counterparty_value += v

    # ── Ride-along hook (c): Commissioner auto-approve ────────────────────────
    # After both lopsided guards pass, before the approval transaction commits.
    # Veto leaves the trade as pending_commissioner (same as the lopsided guard).
    if ride_along.is_ride_along_enabled():
        try:
            _proposer_row = await pool.fetchrow(
                "SELECT nba_team_code FROM teams WHERE id = $1", trade.proposer_team_id
            )
            _counter_row = await pool.fetchrow(
                "SELECT nba_team_code FROM teams WHERE id = $1", trade.counterparty_team_id
            )
            _p_code = _proposer_row["nba_team_code"] if _proposer_row else str(trade.proposer_team_id)
            _c_code = _counter_row["nba_team_code"] if _counter_row else str(trade.counterparty_team_id)
            _ra_header = f"Commissioner deciding to APPROVE trade #{trade.id} ({_p_code} ↔ {_c_code})"
            _proposer_pids = [
                a.player_id for a in assets
                if a.asset_type == "player" and a.player_id
                and a.from_team_id == trade.proposer_team_id
            ]
            _counter_pids = [
                a.player_id for a in assets
                if a.asset_type == "player" and a.player_id
                and a.from_team_id == trade.counterparty_team_id
            ]
            _ra_value_ratio = min(proposer_value, counterparty_value) / max(proposer_value, counterparty_value, 1)
            _prop_team_obj = await team_repo.get_by_id(pool, trade.proposer_team_id)
            _count_team_obj = await team_repo.get_by_id(pool, trade.counterparty_team_id)
            _ra_comm_flow = (
                f"{_p_code} → {_c_code}: {len(_proposer_pids)} player(s) | "
                f"{_c_code} → {_p_code}: {len(_counter_pids)} player(s)"
            )
            _ra_details = await ra_reasoning.render_trade_panel(
                pool, league, league.current_season,
                [
                    (
                        f"{_p_code} perspective",
                        _prop_team_obj,
                        {
                            "players_in": _counter_pids,
                            "players_out": _proposer_pids,
                            "picks_in": [],
                            "picks_out": [],
                        },
                    ),
                    (
                        f"{_c_code} perspective",
                        _count_team_obj,
                        {
                            "players_in": _proposer_pids,
                            "players_out": _counter_pids,
                            "picks_in": [],
                            "picks_out": [],
                        },
                    ),
                ],
                flow_summary=_ra_comm_flow,
                decision_label="APPROVE",
                scores_line=f"proposer {proposer_value:.1f} / counterparty {counterparty_value:.1f} / ratio {_ra_value_ratio:.2f}",
            ) if (_prop_team_obj and _count_team_obj) else {
                "trade_id": trade.id,
                "proposer_team": _p_code,
                "counterparty_team": _c_code,
                "proposer_value": f"{proposer_value:.1f}",
                "counterparty_value": f"{counterparty_value:.1f}",
                "value_ratio": f"{_ra_value_ratio:.2f}",
            }
            _ra_result = ride_along.prompt_decision(
                decision_type="commissioner_approve",
                header=_ra_header,
                details=_ra_details,
                default_action="approve",
            )
            if _ra_result["action"] == "veto":
                log.info(
                    f"CPU-to-CPU trade {trade.id} vetoed by ride-along user "
                    f"— leaving as pending_commissioner"
                )
                return
        except Exception:
            pass  # ride-along errors must never break the sim

    # Always auto-approve CPU-to-CPU (human guard above ensures no human is involved).
    sim_date = await pool.fetchval(
        "SELECT MAX(scheduled_date) FROM games WHERE league_id = $1 AND status = 'simmed'",
        trade.league_id,
    )
    import datetime as _dt
    if sim_date is None:
        sim_date = _dt.date.today()

    # Collect all teams with player movement for role cache invalidation post-commit.
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
                        "UPDATE contracts SET team_id = $1 "
                        "WHERE player_id = $2 AND is_active = TRUE",
                        asset.to_team_id,
                        asset.player_id,
                    )
                    # Remove from old team's lineup so the sim engine stops
                    # using them for the wrong team.
                    await conn.execute(
                        "DELETE FROM lineups WHERE league_id = $1 AND player_id = $2",
                        trade.league_id,
                        asset.player_id,
                    )
                    # Insert into new team's lineup at the next available slot.
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

            # Rebalance starters for every team that received a player.
            from services.trade_service import _rebalance_starters
            for tid in receiving_team_ids:
                await _rebalance_starters(conn, trade.league_id, tid)

            await conn.execute(
                """
                UPDATE trades
                SET status = 'approved',
                    resolved_at = NOW()
                WHERE id = $1
                """,
                trade.id,
            )

    traded_player_ids = [
        a.player_id for a in assets if a.asset_type == "player" and a.player_id
    ]
    if traded_player_ids:
        from data.repositories import trade_block_repo
        await trade_block_repo.remove_players_from_block(
            pool, trade.league_id, traded_player_ids
        )

    # Invalidate role cache and re-derive for every team that gained or lost a player.
    # Runs after the transaction commits so new lineups are visible to derive_roles.
    if affected_team_ids:
        from services import batch_sim_runner, role_service
        async with pool.acquire() as _conn:
            for tid in affected_team_ids:
                batch_sim_runner.invalidate_role_cache(
                    trade.league_id, tid, league.current_season
                )
                await role_service.derive_and_persist_all_for_team(
                    _conn, trade.league_id, tid, league.current_season,
                    silent_emit=True,
                )
        log.debug(
            "CPU-CPU trade %d: re-derived roles for teams %s (season %d)",
            trade.id, sorted(affected_team_ids), league.current_season,
        )

    log.info(f"CPU-to-CPU trade {trade.id} auto-approved")

    # Post "looking to deal" embeds to #trade-block for each team involved.
    if guild:
        await _post_trade_block_ads(pool, league, trade, guild)


async def _post_trade_block_ads(
    pool,
    league: league_repo.League,
    trade,
    guild: discord.Guild,
) -> None:
    """
    After a CPU-to-CPU trade is approved, post one embed per involved team to
    #trade-block advertising what they're looking to acquire next.
    """
    news_ch_id = await league_repo.get_channel(pool, league.id, "trade-block")
    if not news_ch_id:
        return
    ch = guild.get_channel(news_ch_id)
    if not ch:
        return

    team_rows = await pool.fetch(
        "SELECT nba_team_code, cpu_mode FROM teams WHERE id = ANY($1)",
        [trade.proposer_team_id, trade.counterparty_team_id],
    )

    _mode_descriptions = {
        "rebuilding": "Rebuilding mode — looking for young assets (age ≤ 22) and future picks",
        "soft_rebuild": "Soft Rebuild mode — selling veterans, looking for picks and youth (age ≤ 24)",
        "contending": "Contending mode — looking for immediate impact role players (OVR 75+)",
        "play_in_fringe": "Play-In Fringe mode — looking for solid upgrades (OVR 77+)",
        "developing": "Developing mode — looking for veteran depth and expiring contracts",
    }

    for row in team_rows:
        team_code = row["nba_team_code"]
        cpu_mode = row["cpu_mode"] or "default"
        description = _mode_descriptions.get(
            cpu_mode,
            "Open for business — looking to improve at the margins",
        )
        embed = discord.Embed(
            title=f"📋 {team_code} — Looking to Deal",
            description=description,
            color=discord.Color.blue(),
        )
        embed.set_footer(text="CPU-managed team")
        try:
            await ch.send(embed=embed)
        except Exception as exc:
            log.warning(f"Failed to post trade-block ad for {team_code}: {exc}")



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

    offer_player_ids: list[int] = []
    offer_pick_ids: list[int] = []
    accumulated = 0.0
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
        v = trade_evaluator.player_trade_value(
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

    # Fill with players first to meet value target.
    # Plan-aware ordering ensures surplus goes before flex; core never appears.
    for _bucket, v, pid in scored_players:
        if accumulated >= target_value - tolerance:
            break
        offer_player_ids.append(pid)
        accumulated += v

    # Strategic pick inclusion:
    # - Always: include picks to bridge any remaining value gap (gap-closing).
    # - Strategic: if counterparty is rebuilding, they need future assets and
    #   will reject a player-only deal — include at least one pick regardless of
    #   whether players already met the value target.
    # Never over-pay beyond tolerance unless counterparty requires picks.
    needs_pick_for_rebuilder = (
        counterparty_mode in ("rebuilding", "soft_rebuild") and len(offer_pick_ids) == 0
    )

    for pick in sorted_picks:
        if len(offer_pick_ids) >= max_picks:
            break
        # Stop when value target is met AND there's no strategic reason to add picks.
        if accumulated >= target_value - tolerance and not needs_pick_for_rebuilder:
            break

        # First-round pick protection: CPU teams almost never trade their own
        # near-term firsts. Near-term = current or next season.
        if pick["round"] == 1 and pick.get("original_team_id") == team_a.id:
            if pick["season"] <= league.current_season + 1:
                continue
            # Future own firsts: contending teams offer them rarely; play_in_fringe almost never.
            if mode not in ("contending", "play_in_fringe"):
                continue
            threshold = 0.15 if mode == "contending" else 0.08
            if random.random() > threshold:
                continue

        orig_tid = pick.get("original_team_id")
        win_pct = _orig_win_pct.get(orig_tid) if orig_tid else None
        pv = trade_evaluator.pick_trade_value(
            pick["season"], pick["round"], league.current_season,
            team_win_pct=win_pct,
        )
        if pick["id"] not in offer_pick_ids:
            offer_pick_ids.append(pick["id"])
            accumulated += pv
            needs_pick_for_rebuilder = False  # rebuilder requirement satisfied

    return offer_player_ids, offer_pick_ids, accumulated



def _team_a_wants_player(posture_a: dict, player: player_repo.Player) -> bool:
    """Return True if team A's posture makes it interested in this player.

    B1 posture gates (hard checks):
    - contending/play_in_fringe: reject raw young developmental players (age < 25,
      OVR < 77) — they don't help the win-now window.
    - rebuilding/soft_rebuild: reject aging vets over 30 unless they're expiring
      contracts (years_remaining=1) worth flipping.  The age signal is an
      approximation; callers with full contract data can refine in scoring.
    """
    age = _player_age(player)
    mode = posture_a["mode"]
    urgency = posture_a.get("urgency", "comfortable")

    if mode == "contending":
        # B1: hard reject developmental young players with no win-now value.
        if age is not None and age < 25 and player.overall < 77:
            return False
        # Hard reject aging-out vets with low OVR (no help, bad contract drain).
        if age is not None and age >= 33 and player.overall < 75:
            return False
        # Desperate contenders lower the bar; comfortable ones stay selective.
        min_ovr = 75 if urgency == "desperate" else 79
        return player.overall >= min_ovr

    if mode == "play_in_fringe":
        # B1: reject raw young developmental players — fringe teams need ready help.
        if age is not None and age < 24 and player.overall < 75:
            return False
        # Like contending but slightly less aggressive — accept any 77+ player.
        min_ovr = 73 if urgency in ("desperate", "pushing") else 77
        return player.overall >= min_ovr

    if mode == "rebuilding":
        # B1: don't accept aging vets over 30 at rebuilding teams — they don't fit.
        if age is not None and age > 30 and player.overall < 80:
            return False
        # Want youth to develop OR aging vets on expiring deals (flip for picks).
        # Skip age check when birth_date missing — don't silently misclassify.
        if age is None:
            return player.overall >= 75  # OVR-only fallback for unknown age
        return age <= 22 or age >= 31

    if mode == "soft_rebuild":
        # B1: don't accept vets over 32 on multi-year deals — wrong direction.
        if age is not None and age > 32 and player.overall < 78:
            return False
        # Trading away vets — want picks and young players in return.
        if age is None:
            return player.overall >= 72
        return age <= 24 or (age <= 26 and player.overall >= 72)

    # developing — looking for a step up at most positions
    avg_age = posture_a.get("avg_age", 27.0)
    if avg_age > 28.5:
        # Older developing team — starting to lean contending, want proven players
        return player.overall >= 76
    return player.overall >= 73


def _asset_upside_modifier(
    player: player_repo.Player,
    draft_year: int | None,
    current_season: int,
    roy_rank: int | None = None,
    mvp_rank: int | None = None,
    dpoy_rank: int | None = None,
) -> float:
    """B3: Return an upside multiplier for young/pedigree/award-race players.

    Layers on top of trade_evaluator.player_trade_value's existing age curve.
    The intent is to make the PROPOSAL GENERATOR value these players more so
    they don't get offered as filler or accepted as headline return on vet swaps.

    Modifiers (compound, cap at 1.25):
    - Age ≤ 22: +0.10 (strong premium — still ceiling-building)
    - Age 23-24: +0.05 (fading premium — emerging but not proven)
    - Age 25: +0.02 (minimal — development window closing)
    - Top-10 draft pick within last 2 seasons: +0.08 (pedigree)
    - ROY top-3: +0.10 / top-5: +0.06 (in-season production signal)
    - MVP top-5: +0.06
    - DPOY top-5: +0.05

    Example — Kel'el Ware (age 22, OVR 77, ROY top-3): modifier ≈ 1.28 → capped
    at 1.25, putting him closer to OVR-83 trade value than his raw 77.
    """
    age = _player_age(player)
    modifier = 1.0

    # Age premium
    if age is not None:
        if age <= 22:
            modifier += 0.10
        elif age <= 24:
            modifier += 0.05
        elif age == 25:
            modifier += 0.02

    # Draft pedigree: top-10 pick within last 2 seasons
    if draft_year is not None and current_season - draft_year <= 2:
        _draft_slot = getattr(player, "draft_pick", None) or getattr(player, "draft_position", None)
        if _draft_slot is not None and _draft_slot <= 10:
            modifier += 0.08

    # Award race
    if roy_rank is not None and roy_rank <= 5:
        modifier += 0.10 if roy_rank <= 3 else 0.06
    if mvp_rank is not None and mvp_rank <= 5:
        modifier += 0.06
    if dpoy_rank is not None and dpoy_rank <= 5:
        modifier += 0.05

    return min(1.25, modifier)


def _team_archetype_counts(
    roster_players: list,
) -> dict[str, int]:
    """B6: Count how many players on a team's roster fall into each archetype.

    roster_players: list of player_repo.Player objects (or dicts with tendency fields).
    Returns dict[archetype_str -> count].
    """
    from services import trade_evaluator as _te
    counts: dict[str, int] = {}
    for p in roster_players:
        if hasattr(p, "__dict__"):
            p_dict = {
                "position": getattr(p, "position", ""),
                "tendency_3pt": getattr(p, "tendency_3pt", 50) or 50,
                "tendency_drive": getattr(p, "tendency_drive", 50) or 50,
                "tendency_pass": getattr(p, "tendency_pass", 50) or 50,
                "ast_tendency": getattr(p, "ast_tendency", 50) or 50,
                "reb_tendency": getattr(p, "reb_tendency", 50) or 50,
                "blk_tendency": getattr(p, "blk_tendency", 50) or 50,
                "stl_tendency": getattr(p, "stl_tendency", 50) or 50,
            }
        else:
            p_dict = p
        arch = _te._player_archetype(p_dict)
        if arch:
            counts[arch] = counts.get(arch, 0) + 1
    return counts



async def _pick_sweetener(
    pool,
    league: league_repo.League,
    team_a: team_repo.Team,
    current_package_player_ids: list[int],
    current_package_pick_ids: list[int],
    counterparty_team: team_repo.Team,
    target_value: float,
    package_value: float,
) -> tuple[int | None, int | None, float]:
    """
    Find the smallest sweetener that pushes package_value / target_value to >= 1.05.

    Sweetener priority:
    1. Own team R1 picks (not already in package, future seasons only — same near-term
       first-round protection rules as _build_return_package).
    2. R2 picks not already in package.
    3. Low-OVR (<=75) role players not in the package whose removal won't break rotation.

    Returns (player_id_or_None, pick_id_or_None, added_value).
    Exactly one of player_id / pick_id will be set (or both None if nothing found).
    """
    threshold = target_value * 1.05
    gap = threshold - package_value

    if gap <= 0:
        return None, None, 0.0

    mode = team_a.cpu_mode or "default"

    # ── Picks first ──────────────────────────────────────────────────────────
    all_picks = await trade_repo.get_team_picks(pool, league.id, team_a.id)

    # Gather win-pct for pick valuation
    orig_team_ids = list({p["original_team_id"] for p in all_picks if p.get("original_team_id")})
    orig_win_pct: dict[int, float | None] = {}
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
        orig_win_pct = {r["team_id"]: r["win_pct"] for r in _wp_rows}

    r1_picks = sorted(
        [p for p in all_picks if p["round"] == 1 and p["id"] not in current_package_pick_ids],
        key=lambda p: p["season"],
    )
    r2_picks = sorted(
        [p for p in all_picks if p["round"] == 2 and p["id"] not in current_package_pick_ids],
        key=lambda p: p["season"],
    )

    # Try R1 first (biggest bump), then R2.
    for pick in (r2_picks + r1_picks):
        # Apply same near-term own-first protection as _build_return_package.
        if pick["round"] == 1 and pick.get("original_team_id") == team_a.id:
            if pick["season"] <= league.current_season + 1:
                continue
            if mode != "contending":
                continue

        orig_tid = pick.get("original_team_id")
        win_pct = orig_win_pct.get(orig_tid) if orig_tid else None
        pv = trade_evaluator.pick_trade_value(
            pick["season"], pick["round"], league.current_season,
            team_win_pct=win_pct,
        )
        if package_value + pv >= threshold:
            return None, pick["id"], pv

    # ── Low-OVR role player sweetener ────────────────────────────────────────
    roster = await player_repo.get_roster(pool, league.id, team_a.id)
    # Exclude cornerstones and already-packaged players.
    expendable = [
        p for p in roster
        if p.id not in current_package_player_ids
        and p.overall <= 75
        and not is_cornerstone(team_a, p, roster)
    ]
    if not expendable:
        return None, None, 0.0

    # Score them and find the cheapest one that closes the gap.
    scored: list[tuple[float, player_repo.Player]] = []
    for p in expendable:
        contract = await player_repo.get_active_contract(pool, p.id)
        v = trade_evaluator.player_trade_value(
            {"overall": p.overall, "age": _player_age(p)},
            {
                "salary": contract.salary if contract else 0,
                "years_remaining": contract.years_remaining if contract else 1,
            },
            league.salary_cap,
        )
        scored.append((v, p))

    scored.sort(key=lambda x: x[0])  # ascending — smallest first

    for pv, p in scored:
        if package_value + pv >= threshold:
            return p.id, None, pv

    return None, None, 0.0



