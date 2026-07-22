"""B8 gate parity: the final acceptance check every generated trade candidate must pass.

Extracted from cpu_trade_proposal_runner.py — pure/DB-read-only, no discord.
"""
from __future__ import annotations

from core.logging import get_logger
from data.repositories import player_repo
from services import cpu_trade_acceptance, trade_value_math
from services.cpu_trade_posture import _player_age
from services.trade_proposal_scoring import _team_a_wants_player

log = get_logger(__name__)


async def _apply_final_trade_gates(
    pool,
    league,
    team_a,
    team_b,
    outgoing_pids_a: list[int],
    incoming_pids_a: list[int],
    outgoing_picks_a: list[int],
    incoming_picks_a: list[int],
    package_value: float,
    target_value: float,
    posture_a: str,
    cp_contexts: dict,
    cp_r1_counts: dict,
    roster_a: list,
    postures: dict,
) -> tuple[bool, str]:
    """Run the final proposer-side safety gates before submitting a trade proposal.

    Returns (approved, reason).  If not approved, reason names the gate that failed.

    Gates applied (in order):
      1. Sanity floor — package_value/target_value must meet mode-specific floor.
      2. OVR sanity   — proposer must not give an OVR 10+ above best received.
      3. Lopsided     — ratio must be inside [0.50, 2.00].
      4. B1 posture   — each incoming player must match team A's posture.
      5. B5 asymmetric rejection — cpu_should_accept called from team A's POV.

    B6 archetype redundancy is NOT applied here.  Incoming-first applies B6 as a
    soft scoring penalty in pass-2 (0.65×/0.85× multiplier) so high-value targets
    still surface with reduced priority rather than being hard-rejected.  Outgoing-
    first applies B6 as a hard reject inline before calling this helper.

    posture_a: string mode value (e.g. "contending", "developing").
    postures: full posture-dict map (team_id -> posture dict) for B1 gate.
    """
    _mode_a = posture_a

    # ── Gate 1: Sanity floor ────────────────────────────────────────────────
    _sanity_floor = (
        0.85 if _mode_a == "contending"
        else 0.87 if _mode_a == "play_in_fringe"
        else 0.90
    )
    _final_ratio = package_value / max(target_value, 1)
    if _final_ratio < _sanity_floor:
        return False, (
            f"sanity_floor: ratio {_final_ratio:.2f} below {_sanity_floor:.2f} "
            f"({_mode_a} mode)"
        )

    # ── Gate 2: OVR sanity — don't give away a player 10+ OVR above best received ──
    if outgoing_pids_a:
        _best_offered_ovr = 0
        for _pid in outgoing_pids_a:
            _op = await player_repo.get_by_id(pool, _pid)
            if _op and _op.overall > _best_offered_ovr:
                _best_offered_ovr = _op.overall
        _best_incoming_ovr = 0
        for _pid in incoming_pids_a:
            _ip = await player_repo.get_by_id(pool, _pid)
            if _ip and _ip.overall > _best_incoming_ovr:
                _best_incoming_ovr = _ip.overall
        if _best_offered_ovr >= _best_incoming_ovr + 10:
            return False, (
                f"ovr_sanity: giving OVR {_best_offered_ovr} for OVR {_best_incoming_ovr} — gap >= 10"
            )

    # ── Gate 3: Lopsided check ──────────────────────────────────────────────
    if target_value > 0 and (
        package_value < target_value * 0.50 or package_value > target_value * 2.0
    ):
        return False, (
            f"lopsided: pkg {package_value:.1f} vs target {target_value:.1f} "
            f"(ratio {_final_ratio:.2f}) outside [0.50, 2.00]"
        )

    # ── Gate 4: B1 posture — each incoming player must match team A's posture ──
    _posture_a_full = postures.get(team_a.id, {}) if postures else {}
    for _pid in incoming_pids_a:
        _ip = await player_repo.get_by_id(pool, _pid)
        if _ip and not _team_a_wants_player(_posture_a_full, _ip):
            return False, (
                f"B1_posture: {_mode_a} team A does not want incoming player "
                f"{_ip.first_name} {_ip.last_name} OVR {_ip.overall}"
            )

    # ── Gate 5: B5 asymmetric rejection (cpu_should_accept from team A's POV) ──
    # Build minimal asset dicts compatible with cpu_should_accept's interface.
    # This is a proposer-side self-check: team A is the CPU evaluating its own deal.
    _assets_giving_a: list[dict] = []
    for _pid in outgoing_pids_a:
        _op = await player_repo.get_by_id(pool, _pid)
        _oc = await player_repo.get_active_contract(pool, _pid)
        if _op:
            _assets_giving_a.append({
                "asset_type": "player",
                "player": {
                    "id": _op.id,
                    "overall": _op.overall,
                    "age": _player_age(_op) or 27,
                    "first_name": _op.first_name,
                    "last_name": _op.last_name,
                },
                "contract": {
                    "salary": getattr(_oc, "salary", 0) or 0,
                    "years_remaining": getattr(_oc, "years_remaining", 1) or 1,
                },
            })
    for _pkid in outgoing_picks_a:
        _pk = await pool.fetchrow("SELECT season, round FROM draft_picks WHERE id = $1", _pkid)
        if _pk:
            _assets_giving_a.append({
                "asset_type": "pick",
                "round": _pk["round"],
                "season": _pk["season"],
                "pick": {"round": _pk["round"], "season": _pk["season"]},
            })

    _assets_receiving_a: list[dict] = []
    for _pid in incoming_pids_a:
        _ip = await player_repo.get_by_id(pool, _pid)
        _ic = await player_repo.get_active_contract(pool, _pid)
        if _ip:
            _assets_receiving_a.append({
                "asset_type": "player",
                "player": {
                    "id": _ip.id,
                    "overall": _ip.overall,
                    "age": _player_age(_ip) or 27,
                    "first_name": _ip.first_name,
                    "last_name": _ip.last_name,
                },
                "contract": {
                    "salary": getattr(_ic, "salary", 0) or 0,
                    "years_remaining": getattr(_ic, "years_remaining", 1) or 1,
                },
            })
    for _pkid in incoming_picks_a:
        _pk = await pool.fetchrow("SELECT season, round FROM draft_picks WHERE id = $1", _pkid)
        if _pk:
            _assets_receiving_a.append({
                "asset_type": "pick",
                "round": _pk["round"],
                "season": _pk["season"],
                "pick": {"round": _pk["round"], "season": _pk["season"]},
            })

    if _assets_giving_a or _assets_receiving_a:
        _b5_score_a = sum(
            trade_value_math.player_trade_value(
                a["player"],
                a.get("contract", {}),
                league.salary_cap,
            )
            for a in _assets_receiving_a if a.get("asset_type") == "player"
        )
        _b5_score_b = sum(
            trade_value_math.player_trade_value(
                a["player"],
                a.get("contract", {}),
                league.salary_cap,
            )
            for a in _assets_giving_a if a.get("asset_type") == "player"
        )
        _b5_differential = abs(_b5_score_a - _b5_score_b)
        _b5_max_side = max(_b5_score_a, _b5_score_b, 1.0)
        _b5_eval = {
            "score_a": _b5_score_a,
            "score_b": _b5_score_b,
            "differential": _b5_differential,
            "is_fair": _b5_differential < _b5_max_side * 0.20,
            "rationale": "pre-propose gate",
        }
        _b5_ctx = cp_contexts.get(team_a.id, {}) if cp_contexts else {}
        _b5_cap_used = _b5_ctx.get("current_payroll", 0) or 0
        _b5_ok, _b5_reason = await cpu_trade_acceptance.cpu_should_accept(
            cpu_team_mode=_mode_a,
            assets_receiving=_assets_receiving_a,
            assets_giving=_assets_giving_a,
            evaluation=_b5_eval,
            salary_cap=league.salary_cap,
            current_cap_used=_b5_cap_used,
        )
        if not _b5_ok:
            log.debug(
                "[gates] B5 reject for team %s (%s): %s",
                team_a.nba_team_code, _mode_a, _b5_reason,
            )
            return False, f"B5: {_b5_reason}"

    return True, ""
