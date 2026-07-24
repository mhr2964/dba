"""CPU accept/reject decision logic for proposed trades -- the B1/B3/B5/B6/B7/B8
gate rules documented in docs/design/trade-logic-rules.md.

Extracted from trade_evaluator.py (Phase 3 opportunistic split, see
HANDOFF.md).
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from services.trade_grading import _player_archetype, _team_primary_scheme
from services.trade_proposal_scoring import _team_a_wants_player, _team_archetype_counts
from services.trade_value_math import (
    REBUILD_BUYS_YOUNG_FLOOR,
    _ELITE_OVR_FLOOR,
    _effective_fleecing_floor,
    player_trade_value,
)

log = logging.getLogger(__name__)


def _get_trade_context():
    global _trade_context
    if _trade_context is None:
        from services import trade_context as _tc
        _trade_context = _tc
    return _trade_context


# Imported lazily inside cpu_should_accept to avoid import-time circular deps.
# trade_context → trade_evaluator would be a cycle if imported at module level.
_trade_context = None


LEAD_ROLES: frozenset[str] = frozenset({
    "primary_initiator", "iso_scorer", "post_anchor",
    "slashing_lead", "movement_shooter",
    "rim_protector", "two_way_big", "switching_big", "wing_stopper",
})


# Posture values that represent a "contender-tier" team for B5 sub-rules.
# Must match the posture strings emitted by compute_team_mode / _default_posture.
# "win_now" is a plan GOAL, not a posture mode — cpu_team_mode is always a posture
# string ("contending", "play_in_fringe", etc.) so "win_now" here was dead.
_CONTENDER_TIER_MODES: frozenset[str] = frozenset(
    {"contending", "play_in_fringe"}
)


# Minimum OVR to be considered "starting quality" for the 2-for-1 sub-rule.
_STARTING_QUALITY_OVR: int = 75


async def cpu_should_accept(
    cpu_team_mode: str,
    assets_receiving: list,
    assets_giving: list,
    evaluation: dict,
    salary_cap: int,
    current_cap_used: int,
    *,
    giving_role_map: dict[int, str] | None = None,
    pool=None,
    context_kwargs: dict | None = None,
    receiving_team_roster: list | None = None,
) -> tuple[bool, str]:
    """
    Returns (accept: bool, reason: str).
    Rules:
    - Reject if evaluation['differential'] > 25% of max side
    - Rebuilding CPU: prefers picks > vets. Accepts if receiving picks or youth (age < 26).
    - Contending CPU: prefers proven players. Reluctant to give picks.
    - Reject if accepting would put CPU over salary_cap.

    Mutates `evaluation` in place: adds 'context_signals_per_player' and
    'score_a_after_context' keys for ride-along / ra_reasoning access.
    - Never give up a player with overall >= 88 unless rebuilding AND getting 2+ first-rounders.
    - Buy-low fit bonus: underperforming player (form_modifier < 0.92) gets a value bump
      from the receiving team's perspective when archetype fits their scheme or mode.

    receiving_team_roster: the accepting team's current full roster (player_repo.Player
        objects or dicts), used for the B6 archetype-redundancy check below. Optional —
        when omitted (e.g. existing unit tests / the propose-side self-check in
        trade_gates._apply_final_trade_gates) the B6 check is silently skipped, matching
        the giving_role_map precedent. Real callers (cpu_trade_evaluation._cpu_evaluate,
        the accept path every trade funnels through) always pass this.

    context_kwargs may include a "posture" key (the accepting team's full live-posture
    dict — mode/urgency/avg_age/etc, same shape trade_context_builder / team_intel
    produce). When present, the shared B1 "does this team want this player" check
    (trade_proposal_scoring._team_a_wants_player — the SAME helper the propose-side
    self-check in trade_gates.py Gate 4 uses) runs against every incoming player here
    too, so accept-side and propose-side agree instead of drifting independently (#9).
    """
    # Normalize mode string defensively — callers may pass un-stripped or mixed-case values.
    cpu_team_mode = (cpu_team_mode or "").lower().strip()

    # Live posture dict for the shared B1 check (#9) — read here (not inside the
    # pool-gated context block below) so it's available even when pool is None.
    _posture_dict_a: dict = (context_kwargs or {}).get("posture") or {}

    score_a = evaluation["score_a"]
    score_b = evaluation["score_b"]
    max_side = max(score_a, score_b, 1.0)
    differential = evaluation["differential"]

    # CPU is the counterparty — score_b is what CPU gives, score_a is what CPU receives.
    # assets_receiving = what CPU gets, assets_giving = what CPU gives.

    # ── Context modifier (ride-along-aligned signals) ─────────────────────────
    # Apply per-player context modifiers to score_a (what CPU receives).
    # Each incoming player's contribution is scaled by a modifier in [0.85, 1.15]
    # derived from synergy, role-fit, window-fit, defense quality, overperformance,
    # and scheme-fit detectors in trade_context.  The same signals drive the
    # ride-along narrative bullets so user reads what actually moved the math.
    context_signals_per_player: dict[int, list] = {}
    if pool is not None and context_kwargs is not None:
        _tc = _get_trade_context()
        league_id = context_kwargs.get("league_id")
        season = context_kwargs.get("season")
        perspective_team_id = context_kwargs.get("perspective_team_id")
        plan = context_kwargs.get("plan") or {}
        posture = context_kwargs.get("posture") or {}
        coach_philosophy = context_kwargs.get("coach_philosophy")

        # Build per-player coroutines and gather in parallel; filter exceptions.
        _player_assets = [
            (a, a.get("player", {}), a.get("player", {}).get("id"))
            for a in assets_receiving
            if a.get("asset_type") == "player" and a.get("player", {}).get("id")
        ]

        async def _compute_one(a_item, p_dict, pid):
            form_mod = a_item.get("form_modifier", 1.0)
            player_stats = a_item.get("season_stats") or {}
            player_for_ctx = dict(p_dict)
            modifier, signals = await _tc.compute_context_modifier(
                pool=pool,
                league_id=league_id,
                season=season,
                perspective_team_id=perspective_team_id,
                plan=plan,
                posture=posture,
                coach_philosophy=coach_philosophy,
                incoming_player=player_for_ctx,
                form_mod=form_mod,
                stats=player_stats,
            )
            return pid, modifier, signals, a_item, player_for_ctx

        # return_exceptions=True preserves index order so enumerate(_ctx_results)[_idx] maps correctly to _player_assets[_idx].
        _ctx_results = await asyncio.gather(
            *[_compute_one(a, p, pid) for a, p, pid in _player_assets],
            return_exceptions=True,
        )

        for _idx, _res in enumerate(_ctx_results):
            if isinstance(_res, BaseException):
                _pid = _player_assets[_idx][2]
                log.warning("context modifier failed pid=%d: %s", _pid, _res)
                continue
            pid, modifier, signals, a_item, player_for_ctx = _res
            context_signals_per_player[pid] = [s._asdict() for s in signals]
            # Apply context modifier to player's market-value contribution in score_a.
            # Score adjustment: add (modifier - 1.0) * player_value to score_a.
            form_mod = a_item.get("form_modifier", 1.0)
            player_base = player_trade_value(
                player_for_ctx,
                a_item.get("contract", {}),
                salary_cap,
                a_item.get("season_stats") or None,
                form_mod,
            )
            delta_from_context = player_base * (modifier - 1.0)
            score_a += delta_from_context
            if abs(delta_from_context) >= 0.5:
                log.info(
                    "[CPU] context modifier pid=%d modifier=%.4f delta=%.2f "
                    "codes=%s",
                    pid, modifier, delta_from_context,
                    [s.get("code") for s in context_signals_per_player[pid]],
                )

        # Recompute differential/max_side after context adjustment.
        differential = abs(score_a - score_b)
        max_side = max(score_a, score_b, 1.0)

    # Stash signals in evaluation dict so callers (ride-along, ra_reasoning) can read them.
    evaluation["context_signals_per_player"] = context_signals_per_player
    evaluation["score_a_after_context"] = score_a
    # ── End context modifier ──────────────────────────────────────────────────

    # ── Buy-low fit bonus ────────────────────────────────────────────────────
    # When a player arriving is underperforming (form_modifier < 0.92), apply a
    # bonus to score_a (what CPU receives) based on archetype fit and team mode.
    # This can flip close reject → accept when a team is a good landing spot.
    fit_bonus_total = 0.0
    fit_bonus_notes: list[str] = []
    for a in assets_receiving:
        if a.get("asset_type") != "player":
            continue
        form_mod = a.get("form_modifier", 1.0)
        if form_mod >= 0.92:
            continue  # not underperforming — no buy-low consideration
        arch = _player_archetype(a.get("player", {}))
        if arch is None:
            continue  # no clear archetype — skip

        base_player_value = player_trade_value(
            a.get("player", {}),
            a.get("contract", {}),
            salary_cap,
        )

        bonus_pct = 0.0
        if cpu_team_mode in ("rebuilding", "soft_rebuild", "developing"):
            # Rebuilders buy low regardless of scheme — that's their bread and butter.
            bonus_pct = 0.10
        elif cpu_team_mode in ("contending", "play_in_fringe"):
            # Contenders only want a buy-low if the archetype fills a scheme gap.
            # We compare archetype against what they're giving up (their scheme).
            giving_arch = _team_primary_scheme(assets_giving)
            # A different archetype arriving = scheme complement = good fit.
            if giving_arch and arch != giving_arch:
                bonus_pct = 0.05 if cpu_team_mode == "contending" else 0.04

        if bonus_pct > 0:
            bonus = base_player_value * bonus_pct
            fit_bonus_total += bonus
            fit_bonus_notes.append(
                f"{arch} (form={form_mod:.2f}) buy-low +{bonus:.1f}"
            )

    if fit_bonus_total > 0:
        # Re-evaluate with bonus folded into score_a.
        adjusted_score_a = score_a + fit_bonus_total
        log.info(
            f"[CPU] buy-low fit bonus applied: {'; '.join(fit_bonus_notes)} "
            f"| score_a {score_a:.1f} → {adjusted_score_a:.1f}"
        )
        score_a = adjusted_score_a
        differential = abs(score_a - score_b)
        max_side = max(score_a, score_b, 1.0)
    # ── End buy-low fit bonus ─────────────────────────────────────────────────

    # ── Hard fleecing floor ───────────────────────────────────────────────────
    # After all bonuses: if CPU would receive less than the mode-appropriate floor
    # of what it gives up, force-reject.  The floor is 0.85 by default; for
    # rebuild-adjacent teams receiving a young, quality player it softens to 0.70
    # ("rebuild buys young" exemption — they're buying age, not raw value).
    if score_b > 0:
        _effective_ratio = score_a / score_b
        _floor, _is_softened = _effective_fleecing_floor(cpu_team_mode, assets_receiving)
        if _effective_ratio < _floor:
            _suffix = " [rebuild softened]" if _is_softened else ""
            _reason = (
                f"forced reject: fleecing floor "
                f"(effective ratio {_effective_ratio:.2f} < {_floor:.2f}){_suffix}"
            )
            log.info(f"[CPU] counterparty {_reason}")
            return False, _reason
    # ── End fleecing floor ────────────────────────────────────────────────────

    # ── B5: Asymmetric rejection — tighter threshold for contending/fringe ──────
    # After B1/B3/B6 modifiers: if a win-now or fringe team is receiving materially
    # less value than they're giving up with no compensating strategic gain, reject.
    # "Strategic gain" means: at least one R1 pick incoming, OR significant cap
    # relief (incoming salary < outgoing by >15% of cap).  Without strategic gain,
    # the rejection threshold tightens from 25% → 15% differential.
    _b5_threshold = 0.25  # default differential tolerance
    if cpu_team_mode in ("contending", "play_in_fringe") and score_b > score_a:
        _receiving_r1s = sum(
            1 for a in assets_receiving
            if a.get("asset_type") == "pick" and a.get("round") == 1
        )
        _incoming_sal = sum(
            a.get("contract", {}).get("salary", 0)
            for a in assets_receiving if a.get("asset_type") == "player"
        )
        _outgoing_sal = sum(
            a.get("contract", {}).get("salary", 0)
            for a in assets_giving if a.get("asset_type") == "player"
        )
        _cap_relief = _outgoing_sal - _incoming_sal  # positive = we get cap relief
        _has_strategic_gain = (
            _receiving_r1s >= 1
            or _cap_relief >= salary_cap * 0.15
        )
        if not _has_strategic_gain:
            _b5_threshold = 0.15  # tighter: no picks or cap relief = harder standard
    # ── End B5 setup ─────────────────────────────────────────────────────────────

    if differential > max_side * _b5_threshold:
        losing_side = score_b > score_a
        if losing_side:
            _b5_note = " [B5: no strategic gain, tight threshold]" if _b5_threshold < 0.25 else ""
            return False, f"CPU evaluated the trade as too lopsided against its interests.{_b5_note}"

    giving_players = [a for a in assets_giving if a.get("asset_type") == "player"]
    receiving_picks = [a for a in assets_receiving if a.get("asset_type") == "pick"]
    receiving_players = [a for a in assets_receiving if a.get("asset_type") == "player"]

    # ── B1 dedup (#9): shared "does this team want this player" check ────────
    # Reuses trade_proposal_scoring._team_a_wants_player instead of maintaining a
    # second, independently-drifting set of age/OVR cutoffs here. This is the
    # same per-player hard check trade_gates._apply_final_trade_gates Gate 4 runs
    # on the propose side — running it here too closes the gap where a human GM
    # could offer a contender a raw developmental throw-in that the propose-side
    # self-check would have rejected. Only enforced when a live posture dict is
    # supplied (real accept-path callers always pass one); silently skipped for
    # callers that only pass a bare mode string.
    if _posture_dict_a.get("mode") and receiving_players:
        for _rp in receiving_players:
            _rp_player = _rp.get("player", {})
            _rp_stub = SimpleNamespace(overall=_rp_player.get("overall", 0))
            if not _team_a_wants_player(
                _posture_dict_a, _rp_stub, age_override=_rp_player.get("age")
            ):
                _rp_name = f"{_rp_player.get('first_name', '?')} {_rp_player.get('last_name', '?')}"
                return False, (
                    f"B1: {cpu_team_mode} team does not want incoming {_rp_name} "
                    f"(age {_rp_player.get('age', '?')}, OVR {_rp_player.get('overall', '?')}) "
                    f"— fails shared posture-fit check"
                )
    # ── End B1 dedup ─────────────────────────────────────────────────────────

    # ── B6: Archetype redundancy in the accept path ───────────────────────────
    # B6's soft-penalty scoring already lives in trade_proposal_scoring (search
    # side), but nothing checked archetype redundancy on the actual accept
    # decision every trade funnels through — a human GM could offer fair value
    # for a 3rd same-archetype piece and it would slide through. Hard-reject,
    # mirroring the outgoing-first B6 hard-reject threshold (pre-existing count
    # >= 2 on the post-trade roster, before the incoming player is added).
    # Silently skipped when receiving_team_roster isn't supplied (see docstring).
    if receiving_team_roster and receiving_players:
        _giving_player_ids_b6 = {
            a.get("player", {}).get("id")
            for a in assets_giving if a.get("asset_type") == "player"
        }
        _post_trade_roster_b6 = [
            p for p in receiving_team_roster
            if getattr(p, "id", None) not in _giving_player_ids_b6
        ]
        _pre_arch_counts_b6 = _team_archetype_counts(_post_trade_roster_b6)
        for _rp in receiving_players:
            _rp_player = _rp.get("player", {})
            _rp_arch = _player_archetype(_rp_player)
            if _rp_arch and _pre_arch_counts_b6.get(_rp_arch, 0) >= 2:
                _rp_name = f"{_rp_player.get('first_name', '?')} {_rp_player.get('last_name', '?')}"
                return False, (
                    f"B6: incoming {_rp_name} archetype '{_rp_arch}' already has "
                    f"{_pre_arch_counts_b6[_rp_arch]} on the roster post-trade — "
                    f"archetype redundancy"
                )
    # ── End B6 accept-path check ──────────────────────────────────────────────

    # ── B5 sub-rule 1: Pick parity for contenders ─────────────────────────────
    # A contender-tier team should not give away any pick on a lateral or
    # downgrade swap UNLESS they receive a pick of equal-or-better tier in return.
    # "Equal-or-better tier" = receiving R1 when sending R2, or matching tiers.
    # "Lateral or downgrade" = net OVR change <= 0.
    # Catches DEN/HOU (2nd pick + Gordon for Brooks) and LAC/TOR (picks + depth for Poeltl).
    # Does NOT reject legitimate consolidations like "send R2 + player, get R1 + player".
    if cpu_team_mode in _CONTENDER_TIER_MODES:
        _giving_picks = [a for a in assets_giving if a.get("asset_type") == "pick"]
        _giving_pick_count = len(_giving_picks)
        if _giving_pick_count > 0:
            _sum_incoming_ovr = sum(
                a.get("player", {}).get("overall", 0)
                for a in receiving_players
            )
            _sum_outgoing_ovr = sum(
                a.get("player", {}).get("overall", 0)
                for a in giving_players
            )
            _net_ovr_change = _sum_incoming_ovr - _sum_outgoing_ovr
            if _net_ovr_change <= 0:
                # Check if incoming picks compensate with equal-or-better tier.
                # Tier: 1 = R1 (best), 2 = R2 (worse). min() finds the best tier.
                _receiving_picks = [a for a in assets_receiving if a.get("asset_type") == "pick"]
                _outgoing_tiers = [
                    int(a.get("pick", a).get("round", a.get("round", 2)))
                    for a in _giving_picks
                ]
                _incoming_tiers = [
                    int(a.get("pick", a).get("round", a.get("round", 99)))
                    for a in _receiving_picks
                ]
                _outgoing_best = min(_outgoing_tiers) if _outgoing_tiers else 99
                _incoming_best = min(_incoming_tiers) if _incoming_tiers else 99
                # Only reject if incoming picks are WORSE tier than outgoing (or absent).
                # If incoming_best <= outgoing_best → equal-or-better tier → allow.
                if _incoming_best > _outgoing_best:
                    log.debug(
                        "[B5-sub1] contender pick-parity reject: mode=%s giving %d pick(s) "
                        "(best tier R%d), net_OVR_change=%+d, incoming best tier R%d — no parity",
                        cpu_team_mode, _giving_pick_count, _outgoing_best,
                        _net_ovr_change, _incoming_best,
                    )
                    return False, (
                        f"B5-sub1: contender ({cpu_team_mode}) sending {_giving_pick_count} pick(s) "
                        f"(best R{_outgoing_best}) on a lateral/downgrade swap "
                        f"(net OVR {_net_ovr_change:+d}) without equal-or-better pick in return "
                        f"(best incoming R{_incoming_best if _incoming_best < 99 else 'none'})"
                    )
    # ── End B5 sub-rule 1 ────────────────────────────────────────────────────

    # ── B5 sub-rule 2: Contender 2-for-1 needs upgrade ────────────────────────
    # A contender shipping 2+ starting-quality players (OVR >= 75) must receive
    # at least 1 player whose OVR is STRICTLY greater than EACH outgoing player.
    # Catches NYK/GSW (Bridges 76 + Anunoby 82 → Kuminga 76).
    # Only applies when receiving <= 1 player (true consolidation scenario).
    # Contender 2-for-2 deals are not gated here.
    if cpu_team_mode in _CONTENDER_TIER_MODES:
        _giving_starters = [
            a for a in giving_players
            if a.get("player", {}).get("overall", 0) >= _STARTING_QUALITY_OVR
        ]
        if len(_giving_starters) >= 2 and len(receiving_players) <= 1:
            _giving_ovrs = [
                a.get("player", {}).get("overall", 0)
                for a in _giving_starters
            ]
            _max_giving_ovr = max(_giving_ovrs) if _giving_ovrs else 0
            _incoming_ovr = (
                receiving_players[0].get("player", {}).get("overall", 0)
                if receiving_players else 0
            )
            # Strict upgrade: incoming must be better than EACH outgoing starter.
            _is_strict_upgrade = _incoming_ovr > _max_giving_ovr
            if not _is_strict_upgrade:
                log.debug(
                    "[B5-sub2] contender 2-for-1 reject: mode=%s giving %d starters "
                    "(max OVR %d), receiving 1 player (OVR %d) — not a strict upgrade",
                    cpu_team_mode, len(_giving_starters), _max_giving_ovr, _incoming_ovr,
                )
                return False, (
                    f"B5-sub2: contender ({cpu_team_mode}) trading {len(_giving_starters)} starters "
                    f"(max OVR {_max_giving_ovr}) for OVR {_incoming_ovr} — "
                    f"2-for-1 consolidation requires a strict OVR upgrade over each outgoing player"
                )
    # ── End B5 sub-rule 2 ────────────────────────────────────────────────────

    # ── B3: High-upside asset guard — don't give away young/pedigree players cheap ──
    # Applies when the CPU is giving away a player whose upside is materially
    # underpriced in the raw OVR/value calculation: young (≤22) + decent OVR (≥74),
    # or very young (≤21) at any OVR.  When such a player is in assets_giving and
    # score_b is meaningfully higher than score_a, reject rather than let the value
    # math slide it through.
    if giving_players:
        for _ga in giving_players:
            _gp = _ga.get("player", {})
            _g_age = _gp.get("age", 99)
            _g_ovr = _gp.get("overall", 0)
            _is_high_upside = (
                (_g_age <= 21)
                or (_g_age <= 22 and _g_ovr >= 74)
                or (_g_age <= 23 and _g_ovr >= 79)
            )
            if _is_high_upside:
                # Upside floor: must receive at least 90% of what we give up
                # (tighter than the standard 85% fleecing floor).
                if score_b > 0 and (score_a / score_b) < 0.90:
                    _gname = f"{_gp.get('first_name', '?')} {_gp.get('last_name', '?')}"
                    return False, (
                        f"hard reject: giving away high-upside asset {_gname} "
                        f"(age {_g_age}, OVR {_g_ovr}) at only {score_a/score_b:.0%} return value"
                    )
    # ── End B3 guard ─────────────────────────────────────────────────────────────

    # ── Guard 1: Age + OVR asymmetry — won't sell low on a younger, better player ──
    # Blocks trades where the CPU gives up a meaningfully better AND younger player
    # without a proportionate return. The value math can miss this when salary tricks
    # make the trade look balanced — this guard fires regardless of score ratios.
    if giving_players and receiving_players:
        max_giving_ovr = max(a["player"].get("overall", 0) for a in giving_players)
        max_receiving_ovr = max(a["player"].get("overall", 0) for a in receiving_players)
        avg_giving_age = sum(a["player"].get("age") or 28 for a in giving_players) / len(giving_players)
        avg_receiving_age = sum(a["player"].get("age") or 28 for a in receiving_players) / len(receiving_players)

        ovr_gap = max_giving_ovr - max_receiving_ovr
        age_gap = avg_receiving_age - avg_giving_age  # positive = receiving older

        if (ovr_gap >= 3 and age_gap >= 2.0) or (ovr_gap >= 2 and age_gap >= 1.0):
            return False, (
                f"hard reject: parting with OVR {max_giving_ovr} (avg age {avg_giving_age:.1f}) "
                f"for OVR {max_receiving_ovr} (avg age {avg_receiving_age:.1f}) — selling low on younger asset"
            )
    # ── End Guard 1 ───────────────────────────────────────────────────────────────

    # ── Guard 2: Lead-role player requires equivalent return ──────────────────────
    # A player in a lead role (primary initiator, iso scorer, etc.) is harder to
    # replace than their OVR alone suggests. Giving one up without receiving either
    # a starter-quality player (OVR 78+) or a 1st-round pick is a bad deal by design.
    # Silently skips when giving_role_map is None — callers that haven't been updated
    # yet aren't penalised.
    if giving_role_map and giving_players:
        giving_lead_players = [
            a for a in giving_players
            if giving_role_map.get(a["player"].get("id")) in LEAD_ROLES
        ]
        if giving_lead_players:
            has_starter_tier = any(
                (a["player"].get("overall") or 0) >= 78
                for a in receiving_players
            )
            has_first_round_pick = any(
                a.get("asset_type") == "pick" and a.get("round") == 1
                for a in assets_receiving
            )
            # Elite lead-role players (OVR≥82, cornerstone tier) require BOTH
            # a starter-quality player AND a 1st-round pick. The starter OR pick
            # alternative is fine for an OVR-78 starter; an OVR-82+ cornerstone
            # needs more.  See module constant _ELITE_OVR_FLOOR.
            elite_giving = [
                p for p in giving_lead_players
                if (p["player"].get("overall") or 0) >= _ELITE_OVR_FLOOR
            ]
            if elite_giving:
                if not (has_starter_tier and has_first_round_pick):
                    elite_names = ", ".join(
                        f"{p['player'].get('first_name', '?')} {p['player'].get('last_name', '?')} "
                        f"(OVR {p['player'].get('overall', '?')}, {giving_role_map.get(p['player'].get('id'))})"
                        for p in elite_giving
                    )
                    return False, (
                        f"hard reject: giving up ELITE lead-role player(s) {elite_names} "
                        f"requires BOTH a starter-quality player (OVR≥78) AND a 1st-round pick"
                    )
            elif not (has_starter_tier or has_first_round_pick):
                lead_names = ", ".join(
                    f"{p['player'].get('first_name', '?')} {p['player'].get('last_name', '?')} "
                    f"({giving_role_map.get(p['player'].get('id'))})"
                    for p in giving_lead_players
                )
                return False, (
                    f"hard reject: giving up lead-role player(s) {lead_names} "
                    f"without receiving a starter-quality player (OVR≥78) or 1st-round pick"
                )
    # ── End Guard 2 ───────────────────────────────────────────────────────────────

    for asset in giving_players:
        player = asset.get("player", {})
        if player.get("overall", 0) >= 88:
            if cpu_team_mode != "rebuilding":
                return False, "CPU refuses to trade away a franchise cornerstone."
            first_rounders_incoming = sum(
                1 for p in receiving_picks
                if p.get("pick", {}).get("round", 2) == 1
            )
            if first_rounders_incoming < 2:
                return False, "CPU won't give up a star player without at least 2 first-round picks in return."

    incoming_salary = sum(
        a.get("contract", {}).get("salary", 0)
        for a in assets_receiving
        if a.get("asset_type") == "player"
    )
    outgoing_salary = sum(
        a.get("contract", {}).get("salary", 0)
        for a in assets_giving
        if a.get("asset_type") == "player"
    )
    net_cap_change = incoming_salary - outgoing_salary
    if current_cap_used + net_cap_change > salary_cap:
        return False, "Accepting this trade would put CPU over the salary cap."

    if cpu_team_mode == "rebuilding":
        has_picks = len(receiving_picks) > 0
        has_youth = any(
            a.get("player", {}).get("age", 30) < 26
            for a in receiving_players
        )
        # Buy-low on a high-OVR underperformer counts as a valid rebuild asset —
        # a player the team can develop/deploy better than their current situation allows.
        has_buy_low_star = any(
            a.get("form_modifier", 1.0) < 0.92 and a.get("player", {}).get("overall", 0) >= 80
            for a in receiving_players
            if a.get("asset_type") == "player"
        )
        if has_picks or has_youth or has_buy_low_star:
            reason = "CPU accepts — acquiring picks and young talent fits the rebuild."
            if has_buy_low_star and not (has_picks or has_youth):
                reason = "CPU accepts — buy-low on underperforming star fits the rebuild."
            if fit_bonus_notes:
                reason += f" [fit bonus: {'; '.join(fit_bonus_notes)}]"
            return True, reason
        return False, "CPU declined — rebuilding teams need picks and youth, not veteran salaries."

    if cpu_team_mode == "soft_rebuild":
        # Sell veterans — accept any deal that returns picks or youth (age < 25).
        # Less strict than rebuilding: will also accept OVR 72+ players under 26.
        has_picks = len(receiving_picks) > 0
        has_youth = any(
            a.get("player", {}).get("age", 30) < 25
            for a in receiving_players
        )
        has_young_talent = any(
            a.get("player", {}).get("age", 30) < 27
            and a.get("player", {}).get("overall", 0) >= 72
            for a in receiving_players
        )
        # "Rebuild buys young" — quality young player (age ≤ 25, OVR ≥ 75) incoming.
        # Mirrors the fleecing-floor exemption: soften the value threshold to 0.70
        # because the CPU is buying age/upside, not raw value.
        has_quality_young = any(
            a.get("player", {}).get("age", 30) <= 25
            and a.get("player", {}).get("overall", 0) >= 75
            for a in receiving_players
        )
        has_first_rounders = any(
            a.get("pick", {}).get("round", 2) == 1
            for a in receiving_picks
        )
        # Accept if we get picks or youth; be extra lenient if first-rounders included.
        if has_first_rounders and score_a >= score_b * 0.92:
            reason = "CPU accepts — first-round pick return fits the soft rebuild."
            if fit_bonus_notes:
                reason += f" [fit bonus: {'; '.join(fit_bonus_notes)}]"
            return True, reason
        # Quality young player incoming: use the softened 0.70 floor (buy age, not value).
        if has_quality_young and score_a >= score_b * REBUILD_BUYS_YOUNG_FLOOR:
            reason = "CPU accepts — acquiring quality young talent fits the soft rebuild [rebuild softened]."
            if fit_bonus_notes:
                reason += f" [fit bonus: {'; '.join(fit_bonus_notes)}]"
            return True, reason
        if (has_picks or has_youth or has_young_talent) and score_a >= score_b * 0.92:
            reason = "CPU accepts — acquiring picks and young talent moves the soft rebuild forward."
            if fit_bonus_notes:
                reason += f" [fit bonus: {'; '.join(fit_bonus_notes)}]"
            return True, reason
        return False, "CPU declined — soft rebuild teams need picks and young players."

    if cpu_team_mode == "contending":
        giving_picks = [a for a in assets_giving if a.get("asset_type") == "pick"]
        if giving_picks and not receiving_players:
            return False, "CPU declines — contending teams protect their future draft capital."
        high_value_incoming = any(
            a.get("player", {}).get("overall", 0) >= 80
            for a in receiving_players
        )
        if high_value_incoming:
            reason = "CPU accepts — acquiring a proven starter strengthens the contending roster."
            if fit_bonus_notes:
                reason += f" [fit bonus: {'; '.join(fit_bonus_notes)}]"
            return True, reason
        # Buy-low bonus may have made score_a competitive even without a high-OVR player.
        if fit_bonus_total > 0 and score_a >= score_b * 0.85:
            return True, f"CPU accepts — buy-low fit flips valuation: {'; '.join(fit_bonus_notes)}"
        return False, "CPU declined — assets don't meaningfully improve the contending roster."

    if cpu_team_mode == "play_in_fringe":
        # Like contending but slightly more flexible — will accept OVR 77+ players.
        giving_picks = [a for a in assets_giving if a.get("asset_type") == "pick"]
        if giving_picks and not receiving_players:
            return False, "CPU declines — fringe teams still protect draft capital."
        solid_incoming = any(
            a.get("player", {}).get("overall", 0) >= 77
            for a in receiving_players
        )
        if solid_incoming and score_a >= score_b * 0.97:
            reason = "CPU accepts — solid player upgrade fits the play-in push."
            if fit_bonus_notes:
                reason += f" [fit bonus: {'; '.join(fit_bonus_notes)}]"
            return True, reason
        if fit_bonus_total > 0 and score_a >= score_b * 0.90:
            return True, f"CPU accepts — buy-low fits the fringe push: {'; '.join(fit_bonus_notes)}"
        return False, "CPU declined — play-in fringe teams need clear upgrades."

    # developing mode: balanced, accept fair trades
    if evaluation["is_fair"] or score_a >= score_b * 0.80:
        reason = "CPU accepts — trade is balanced and fits team development."
        if fit_bonus_notes:
            reason += f" [fit bonus: {'; '.join(fit_bonus_notes)}]"
        return True, reason
    return False, "CPU declined — trade doesn't offer sufficient value."
