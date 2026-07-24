"""Pure scoring/routing helpers for CPU trade proposal generation.

No pool access, no discord — every function here takes plain data in and
returns plain data out, which is what makes them cheap to unit test directly
(see tests/test_pick_proposal_modes.py, tests/test_fill_to_value.py,
tests/test_outgoing_first_smoke.py). Extracted from cpu_trade_proposal_runner.py.
"""
from __future__ import annotations

import random

from data.repositories import player_repo
from services import trade_grading, trade_value_math
from services.cpu_trade_posture import _player_age


def _derive_cap_state(
    team,
    cp_contexts: dict,
    league,
) -> str:
    """Classify team's cap situation into a routing bucket.

    Thresholds (per design §3.2):
      under      — payroll < salary_cap
      near_cap   — salary_cap <= payroll < salary_cap * 1.05
      over_luxury — payroll >= salary_cap * 1.18

    Hard-cap bucket omitted — not used for routing in PR 1.
    """
    ctx = cp_contexts.get(team.id, {})
    payroll = ctx.get("current_payroll", 0) or 0
    cap = league.salary_cap or 1
    if payroll >= cap * 1.18:
        return "over_luxury"
    if payroll >= cap:
        return "near_cap"
    return "under"


def _weighted_mode_choice(
    team_id: int,
    round_seed: int | None,
    weighted_options: list[tuple[list[str], float]],
) -> list[str]:
    """Weighted-random pick among several plausible mode lists for a posture/goal
    state that doesn't have one single objectively-correct answer (finding #7:
    pick_proposal_modes was a pure function of team state with zero variety —
    a team in a given state ran the identical mode every round until its state
    changed).

    Seeded off `round_seed ^ team_id` — same pattern as
    cpu_coach_service._compute_cpu_gameplan's `rng = random.Random(game_row["id"] ^ team_id)`
    — so the choice is reproducible for a given round but varies round to round.

    When round_seed is None (caller hasn't opted in — every existing decision-table
    unit test calls pick_proposal_modes without it), always returns the first
    (primary/highest-weight) option, preserving the pre-#7 deterministic behavior
    exactly. weighted_options[0] must be the primary/majority choice by convention.
    """
    if round_seed is None:
        return weighted_options[0][0]
    rng = random.Random(round_seed ^ team_id)
    choices, weights = zip(*weighted_options)
    return rng.choices(choices, weights=weights, k=1)[0]


def pick_proposal_modes(
    team,
    posture: str,
    plan: dict,
    cap_state: str,
    roster_size: int,
    *,
    round_seed: int | None = None,
) -> list[str]:
    """Return ordered list of modes to attempt for a team's trade proposal.

    Decision table applied top-to-bottom; first match wins.
    Falls back to ["incoming_first"] when no rule matches.

    posture: string mode from _compute_team_posture (e.g. "contend", "rebuild",
             "tank", "soft_rebuild", "play_in_fringe", "developing", "contending",
             "rebuilding").  Both the short ("contend") and long ("contending") forms
             are accepted for robustness.
    plan: franchise plan dict with goal, surplus_player_ids, asset_targets, and
          optionally derived_from_record.shop_intent (added in PR 3).
    cap_state: one of "under", "near_cap", "over_luxury" (from _derive_cap_state).
    roster_size: current roster size (sanity check — unused in routing for PR 1).
    round_seed: opt-in variety-injection seed (#7) — pass a value that changes
        call to call (e.g. current_game_index combined with an offer index) so
        the handful of rules below that are genuinely a coaching-philosophy
        judgment call (not a hard requirement) occasionally pick a plausible
        alternative mode list instead of the same one every single round.
        Rules with a hard requirement (cap-dump/flip-asset shop_intent,
        over_luxury, rebuild two-way, win_now consolidation) are NOT varied —
        those aren't a matter of taste. Omit to get the original fully
        deterministic behavior (default for all existing callers/tests).
    """
    goal: str = (plan.get("goal") or "") if plan else ""
    surplus: list = (plan.get("surplus_player_ids") or []) if plan else []
    asset_targets: list = (plan.get("asset_targets") or []) if plan else []

    surplus_nonempty = bool(surplus)
    assets_nonempty = bool(asset_targets)

    # Read shop_intent from derived_from_record (keyed by str(player_id)).
    _dfr = (plan.get("derived_from_record") or {}) if plan else {}
    _shop_intent: dict[str, str] = _dfr.get("shop_intent") or {}

    # Normalise posture to the long form used internally.
    _p = posture or "developing"
    is_tank = _p in ("tank", "tanking")
    is_soft_rebuild = _p == "soft_rebuild"
    is_win_now = goal == "win_now"
    is_rebuild_goal = goal in ("rebuild", "tank")

    # ── shop_intent priority rules (highest precedence) ───────────────────────
    # cap_dump surplus: team must shed salary first, regardless of posture.
    _has_cap_dump = any(v == "cap_dump" for v in _shop_intent.values())
    if _has_cap_dump:
        return ["outgoing_first"]

    # flip_asset surplus: team acquired this player intending to re-sell.
    _has_flip_asset = any(v == "flip_asset" for v in _shop_intent.values())
    if _has_flip_asset:
        return ["outgoing_first"]
    # ── End shop_intent priority rules ───────────────────────────────────────

    # Rule 1: over luxury + win_now → outgoing first, then incoming (cap-strapped contender).
    # USER DECISION: real GMs know their budget before shopping.
    if cap_state == "over_luxury" and is_win_now:
        return ["outgoing_first", "incoming_first"]

    # Rule 2: over luxury, any other posture → outgoing only (must shed salary).
    if cap_state == "over_luxury":
        return ["outgoing_first"]

    # Rule 3: tank posture with surplus → dump assets (usually outgoing-only, but
    # occasionally a tanking team still opportunistically shops for cheap youth —
    # #7 variety, not a hard requirement).
    if is_tank and surplus_nonempty:
        return _weighted_mode_choice(
            team.id, round_seed,
            [(["outgoing_first"], 0.85), (["outgoing_first", "incoming_first"], 0.15)],
        )

    # Rule 4: rebuild goal with surplus AND asset targets → two-way (sell and buy).
    if is_rebuild_goal and surplus_nonempty and assets_nonempty:
        return ["outgoing_first", "incoming_first"]

    # Rule 5: win_now goal with surplus → consolidation buyer (incoming leads).
    if is_win_now and surplus_nonempty:
        return ["incoming_first", "outgoing_first"]

    # Rule 6: soft_rebuild with any surplus → usually outgoing-only (selling vets),
    # but a soft-rebuild team occasionally also looks to add young pieces in the
    # same round (#7 variety — a plausible alternative, not a hard requirement).
    if is_soft_rebuild and surplus_nonempty:
        return _weighted_mode_choice(
            team.id, round_seed,
            [(["outgoing_first"], 0.80), (["outgoing_first", "incoming_first"], 0.20)],
        )

    # Rule 7: play_in_fringe or developing → usually incoming-first (seeking an
    # upgrade), but occasionally these borderline teams also shop surplus depth
    # in the same round (#7 variety — a plausible alternative, not a hard
    # requirement).
    if _p in ("play_in_fringe", "developing"):
        return _weighted_mode_choice(
            team.id, round_seed,
            [(["incoming_first"], 0.75), (["incoming_first", "outgoing_first"], 0.25)],
        )

    # Default fallback.
    return ["incoming_first"]


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


def _is_stacked_without_upgrade(
    position: str,
    incoming_overall: int,
    pos_count_map: dict[str, int],
    roster: list,
    stacked_floor: int = 3,
    upgrade_margin: int = 3,
) -> bool:
    """#2: True when `position` is already stacked (>= stacked_floor rostered
    players) AND the incoming player isn't a clear value upgrade over the
    weakest player the team already has at that position.

    Before this, only contending/play_in_fringe modes had a hard positional
    skip (via _get_trade_target_positions/_position_matches_need) — every
    other mode (the majority of CPU proposals: rebuilding, soft_rebuild,
    developing) had zero positional targeting, so a team with four centers
    would keep adding a fifth. Applies to all modes at the call site; the
    upgrade_margin escape hatch keeps this a soft-adjacent gate rather than
    an absolute ban — a genuine upgrade still goes through, mirroring the
    B5 "strict upgrade" precedent used for consolidation trades elsewhere.
    """
    if pos_count_map.get(position, 0) < stacked_floor:
        return False
    _same_pos_ovrs = [
        getattr(rp, "overall", 0) for rp in roster
        if getattr(rp, "position", None) == position
    ]
    if not _same_pos_ovrs:
        return False
    _weakest_ovr = min(_same_pos_ovrs)
    return incoming_overall < _weakest_ovr + upgrade_margin


def _score_outgoing_pair(
    team_a,
    outgoing_pid: int,
    team_b,
    speculative_return_player_ids: list[int],
    speculative_return_pick_ids: list[int],
    speculative_return_players: list,
    plan_a: dict | None,
    posture_a: str,
    roster_a: list,
    cp_contexts: dict,
    cp_r1_counts: dict | None = None,
    incoming_contracts: dict[int, player_repo.Contract | None] | None = None,
) -> float:
    """Score (team_a, outgoing_pid, team_b, speculative_return) from A's perspective.

    Pure function — no DB calls. All data must be pre-fetched by the caller.

    speculative_return_players: resolved Player objects for the returned player IDs
        (needed for archetype check and team-specific value; must match
        speculative_return_player_ids order exactly).
    roster_a: A's current roster (Player objects) — used to compute post-trade arch counts.
    cp_contexts: {team_id -> context dict} from the memoized Phase 3 block.
    incoming_contracts: {player_id -> Contract | None} pre-fetched by the caller
        (_derive_return_from_b returns these).  When present, real salary/years are
        used in player_trade_value instead of the synthetic {salary:0, years:1} shim.
        If a player's contract is absent from the dict (FA edge case), that player's
        pair_score contribution is skipped (returns 0 for that player) to avoid
        fabricating a value.

    Returns a float score (higher = more desirable from A's perspective).
    """
    _plan_a = plan_a or {}
    _asset_targets_a: list[str] = _plan_a.get("asset_targets") or []
    _plan_goal_a: str = _plan_a.get("goal", "")
    _ctx_a = cp_contexts.get(team_a.id, {})
    _cap_a = _ctx_a.get("current_payroll", 0)
    _inc_contracts: dict[int, player_repo.Contract | None] = incoming_contracts or {}

    # ── Base value: team-specific value of each incoming item to A ────────────
    base_value = 0.0
    for p in speculative_return_players:
        _age_p = _player_age(p) or 27
        if _inc_contracts:
            # Use real contract when available; skip player if contract unresolvable.
            _c = _inc_contracts.get(p.id)
            if p.id in _inc_contracts and _c is None:
                # Contract is known-absent (FA edge case) — skip this player.
                continue
            # _c may be None when p.id is missing from _inc_contracts entirely
            # (caller should always populate, but defend with neutral defaults).
            _salary = getattr(_c, "salary", 0) or 0
            _years = getattr(_c, "years_remaining", 1) or 1
        else:
            # No contracts provided (e.g. from unit tests) — use neutral defaults.
            _salary = 0
            _years = 1
        raw_v = trade_value_math.player_trade_value(
            {"overall": p.overall, "age": _age_p, "position": p.position},
            {"salary": _salary, "years_remaining": _years},
            _ctx_a.get("salary_cap", 140_000_000),
        )
        base_value += raw_v

    if base_value <= 0:
        return 0.0

    # ── Need multiplier: positional need AFTER the trade ─────────────────────
    # Post-trade A roster = current roster - outgoing + speculative_incoming.
    _pos_counts_post: dict[str, int] = {}
    for p in roster_a:
        if p.id == outgoing_pid:
            continue
        pos = getattr(p, "position", "") or ""
        _pos_counts_post[pos] = _pos_counts_post.get(pos, 0) + 1
    for p in speculative_return_players:
        pos = getattr(p, "position", "") or ""
        _pos_counts_post[pos] = _pos_counts_post.get(pos, 0) + 1

    # Average need multiplier across incoming players.
    if speculative_return_players:
        _need_sum = 0.0
        for p in speculative_return_players:
            pos = getattr(p, "position", "") or ""
            # Post-trade count including this player.
            cnt = _pos_counts_post.get(pos, 0)
            if cnt >= 3:
                _need_sum += 0.6
            elif cnt <= 1:
                _need_sum += 1.4
            else:
                _need_sum += 1.0
        need_multiplier = _need_sum / len(speculative_return_players)
    else:
        need_multiplier = 1.0

    # ── Plan bias: asset_targets match ────────────────────────────────────────
    _plan_bias = 1.0
    if _asset_targets_a:
        for p in speculative_return_players:
            _age_p = _player_age(p)
            if "role_players" in _asset_targets_a and 78 <= p.overall <= 85:
                _plan_bias += 0.20
            if "veterans" in _asset_targets_a and _age_p is not None and _age_p >= 28 and p.overall >= 78:
                _plan_bias += 0.20
            if "young_u23" in _asset_targets_a and _age_p is not None and _age_p < 23:
                _plan_bias += 0.20
        _r1_counts = cp_r1_counts or {}
        _b_r1_count = _r1_counts.get(team_b.id, 0)
        if "picks_r1" in _asset_targets_a and speculative_return_pick_ids:
            _plan_bias += 0.25
        if "picks_any" in _asset_targets_a and speculative_return_pick_ids:
            _plan_bias += 0.20
    # Clamp bias.
    _plan_bias = min(_plan_bias, 2.0)

    # ── B6 archetype penalty: EXACT post-trade roster count ───────────────────
    # Post-trade A roster = current roster - {outgoing_pid} + speculative incoming.
    _post_trade_roster_a = [p for p in roster_a if p.id != outgoing_pid] + list(speculative_return_players)
    _post_arch_counts = _team_archetype_counts(_post_trade_roster_a)

    _arch_penalty = 1.0
    for p in speculative_return_players:
        _incoming_arch = trade_grading._player_archetype({
            "position": getattr(p, "position", ""),
            "tendency_3pt": getattr(p, "tendency_3pt", 50) or 50,
            "tendency_drive": getattr(p, "tendency_drive", 50) or 50,
            "tendency_pass": getattr(p, "tendency_pass", 50) or 50,
            "ast_tendency": getattr(p, "ast_tendency", 50) or 50,
            "reb_tendency": getattr(p, "reb_tendency", 50) or 50,
            "blk_tendency": getattr(p, "blk_tendency", 50) or 50,
            "stl_tendency": getattr(p, "stl_tendency", 50) or 50,
        })
        if _incoming_arch:
            # Count BEFORE adding this player (post minus this player).
            _pre_count = _post_arch_counts.get(_incoming_arch, 0) - 1
            if _pre_count >= 2:
                _arch_penalty = min(_arch_penalty, 0.65)
            elif _pre_count == 1:
                _arch_penalty = min(_arch_penalty, 0.85)

    return base_value * need_multiplier * _plan_bias * _arch_penalty


def _fill_to_value(
    scored_players: list[tuple[int, float, int]],
    sorted_picks: list[dict],
    target_value: float,
    max_picks: int,
    tolerance: float,
    orig_win_pct: dict[int, float | None],
    current_season: int,
    team_id: int,
    mode: str,
    counterparty_mode: str = "default",
    existing_pick_ids: set[int] | None = None,
) -> tuple[list[int], list[int], float]:
    """Select players and picks from pre-sorted pools to meet target_value.

    Pure Python helper shared by _build_return_package and _derive_return_from_b.
    No DB calls — all inputs must be pre-fetched and pre-sorted by the caller.

    scored_players: list of (bucket_rank, value, player_id) sorted ascending by
        (bucket, -value) so surplus comes before flex, high value first within bucket.
    sorted_picks: list of pick dicts with keys: id, round, season, original_team_id.
    target_value: value to match.
    max_picks: hard cap on picks included.
    tolerance: absolute value tolerance (target_value * 0.25 typically).
    orig_win_pct: {original_team_id -> win_pct} for pick valuation.
    current_season: league.current_season for pick near-term protection.
    team_id: ID of the offering team (used for own-pick protection logic).
    mode: cpu_mode of the offering team (governs pick willingness thresholds).
    counterparty_mode: receiver's mode — triggers strategic pick inclusion for rebuilder targets.
    existing_pick_ids: pick IDs already in the package (prevents duplicates).

    Returns (player_ids, pick_ids, accumulated_value).
    """
    offer_player_ids: list[int] = []
    offer_pick_ids: list[int] = []
    accumulated = 0.0
    _existing = existing_pick_ids or set()

    # Fill with players first.
    for _bucket, v, pid in scored_players:
        if accumulated >= target_value - tolerance:
            break
        offer_player_ids.append(pid)
        accumulated += v

    # Strategic pick inclusion — rebuilder counterparties want picks.
    needs_pick_for_rebuilder = (
        counterparty_mode in ("rebuilding", "soft_rebuild") and len(offer_pick_ids) == 0
    )

    for pick in sorted_picks:
        if len(offer_pick_ids) >= max_picks:
            break
        if accumulated >= target_value - tolerance and not needs_pick_for_rebuilder:
            break

        # First-round pick protection: own near-term firsts almost never move.
        if pick["round"] == 1 and pick.get("original_team_id") == team_id:
            if pick["season"] <= current_season + 1:
                continue
            if mode not in ("contending", "play_in_fringe"):
                continue
            threshold = 0.15 if mode == "contending" else 0.08
            if random.random() > threshold:
                continue

        orig_tid = pick.get("original_team_id")
        win_pct = orig_win_pct.get(orig_tid) if orig_tid else None
        pv = trade_value_math.pick_trade_value(
            pick["season"], pick["round"], current_season,
            team_win_pct=win_pct,
        )
        if pick["id"] not in _existing and pick["id"] not in offer_pick_ids:
            offer_pick_ids.append(pick["id"])
            accumulated += pv
            needs_pick_for_rebuilder = False

    return offer_player_ids, offer_pick_ids, accumulated


def _team_a_wants_player(
    posture_a: dict, player: player_repo.Player, *, age_override: int | None = None
) -> bool:
    """Return True if team A's posture makes it interested in this player.

    B1 posture gates (hard checks):
    - contending/play_in_fringe: reject raw young developmental players (age < 25,
      OVR < 77) — they don't help the win-now window.
    - rebuilding/soft_rebuild: reject aging vets over 30 unless they're expiring
      contracts (years_remaining=1) worth flipping.  The age signal is an
      approximation; callers with full contract data can refine in scoring.

    age_override: use this age directly instead of deriving it from
      player.birth_date via _player_age. Lets callers that only have a plain
      dict with a precomputed "age" field (e.g. cpu_should_accept's asset
      dicts, which never carry birth_date) reuse this helper without
      constructing a fake Player object.
    """
    age = age_override if age_override is not None else _player_age(player)
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


def _team_archetype_counts(
    roster_players: list,
) -> dict[str, int]:
    """B6: Count how many players on a team's roster fall into each archetype.

    roster_players: list of player_repo.Player objects (or dicts with tendency fields).
    Returns dict[archetype_str -> count].
    """
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
        arch = trade_grading._player_archetype(p_dict)
        if arch:
            counts[arch] = counts.get(arch, 0) + 1
    return counts
