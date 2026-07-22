from __future__ import annotations

import datetime

from core.errors import DBAError
from core.logging import get_logger
from data.db import get_pool
from data.repositories import league_repo, player_repo, team_repo, trade_block_repo, trade_repo
from services import franchise_plan_service, ra_helpers, ra_reasoning, ride_along, trade_evaluator

# Used when persisting context signals — imported at module level so the timestamp
# helper is available without a deferred import inside the hot path.
from datetime import timezone as _timezone

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


async def _compute_live_mode(pool, league: league_repo.League, team_id: int) -> str:
    """
    Compute a CPU team's trade posture mode from current season data.

    Mirrors the DB queries in cpu_trade_service._compute_team_posture and delegates
    mode derivation to trade_evaluator.compute_team_mode — the single source of truth.
    This avoids reading the static cpu_mode column on the teams table, which caused
    propose-side and accept-side to see different modes for the same team.

    Falls back to 'developing' on any DB error (safe neutral default).
    """
    try:
        row = await pool.fetchrow(
            """
            SELECT sc.wins, sc.losses, t.conference
            FROM standings_cache sc
            JOIN teams t ON t.id = sc.team_id
            WHERE sc.league_id = $1 AND sc.team_id = $2 AND sc.season = $3
            """,
            league.id, team_id, league.current_season,
        )
        wins = row["wins"] if row else 0
        losses = row["losses"] if row else 0
        conference = row["conference"] if row else None
        games_played = wins + losses

        projected_wins: int | None = None
        if games_played >= 10:
            projected_wins = round((wins / games_played) * 82)

        conf_rank: int | None = None
        if conference:
            rank_val = await pool.fetchval(
                """
                SELECT COUNT(*) + 1
                FROM standings_cache sc2
                JOIN teams t2 ON t2.id = sc2.team_id
                WHERE sc2.league_id = $1 AND sc2.season = $2
                  AND t2.conference = $3
                  AND sc2.wins > $4
                """,
                league.id, league.current_season, conference, wins,
            )
            conf_rank = int(rank_val) if rank_val is not None else None

        age_rows = await pool.fetch(
            """
            SELECT EXTRACT(YEAR FROM AGE(p.birth_date))::int AS age
            FROM lineups l
            JOIN players p ON p.id = l.player_id
            WHERE l.league_id = $1 AND l.team_id = $2
              AND p.birth_date IS NOT NULL
            ORDER BY l.slot ASC
            LIMIT 8
            """,
            league.id, team_id,
        )
        ages = [r["age"] for r in age_rows if r["age"] is not None]
        avg_age = sum(ages) / len(ages) if ages else 27.0

        # B7 posture floor: star_count and plan_goal required — same queries as
        # cpu_trade_posture._compute_team_posture so accept-side and propose-side agree.
        star_count_val = await pool.fetchval(
            """
            SELECT COUNT(*)
            FROM lineups l
            JOIN players p ON p.id = l.player_id
            WHERE l.league_id = $1 AND l.team_id = $2 AND p.overall >= 85
            """,
            league.id, team_id,
        )
        star_count = int(star_count_val or 0)

        plan_goal_row = await pool.fetchrow(
            """
            SELECT goal FROM franchise_plans
            WHERE league_id = $1 AND team_id = $2 AND season = $3
            """,
            league.id, team_id, league.current_season,
        )
        plan_goal = plan_goal_row["goal"] if plan_goal_row else None

        return trade_evaluator.compute_team_mode(
            projected_wins, avg_age, conf_rank,
            star_count=star_count, plan_goal=plan_goal,
        )
    except Exception as exc:
        log.warning(f"_compute_live_mode failed for team {team_id}, falling back to 'developing': {exc}")
        return "developing"


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

    # Fetch form modifiers for all players in this trade in one batch query.
    # These feed into the buy-low archetype bonus inside cpu_should_accept.
    all_trade_players = list(proposer_players) + list(counterparty_players)
    _form_player_ids = [p.id for p in all_trade_players]
    _form_ovr_map = {p.id: p.overall for p in all_trade_players}
    _form_pos_map = {p.id: p.position for p in all_trade_players}
    try:
        _form_map = await trade_evaluator.compute_form_map(
            pool, _form_player_ids, _form_ovr_map, _form_pos_map,
            league.id, league.current_season,
        )
    except Exception:
        _form_map = {}

    async def _build_player_dicts(players, team_id: int) -> list[dict]:
        result = []
        for p in players:
            contract = await player_repo.get_active_contract(pool, p.id)
            form_mod, stats = _form_map.get(p.id, (1.0, {}))
            result.append({
                "asset_type": "player",
                "player": {
                    "id": p.id,
                    "first_name": getattr(p, "first_name", None),
                    "last_name": getattr(p, "last_name", None),
                    "overall": p.overall,
                    "age": _player_age(p),
                    "position": p.position,
                    # Tendency fields for archetype classification in buy-low logic.
                    "tendency_3pt": p.tendency_3pt,
                    "tendency_drive": p.tendency_drive,
                    "tendency_pass": p.tendency_pass,
                    "ast_tendency": p.ast_tendency,
                    "reb_tendency": p.reb_tendency,
                    "blk_tendency": p.blk_tendency,
                    "stl_tendency": p.stl_tendency,
                },
                "contract": {
                    "salary": contract.salary if contract else 0,
                    "years_remaining": contract.years_remaining if contract else 1,
                },
                # form_modifier < 1.0 means underperforming; used by buy-low bonus.
                "form_modifier": form_mod,
                # season_stats passed to player_trade_value so experience_premium fires.
                "season_stats": stats or {},
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

    # Build team-specific contexts for both sides so evaluate_trade can apply
    # cap-fit, roster-fit, and window-match modifiers from each team's perspective.
    # Falls back silently — build_team_context returns a neutral context on error.
    _ctx_proposer = await trade_evaluator.build_team_context(
        pool, league.id, proposer_team.id, league.current_season
    )
    _ctx_counterparty = await trade_evaluator.build_team_context(
        pool, league.id, counterparty_team.id, league.current_season
    )

    # From CPU perspective: side_a = what CPU receives (proposer gives), side_b = what CPU gives.
    # season_stats + form_modifier included so player_trade_value gets full compound value
    # (base × age × contract × form × experience_premium), which drives the fleecing floor.
    # receiving_team_context_a = counterparty context (they receive side_b assets)
    # receiving_team_context_b = proposer context (they receive side_a assets)
    evaluation = trade_evaluator.evaluate_trade(
        side_a_players=[
            {
                "player": d["player"],
                "contract": d["contract"],
                "season_stats": d.get("season_stats"),
                "form_modifier": d.get("form_modifier", 1.0),
            }
            for d in proposer_asset_dicts
        ],
        side_a_picks=proposer_pick_dicts,
        side_b_players=[
            {
                "player": d["player"],
                "contract": d["contract"],
                "season_stats": d.get("season_stats"),
                "form_modifier": d.get("form_modifier", 1.0),
            }
            for d in counterparty_asset_dicts
        ],
        side_b_picks=counterparty_pick_dicts,
        salary_cap=league.salary_cap,
        current_season=league.current_season,
        receiving_team_context_a=_ctx_counterparty,
        receiving_team_context_b=_ctx_proposer,
    )

    cpu_cap_used = await player_repo.get_team_cap_usage(pool, league.id, counterparty_team.id)

    # Compute CPU team mode live from current season record — same path as the
    # propose side (_compute_team_posture) so both sides always agree on mode.
    # Reads standings_cache + lineups; falls back to cpu_mode column on any error.
    _cpu_live_mode = await _compute_live_mode(pool, league, counterparty_team.id)

    assets_receiving = proposer_asset_dicts + proposer_pick_dicts
    assets_giving = counterparty_asset_dicts + counterparty_pick_dicts

    # Fetch role map for the players the CPU is giving up (counterparty_players).
    # Used by Guard 2 in cpu_should_accept to detect lead-role give-ups that lack
    # a starter-quality or 1st-round-pick return.
    _giving_role_map: dict[int, str] = {}
    _giving_pids = [p.id for p in counterparty_players]
    if _giving_pids:
        try:
            _role_rows = await pool.fetch(
                """
                SELECT player_id, role FROM player_roles
                WHERE league_id = $1 AND season = $2 AND player_id = ANY($3::int[])
                """,
                league.id, league.current_season, _giving_pids,
            )
            _giving_role_map = {r["player_id"]: r["role"] for r in _role_rows}
        except Exception as _role_exc:
            log.warning(f"[trade] role map fetch failed, Guard 2 skipped: {_role_exc}")

    # Build context_kwargs for the context-signal detectors inside cpu_should_accept.
    # Fetch the CPU team's franchise plan and posture so detectors have full context.
    _cpu_plan: dict = {}
    _cpu_posture: dict = {}
    _cpu_philosophy: str | None = None
    try:
        _cpu_plan = await franchise_plan_service.get_or_derive(
            pool, league.id, counterparty_team.id, league.current_season
        ) or {}
    except Exception:
        pass
    try:
        from services.ra_reasoning import _compute_posture as _ra_posture
        _cpu_posture = await _ra_posture(pool, league, counterparty_team.id) or {}
    except Exception:
        pass
    try:
        _cpu_philosophy = await pool.fetchval(
            "SELECT coach_philosophy FROM teams WHERE id = $1", counterparty_team.id
        )
    except Exception:
        pass

    accept, reason = await trade_evaluator.cpu_should_accept(
        cpu_team_mode=_cpu_live_mode,
        assets_receiving=assets_receiving,
        assets_giving=assets_giving,
        evaluation=evaluation,
        salary_cap=league.salary_cap,
        current_cap_used=cpu_cap_used,
        giving_role_map=_giving_role_map or None,
        pool=pool,
        context_kwargs={
            "league_id": league.id,
            "season": league.current_season,
            "perspective_team_id": counterparty_team.id,
            "plan": _cpu_plan,
            "posture": _cpu_posture,
            "coach_philosophy": _cpu_philosophy,
        },
    )

    cpu_score = evaluation["score_a"] - evaluation["score_b"]
    await trade_repo.set_cpu_evaluation(pool, trade.id, cpu_score, reason)

    # ── Persist context signals ───────────────────────────────────────────────
    # Store the per-player signals that drove this CPU decision so /trade why
    # can surface them later without re-deriving from team state that will differ.
    # Best-effort: signal persistence must never break the trade flow.
    _ctx_sigs_map = evaluation.get("context_signals_per_player") or {}
    if _ctx_sigs_map:
        try:
            # Build context_modifier_per_player from the signal deltas: sum all
            # deltas per player, clamp to [-0.15, 0.15], then 1.0 + clamped_sum.
            _mod_per_player: dict[str, float] = {}
            for _pid, _sigs in _ctx_sigs_map.items():
                _total_delta = sum(s.get("delta", 0.0) for s in _sigs)
                _clamped = max(-0.15, min(0.15, _total_delta))
                _mod_per_player[str(_pid)] = round(1.0 + _clamped, 4)

            await trade_repo.set_context_signals(pool, trade.id, {
                "per_player": {
                    str(pid): [
                        {"code": s.get("code", ""), "delta": s.get("delta", 0.0), "reason": s.get("reason", "")}
                        for s in sigs
                    ]
                    for pid, sigs in _ctx_sigs_map.items()
                },
                "context_modifier_per_player": _mod_per_player,
                "computed_at": datetime.datetime.now(_timezone.utc).isoformat(),
            })
        except Exception as _sig_exc:
            log.warning(f"[trade] context signal persistence failed for trade {trade.id}: {_sig_exc}")
    # ── End context signal persistence ───────────────────────────────────────

    # ── Ride-along hook (b): CPU Accept/Reject ────────────────────────────────
    # After CPU has evaluated the offer, before the decision is executed.
    # Veto flips: accept→reject, reject→accept.
    if ride_along.is_ride_along_enabled():
        try:
            _cpu_decision_label = "ACCEPT" if accept else "REJECT"
            _ra_header = (
                f"CPU [{counterparty_team.nba_team_code}] received offer from "
                f"[{proposer_team.nba_team_code}] — deciding {_cpu_decision_label}"
            )

            def _asset_label(asset_dict: dict) -> str:
                if asset_dict.get("asset_type") == "player":
                    p = asset_dict.get("player", {})
                    st = asset_dict.get("season_stats") or {}
                    form_mod = asset_dict.get("form_modifier", 1.0)
                    name = (
                        f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
                        or f"player#{p.get('id', '?')}"
                    )
                    ppg = st.get("ppg") or 0.0
                    apg = st.get("apg") or 0.0
                    rpg = st.get("rpg") or 0.0
                    bpg = st.get("bpg") or 0.0
                    spg = st.get("spg") or 0.0
                    gp  = st.get("gp") or st.get("games_played") or 0
                    fg_pct  = st.get("fg_pct") or 0.0
                    fg3_pct = st.get("fg3_pct") or 0.0
                    ft_pct  = st.get("ft_pct") or 0.0
                    # Base identity: name (OVR, position)
                    label = (
                        f"{name} (OVR {p.get('overall', '?')}, {p.get('position', '?')})"
                    )
                    if gp >= 5:
                        # Core production line: PPG/APG/RPG
                        stat_line = f"{ppg:.1f}/{apg:.1f}/{rpg:.1f}"
                        # Shooting efficiency: always shown when there is a sample
                        shoot_line = (
                            f"{fg_pct*100:.1f} FG / {fg3_pct*100:.1f} 3P / {ft_pct*100:.1f} FT"
                        )
                        # Defensive stats: only shown when at least one is >= 1.0 (reduces noise)
                        if bpg >= 1.0 or spg >= 1.0:
                            def_line = f"{bpg:.1f} BPG / {spg:.1f} SPG"
                            label += f" — {stat_line} on {shoot_line}, {def_line} ({gp} GP)"
                        else:
                            label += f" — {stat_line} on {shoot_line} ({gp} GP)"
                    if form_mod < 0.92:
                        label += f" [form={form_mod:.2f} underperforming]"
                    return label
                elif asset_dict.get("asset_type") == "pick":
                    return f"{asset_dict.get('season', '?')} R{asset_dict.get('round', '?')} pick"
                return "unknown asset"

            # Franchise plans for both sides — what each team is trying to build
            _plan_cpu = await franchise_plan_service.get_or_derive(
                pool, league.id, counterparty_team.id, league.current_season
            )
            _plan_prop = await franchise_plan_service.get_or_derive(
                pool, league.id, proposer_team.id, league.current_season
            )

            def _plan_short(plan: dict | None, code: str) -> str:
                if not plan:
                    return f"{code}: no plan"
                return (
                    f"{code}: {plan.get('goal', '?')} (h:{plan.get('horizon_seasons', '?')}) — "
                    f"core={len(plan.get('core_player_ids') or [])} "
                    f"flex={len(plan.get('flex_player_ids') or [])} "
                    f"surplus={len(plan.get('surplus_player_ids') or [])} "
                    f"pursuing={','.join(plan.get('asset_targets') or []) or 'none'}"
                )

            # Bucket the players the CPU is GIVING UP on its own plan — are they
            # parting with core (warning sign) or surplus (clean exit)?
            _cpu_core = set((_plan_cpu or {}).get("core_player_ids") or [])
            _cpu_flex = set((_plan_cpu or {}).get("flex_player_ids") or [])
            _cpu_surplus = set((_plan_cpu or {}).get("surplus_player_ids") or [])
            _giving_player_ids = [
                a.get("player", {}).get("id")
                for a in counterparty_asset_dicts
                if a.get("player", {}).get("id") is not None
            ]
            _give_core = sum(1 for pid in _giving_player_ids if pid in _cpu_core)
            _give_flex = sum(1 for pid in _giving_player_ids if pid in _cpu_flex)
            _give_surplus = sum(1 for pid in _giving_player_ids if pid in _cpu_surplus)

            # Roles moving (both sides) — uses the same shared helper as propose
            _receiving_player_ids = [
                a.get("player", {}).get("id")
                for a in proposer_asset_dicts
                if a.get("player", {}).get("id") is not None
            ]
            _ra_roles = await ra_helpers.build_ra_role_block(
                pool, league.id, league.current_season,
                {
                    counterparty_team.nba_team_code: _giving_player_ids,
                    proposer_team.nba_team_code: _receiving_player_ids,
                },
            )

            # proposer_picks / counterparty_picks are already full draft_pick dicts.
            _proposer_pick_dicts_ar = [dict(p) for p in proposer_picks]
            _counterparty_pick_dicts_ar = [dict(p) for p in counterparty_picks]

            # Show both market and team-specific ratios when they diverge by >5%.
            _msa = evaluation.get("market_score_a", evaluation["score_a"])
            _msb = evaluation.get("market_score_b", evaluation["score_b"])
            _market_ratio = _msa / max(_msb, 1.0)
            _team_ratio = evaluation["score_a"] / max(evaluation["score_b"], 1.0)
            _ratio_diverges = abs(_team_ratio - _market_ratio) > 0.05
            _scores_ar = (
                f"team {_team_ratio:.2f}"
                + (f" / market {_market_ratio:.2f}" if _ratio_diverges else "")
            )
            _flow_ar = (
                f"{proposer_team.nba_team_code} → {counterparty_team.nba_team_code}: "
                f"{', '.join(_asset_label(a) for a in assets_receiving) or '(none)'} | "
                f"{counterparty_team.nba_team_code} → {proposer_team.nba_team_code}: "
                f"{', '.join(_asset_label(a) for a in assets_giving) or '(none)'}"
            )
            # Extract pre-computed context signals from evaluation dict so
            # ra_reasoning renders bullets from the SAME signals that drove math.
            _ctx_signals_map = evaluation.get("context_signals_per_player") or {}
            # Per-perspective ratios: value_received / value_given from each team's POV.
            # score_a = what counterparty receives (proposer gives); score_b = what proposer receives.
            _sa = evaluation["score_a"]
            _sb = evaluation["score_b"]
            _count_ratio = _sa / max(_sb, 1.0)   # counterparty: receives _sa, gives _sb
            _prop_ratio = _sb / max(_sa, 1.0)    # proposer: receives _sb, gives _sa
            _ra_details = await ra_reasoning.render_trade_panel(
                pool, league, league.current_season,
                [
                    (
                        f"{counterparty_team.nba_team_code} perspective",
                        counterparty_team,
                        {
                            "players_in": _receiving_player_ids,
                            "players_out": _giving_player_ids,
                            "picks_in": _proposer_pick_dicts_ar,
                            "picks_out": _counterparty_pick_dicts_ar,
                            "cpu_reason": reason,
                            "context_signals_per_player": _ctx_signals_map,
                            "team_ratio_for_perspective": _count_ratio,
                        },
                    ),
                    (
                        f"{proposer_team.nba_team_code} perspective",
                        proposer_team,
                        {
                            "players_in": _giving_player_ids,
                            "players_out": _receiving_player_ids,
                            "picks_in": _counterparty_pick_dicts_ar,
                            "picks_out": _proposer_pick_dicts_ar,
                            "team_ratio_for_perspective": _prop_ratio,
                        },
                    ),
                ],
                flow_summary=_flow_ar,
                decision_label=_cpu_decision_label,
                scores_line=_scores_ar,
            )
            _ra_result = ride_along.prompt_decision(
                decision_type="accept_reject",
                header=_ra_header,
                details=_ra_details,
                default_action=_cpu_decision_label.lower(),
            )
            if _ra_result["action"] == "veto":
                # Flip the CPU's decision.
                accept = not accept
                log.info(
                    f"Ride-along veto: flipped CPU decision for trade {trade.id} "
                    f"from {_cpu_decision_label} → {'ACCEPT' if accept else 'REJECT'}"
                )
        except Exception:
            pass  # ride-along errors must never break the sim

    if accept:
        await trade_repo.update_status(pool, trade.id, "pending_commissioner")
        log.info(f"CPU auto-accepted trade {trade.id}: {reason}")
        updated = await trade_repo.get_trade(pool, trade.id)
        return updated

    # CPU declined — determine whether to counter-offer or reject outright.
    # Soft zone  (≤ 30%): CPU is close — try standard counter strategies first.
    # Hard zone  (30-50%): larger gap but a 1st-round pick demand could still bridge it
    #                      when CPU is giving up a starter/lead-role player.
    # Outside 50%: offer is too far off; hard reject.
    score_a = evaluation["score_a"]
    score_b = evaluation["score_b"]
    differential = evaluation["differential"]
    # closeness_pct: fraction of score_b that is the gap.  Equivalent to 1 - (score_a / score_b).
    closeness_pct = differential / max(score_b, 1.0)

    _SOFT_COUNTER_CLOSENESS = 0.30   # was 0.15 — widened to catch near-miss rejections
    _HARD_COUNTER_CLOSENESS = 0.50   # cap: gap > 50% → hard reject, no realistic sweetener

    # Only attempt a counter when the CPU is the losing side (giving more than receiving).
    # If the CPU is already winning the trade but still declined (e.g. franchise cornerstone
    # rule), just reject — there's no rational counter that makes it worse for the CPU.
    cpu_is_losing = score_b > score_a  # CPU gives score_b, receives score_a

    if cpu_is_losing and closeness_pct <= _HARD_COUNTER_CLOSENESS:
        # Try to build a counter-offer.  _attempt_counter_offer handles:
        #   - drop cheapest player from CPU's side (when CPU gives ≥2 players)
        #   - demand R2 pick from proposer (when CPU gives only 1 player)
        #   - demand R1 pick from proposer (when CPU gives a starter/lead-role player)
        counter_trade = await _attempt_counter_offer(
            pool,
            trade,
            league,
            proposer_team,
            counterparty_team,
            proposer_players,
            proposer_picks,
            counterparty_players,
            counterparty_picks,
            giving_role_map=_giving_role_map or None,
        )
        if counter_trade is not None:
            # Mark the original as superseded — it still blocks the player from
            # appearing in additional offers (unlike 'declined' which releases them).
            await trade_repo.update_status(pool, trade.id, "superseded")
            log.info(
                f"CPU counter-offered trade {trade.id} with new trade {counter_trade.id}: {reason}"
            )
            return counter_trade

    # Outright rejection — use 'rejected' (resolved) rather than 'declined' so callers
    # can distinguish CPU hard-refusal from a counter that didn't materialise.
    await trade_repo.update_status(pool, trade.id, "rejected")
    log.info(f"CPU rejected trade {trade.id}: {reason}")
    updated = await trade_repo.get_trade(pool, trade.id)
    return updated


async def _attempt_counter_offer(
    pool,
    original_trade: trade_repo.Trade,
    league: league_repo.League,
    proposer_team,
    counterparty_team,
    proposer_players,
    proposer_picks: list[dict],
    counterparty_players,
    counterparty_picks: list[dict],
    giving_role_map: dict[int, str] | None = None,
) -> "trade_repo.Trade | None":
    """
    Build a counter-offer when the CPU team's valuation is close but not quite there.

    Strategies (tried in order):
    1. Drop cheapest player from CPU's side (when CPU gives ≥2 players).
    2. Demand a 1st-round pick from proposer (when CPU gives exactly 1 player who
       is starter-tier OVR ≥ 78 or holds a lead role).
    3. Demand a 2nd-round pick from proposer (fallback when CPU gives 1 player
       that is not starter-tier, or when proposer has no R1 picks).

    giving_role_map: player_id → role string for the players the CPU is giving up.
    Used to detect lead-role players that merit an R1 demand.

    Returns the new Trade if successfully created, else None.
    """
    def _player_age(player: player_repo.Player) -> int:
        if player.birth_date:
            today = datetime.date.today()
            age = today.year - player.birth_date.year
            if (today.month, today.day) < (player.birth_date.month, player.birth_date.day):
                age -= 1
            return age
        return 28

    try:
        # Mirror-image deduplication: if there's already a pending/counter_offered trade
        # between these two teams with the same asset set (either direction), skip the
        # counter and let the original expire rather than create a duplicate.
        proposer_pid_set = frozenset(p.id for p in proposer_players)
        counterparty_pid_set = frozenset(p.id for p in counterparty_players)
        existing_trades = await pool.fetch(
            """SELECT id FROM trades
               WHERE league_id = $1
                 AND ((proposer_team_id = $2 AND counterparty_team_id = $3)
                      OR (proposer_team_id = $3 AND counterparty_team_id = $2))
                 AND status IN ('pending_counterparty', 'counter_offered')
            """,
            league.id, proposer_team.id, counterparty_team.id,
        )
        for existing in existing_trades:
            existing_assets = await pool.fetch(
                "SELECT player_id, from_team_id FROM trade_assets WHERE trade_id = $1 AND asset_type = 'player'",
                existing["id"],
            )
            ex_prop_pids = frozenset(
                r["player_id"] for r in existing_assets if r["from_team_id"] == proposer_team.id
            )
            ex_count_pids = frozenset(
                r["player_id"] for r in existing_assets if r["from_team_id"] == counterparty_team.id
            )
            if (ex_prop_pids == proposer_pid_set and ex_count_pids == counterparty_pid_set) or \
               (ex_prop_pids == counterparty_pid_set and ex_count_pids == proposer_pid_set):
                log.info(
                    f"Counter-offer skipped for trade {original_trade.id}: mirror-image duplicate "
                    f"of existing trade {existing['id']}"
                )
                return None

        # Score each player the CPU is giving up and find the cheapest one to drop.
        scored_cpu_players: list[tuple[float, int]] = []
        for p in counterparty_players:
            contract = await player_repo.get_active_contract(pool, p.id)
            v = trade_evaluator.player_trade_value(
                {"overall": p.overall, "age": _player_age(p)},
                {
                    "salary": contract.salary if contract else 0,
                    "years_remaining": contract.years_remaining if contract else 1,
                },
                league.salary_cap,
            )
            scored_cpu_players.append((v, p.id))

        scored_cpu_players.sort()  # ascending — cheapest first

        if len(scored_cpu_players) > 1:
            # Drop the cheapest player from the CPU's side.
            drop_id = scored_cpu_players[0][1]
            new_counterparty_players = [p for p in counterparty_players if p.id != drop_id]
            new_counterparty_picks = counterparty_picks
            new_proposer_players = proposer_players
            new_proposer_picks = proposer_picks
        else:
            # CPU is only giving one player — ask proposer to add a pick.
            # Determine whether the CPU player is starter-tier or holds a lead role;
            # if so, demand a 1st-round pick.  Otherwise fall back to a 2nd-round pick.
            _role_map = giving_role_map or {}
            _giving_lead_or_starter = any(
                (p.overall >= 78) or (_role_map.get(p.id) in trade_evaluator.LEAD_ROLES)
                for p in counterparty_players
            )
            available_picks = await trade_repo.get_team_picks(pool, league.id, proposer_team.id)
            r1_picks = sorted(
                [pk for pk in available_picks if pk["round"] == 1],
                key=lambda pk: pk["season"],
            )
            r2_picks = sorted(
                [pk for pk in available_picks if pk["round"] == 2],
                key=lambda pk: pk["season"],
            )

            pk_row = None
            if _giving_lead_or_starter and r1_picks:
                # Demand the nearest 1st-round pick the proposer owns.
                candidate_id = r1_picks[0]["id"]
                _candidate = await pool.fetchrow(
                    "SELECT * FROM draft_picks WHERE id = $1", candidate_id
                )
                if _candidate and _candidate["current_team_id"] == proposer_team.id:
                    pk_row = _candidate
                    _lead_player = next(
                        (p for p in counterparty_players if
                         p.overall >= 78 or _role_map.get(p.id) in trade_evaluator.LEAD_ROLES),
                        counterparty_players[0],
                    )
                    log.info(
                        f"Counter-offer: demanding R1 pick from {proposer_team.nba_team_code} "
                        f"because CPU giving lead-role/starter "
                        f"({_lead_player.first_name} {_lead_player.last_name} "
                        f"OVR {_lead_player.overall})"
                    )

            if pk_row is None:
                # Fall back to R2 demand (no R1 available or player is not starter-tier).
                if not r2_picks:
                    # Nothing to add — can't build a useful counter.
                    return None
                candidate_id = r2_picks[0]["id"]
                _candidate = await pool.fetchrow(
                    "SELECT * FROM draft_picks WHERE id = $1", candidate_id
                )
                if not _candidate or _candidate["current_team_id"] != proposer_team.id:
                    return None
                pk_row = _candidate

            new_counterparty_players = counterparty_players
            new_counterparty_picks = counterparty_picks
            new_proposer_players = proposer_players
            new_proposer_picks = list(proposer_picks) + [dict(pk_row)]

        if not new_counterparty_players and not new_counterparty_picks:
            return None

        # ── Ride-along hook: counter-offer construction ───────────────────────
        # Surface the CPU's planned counter (what dropped from their side, what
        # extra was demanded) before any DB writes.  Veto → no counter is made,
        # caller falls through to outright rejection.
        if ride_along.is_ride_along_enabled():
            try:
                _ra_header = (
                    f"CPU [{counterparty_team.nba_team_code}] building COUNTER to "
                    f"[{proposer_team.nba_team_code}]'s offer (original trade #{original_trade.id})"
                )

                # Describe what changed vs the original
                if len(scored_cpu_players) > 1:
                    _dropped = next((p for p in counterparty_players if p.id == drop_id), None)
                    _drop_name = (
                        f"{_dropped.first_name} {_dropped.last_name}"
                        if _dropped else f"player#{drop_id}"
                    )
                    _change_summary = (
                        f"drop {_drop_name} (OVR {_dropped.overall if _dropped else '?'}) "
                        f"from CPU's side"
                    )
                else:
                    _change_summary = (
                        f"demand extra {pk_row['season']} R{pk_row['round']} pick "
                        f"from {proposer_team.nba_team_code}"
                    )

                _ra_new_count_pick_dicts = [dict(pk) for pk in new_counterparty_picks]
                _ra_new_prop_pick_dicts = [dict(pk) for pk in new_proposer_picks]
                _ra_ct_gives_str = ", ".join(
                    [f"{p.first_name} {p.last_name} (OVR {p.overall})" for p in new_counterparty_players]
                    + [f"{pk['season']} R{pk['round']}" for pk in new_counterparty_picks]
                ) or "(none)"
                _ra_ct_gets_str = ", ".join(
                    [f"{p.first_name} {p.last_name} (OVR {p.overall})" for p in new_proposer_players]
                    + [f"{pk['season']} R{pk['round']}" for pk in new_proposer_picks]
                ) or "(none)"
                _ra_ct_flow = (
                    f"{counterparty_team.nba_team_code} → {proposer_team.nba_team_code}: {_ra_ct_gives_str} | "
                    f"{proposer_team.nba_team_code} → {counterparty_team.nba_team_code}: {_ra_ct_gets_str}"
                )
                _ra_details = await ra_reasoning.render_trade_panel(
                    pool, league, league.current_season,
                    [
                        (
                            f"{counterparty_team.nba_team_code} perspective",
                            counterparty_team,
                            {
                                "players_in": [p.id for p in new_proposer_players],
                                "players_out": [p.id for p in new_counterparty_players],
                                "picks_in": _ra_new_prop_pick_dicts,
                                "picks_out": _ra_new_count_pick_dicts,
                                "cpu_reason": f"counter: {_change_summary}",
                            },
                        ),
                        (
                            f"{proposer_team.nba_team_code} perspective",
                            proposer_team,
                            {
                                "players_in": [p.id for p in new_counterparty_players],
                                "players_out": [p.id for p in new_proposer_players],
                                "picks_in": _ra_new_count_pick_dicts,
                                "picks_out": _ra_new_prop_pick_dicts,
                            },
                        ),
                    ],
                    flow_summary=_ra_ct_flow,
                    decision_label="COUNTER",
                )
                _ra_result = ride_along.prompt_decision(
                    decision_type="counter_offer",
                    header=_ra_header,
                    details=_ra_details,
                    default_action="approve",
                )
                if _ra_result["action"] == "veto":
                    log.info(
                        f"Ride-along veto: CPU counter for original trade "
                        f"{original_trade.id} cancelled; caller will outright-reject"
                    )
                    return None
            except Exception:
                pass  # ride-along errors must never break the sim

        counter_trade = await create_trade_record(
            pool,
            league=league,
            proposer_team=counterparty_team,   # CPU is now the proposer
            counterparty_team=proposer_team,   # original proposer receives the counter
            proposer_player_ids=[p.id for p in new_counterparty_players],
            proposer_pick_ids=[pk["id"] for pk in new_counterparty_picks],
            counterparty_player_ids=[p.id for p in new_proposer_players],
            counterparty_pick_ids=[pk["id"] for pk in new_proposer_picks],
            initial_status="counter_offered",
        )
        log.info(
            f"CPU built counter-offer trade {counter_trade.id} "
            f"in response to original trade {original_trade.id}"
        )
        return counter_trade
    except Exception as exc:
        log.warning(f"Counter-offer construction failed for trade {original_trade.id}: {exc}")
        return None


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
