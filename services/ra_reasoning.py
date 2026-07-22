"""Trade-reasoning generator for ride-along trade panels.

Replaces key-value field dumps with motivation-driven, coach-voice bullets.
Each team gets a 5-9 bullet read-out: a scene-setter, per-player "why we want
them / why we're moving them" lines, pick/cap lines in plain English, and a
closing alignment verdict.

Public API
----------
build_team_perspective(pool, league, season, perspective_team, other_team,
                       players_in, players_out, picks_in, picks_out, *,
                       decision_context=None) -> list[str]

build_perspective_header(pool, league, season, team) -> str

render_trade_panel(pool, league, season, perspectives, *, flow_summary,
                   decision_label, scores_line=None) -> dict

Sentence-builder helpers live in trade_narrative_lines.py, DB-touching
per-player/pick lookups live in trade_reasoning_fetchers.py (Phase 3
opportunistic split, see HANDOFF.md) -- this module is now the
orchestration layer only.
"""
from __future__ import annotations

from typing import Any

from core.logging import get_logger
from data.repositories import league_repo, player_repo, team_repo
from services import franchise_plan_service
from services.trade_narrative_lines import (
    _bucket_for_player,
    _defense_quality_line,
    _motivation_incoming,
    _motivation_outgoing,
    _overperforming_line_incoming,
    _overperforming_line_outgoing,
    _pick_context_bullet,
    _posture_mode_label,
    _role_fit_line,
    _scheme_implication_line,
    _window_fit_line_incoming,
    _window_fit_line_outgoing,
    _window_line,
)
from services.trade_reasoning_fetchers import (
    _compute_posture,
    _fetch_player_form,
    _fetch_player_full,
    _fetch_player_role_on_team,
    _fetch_position_depth,
    _fetch_roster_median_ovr,
    _fetch_top_role_at_position,
    _project_pick_slot,
    _synergy_line,
)

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def build_perspective_header(
    pool,
    league: league_repo.League,
    season: int,
    team: team_repo.Team,
) -> str:
    """One-line meta header: coach | plan | posture."""
    try:
        coach = team.cpu_mode or "unknown"
        # Fetch actual coach_philosophy from teams table
        phil = await pool.fetchval(
            "SELECT coach_philosophy FROM teams WHERE id = $1", team.id
        )
        if phil:
            coach = phil

        plan = await franchise_plan_service.get_or_derive(pool, league.id, team.id, season)
        posture = await _compute_posture(pool, league, team.id)

        plan_label = f"{plan.get('goal', '?')} h:{plan.get('horizon_seasons', '?')}"
        posture_label = _posture_mode_label(posture.get("mode", "developing"), posture.get("urgency", "comfortable"))

        return f"coach: {coach}  |  plan: {plan_label}  |  posture: {posture_label}"
    except Exception as exc:
        log.debug("build_perspective_header failed for team %d: %s", team.id, exc)
        return f"coach: {team.cpu_mode or '?'}  |  plan: ?  |  posture: ?"


async def build_team_perspective(
    pool,
    league: league_repo.League,
    season: int,
    perspective_team: team_repo.Team,
    other_team: team_repo.Team,
    players_in: list[int],
    players_out: list[int],
    picks_in: list[dict],
    picks_out: list[dict],
    *,
    decision_context: dict | None = None,
    context_signals_map: dict[int, list[dict]] | None = None,
) -> list[str]:
    """
    Build a narrative read of this trade from perspective_team's POV.

    Returns a list of bullet-point strings, each starting with "• ".
    Target: 3-4 bullets per side.  Skips lines when data is absent or irrelevant.
    Each bullet must be a single line — no run-ons.
    """
    bullets: list[str] = []

    try:
        plan = await franchise_plan_service.get_or_derive(pool, league.id, perspective_team.id, season)
    except Exception as exc:
        log.debug("plan fetch failed for %d: %s", perspective_team.id, exc)
        plan = {}

    try:
        posture = await _compute_posture(pool, league, perspective_team.id)
    except Exception as exc:
        log.debug("posture fetch failed for %d: %s", perspective_team.id, exc)
        posture = {"mode": "developing", "urgency": "comfortable"}

    # ── Bullet 1: Scene-setter / window line ───────────────────────────────
    try:
        bullets.append("• " + _window_line(plan, posture))
    except Exception as exc:
        log.debug("window_line failed: %s", exc)

    # Fetch the receiving team's coach_philosophy once — used for scheme bullets
    try:
        _team_philosophy: str | None = await pool.fetchval(
            "SELECT coach_philosophy FROM teams WHERE id = $1", perspective_team.id
        )
    except Exception:
        _team_philosophy = None

    # Roster median OVR for the receiving team — used to gate honest "depth add"
    # framing vs "fits the scheme" for incoming players who are not upgrades.
    _roster_median_ovr: float = await _fetch_roster_median_ovr(
        pool, league.id, perspective_team.id
    )

    # ── Bullets: Incoming player motivation lines ──────────────────────────
    # Each player: 1 primary bullet + at most 1 context bullet.
    # Context priority: role-mismatch > scheme > synergy > defense-elite > window-fit > role-fit-positive > overperformance
    for pid in players_in:
        try:
            p = await _fetch_player_full(pool, league.id, pid)
            if not p:
                continue
            pos = p.get("position", "?")
            ovr = p.get("overall", 75)
            name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()

            # Form data (uses cached compute_form_map)
            form_mod, stats = await _fetch_player_form(
                pool, league.id, season, pid, ovr
            )

            # Position depth on the receiving team (pre-trade snapshot)
            depth = await _fetch_position_depth(
                pool, league.id, season, perspective_team.id, pos
            )

            # Who's currently atop that position on this team
            top_at_pos = await _fetch_top_role_at_position(
                pool, league.id, season, perspective_team.id, pos
            )

            # Current role / team context for the player
            current_role_info = await _fetch_player_role_on_team(pool, league.id, season, pid)
            current_role = current_role_info.get("role", "unknown")
            current_team_code = current_role_info.get("nba_team_code") or ""
            # Fallback: player has no player_roles row yet (fresh trade target,
            # pre-derive). Look up team code directly from player.team_id.
            if not current_team_code and p.get("team_id"):
                _tc = await pool.fetchval(
                    "SELECT nba_team_code FROM teams WHERE id = $1",
                    p["team_id"],
                )
                current_team_code = _tc or "their old team"
            if not current_team_code:
                current_team_code = "their old team"

            line = _motivation_incoming(
                player=p,
                form_mod=form_mod,
                stats=stats,
                plan=plan,
                posture=posture,
                depth=depth,
                top_at_pos=top_at_pos,
                current_team_code=current_team_code,
                current_role=current_role,
                roster_median_ovr=_roster_median_ovr,
            )
            bullets.append(f"• {line}")

            # ── Context bullet (one per player, highest-priority wins) ──────
            # When context_signals_map is present (pre-computed by cpu_should_accept),
            # read the reason text directly from the signals that drove the math.
            # This guarantees narrative ↔ value-math alignment.
            # Fall back to local detector calls when signals map is absent.
            try:
                _ctx_signals: list[dict] = (context_signals_map or {}).get(pid) or []
                if _ctx_signals:
                    # Render the highest-delta signal as the context bullet.
                    # Priority order: negative signals first (actionable), then positive.
                    _negative = [s for s in _ctx_signals if s.get("delta", 0) < 0]
                    _positive = [s for s in _ctx_signals if s.get("delta", 0) >= 0]
                    # Sort negatives by most negative delta; positives by highest delta.
                    _negative.sort(key=lambda s: s.get("delta", 0))
                    _positive.sort(key=lambda s: s.get("delta", 0), reverse=True)
                    _ordered = _negative + _positive
                    if _ordered:
                        _top_sig = _ordered[0]
                        _sig_reason = _top_sig.get("reason", "")
                        _sig_code = _top_sig.get("code", "")
                        _sig_delta = _top_sig.get("delta", 0.0)
                        _delta_tag = f" [Δ{_sig_delta:+.2f}]"
                        bullets.append(f"  -> {name}: {_sig_reason}{_delta_tag}")
                else:
                    # Fallback: re-detect using local helpers (no pool-computed signals).
                    _role_fit = _role_fit_line(p, current_role)
                    _role_mismatch: str | None = None
                    _role_match: str | None = None
                    if _role_fit:
                        if "doesn't match" in _role_fit:
                            _role_mismatch = f"{name} {_role_fit}"
                        else:
                            _role_match = f"{name}: {_role_fit}"

                    _scheme = _scheme_implication_line(current_role, _team_philosophy)
                    _synergy = await _synergy_line(
                        pool, league.id, season, perspective_team.id, current_role
                    )
                    _def_quality = _defense_quality_line(p)
                    _def_elite: str | None = None
                    if _def_quality and ("elite" in _def_quality or "best perimeter" in _def_quality or "genuinely two-way" in _def_quality):
                        _def_elite = f"{name}: {_def_quality}"
                    _win_fit = _window_fit_line_incoming(p, plan)
                    _overperf = _overperforming_line_incoming(name, form_mod, ovr)

                    ctx = _pick_context_bullet(p, [
                        _role_mismatch,      # 1. role-mismatch (negative, actionable)
                        _scheme,             # 2. scheme implication
                        _synergy,            # 3. roster synergy / overlap
                        _def_elite,          # 4. elite defense signal
                        _win_fit,            # 5. window-fit / timeline
                        _role_match,         # 6. role-fit positive
                        _overperf,           # 7. overperformance (interesting but lowest)
                    ])
                    if ctx:
                        bullets.append(f"  -> {ctx}")
            except Exception as exc:
                log.debug("incoming_context failed pid=%d: %s", pid, exc)

        except Exception as exc:
            log.debug("incoming_motivation failed pid=%d: %s", pid, exc)

    # ── Bullets: Outgoing player motivation lines ─────────────────────────
    # Each player: 1 primary bullet + at most 1 context bullet (overperf or window-out).
    for pid in players_out:
        try:
            p = await _fetch_player_full(pool, league.id, pid)
            if not p:
                continue
            ovr = p.get("overall", 75)
            pos = p.get("position", "?")
            name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            bucket = _bucket_for_player(pid, plan)

            form_mod, _stats = await _fetch_player_form(
                pool, league.id, season, pid, ovr
            )
            # Depth on THIS team (pre-trade)
            pos_depth = await _fetch_position_depth(
                pool, league.id, season, perspective_team.id, pos
            )

            line = _motivation_outgoing(
                player=p,
                form_mod=form_mod,
                plan=plan,
                posture=posture,
                bucket=bucket,
                pos_depth=pos_depth,
            )
            if line:
                bullets.append(f"• {line}")

            # ── Context bullet for outgoing (overperf sell-high > window-out) ─
            try:
                _overperf_out = _overperforming_line_outgoing(name, form_mod, ovr)
                _win_out = _window_fit_line_outgoing(p, plan)
                ctx_out = _pick_context_bullet(p, [_overperf_out, _win_out])
                if ctx_out and line:  # only add context if primary line fired
                    bullets.append(f"  ->{ctx_out}")
            except Exception as exc:
                log.debug("outgoing_context failed pid=%d: %s", pid, exc)

        except Exception as exc:
            log.debug("outgoing_motivation failed pid=%d: %s", pid, exc)

    # ── Bullets: Pick-in lines ─────────────────────────────────────────────
    for pick in picks_in:
        try:
            pick_season = pick.get("season", season + 1)
            pick_round = pick.get("round", 1)
            orig_team_id = pick.get("original_team_id") or pick.get("current_team_id")

            # Resolve original team code
            orig_code = await pool.fetchval(
                "SELECT nba_team_code FROM teams WHERE id = $1", orig_team_id
            ) if orig_team_id else None
            orig_str = f" (via {orig_code})" if orig_code and orig_code != perspective_team.nba_team_code else ""

            slot, verdict, proj_w, proj_l = await _project_pick_slot(
                pool, league.id, pick, season
            )

            if pick_round != 1:
                bullets.append(
                    f"• Getting a 2nd-rounder{orig_str} projected around #{slot} — "
                    f"basically a developmental flier, but we'll take it."
                )
            elif slot <= 14:
                record_note = f" ({proj_w}-{proj_l} pace)" if proj_w is not None else ""
                bullets.append(
                    f"• {pick_season} first-rounder{orig_str} projects #{slot}{record_note} — "
                    f"that's real lottery value, meaningful pickup."
                )
            else:
                record_note = f" ({proj_w}-{proj_l} pace)" if proj_w is not None else ""
                bullets.append(
                    f"• {pick_season} first-rounder{orig_str} projects #{slot}{record_note} — "
                    f"late first, but a pick's a pick."
                )
        except Exception as exc:
            log.debug("pick_in_line failed: %s", exc)

    # ── Bullets: Pick-out lines ────────────────────────────────────────────
    for pick in picks_out:
        try:
            pick_season = pick.get("season", season + 1)
            pick_round = pick.get("round", 1)

            slot, verdict, proj_w, proj_l = await _project_pick_slot(
                pool, league.id, pick, season
            )

            if pick_round != 1:
                bullets.append(
                    f"• Losing a 2nd-rounder projected around #{slot} — "
                    f"basically a flier we won't miss."
                )
            elif slot >= 20:
                bullets.append(
                    f"• Giving up our {pick_season} first projected #{slot} — "
                    f"late pick, cost is modest."
                )
            else:
                bullets.append(
                    f"• Giving up our {pick_season} first projected #{slot} — "
                    f"that's real asset cost, has to be worth it."
                )
        except Exception as exc:
            log.debug("pick_out_line failed: %s", exc)

    # ── Cap math line ──────────────────────────────────────────────────────
    try:
        cap = league.salary_cap
        current_usage = await player_repo.get_team_cap_usage(pool, league.id, perspective_team.id)

        sal_in = 0
        sal_in_yrs: list[int] = []
        for pid in players_in:
            c = await player_repo.get_active_contract(pool, pid)
            if c:
                sal_in += c.salary
                sal_in_yrs.append(c.years_remaining)

        sal_out = 0
        for pid in players_out:
            c = await player_repo.get_active_contract(pool, pid)
            if c:
                sal_out += c.salary

        delta = sal_in - sal_out

        if not (delta == 0 and not players_in and not players_out):
            delta_m = round(abs(delta) / 1_000_000, 1)
            avg_yrs = round(sum(sal_in_yrs) / len(sal_in_yrs), 1) if sal_in_yrs else 0

            new_usage_m = round((current_usage + delta) / 1_000_000, 1)
            cap_m = round(cap / 1_000_000, 1)

            if new_usage_m > cap_m:
                if delta > 0:
                    yrs_note = f"{avg_yrs:.0f}y on the contract" if avg_yrs > 0 else "contract"
                    bullets.append(
                        f"• Cap-wise this is tight — adds ${delta_m}M ({yrs_note}), "
                        f"puts us at ${new_usage_m}M against a ${cap_m}M cap. Over the line."
                    )
                else:
                    bullets.append(
                        f"• Cap situation: we're still over after this at ${new_usage_m}M — "
                        f"saves ${delta_m}M but doesn't fix the crunch."
                    )
            elif new_usage_m > cap_m * 0.95:
                direction = f"adds ${delta_m}M" if delta > 0 else f"saves ${delta_m}M"
                bullets.append(
                    f"• Cap-wise — {direction}, leaves us at ${new_usage_m}M. Tight but workable."
                )
            elif delta < 0:
                bullets.append(
                    f"• Cap-wise this opens ${round(-delta / 1_000_000, 1)}M in space — "
                    f"drops us to ${new_usage_m}M, gives us breathing room."
                )
            else:
                yrs_note = f", {avg_yrs:.0f}y on the deal" if avg_yrs > 0 else ""
                bullets.append(
                    f"• Cap-wise we're comfortable — ${delta_m}M added{yrs_note}, "
                    f"plenty of room at ${new_usage_m}M / ${cap_m}M cap."
                )
    except Exception as exc:
        log.debug("cap_math_line failed: %s", exc)

    # ── Closing alignment line ─────────────────────────────────────────────
    # Value-ratio override: when the caller supplies team_ratio (value_received /
    # value_given from this team's POV), decisive math overrides plan_alignment
    # so the Net verdict reflects reality rather than just plan bucket logic.
    # Thresholds: ratio < 0.92 → step back; ratio > 1.08 → advances.
    # Neutral band (0.92–1.08) falls through to the plan_alignment verdict below.
    try:
        _team_ratio: float | None = (decision_context or {}).get("team_ratio")
        _ratio_net_fired = False
        if _team_ratio is not None:
            if _team_ratio < 0.92:
                bullets.append(
                    f"• Net: step back — we'd give up real value here (ratio {_team_ratio:.2f})."
                )
                _ratio_net_fired = True
            elif _team_ratio > 1.08:
                bullets.append(
                    f"• Net: advances — we get more than we send (ratio {_team_ratio:.2f})."
                )
                _ratio_net_fired = True

        if not _ratio_net_fired:
            goal = plan.get("goal", "transition")
            core_ids = set(plan.get("core_player_ids") or [])
            surplus_ids = set(plan.get("surplus_player_ids") or [])
            flex_ids = set(plan.get("flex_player_ids") or [])

            core_lost = sum(1 for pid in players_out if pid in core_ids)
            surplus_cleared = sum(1 for pid in players_out if pid in surplus_ids)
            flex_lost = sum(1 for pid in players_out if pid in flex_ids)

            picks_gained = len(picks_in)

            if core_lost > 0:
                bullets.append(
                    "• Net: this is a step back — we're giving up core piece(s). "
                    "Hard pass unless we're missing something."
                )
            elif goal in ("tank", "rebuild") and surplus_cleared > 0 and (picks_gained > 0 or players_in):
                bullets.append(
                    "• Net: this is a plan-advancing move — sheds surplus, gets younger, "
                    "adds the assets our rebuild needs."
                )
            elif goal == "win_now" and not core_lost and not flex_lost and players_in:
                bullets.append(
                    "• Net: adds a piece without touching the core window. We like this move."
                )
            elif surplus_cleared > 0:
                bullets.append(
                    "• Net: clears a guy we didn't need anymore for a player who fits. Move on."
                )
            elif flex_lost and not core_lost and goal not in ("win_now",):
                bullets.append(
                    "• Net: lateral move. Acceptable, not exciting — "
                    "flex for return at the right value."
                )
            else:
                bullets.append(
                    "• Net: fits the plan without moving the needle much. "
                    "Acceptable if the price is right."
                )
    except Exception as exc:
        log.debug("plan_alignment_line failed: %s", exc)

    # ── CPU model reason (if provided) ────────────────────────────────────
    if decision_context:
        cpu_reason = decision_context.get("cpu_reason")
        decision_label = decision_context.get("decision_label")
        if cpu_reason and decision_label:
            bullets.append(f"• Model says {decision_label}: {cpu_reason}")

    # Clamp to 4 bullets max.
    # Drop context (-> sub-lines) first — they're supplementary.
    # Then drop lowest-impact primary bullets (those added later have less structural weight).
    if len(bullets) > 4:
        ctx_indices = [i for i, b in enumerate(bullets) if b.startswith("  -> ")]
        removed = set()
        for idx in reversed(ctx_indices):
            if len(bullets) - len(removed) <= 4:
                break
            removed.add(idx)
        bullets = [b for i, b in enumerate(bullets) if i not in removed]
        # Hard cap: keep first bullet (scene-setter), last bullet (alignment/net),
        # and trim mid-section down to fit 4 total.
        if len(bullets) > 4:
            bullets = [bullets[0]] + bullets[1:-1][:2] + [bullets[-1]]

    return bullets


async def pick_ids_to_dicts(pool, pick_ids: list[int]) -> list[dict]:
    """Resolve a list of pick IDs to full pick dicts (season, round, original_team_id, etc.)."""
    if not pick_ids:
        return []
    rows = await pool.fetch(
        "SELECT * FROM draft_picks WHERE id = ANY($1::int[])",
        pick_ids,
    )
    by_id = {r["id"]: dict(r) for r in rows}
    return [by_id[pid] for pid in pick_ids if pid in by_id]


async def render_trade_panel(
    pool,
    league: league_repo.League,
    season: int,
    perspectives: list[tuple[str, Any, dict]],
    *,
    flow_summary: str,
    decision_label: str,
    scores_line: str | None = None,
) -> dict:
    """
    Top-level orchestrator for trade panels.

    perspectives: list of (label, team, swap_dict) where swap_dict has:
      players_in, players_out, picks_in, picks_out, cpu_reason (optional),
      context_signals_per_player (optional): dict[player_id → list[signal_dict]]
      team_ratio_for_perspective (optional): float — value_received / value_given
          from THIS team's POV.  When provided, overrides the plan_alignment Net
          verdict when value math is decisive (ratio < 0.92 or > 1.08).

    Returns a dict suitable for ride_along.prompt_decision's `details` param.
    The dict survives json.dumps (no sets, no dataclasses).
    """
    out: dict[str, Any] = {"flow": flow_summary}

    for label, team, swap in perspectives:
        players_in: list[int] = swap.get("players_in") or []
        players_out: list[int] = swap.get("players_out") or []
        picks_in: list[dict] = swap.get("picks_in") or []
        picks_out: list[dict] = swap.get("picks_out") or []
        cpu_reason: str | None = swap.get("cpu_reason")
        # Context signals pre-computed by cpu_should_accept — keyed by player_id (int).
        # Values are lists of dicts with keys: delta, reason, code.
        context_signals_map: dict[int, list[dict]] = swap.get("context_signals_per_player") or {}
        # Per-perspective team value ratio: value_received / value_given for this team.
        # None when the caller didn't supply score data (e.g. pure propose panels).
        team_ratio_for_perspective: float | None = swap.get("team_ratio_for_perspective")

        # Determine other team: look for first team in perspectives with a different id.
        other_team = None
        for other_label, other_t, _ in perspectives:
            if other_t.id != team.id:
                other_team = other_t
                break
        if other_team is None:
            other_team = team  # degenerate fallback

        decision_context: dict | None = None
        if cpu_reason or team_ratio_for_perspective is not None:
            decision_context = {
                "cpu_reason": cpu_reason,
                "decision_label": decision_label,
                "team_ratio": team_ratio_for_perspective,
            }

        try:
            header = await build_perspective_header(pool, league, season, team)
            bullets = await build_team_perspective(
                pool, league, season,
                perspective_team=team,
                other_team=other_team,
                players_in=players_in,
                players_out=players_out,
                picks_in=picks_in,
                picks_out=picks_out,
                decision_context=decision_context,
                context_signals_map=context_signals_map,
            )
        except Exception as exc:
            log.warning("render_trade_panel perspective %s failed: %s", label, exc)
            header = "coach: ?  |  plan: ?  |  posture: ?"
            bullets = [f"• (perspective unavailable: {exc})"]

        out[label] = {
            "_header": header,
            "_bullets": bullets,
        }

        # Include context signals in the JSONL output under the perspective key
        # so session logs show exactly which signals fired and their deltas.
        if context_signals_map:
            out[label]["context_signals"] = {
                str(pid): signals
                for pid, signals in context_signals_map.items()
            }

    if scores_line:
        out["score"] = scores_line

    return out
