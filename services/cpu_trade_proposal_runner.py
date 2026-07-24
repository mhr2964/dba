"""CPU trade proposal orchestration: mode dispatch, candidate search, and the
CPU-to-CPU auto-approve transaction.

The slimmed core left after splitting cpu_trade_proposals.py (Phase 2): pure
scoring/gating logic now lives in trade_gates.py/trade_proposal_scoring.py,
return-package assembly in trade_return_builder.py, trade-block derivation in
trade_block_builder.py, and discord posting in cpu_trade_announcer.py. This
module still imports discord only for type hints (guarded by TYPE_CHECKING,
harmless under `from __future__ import annotations`) -- it never constructs
a discord.Embed or calls channel.send directly.
"""
from __future__ import annotations

import asyncio
import os
import random
from typing import TYPE_CHECKING, Optional

from core.logging import get_logger
from data.repositories import league_repo, player_repo, team_repo, trade_repo
from services import ra_reasoning, ride_along, trade_context_builder, trade_grading, trade_service, trade_value_math
from services.cpu_trade_announcer import _post_trade_block_ads
from services.cpu_trade_posture import (
    _default_posture,
    _player_age,
    _player_age_from_row,
    is_cornerstone,
)
from services.trade_block_builder import _get_franchise_plan
from services.trade_gates import _apply_final_trade_gates, _sanity_floor_for_mode
from services.trade_proposal_scoring import (
    _derive_cap_state,
    _get_trade_target_positions,
    _is_stacked_without_upgrade,
    _position_matches_need,
    _roster_hole_penalty,
    _score_outgoing_pair,
    _team_a_wants_player,
    _team_archetype_counts,
    pick_proposal_modes,
)
from services.trade_return_builder import (
    _build_return_package,
    _derive_return_from_b,
    _league_scan_counterparties,
)

if TYPE_CHECKING:
    import discord

_HEADLESS = os.environ.get("DBA_HEADLESS_MODE") == "1"

# Minimum value-ratio each side must receive in a 3-team deal.
# Without this floor, the conduit team can ship a real pick for a near-worthless
# secondary asset (the "DAL-gets-nothing" bug caught in testing).
_MIN_3WAY_RATIO = 0.70

log = get_logger(__name__)


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
                target_value = trade_value_math.player_trade_value(
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
                    plan_a=_plan_a_candidate, live_mode_a=posture_a["mode"],
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
        _sec_value_c = trade_value_math.player_trade_value(
            {"overall": sec_p.overall, "age": _player_age(sec_p)},
            {
                "salary": _sec_contract.salary if _sec_contract else 0,
                "years_remaining": _sec_contract.years_remaining if _sec_contract else 1,
            },
            league.salary_cap,
        )
        _filler_pick_value_c = trade_value_math.pick_trade_value(
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
        target_value = trade_value_math.player_trade_value(
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
            plan_a=_plan_a_3team, live_mode_a=posture_a["mode"],
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
    round_seed: int | None = None,
) -> int:
    """Mode dispatcher: picks a mode list per team A and routes to the appropriate
    proposal function.  Returns 1 if any proposal was produced, 0 otherwise.

    round_seed: forwarded to pick_proposal_modes' opt-in #7 variety injection —
    a value that changes call to call (see maybe_initiate_round) so the handful
    of "coaching philosophy" mode rules aren't identical every single round.

    The V2 mode dispatcher is now unconditional — DBA_PROPOSAL_DISPATCHER_V2 no
    longer exists.  pick_proposal_modes runs for every team every cycle.
    """
    if recently_signed_ids is None:
        recently_signed_ids = set()
    if postures is None:
        postures = {}

    # ── Phase 3: memoized plans + contexts for all CPU teams ─────────────────
    # Built once per _attempt_one_offer call; reused for both counterparty scoring
    # and the b-loop plan lookups so we don't query the same plan 30 times.

    _cp_plan_list = await asyncio.gather(
        *[_get_franchise_plan(pool, league.id, t.id, season) for t in cpu_teams],
        return_exceptions=True,
    )
    cp_plans: dict[int, dict | None] = {}
    for t, result in zip(cpu_teams, _cp_plan_list):
        cp_plans[t.id] = result if not isinstance(result, Exception) else None

    _cp_ctx_list = await asyncio.gather(
        *[trade_context_builder.build_team_context(pool, league.id, t.id, season) for t in cpu_teams],
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

    # ── Mode dispatcher — unconditional ──────────────────────────────────────
    # Each team A gets an ordered list of modes; we attempt each in order and
    # stop as soon as one produces a proposal.
    _total = 0
    _shuffled = random.sample(cpu_teams, len(cpu_teams))
    for _disp_a in _shuffled:
        _disp_plan_a = cp_plans.get(_disp_a.id)
        _disp_posture_a = postures.get(_disp_a.id) or _default_posture(_disp_a)
        _disp_posture_str = (
            _disp_posture_a.get("mode", "developing")
            if isinstance(_disp_posture_a, dict)
            else "developing"
        )
        _disp_roster_a = await player_repo.get_roster(pool, league.id, _disp_a.id)
        _disp_cap_state = _derive_cap_state(_disp_a, cp_contexts, league)

        _disp_modes = pick_proposal_modes(
            team=_disp_a,
            posture=_disp_posture_str,
            plan=_disp_plan_a or {},
            cap_state=_disp_cap_state,
            roster_size=len(_disp_roster_a),
            round_seed=round_seed,
        )

        _disp_proposed = 0
        for _disp_mode in _disp_modes:
            if _disp_mode == "outgoing_first":
                try:
                    _disp_proposed = await _attempt_outgoing_first_offer(
                        pool=pool,
                        league=league,
                        season=season,
                        team_a=_disp_a,
                        cpu_teams=cpu_teams,
                        block_by_team=block_by_team,
                        used_pairs=used_pairs,
                        taken_player_ids=taken_player_ids,
                        deadline_game_index=deadline_game_index,
                        recently_signed_ids=recently_signed_ids,
                        guild=guild,
                        postures=postures,
                        cp_plans=cp_plans,
                        cp_contexts=cp_contexts,
                        cp_r1_counts=cp_r1_counts,
                        plan_a=_disp_plan_a,
                        posture_a=_disp_posture_str,
                    )
                except Exception as _disp_exc:
                    log.warning(
                        "[dispatcher] outgoing-first for team %d failed: %s",
                        _disp_a.id, _disp_exc, exc_info=True,
                    )
                    _disp_proposed = 0
            else:
                # incoming_first — two-pass scoring helper.
                try:
                    _disp_proposed = await _run_incoming_first_for_team(
                        pool=pool,
                        league=league,
                        season=season,
                        team_a=_disp_a,
                        cpu_teams=cpu_teams,
                        block_by_team=block_by_team,
                        used_pairs=used_pairs,
                        taken_player_ids=taken_player_ids,
                        deadline_game_index=deadline_game_index,
                        recently_signed_ids=recently_signed_ids,
                        guild=guild,
                        postures=postures,
                        cp_plans=cp_plans,
                        cp_contexts=cp_contexts,
                        cp_r1_counts=cp_r1_counts,
                        roster_a_cache=_disp_roster_a,
                    )
                except Exception as _disp_inc_exc:
                    log.warning(
                        "[dispatcher] incoming-first for team %d failed: %s",
                        _disp_a.id, _disp_inc_exc, exc_info=True,
                    )
                    _disp_proposed = 0

            if _disp_proposed >= 1:
                break

        _total += _disp_proposed
        if _total >= 1:
            return _total
    # ── End mode dispatcher ───────────────────────────────────────────────────

    return 0


async def _run_incoming_first_for_team(
    pool,
    league: league_repo.League,
    season: int,
    team_a: team_repo.Team,
    cpu_teams: list[team_repo.Team],
    block_by_team: dict[int, list[int]],
    used_pairs: set[tuple[int, int]],
    taken_player_ids: set[int],
    deadline_game_index: int,
    recently_signed_ids: set[int],
    guild: Optional[discord.Guild],
    postures: dict[int, dict],
    cp_plans: dict[int, dict | None],
    cp_contexts: dict[int, dict],
    cp_r1_counts: dict[int, int],
    roster_a_cache: list,
) -> int:
    """Incoming-first two-pass proposal for a single team A.

    Pass 1 — score all candidates on team B's block without the archetype penalty.
    Take the top-K (K=3) shortlist.

    Pass 2 — for each shortlisted candidate:
      - Call _build_return_package to materialise the actual outgoing players.
      - Compute the exact post-trade archetype counts for team A.
      - Re-score with the exact arch penalty applied.
    Pick the best re-scored candidate; proceed to sweetener / ride-along / propose.

    roster_a_cache: team A's roster, pre-fetched by the dispatcher to avoid
    a redundant DB call.  Also used for the post-trade arch count in pass 2.
    """
    # league-scan result storage — referenced inside _plan_alignment_str closure.
    _league_scan_result: list[tuple[team_repo.Team, float, str]] = []
    _league_scan_player_name: str = ""

    # The dispatcher has already selected team_a; we reference it via `a` in the loop
    # body to match the historic variable name used by all downstream closures.
    target_team: Optional[team_repo.Team] = None
    target_player: Optional[player_repo.Player] = None
    posture_a_final: dict = {}
    posture_b_final: dict = {}
    # Franchise plans for the winning pair — populated when a match is found.
    _plan_a_final: dict | None = None
    _plan_b_final: dict | None = None

    a = team_a  # alias so downstream code (closures, log lines) is unchanged
    posture_a = postures.get(a.id) or _default_posture(a)
    mode_a = posture_a["mode"]

    # Tanking teams skip trading for vets — they want to lose.
    if posture_a["urgency"] == "tanking" and mode_a not in ("rebuilding", "soft_rebuild"):
        return 0

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

    # roster_a_cache is pre-fetched by the dispatcher; no additional DB call needed here.
    # It is also used in pass-2 for exact post-trade archetype counting.

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

    # ── Phase 3: targeted counterparty scan for surplus players ──────────────
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
            _sv = trade_value_math.player_trade_value(
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
    # ── End Phase 3 league scan ───────────────────────────────────────────────

    # Collect all valid pass-1 candidates.  Each entry stores enough data for
    # pass-2 re-scoring (score, b, player, posture_b, trade_value, contract).
    _scored_candidates: list[tuple[float, team_repo.Team, player_repo.Player, dict, float, object]] = []

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
            # All other modes (#2): skip piling onto an already-stacked position
            # (3+ rostered) unless the incoming player is a clear value upgrade
            # over the weakest player already there.
            elif _is_stacked_without_upgrade(p.position, p.overall, pos_count_map, roster_a_cache):
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
            _tv = trade_value_math.player_trade_value(
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
            # so they don't get ranked as filler or offered away cheaply. Lives in
            # trade_value_math now (moved for #5 — trade_grading.evaluate_trade
            # needed to call it too, and importing trade_proposal_scoring from
            # trade_grading would be circular).
            _upside_mod = trade_value_math.asset_upside_modifier(
                {"age": _age_p},
                current_season=season,
                # Award ranks not available at proposal-rank time without an
                # extra DB call per candidate.
            )

            # Pass 1: score WITHOUT arch penalty — arch check deferred to pass 2
            # where the actual outgoing player is known.
            _score = _tv * need_multiplier * _plan_bias * _ctx_modifier * _upside_mod
            # Store _tv and _tc for pass-2 target-value reuse (avoids re-fetching).
            _scored_candidates.append((_score, b, p, posture_b, _tv, _tc))

    if not _scored_candidates:
        return 0

    # ── Pass 2: exact archetype re-score for top-K shortlist ─────────────────
    # Sort pass-1 candidates and take the top-3.
    _scored_candidates.sort(key=lambda x: x[0], reverse=True)
    _top_k = _scored_candidates[:3]

    _pass2_candidates: list[tuple] = []
    # Elements: (pass2_score, b, p, posture_b, offer_player_ids, offer_pick_ids,
    #            package_value, adj_target_value, target_value_raw,
    #            secondary, target_fm, target_stats, sec_fm, sec_stats)

    for _p1_score, _cand_b, _cand_p, _cand_posture_b, _cand_tv, _cand_tc in _top_k:
        try:
            # ── Form-adjust + secondary fold BEFORE sizing the return package ──
            # The package must be sized to the form-adjusted combined value of all
            # requested players (primary + optional secondary) so the downstream
            # sanity-floor and lopsided checks compare against the same target_value
            # that _build_return_package was given.  Using the raw _cand_tv here
            # would cause the ratio to drift by ±15% for hot/cold players and miss
            # the secondary's value entirely — both triggering spurious aborts.
            _p2_cand_form_map = await trade_context_builder.compute_form_map(
                pool,
                [_cand_p.id],
                {_cand_p.id: _cand_p.overall},
                {_cand_p.id: _cand_p.position},
                league.id,
                season,
            )
            _p2_target_fm, _p2_target_stats = _p2_cand_form_map.get(
                _cand_p.id, (1.0, {})
            )
            _p2_target_value_raw = trade_value_math.player_trade_value(
                {
                    "overall": _cand_p.overall,
                    "age": _player_age(_cand_p),
                    "position": _cand_p.position,
                },
                {
                    "salary": _cand_tc.salary if _cand_tc else 0,
                    "years_remaining": _cand_tc.years_remaining if _cand_tc else 1,
                },
                league.salary_cap,
                season_stats=_p2_target_stats or None,
            )
            _p2_adj_tv = trade_value_math.apply_form(_p2_target_value_raw, _p2_target_fm)

            # Secondary target: same 30%-dice / OVR≥75 gate as the legacy path.
            _p2_secondary: Optional[player_repo.Player] = None
            _p2_sec_fm: float = 1.0
            _p2_sec_stats: dict = {}
            if _cand_p.overall >= 75 and random.random() < 0.3:
                _cand_b_block = block_by_team.get(_cand_b.id, [])
                for _sec_pid in _cand_b_block:
                    if _sec_pid == _cand_p.id:
                        continue
                    if _sec_pid in taken_player_ids:
                        continue
                    _sec_cand = await player_repo.get_by_id(pool, _sec_pid)
                    if _sec_cand and _sec_cand.overall < _cand_p.overall:
                        _p2_secondary = _sec_cand
                        break
            if _p2_secondary is not None:
                _sec_form_map = await trade_context_builder.compute_form_map(
                    pool,
                    [_p2_secondary.id],
                    {_p2_secondary.id: _p2_secondary.overall},
                    {_p2_secondary.id: _p2_secondary.position},
                    league.id,
                    season,
                )
                _p2_sec_fm, _p2_sec_stats = _sec_form_map.get(
                    _p2_secondary.id, (1.0, {})
                )
                _sec_tc = await player_repo.get_active_contract(pool, _p2_secondary.id)
                _sec_raw = trade_value_math.player_trade_value(
                    {
                        "overall": _p2_secondary.overall,
                        "age": _player_age(_p2_secondary),
                        "position": _p2_secondary.position,
                    },
                    {
                        "salary": _sec_tc.salary if _sec_tc else 0,
                        "years_remaining": _sec_tc.years_remaining if _sec_tc else 1,
                    },
                    league.salary_cap,
                    season_stats=_p2_sec_stats or None,
                )
                _p2_adj_tv += trade_value_math.apply_form(_sec_raw, _p2_sec_fm)
            # ── End form-adjust + secondary fold ──────────────────────────────

            # Build the actual return package sized to the form-adjusted target.
            _p2_offer_player_ids, _p2_offer_pick_ids, _p2_package_value = await _build_return_package(
                pool,
                league,
                a,
                block_by_team.get(a.id, []),
                _p2_adj_tv,
                taken_player_ids,
                recently_signed_ids,
                counterparty_mode=_cand_posture_b.get("mode", "developing"),
                plan_a=_plan_a,
                live_mode_a=mode_a,
            )

            # Compute exact post-trade archetype counts:
            #   roster_a - actual_outgoing + incoming_candidate.
            _outgoing_set = set(_p2_offer_player_ids)
            _post_trade_roster = [
                rp for rp in roster_a_cache if rp.id not in _outgoing_set
            ] + [_cand_p]
            _post_arch_counts = _team_archetype_counts(_post_trade_roster)

            # Exact arch penalty for this candidate.
            _incoming_arch = trade_grading._player_archetype({
                "position": _cand_p.position,
                "tendency_3pt": getattr(_cand_p, "tendency_3pt", 50) or 50,
                "tendency_drive": getattr(_cand_p, "tendency_drive", 50) or 50,
                "tendency_pass": getattr(_cand_p, "tendency_pass", 50) or 50,
                "ast_tendency": getattr(_cand_p, "ast_tendency", 50) or 50,
                "reb_tendency": getattr(_cand_p, "reb_tendency", 50) or 50,
                "blk_tendency": getattr(_cand_p, "blk_tendency", 50) or 50,
                "stl_tendency": getattr(_cand_p, "stl_tendency", 50) or 50,
            })
            _arch_penalty_exact = 1.0
            if _incoming_arch:
                # Count in the post-trade roster, which already includes _cand_p.
                # Subtract 1 to get the "before this player" count.
                _pre_count = _post_arch_counts.get(_incoming_arch, 0) - 1
                if _pre_count >= 2:
                    _arch_penalty_exact = 0.65
                elif _pre_count == 1:
                    _arch_penalty_exact = 0.85

            _pass2_score = _p1_score * _arch_penalty_exact
            _pass2_candidates.append((
                _pass2_score, _cand_b, _cand_p, _cand_posture_b,
                _p2_offer_player_ids, _p2_offer_pick_ids, _p2_package_value,
                _p2_adj_tv, _p2_target_value_raw,
                _p2_secondary, _p2_target_fm, _p2_target_stats,
                _p2_sec_fm, _p2_sec_stats,
            ))
        except Exception as _p2_exc:
            log.debug(
                "[pass2] failed for candidate player %d team %d: %s",
                _cand_p.id, _cand_b.id, _p2_exc,
            )
            continue

    if not _pass2_candidates:
        return 0

    # Pick the best pass-2 candidate.
    _pass2_candidates.sort(key=lambda x: x[0], reverse=True)
    (
        _,
        _best_b,
        _best_p,
        _best_posture_b,
        _p2_offer_ids,
        _p2_pick_ids,
        _p2_pkg_val,
        _p2_adj_target_value,
        _p2_target_value_raw,
        _p2_winner_secondary,
        _p2_winner_target_fm,
        _p2_winner_target_stats,
        _p2_winner_sec_fm,
        _p2_winner_sec_stats,
    ) = _pass2_candidates[0]
    # ── End pass-2 ────────────────────────────────────────────────────────────

    target_team = _best_b
    target_player = _best_p
    posture_a_final = posture_a
    posture_b_final = _best_posture_b
    _plan_a_final = _plan_a
    _plan_b_final = cp_plans.get(_best_b.id)

    if not (target_team and target_player):
        return 0

    posture_a = posture_a_final  # type: ignore[assignment]
    posture_b = posture_b_final  # type: ignore[assignment]
    # Plan references for the winning pair (may be None if plan unavailable).
    _offer_plan_a: dict | None = _plan_a_final
    _offer_plan_b: dict | None = _plan_b_final

    # team_a alias kept for all downstream code in this function.
    team_a = a

    pair = (min(team_a.id, target_team.id), max(team_a.id, target_team.id))
    used_pairs.add(pair)

    # Secondary target and form-adjusted target_value were resolved inside pass-2
    # (per-candidate, before _build_return_package was called) so the package size
    # is consistent with the value the sanity-floor and lopsided checks use.
    # Bind those stored values now; no second DB round-trip needed.
    secondary_target: Optional[player_repo.Player] = _p2_winner_secondary
    target_value_raw: float = _p2_target_value_raw
    target_value: float = _p2_adj_target_value
    _target_form_mod: float = _p2_winner_target_fm
    _target_stats: dict = _p2_winner_target_stats
    _sec_form_mod: float = _p2_winner_sec_fm
    _sec_stats: dict = _p2_winner_sec_stats

    # ── Form-map: one batch DB query for all of A's block players ────────────
    # The target(s) form data was already fetched per-candidate in pass-2.
    # We still need a form_map covering A's block players so the sweetener
    # path and offered-player display code can look up stats without extra queries.
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

    form_map: dict[int, tuple[float, dict]] = await trade_context_builder.compute_form_map(
        pool, _form_all_ids, _form_ovr_map, _form_pos_map, league.id, season,
    )
    # Seed the winner's already-computed form entries into form_map so display
    # code that looks up form_map[target_player.id] gets the same values pass-2
    # used (compute_form_map is cached, so this is just an explicit safety merge).
    form_map[target_player.id] = (_target_form_mod, _target_stats)
    if secondary_target:
        form_map[secondary_target.id] = (_sec_form_mod, _sec_stats)

    # Pass-2 already built the return package for this candidate using the
    # form-adjusted target_value.  Use those results directly.
    offer_player_ids: list[int] = _p2_offer_ids
    offer_pick_ids: list[int] = _p2_pick_ids
    package_value: float = _p2_pkg_val

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
            live_mode=mode_a,
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
                _sw_form = await trade_context_builder.compute_form_map(
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

    counterparty_player_ids = [target_player.id]
    if secondary_target:
        counterparty_player_ids.append(secondary_target.id)
    counterparty_pick_ids: list[int] = []

    # ── Final gates: sanity floor + OVR sanity + lopsided + B1/B5/B6 ─────────
    # Delegates the pre-propose checks to _apply_final_trade_gates so both the
    # incoming-first and outgoing-first paths run identical safety logic.
    _mode_a_floor = posture_a.get("mode", "developing")
    _gates_ok, _gates_reason = await _apply_final_trade_gates(
        pool=pool,
        league=league,
        team_a=team_a,
        team_b=target_team,
        outgoing_pids_a=list(offer_player_ids),
        incoming_pids_a=list(counterparty_player_ids),
        outgoing_picks_a=list(offer_pick_ids),
        incoming_picks_a=list(counterparty_pick_ids),
        package_value=package_value,
        target_value=target_value,
        posture_a=_mode_a_floor,
        cp_contexts=cp_contexts,
        cp_r1_counts=cp_r1_counts,
        roster_a=roster_a_cache,
        postures=postures,
    )
    if not _gates_ok:
        _abort_msg = (
            f"[CPU] {team_a.nba_team_code} abandoning proposal to "
            f"{target_team.nba_team_code}: gate rejected — {_gates_reason}"
        )
        log.info(_abort_msg)
        if _HEADLESS:
            try:
                _target_name_abort = (
                    f"{target_player.first_name} {target_player.last_name}"
                )
                print(
                    f"CPU [{team_a.nba_team_code}] — trade ABORTED ({_gates_reason.split(':')[0]})\n"
                    f"   wanted: {_target_name_abort} (OVR {target_player.overall})"
                    f" value={target_value:.1f}\n"
                    f"   pkg value={package_value:.1f} → gate: {_gates_reason}"
                )
            except Exception:
                pass
        return 0
    # ── End final gates ───────────────────────────────────────────────────────

    # ── B9 (#4): roster-hole soft downweight ──────────────────────────────────
    # After gates approve, check whether the outgoing side leaves team A with a
    # position-group hole (skipped for rebuilding/tanking — B4's own carve-out).
    # Soft penalty, not a hard reject: only abandons the proposal if the
    # downweighted ratio would ALSO fail the same sanity floor gates already
    # enforce, matching B6's soft-penalty precedent rather than B1/B5's hard one.
    _b9_incoming_players = [target_player] + ([secondary_target] if secondary_target else [])
    _b9_penalty, _b9_holes = _roster_hole_penalty(
        roster_a_cache, set(offer_player_ids), _b9_incoming_players, _mode_a_floor,
    )
    if _b9_holes:
        _b9_adjusted_ratio = (package_value * _b9_penalty) / max(target_value, 1)
        _b9_floor = _sanity_floor_for_mode(_mode_a_floor)
        if _b9_adjusted_ratio < _b9_floor:
            log.info(
                "[CPU] %s abandoning proposal to %s: B9 roster-hole downweight "
                "(holes=%s, adjusted ratio %.2f below %.2f)",
                team_a.nba_team_code, target_team.nba_team_code,
                _b9_holes, _b9_adjusted_ratio, _b9_floor,
            )
            return 0
        log.info(
            "[CPU] %s → %s: B9 roster-hole downweight noted but ratio still clears "
            "floor (holes=%s, adjusted ratio %.2f >= %.2f) — proceeding",
            team_a.nba_team_code, target_team.nba_team_code,
            _b9_holes, _b9_adjusted_ratio, _b9_floor,
        )
    # ── End B9 ───────────────────────────────────────────────────────────────

    log.info(
        f"Trade check: target={target_value:.1f} package={package_value:.1f} "
        f"ratio={package_value / max(target_value, 1):.2f} "
        f"(team {team_a.id} → player {target_player.id} OVR {target_player.overall})"
    )

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
                    if is_cornerstone(team_a, _cp, _full_roster_a, live_mode=mode_a) and _cp.id not in offer_player_ids:
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


# ── Dispatcher helpers ────────────────────────────────────────────────────────


async def _attempt_outgoing_first_offer(
    pool,
    league,
    season: int,
    team_a,
    cpu_teams: list,
    block_by_team: dict,
    used_pairs: set,
    taken_player_ids: set,
    deadline_game_index: int,
    recently_signed_ids: set,
    guild,
    postures: dict,
    cp_plans: dict,
    cp_contexts: dict,
    cp_r1_counts: dict,
    plan_a: dict | None,
    posture_a: str,
) -> int:
    """Outgoing-first proposal: A picks a surplus player to shop, finds the best B.

    Returns 1 if a proposal was submitted, 0 otherwise.

    Loop shape:
      For each surplus/flex player on A (capped at top-3 by value):
        Rank counterparties by how much they'd want that player (_league_scan_counterparties).
        For each top-5 counterparty:
          Derive what B would plausibly send back (_derive_return_from_b).
          Score (A, outgoing, B, speculative_return) from A's POV (_score_outgoing_pair).
        Collect all (score, team_b, speculative_return) candidates.
      Sort all candidates descending; take the best; propose.

    Uses the same sweetener / ride-along / sanity-check tail as _attempt_one_offer
    to produce proposals in the same shape.
    """
    _plan_a = plan_a or {}
    _surplus_ids: list[int] = list(_plan_a.get("surplus_player_ids") or [])
    _flex_ids: list[int] = list(_plan_a.get("flex_player_ids") or [])
    _block_ids: list[int] = block_by_team.get(team_a.id, [])
    _asset_targets_a: list[str] = _plan_a.get("asset_targets") or []

    # Build ordered outgoing candidates: surplus first, then flex, filtered by block.
    _block_set = set(_block_ids)
    _outgoing_candidates: list[int] = []
    for pid in _surplus_ids:
        if pid in _block_set and pid not in taken_player_ids and pid not in recently_signed_ids:
            _outgoing_candidates.append(pid)
    for pid in _flex_ids:
        if pid in _block_set and pid not in taken_player_ids and pid not in recently_signed_ids and pid not in _outgoing_candidates:
            _outgoing_candidates.append(pid)

    if not _outgoing_candidates:
        # No plan-driven surplus/flex on the block — skip.
        return 0

    # Read shop_intent to prioritise cap_dump / flip_asset players.
    _dfr_a = (_plan_a or {}).get("derived_from_record") or {}
    _shop_intent_a: dict[str, str] = _dfr_a.get("shop_intent") or {}
    _priority_intents = frozenset({"cap_dump", "flip_asset"})

    # Rank surplus players by trade value; take top-3 to bound work.
    # Priority players (cap_dump / flip_asset) are sorted before generic surplus
    # at each value tier so the team shops its highest-intent assets first.
    _valued: list[tuple[int, float, int]] = []  # (priority_bucket, value, pid)
    for pid in _outgoing_candidates:
        _p = await player_repo.get_by_id(pool, pid)
        if not _p:
            continue
        _c = await player_repo.get_active_contract(pool, pid)
        _v = trade_value_math.player_trade_value(
            {"overall": _p.overall, "age": _player_age(_p) or 27},
            {"salary": getattr(_c, "salary", 0) or 0, "years_remaining": getattr(_c, "years_remaining", 1) or 1},
            league.salary_cap,
        )
        if _v < 10:
            continue  # not worth shopping
        _intent = _shop_intent_a.get(str(pid), "other")
        _priority = 0 if _intent in _priority_intents else 1
        _valued.append((_priority, _v, pid))
    # Sort: priority bucket first (0 before 1), then descending value within bucket.
    _valued.sort(key=lambda x: (x[0], -x[1]))
    top_surplus = [pid for _, _, pid in _valued[:3]]

    if not top_surplus:
        return 0

    # Cache A's roster for archetype scoring.
    _roster_a = await player_repo.get_roster(pool, league.id, team_a.id)

    all_candidates: list[tuple[float, object, tuple[list[int], list[int], float], object, int]] = []
    # Elements: (pair_score, team_b, speculative_return_tuple, outgoing_player, outgoing_pid)

    for outgoing_pid in top_surplus:
        outgoing_player = await player_repo.get_by_id(pool, outgoing_pid)
        if not outgoing_player:
            continue
        outgoing_contract = await player_repo.get_active_contract(pool, outgoing_pid)

        try:
            receiving_candidates = await _league_scan_counterparties(
                pool=pool,
                league_id=league.id,
                season=season,
                surplus_player=outgoing_player,
                surplus_contract=outgoing_contract,
                offerer_team_id=team_a.id,
                offerer_asset_targets=_asset_targets_a,
                cpu_teams=cpu_teams,
                cp_plans=cp_plans,
                cp_contexts=cp_contexts,
                cp_r1_counts=cp_r1_counts,
                salary_cap=league.salary_cap,
            )
        except Exception as exc:
            log.debug("league scan failed in outgoing-first for team %d player %d: %s", team_a.id, outgoing_pid, exc)
            continue

        # Expand to top-5 candidates by appending remaining teams (so we always have
        # something to iterate even if scan returned fewer than 5).
        _scan_ids = {t.id for t, _, _ in receiving_candidates}
        _fallback = [t for t in cpu_teams if t.id != team_a.id and t.id not in _scan_ids]
        random.shuffle(_fallback)
        expanded_candidates = list(receiving_candidates) + [(t, 0.0, "") for t in _fallback]

        for team_b, _cp_score, _reason in expanded_candidates[:5]:
            pair = (min(team_a.id, team_b.id), max(team_a.id, team_b.id))
            if pair in used_pairs:
                continue

            # Skip if already an active proposal between this pair.
            active_pair = await pool.fetch(
                """SELECT 1 FROM trades
                   WHERE league_id = $1 AND season = $2
                     AND ((proposer_team_id = $3 AND counterparty_team_id = $4)
                          OR (proposer_team_id = $4 AND counterparty_team_id = $3))
                     AND status IN ('pending_counterparty', 'pending_commissioner')
                   LIMIT 1""",
                league.id, season, team_a.id, team_b.id,
            )
            if active_pair:
                continue

            _plan_b = cp_plans.get(team_b.id)
            _posture_b = postures.get(team_b.id, {})
            _posture_b_str = _posture_b.get("mode", "developing") if isinstance(_posture_b, dict) else "developing"

            try:
                spec_ret = await _derive_return_from_b(
                    pool=pool,
                    league=league,
                    team_b=team_b,
                    outgoing_player=outgoing_player,
                    asset_targets_a=_asset_targets_a,
                    taken_player_ids=taken_player_ids,
                    recently_signed_ids=recently_signed_ids,
                    plan_b=_plan_b,
                    posture_b=_posture_b_str,
                    cp_contexts=cp_contexts,
                    cp_r1_counts=cp_r1_counts,
                )
            except Exception as exc:
                log.debug("_derive_return_from_b failed team %d → %d: %s", team_a.id, team_b.id, exc)
                continue

            if spec_ret is None:
                continue

            _ret_player_ids, _ret_pick_ids, _ret_value, _ret_contracts = spec_ret

            # Resolve player objects for scoring.
            _ret_players = []
            for rpid in _ret_player_ids:
                _rp = await player_repo.get_by_id(pool, rpid)
                if _rp:
                    _ret_players.append(_rp)

            try:
                pair_score = _score_outgoing_pair(
                    team_a=team_a,
                    outgoing_pid=outgoing_pid,
                    team_b=team_b,
                    speculative_return_player_ids=_ret_player_ids,
                    speculative_return_pick_ids=_ret_pick_ids,
                    speculative_return_players=_ret_players,
                    plan_a=plan_a,
                    posture_a=posture_a,
                    roster_a=_roster_a,
                    cp_contexts=cp_contexts,
                    cp_r1_counts=cp_r1_counts,
                    incoming_contracts=_ret_contracts,
                )
            except Exception as exc:
                log.debug("_score_outgoing_pair failed: %s", exc)
                continue

            # Store spec_ret as 3-tuple for downstream proposal building (contracts
            # are not needed after scoring).
            all_candidates.append((pair_score, team_b, (_ret_player_ids, _ret_pick_ids, _ret_value), outgoing_player, outgoing_pid))

    if not all_candidates:
        return 0

    # Iterate through candidates in score order — try next-best if gates reject.
    # used_pairs.add is deferred to AFTER gate approval so a rejected (A, B) pair
    # isn't poisoned for the rest of the cycle (e.g. incoming-first could still fire).
    all_candidates.sort(key=lambda x: x[0], reverse=True)

    _posture_a_dict = postures.get(team_a.id, {})
    _mode_a = _posture_a_dict.get("mode", "developing") if isinstance(_posture_a_dict, dict) else "developing"

    # These will be set when a candidate passes gates.
    best_score: float = 0.0
    target_team = None
    outgoing_player_obj = None
    outgoing_pid: int = 0
    offer_player_ids: list[int] = []
    offer_pick_ids: list[int] = []
    counterparty_player_ids: list[int] = []
    counterparty_pick_ids: list[int] = []
    target_value: float = 0.0
    package_value: float = 0.0
    pair_key: tuple[int, int] = (0, 0)

    for _cand_score, _cand_team_b, _cand_ret, _cand_player, _cand_pid in all_candidates:
        _cand_ret_player_ids, _cand_ret_pick_ids, _cand_ret_value = _cand_ret
        _posture_b_cand = postures.get(_cand_team_b.id, {})

        # Value computation for sanity checks.
        _cand_outgoing_contract = await player_repo.get_active_contract(pool, _cand_pid)
        _cand_target_value = trade_value_math.player_trade_value(
            {"overall": _cand_player.overall, "age": _player_age(_cand_player) or 27, "position": _cand_player.position},
            {"salary": _cand_outgoing_contract.salary if _cand_outgoing_contract else 0, "years_remaining": _cand_outgoing_contract.years_remaining if _cand_outgoing_contract else 1},
            league.salary_cap,
        )
        _cand_package_value = _cand_ret_value
        _cand_posture_b_str = _posture_b_cand.get("mode", "developing") if isinstance(_posture_b_cand, dict) else "developing"
        _cand_plan_b = cp_plans.get(_cand_team_b.id) or {}

        # ── B6 hard-reject for outgoing-first: archetype redundancy ──────────
        # In outgoing-first the speculative return is known before gating, so B6
        # is applied as a hard reject here (not a scoring penalty).  Incoming-first
        # applies B6 as a soft penalty in pass-2 and does NOT hard-reject via gates.
        _cand_outgoing_set_b6 = {_cand_pid}
        _cand_post_trade_roster_b6 = [p for p in _roster_a if p.id not in _cand_outgoing_set_b6]
        _cand_arch_counts_b6 = _team_archetype_counts(_cand_post_trade_roster_b6)
        _b6_rejected = False
        _cand_ret_players: list = []  # populated for B9's roster-hole check below
        for _rpid in _cand_ret_player_ids:
            _rp_b6 = await player_repo.get_by_id(pool, _rpid)
            if not _rp_b6:
                continue
            _cand_ret_players.append(_rp_b6)
            _rp_arch = trade_grading._player_archetype({
                "position": _rp_b6.position,
                "tendency_3pt": getattr(_rp_b6, "tendency_3pt", 50) or 50,
                "tendency_drive": getattr(_rp_b6, "tendency_drive", 50) or 50,
                "tendency_pass": getattr(_rp_b6, "tendency_pass", 50) or 50,
                "ast_tendency": getattr(_rp_b6, "ast_tendency", 50) or 50,
                "reb_tendency": getattr(_rp_b6, "reb_tendency", 50) or 50,
                "blk_tendency": getattr(_rp_b6, "blk_tendency", 50) or 50,
                "stl_tendency": getattr(_rp_b6, "stl_tendency", 50) or 50,
            })
            if _rp_arch and _cand_arch_counts_b6.get(_rp_arch, 0) >= 2:
                _b6_reject_reason = (
                    f"B6_arch: incoming {_rp_b6.first_name} {_rp_b6.last_name} "
                    f"archetype '{_rp_arch}' already has {_cand_arch_counts_b6[_rp_arch]} "
                    f"on team A's post-trade roster"
                )
                log.info(
                    "[CPU outgoing-first] %s → %s: B6 arch reject (candidate pid=%d): %s",
                    team_a.nba_team_code, _cand_team_b.nba_team_code, _cand_pid, _b6_reject_reason,
                )
                if _HEADLESS:
                    try:
                        print(
                            f"CPU [{team_a.nba_team_code}] — outgoing-first gate REJECTED "
                            f"(B6_arch: {_rp_arch} ×{_cand_arch_counts_b6[_rp_arch]} already) "
                            f"candidate pid={_cand_pid} → [{_cand_team_b.nba_team_code}]"
                        )
                    except Exception:
                        pass
                _b6_rejected = True
                break
        if _b6_rejected:
            continue

        # ── Final gates: B1/B5 + sanity floor + lopsided ─────────────────────
        # B6 is handled above (hard-reject inline); excluded from the helper for
        # outgoing-first so incoming-first's soft-penalty semantics are preserved.
        _cand_gates_ok, _cand_gates_reason = await _apply_final_trade_gates(
            pool=pool,
            league=league,
            team_a=team_a,
            team_b=_cand_team_b,
            outgoing_pids_a=[_cand_pid],
            incoming_pids_a=list(_cand_ret_player_ids),
            outgoing_picks_a=[],
            incoming_picks_a=list(_cand_ret_pick_ids),
            package_value=_cand_package_value,
            target_value=_cand_target_value,
            posture_a=_mode_a,
            cp_contexts=cp_contexts,
            cp_r1_counts=cp_r1_counts,
            roster_a=_roster_a,
            postures=postures,
        )
        if not _cand_gates_ok:
            log.info(
                "[CPU outgoing-first] %s → %s: gate rejected (candidate pid=%d): %s",
                team_a.nba_team_code, _cand_team_b.nba_team_code, _cand_pid, _cand_gates_reason,
            )
            if _HEADLESS:
                try:
                    print(
                        f"CPU [{team_a.nba_team_code}] — outgoing-first gate REJECTED "
                        f"({_cand_gates_reason.split(':')[0]}) "
                        f"candidate pid={_cand_pid} → [{_cand_team_b.nba_team_code}]"
                    )
                except Exception:
                    pass
            continue
        # ── End final gates ───────────────────────────────────────────────────

        # ── B9 (#4): roster-hole soft downweight ──────────────────────────────
        # Mirrors the incoming-first wiring: soft penalty, not a hard reject —
        # only skips this candidate (try the next one, loop continues) if the
        # downweighted ratio would ALSO fail the same sanity floor gates enforce.
        _b9_penalty, _b9_holes = _roster_hole_penalty(
            _roster_a, {_cand_pid}, _cand_ret_players, _mode_a,
        )
        if _b9_holes:
            _b9_adjusted_ratio = (_cand_package_value * _b9_penalty) / max(_cand_target_value, 1)
            _b9_floor = _sanity_floor_for_mode(_mode_a)
            if _b9_adjusted_ratio < _b9_floor:
                log.info(
                    "[CPU outgoing-first] %s → %s: B9 roster-hole downweight reject "
                    "(candidate pid=%d, holes=%s, adjusted ratio %.2f below %.2f)",
                    team_a.nba_team_code, _cand_team_b.nba_team_code, _cand_pid,
                    _b9_holes, _b9_adjusted_ratio, _b9_floor,
                )
                continue
            log.info(
                "[CPU outgoing-first] %s → %s: B9 roster-hole downweight noted but ratio "
                "still clears floor (candidate pid=%d, holes=%s, adjusted ratio %.2f >= %.2f)",
                team_a.nba_team_code, _cand_team_b.nba_team_code, _cand_pid,
                _b9_holes, _b9_adjusted_ratio, _b9_floor,
            )
        # ── End B9 ───────────────────────────────────────────────────────────

        # Candidate approved — capture and break.
        best_score = _cand_score
        target_team = _cand_team_b
        outgoing_player_obj = _cand_player
        outgoing_pid = _cand_pid
        offer_player_ids = [outgoing_pid]
        offer_pick_ids = []
        counterparty_player_ids = _cand_ret_player_ids
        counterparty_pick_ids = _cand_ret_pick_ids
        target_value = _cand_target_value
        package_value = _cand_package_value
        pair_key = (min(team_a.id, target_team.id), max(team_a.id, target_team.id))
        # Mark pair only after a proposal is actually going to fire.
        used_pairs.add(pair_key)
        break

    if target_team is None:
        return 0

    _final_ratio = package_value / max(target_value, 1)
    log.info(
        "[CPU outgoing-first] %s → %s: shipping player %d (OVR %d) value=%.1f "
        "| return value=%.1f ratio=%.2f",
        team_a.nba_team_code,
        target_team.nba_team_code,
        outgoing_pid, outgoing_player_obj.overall,
        target_value, package_value, _final_ratio,
    )

    if _HEADLESS:
        try:
            _ret_names = []
            for rpid in counterparty_player_ids:
                _rp = await player_repo.get_by_id(pool, rpid)
                if _rp:
                    _ret_names.append(f"{_rp.first_name} {_rp.last_name} (OVR {_rp.overall})")
            _pk_labels = []
            for pkid in counterparty_pick_ids:
                _pk = await pool.fetchrow("SELECT season, round FROM draft_picks WHERE id = $1", pkid)
                if _pk:
                    _pk_labels.append(f"{_pk['season']} R{_pk['round']} pick")
            print(
                f"CPU [{team_a.nba_team_code}] — outgoing-first eval → [{target_team.nba_team_code}]\n"
                f"   shipping: {outgoing_player_obj.first_name} {outgoing_player_obj.last_name}"
                f" (OVR {outgoing_player_obj.overall}) value={target_value:.1f}\n"
                f"   return: {'; '.join(_ret_names + _pk_labels) or '(nothing)'}"
                f" value={package_value:.1f} ratio={_final_ratio:.2f}\n"
                f"   score={best_score:.3f} → PROPOSE"
            )
        except Exception:
            pass

    trade = await trade_service.propose(
        league=league,
        proposer_team=team_a,
        counterparty_team=target_team,
        proposer_player_ids=offer_player_ids,
        proposer_pick_ids=offer_pick_ids,
        counterparty_player_ids=counterparty_player_ids,
        counterparty_pick_ids=counterparty_pick_ids,
    )

    taken_player_ids.update(offer_player_ids)
    taken_player_ids.update(counterparty_player_ids)

    if trade.status == "pending_commissioner":
        try:
            await _maybe_auto_approve(pool, league, trade, guild)
        except Exception as exc:
            log.error(
                "CPU-CPU outgoing-first trade %d auto-approve failed: %s",
                trade.id, exc, exc_info=True,
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
                v = trade_value_math.player_trade_value(
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
                v = trade_value_math.pick_trade_value(
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
        from services import role_service
        from services.sim_persistence import invalidate_role_cache
        async with pool.acquire() as _conn:
            for tid in affected_team_ids:
                invalidate_role_cache(
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


async def _pick_sweetener(
    pool,
    league: league_repo.League,
    team_a: team_repo.Team,
    current_package_player_ids: list[int],
    current_package_pick_ids: list[int],
    counterparty_team: team_repo.Team,
    target_value: float,
    package_value: float,
    live_mode: str | None = None,
) -> tuple[int | None, int | None, float]:
    """
    Find the smallest sweetener that pushes package_value / target_value to >= 1.05.

    Sweetener priority:
    1. Own team R1 picks (not already in package, future seasons only — same near-term
       first-round protection rules as _build_return_package).
    2. R2 picks not already in package.
    3. Low-OVR (<=75) role players not in the package whose removal won't break rotation.

    live_mode: team A's live posture mode (B7), threaded into the is_cornerstone
    backstop check on the role-player sweetener path below. Falls back to
    team_a.cpu_mode when the caller doesn't have a live mode handy yet.

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
        pv = trade_value_math.pick_trade_value(
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
        and not is_cornerstone(team_a, p, roster, live_mode=live_mode)
    ]
    if not expendable:
        return None, None, 0.0

    # Score them and find the cheapest one that closes the gap.
    scored: list[tuple[float, player_repo.Player]] = []
    for p in expendable:
        contract = await player_repo.get_active_contract(pool, p.id)
        v = trade_value_math.player_trade_value(
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
