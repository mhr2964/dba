"""Ride-along interactive mode for DBA CPU decisions.

When DBA_RIDE_ALONG=1 is set, CPU trade decisions pause execution and wait
for user input before proceeding. Role-assignment and philosophy events log
silently to the JSONL session log by default — they only pause when
RIDE_ALONG_ROLE_PAUSE=1 is explicitly set.

Sub-flags:
  RIDE_ALONG_ROLE_PAUSE=1   — opt in to role-assignment + philosophy pauses
                              (default: off; events still log silently)
  RIDE_ALONG_COMPACT=1      — compact franchise-plan panel format

This module is a pure side-channel — it never alters trade or role engine
logic directly. Callers check is_ride_along_enabled() / is_role_pause_enabled()
and branch; if the env var is absent, none of this code runs in production.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from datetime import timezone
from pathlib import Path
from typing import Any

# Absolute path so the log file lands next to the test harness regardless of
# where Python is invoked from.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOG_DIR = _PROJECT_ROOT / "headless_logs"

# Resolved once at import time so all prompt_decision calls in a single run
# share the same log file (timestamp is per-session, not per-decision).
_SESSION_TS = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
_LOG_FILE: Path | None = None  # initialised lazily on first write


def is_ride_along_enabled() -> bool:
    """True when DBA_RIDE_ALONG=1 is set in the environment."""
    return os.environ.get("DBA_RIDE_ALONG") == "1"


def is_role_pause_enabled() -> bool:
    """True when ride-along is on AND role pauses are explicitly opted in.

    Default is OFF — role-assignment and philosophy events log silently
    to JSONL but do not interrupt the user. Set RIDE_ALONG_ROLE_PAUSE=1
    to surface them as interactive prompts.

    Why off-by-default: trade decisions are the surface the user cares
    about reviewing live; role/philosophy data is still considered and
    shown WITHIN each trade prompt's details block.
    """
    if not is_ride_along_enabled():
        return False
    return os.environ.get("RIDE_ALONG_ROLE_PAUSE") == "1"


def _get_log_file() -> Path:
    """Return the JSONL log path for this session, creating the directory if needed."""
    global _LOG_FILE
    if _LOG_FILE is None:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        _LOG_FILE = _LOG_DIR / f"ride_along_{_SESSION_TS}.jsonl"
    return _LOG_FILE


def _write_log(record: dict) -> None:
    """Append one JSON line to the session log file. Never raises."""
    try:
        log_path = _get_log_file()
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001
        # Log failures must never break the sim.
        print(f"[ride_along] WARNING: failed to write log: {exc}", file=sys.stderr)


_COMPACT_MODE: bool = os.environ.get("RIDE_ALONG_COMPACT") == "1"


def _format_franchise_plan_panel(team_code: str, plan: dict) -> list[str]:
    """
    Render one team's franchise plan as indented lines.

    In compact mode (RIDE_ALONG_COMPACT=1) this collapses to a single line.
    """
    if _COMPACT_MODE:
        pivot_tag = ""
        ph = plan.get("pivot_history")
        if ph:
            pivot_tag = f" [recent pivot: {ph}]"
        elif (plan.get("status") or "").startswith("initial"):
            pivot_tag = " [initial]"
        return [
            f"   plan {team_code}: {plan.get('goal', '?')} h:{plan.get('horizon_seasons', '?')}"
            f" (pursuing: {plan.get('pursuing', 'none')}){pivot_tag}"
        ]

    out: list[str] = [
        f"   franchise plan {team_code} — {plan.get('goal', '?')} (h:{plan.get('horizon_seasons', '?')})"
    ]
    if plan.get("core_names"):
        out.append(f"     core (untouchable): {plan['core_names']}")
    if plan.get("flex_names"):
        out.append(f"     flex (situational): {plan['flex_names']}")
    if plan.get("surplus_names"):
        out.append(f"     surplus (shopping): {plan['surplus_names']}")
    pursuing = plan.get("pursuing")
    if pursuing and pursuing != "none":
        out.append(f"     pursuing: {pursuing}")
    status = plan.get("status")
    if status:
        out.append(f"     status: {status}")
    return out


def _format_details(details: dict) -> str:
    """Return a human-readable block for the details dict.

    Handles three special cases:
    - Perspective blocks: dicts with ``_header`` + ``_bullets`` keys — rendered
      as a section header, indented meta line, and bullet list.
    - ``franchise_plans``: a dict keyed by team code — rendered as structured panels.
    - ``roles_moving``: team-keyed role lines — rendered as an indented block.

    Everything else falls back to generic key-value rendering.
    """
    lines: list[str] = []
    for key, value in details.items():
        if isinstance(value, dict) and "_header" in value and "_bullets" in value:
            # Perspective block — narrative format.
            lines.append(f"   {key}  ({value['_header']})")
            for bullet in value.get("_bullets") or []:
                lines.append(f"     {bullet}")
        elif key == "franchise_plans" and isinstance(value, dict):
            # Each value is a plan block dict — render side-by-side as panels.
            for team_code, plan_block in value.items():
                if isinstance(plan_block, dict):
                    lines.extend(_format_franchise_plan_panel(team_code, plan_block))
                else:
                    lines.append(f"   franchise plan {team_code}: {plan_block}")
        elif key == "roles_moving" and isinstance(value, dict):
            if not value:
                continue
            lines.append("   roles moving:")
            for team_code, role_lines in value.items():
                lines.append(f"     {team_code}:")
                for rl in role_lines:
                    lines.append(f"       {rl}")
        elif isinstance(value, dict):
            lines.append(f"   {key}:")
            for k2, v2 in value.items():
                lines.append(f"     {k2}: {v2}")
        elif isinstance(value, list):
            lines.append(f"   {key}: {', '.join(str(v) for v in value)}")
        else:
            lines.append(f"   {key}: {value}")
    return "\n".join(lines)


def prompt_decision(
    decision_type: str,
    header: str,
    details: dict[str, Any],
    default_action: str = "approve",
) -> dict:
    """
    Block execution and ask the user to approve, veto, comment, or skip a CPU
    trade decision.

    Parameters
    ----------
    decision_type : str
        One of: "propose", "accept_reject", "commissioner_approve", "three_team"
    header : str
        One-line description of what the CPU is about to do.
        Example: "CPU [ORL] wants to PROPOSE → [NOP]"
    details : dict
        Structured context for the decision (players, OVRs, scores, etc.).
        Printed verbatim and persisted to the JSONL log.
    default_action : str
        Action taken when the user presses Enter with no input. Typically
        "approve" (CPU's computed choice stands).

    Returns
    -------
    dict with keys:
        action        - "approve" | "veto" | "comment" | "skip"
        comment       - str or None
        default_action - the default that was offered
        timestamp     - ISO-8601 string
    """
    sep = "-" * 57

    print(f"\n{sep}")
    print(header)
    detail_text = _format_details(details)
    if detail_text:
        print(detail_text)
    print(f"   CPU's choice: {default_action.upper()}")
    print()

    prompt_str = (
        f"Your call: [a]pprove / [v]eto / [c]omment <text> / [s]kip "
        f"(default={default_action}) > "
    )

    action = default_action
    comment: str | None = None

    try:
        raw = input(prompt_str).strip()
    except (EOFError, KeyboardInterrupt):
        # Non-interactive context or user hit Ctrl-C — fall back to default.
        raw = ""
        print()

    if not raw:
        action = default_action
    elif raw.lower().startswith("v"):
        action = "veto"
    elif raw.lower().startswith("s"):
        action = "skip"
    elif raw.lower().startswith("c"):
        # "c Some comment text" — everything after the first token is the comment
        parts = raw.split(None, 1)
        action = "comment"
        comment = parts[1] if len(parts) > 1 else None
    else:
        # "a" or anything unrecognised — treat as approve
        action = "approve"

    timestamp = datetime.datetime.now().isoformat()

    result = {
        "action": action,
        "comment": comment,
        "default_action": default_action,
        "timestamp": timestamp,
    }

    # Print acknowledgment so the user knows what was recorded.
    label = action.upper()
    if comment:
        label += f' -- "{comment}"'
    print(f"   >> {label}")

    # Persist to JSONL log.
    _write_log({
        "timestamp": timestamp,
        "decision_type": decision_type,
        "header": header,
        "details": details,
        "user_action": action,
        "user_comment": comment,
        "default_action": default_action,
    })

    return result


# ---------------------------------------------------------------------------
# Role-assignment event helpers
# ---------------------------------------------------------------------------

_SEP_WIDE = "=" * 60
_SEP_NARROW = "-" * 60


def _format_role_table(assignments: list[dict]) -> str:
    """Render a list of role-assignment dicts as a formatted table string.

    Each dict must have: name, position, overall, role, touch_share, rationale.
    Philosophy bonus hints (e.g. '[star_maxer +25]') are extracted from rationale
    when present and appended as the final column.
    """
    lines: list[str] = []
    header = f"  {'name':<20}{'pos':<5}{'ovr':<6}{'role':<22}{'share':<8}rationale"
    lines.append(header)
    lines.append("  " + "-" * 80)
    for a in sorted(assignments, key=lambda x: x.get("touch_share", 0), reverse=True):
        name = (a.get("name") or f"player#{a.get('player_id', '?')}")[:19]
        pos = (a.get("position") or "?")[:4]
        ovr = str(a.get("overall") or "?")[:4]
        role = (a.get("role") or "?")[:21]
        share = f"{a.get('touch_share', 0) * 100:.0f}%"
        rationale = a.get("rationale") or ""
        # Truncate long rationales to keep lines readable
        if len(rationale) > 55:
            rationale = rationale[:52] + "…"
        lines.append(f"  {name:<20}{pos:<5}{ovr:<6}{role:<22}{share:<8}{rationale}")
    return "\n".join(lines)


def emit_role_assignment(
    league_id: int,
    team_id: int,
    team_code: str,
    philosophy: str,
    assignments: list[dict],
) -> str:
    """Surface a full role-assignment table to the user and wait for input.

    ``assignments`` is the list returned by ``derive_roles`` enriched with
    player display fields (name, position, overall).  The caller is
    responsible for fetching those and merging them before calling here.

    Returns:
        "accept"  — user pressed Enter or typed 'a'
        "veto"    — user typed 'v' (caller should re-derive and call again)
        "comment" — user typed 'c <text>' (logged; returns "comment", continues)

    When role pauses are suppressed (RIDE_ALONG_ROLE_PAUSE=0) or ride-along
    is off, this is a no-op that returns "accept" immediately.
    """
    if not is_role_pause_enabled():
        # Still log silently so the session summary has accurate counts.
        _write_log({
            "ts": datetime.datetime.now(timezone.utc).isoformat(),
            "event_type": "role_assignment",
            "league_id": league_id,
            "team_id": team_id,
            "team_code": team_code,
            "philosophy": philosophy,
            "assignments": [
                {k: v for k, v in a.items() if k != "birth_date"}
                for a in assignments
            ],
            "user_action": "accept",
            "user_comment": None,
        })
        return "accept"

    print(f"\n{_SEP_WIDE}")
    print(f"=== ROLE ASSIGNMENT === Team: {team_code}  (coach: {philosophy})")
    print(_SEP_WIDE)
    print(_format_role_table(assignments))
    print()
    prompt_str = (
        "Press Enter to accept | 'v' to veto+re-derive | 'c <comment>' to log feedback > "
    )

    action = "accept"
    comment: str | None = None

    try:
        raw = input(prompt_str).strip()
    except (EOFError, KeyboardInterrupt):
        raw = ""
        print()

    if not raw:
        action = "accept"
    elif raw.lower().startswith("v"):
        action = "veto"
    elif raw.lower().startswith("c"):
        parts = raw.split(None, 1)
        action = "comment"
        comment = parts[1] if len(parts) > 1 else None
    else:
        action = "accept"

    label = action.upper()
    if comment:
        label += f' -- "{comment}"'
    print(f"   >> {label}")

    _write_log({
        "ts": datetime.datetime.now(timezone.utc).isoformat(),
        "event_type": "role_assignment",
        "league_id": league_id,
        "team_id": team_id,
        "team_code": team_code,
        "philosophy": philosophy,
        "assignments": [
            {k: v for k, v in a.items() if k != "birth_date"}
            for a in assignments
        ],
        "user_action": action,
        "user_comment": comment,
    })

    return action


def emit_role_change(
    league_id: int,
    team_id: int,
    team_code: str,
    philosophy: str,
    deltas: list[dict],
    reason: str = "",
    *,
    pause: bool = True,
) -> None:
    """Surface role-change deltas (post-trade re-derive, etc.) to the user.

    ``deltas`` is a list of dicts:
        {player_id, name, old_role, old_touch_share, new_role, new_touch_share}

    No veto option — role changes from roster moves are structural; the user
    can override individual roles via /coach role assign (Phase 5).

    Parameters
    ----------
    pause : bool
        When False, the change table is still written to JSONL but the
        input() pause is skipped.  Pass False from silent_emit call sites
        (trade-driven re-derives) so the user isn't interrupted mid-trade.

    Silent no-op (log only) when ride-along or role pauses are off.
    """
    if not is_role_pause_enabled() or not deltas:
        if deltas:
            _write_log({
                "ts": datetime.datetime.now(timezone.utc).isoformat(),
                "event_type": "role_change",
                "league_id": league_id,
                "team_id": team_id,
                "team_code": team_code,
                "philosophy": philosophy,
                "deltas": deltas,
                "reason": reason,
                "user_action": "noted",
            })
        return

    print(f"\n{_SEP_WIDE}")
    print(f"=== ROLE CHANGE === Team: {team_code}  (coach: {philosophy})")
    if reason:
        print(f"  Reason: {reason}")
    print(_SEP_WIDE)
    for d in deltas:
        name = (d.get("name") or f"player#{d.get('player_id', '?')}")[:22]
        old_r = d.get("old_role", "?")
        old_ts = d.get("old_touch_share", 0)
        new_r = d.get("new_role", "?")
        new_ts = d.get("new_touch_share", 0)
        print(
            f"  {name:<23}  {old_r} ({old_ts * 100:.0f}%)  ->  {new_r} ({new_ts * 100:.0f}%)"
        )
    print()

    if pause:
        try:
            input("Press Enter to continue > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()

    _write_log({
        "ts": datetime.datetime.now(timezone.utc).isoformat(),
        "event_type": "role_change",
        "league_id": league_id,
        "team_id": team_id,
        "team_code": team_code,
        "philosophy": philosophy,
        "deltas": deltas,
        "reason": reason,
        "user_action": "noted",
    })


def emit_philosophy_assigned(
    league_id: int,
    team_id: int,
    team_code: str,
    philosophy: str,
) -> str:
    """Surface a philosophy assignment to the user and wait for input.

    Returns:
        "accept"  — user pressed Enter or typed 'a'
        "reroll"  — user typed 'r' (caller should re-roll and call again)
        "comment" — user typed 'c <text>' (logged; caller continues)

    Silent no-op (returns "accept") when role pauses are suppressed or
    ride-along is off.
    """
    _PHILOSOPHY_BIAS_DESC: dict[str, str] = {
        "star_maxer":         "top-2 OVR stars preferred for iso_scorer / primary_initiator",
        "egalitarian":        "ball-dominant roles penalised; secondary/distributor roles boosted",
        "defense_first":      "defensive tendencies push players toward defensive archetypes",
        "tendency_respecter": "pure tendency-based scoring, no philosophy bias",
        "vet_overrater":      "aging vets inflated into scoring roles; young -> developmental",
        "youth_developer":    "young players get bigger roles than OVR warrants; vets pushed down",
        "chaos":              "deterministic per-(player, role, season) chaos, reproducible but illogical",
    }

    if not is_role_pause_enabled():
        _write_log({
            "ts": datetime.datetime.now(timezone.utc).isoformat(),
            "event_type": "philosophy_assigned",
            "league_id": league_id,
            "team_id": team_id,
            "team_code": team_code,
            "philosophy": philosophy,
            "user_action": "accept",
            "user_comment": None,
        })
        return "accept"

    bias_desc = _PHILOSOPHY_BIAS_DESC.get(philosophy, "unknown philosophy")

    print(f"\n{_SEP_WIDE}")
    print(f"=== PHILOSOPHY ASSIGNED === Team: {team_code}  ->  {philosophy}")
    print(f"  Bias: {bias_desc}")
    print(_SEP_WIDE)
    prompt_str = (
        "Press Enter to accept | 'r' to re-roll | 'c <comment>' to log feedback > "
    )

    action = "accept"
    comment: str | None = None

    try:
        raw = input(prompt_str).strip()
    except (EOFError, KeyboardInterrupt):
        raw = ""
        print()

    if not raw:
        action = "accept"
    elif raw.lower().startswith("r"):
        action = "reroll"
    elif raw.lower().startswith("c"):
        parts = raw.split(None, 1)
        action = "comment"
        comment = parts[1] if len(parts) > 1 else None
    else:
        action = "accept"

    label = action.upper()
    if comment:
        label += f' -- "{comment}"'
    print(f"   >> {label}")

    _write_log({
        "ts": datetime.datetime.now(timezone.utc).isoformat(),
        "event_type": "philosophy_assigned",
        "league_id": league_id,
        "team_id": team_id,
        "team_code": team_code,
        "philosophy": philosophy,
        "user_action": action,
        "user_comment": comment,
    })

    return action


def summarize_session() -> None:
    """Read this session's JSONL log and print a structured post-run summary.

    Called from headless_test.py or the demo script at the end of a
    --ride-along run.  Surfaces:
    - total prompts seen, grouped by event_type / decision_type
    - role assignment and philosophy tallies (new in Phase 4)
    - veto patterns (which teams / philosophies were vetoed most)
    - every comment, in order, with context attached
    - franchise plan analytics (carried from prior phases)
    """
    if _LOG_FILE is None or not _LOG_FILE.exists():
        # User skipped every decision or no decisions fired — nothing to summarize.
        return

    records: list[dict] = []
    try:
        with _LOG_FILE.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as exc:  # noqa: BLE001
        print(f"\n[ride_along] could not read log for summary: {exc}", file=sys.stderr)
        return

    if not records:
        return

    sep = "=" * 60
    print(f"\n{sep}")
    print("RIDE-ALONG SESSION SUMMARY")
    print(sep)
    print(f"  Log file: {_LOG_FILE}")

    # ── Partition records by event type ──────────────────────────────────────
    # Legacy trade records use "decision_type"; new role events use "event_type".
    trade_records = [r for r in records if r.get("decision_type")]
    role_assignment_records = [r for r in records if r.get("event_type") == "role_assignment"]
    role_change_records = [r for r in records if r.get("event_type") == "role_change"]
    philosophy_records = [r for r in records if r.get("event_type") == "philosophy_assigned"]

    print(f"  Events logged: {len(records)}")
    print()

    # ── Trades ───────────────────────────────────────────────────────────────
    if trade_records:
        trade_approved = sum(1 for r in trade_records if r.get("user_action") in ("approve", "accept"))
        trade_vetoed   = sum(1 for r in trade_records if r.get("user_action") == "veto")
        trade_commented = sum(1 for r in trade_records if r.get("user_action") == "comment")
        print(
            f"  Trades evaluated:  {len(trade_records)}"
            f"  ({trade_approved} approved, {trade_vetoed} vetoed, {trade_commented} commented)"
        )

        by_type: dict[str, dict[str, int]] = {}
        for r in trade_records:
            dt = r.get("decision_type", "unknown")
            act = r.get("user_action", "unknown")
            by_type.setdefault(dt, {}).setdefault(act, 0)
            by_type[dt][act] += 1
        print("    By trade decision type:")
        for dt, actions in sorted(by_type.items()):
            total = sum(actions.values())
            breakdown = ", ".join(f"{k}={v}" for k, v in sorted(actions.items()))
            print(f"      {dt:<22} total={total:<4}  ({breakdown})")

        trade_vetoes = [r for r in trade_records if r.get("user_action") == "veto"]
        if trade_vetoes:
            import re
            offerer_count: dict[str, int] = {}
            receiver_count: dict[str, int] = {}
            for v in trade_vetoes:
                header = v.get("header", "")
                codes = re.findall(r"\[([A-Z]{2,4})\]", header)
                if len(codes) >= 1:
                    offerer_count[codes[0]] = offerer_count.get(codes[0], 0) + 1
                if len(codes) >= 2:
                    receiver_count[codes[1]] = receiver_count.get(codes[1], 0) + 1
            if offerer_count:
                top_off = sorted(offerer_count.items(), key=lambda x: -x[1])[:5]
                print(f"    most-vetoed initiator: {', '.join(f'{c}({n})' for c, n in top_off)}")
            if receiver_count:
                top_rec = sorted(receiver_count.items(), key=lambda x: -x[1])[:5]
                print(f"    most-vetoed counterparty: {', '.join(f'{c}({n})' for c, n in top_rec)}")

    # ── Role assignments ─────────────────────────────────────────────────────
    if role_assignment_records:
        ra_accepted = sum(1 for r in role_assignment_records if r.get("user_action") == "accept")
        ra_vetoed   = sum(1 for r in role_assignment_records if r.get("user_action") == "veto")
        ra_commented = sum(1 for r in role_assignment_records if r.get("user_action") == "comment")
        print()
        print(
            f"  Role assignments shown: {len(role_assignment_records)}"
            f"  ({ra_accepted} accepted, {ra_vetoed} vetoed, {ra_commented} commented)"
        )

        # Most-vetoed philosophy
        if ra_vetoed:
            phil_vetoes: dict[str, int] = {}
            phil_total: dict[str, int] = {}
            for r in role_assignment_records:
                ph = r.get("philosophy") or "unknown"
                phil_total[ph] = phil_total.get(ph, 0) + 1
                if r.get("user_action") == "veto":
                    phil_vetoes[ph] = phil_vetoes.get(ph, 0) + 1
            if phil_vetoes:
                most_vetoed_ph = max(phil_vetoes, key=lambda k: phil_vetoes[k])
                mv_count = phil_vetoes[most_vetoed_ph]
                mv_total = phil_total.get(most_vetoed_ph, 0)
                print(
                    f"    Most-vetoed philosophy: {most_vetoed_ph}"
                    f"  ({mv_count} vetoes / {mv_total} assignments)"
                )

        if role_change_records:
            print(f"  Role changes noted:   {len(role_change_records)}")

    # ── Philosophy assignments ───────────────────────────────────────────────
    if philosophy_records:
        ph_accepted = sum(1 for r in philosophy_records if r.get("user_action") == "accept")
        ph_rerolled = sum(1 for r in philosophy_records if r.get("user_action") == "reroll")
        ph_commented = sum(1 for r in philosophy_records if r.get("user_action") == "comment")
        print()
        print(
            f"  Philosophy assignments: {len(philosophy_records)}"
            f"  ({ph_accepted} accepted, {ph_rerolled} re-rolled, {ph_commented} commented)"
        )

    # ── Comments (all event types) ───────────────────────────────────────────
    all_commented = [r for r in records if r.get("user_comment")]
    if all_commented:
        print(f"\n  Comments ({len(all_commented)}):")
        for c in all_commented:
            # Support both "ts" (new events) and "timestamp" (legacy trade events).
            ts_raw = c.get("ts") or c.get("timestamp") or ""
            ts = ts_raw[:19].replace("T", " ")
            evt = c.get("event_type") or c.get("decision_type") or "unknown"
            team = c.get("team_code") or ""
            header = c.get("header", "")
            comment = c.get("user_comment", "")
            action = c.get("user_action", "")
            context = header or f"{evt} {team}".strip()
            print(f"    [{ts}] {action} on: {context}")
            print(f"      > {comment}")

    # ── Franchise plan analytics (legacy — carried from Phase 2/3) ───────────
    goal_teams: dict[str, set[str]] = {}
    pivots_seen: list[tuple[int | None, str, str]] = []

    for r in trade_records:
        fp_map: dict = (r.get("details") or {}).get("franchise_plans") or {}
        for team_code, plan_block in fp_map.items():
            if not isinstance(plan_block, dict):
                continue
            goal = plan_block.get("goal") or "unknown"
            goal_teams.setdefault(goal, set()).add(team_code)
            ph = plan_block.get("pivot_history")
            if ph:
                pivot_game = plan_block.get("last_reassessed_game")
                pivots_seen.append((pivot_game, team_code, ph))

    if goal_teams:
        print("\n  === FRANCHISE PLANS SEEN THIS SESSION ===")
        _ALL_GOALS = ["win_now", "transition", "soft_rebuild", "rebuild", "tank"]
        for g in _ALL_GOALS:
            teams = sorted(goal_teams.get(g, set()))
            count = len(teams)
            if count == 0:
                label = "(none)"
            else:
                label = f"{', '.join(teams)} ({count})"
            if g == "tank" and count == 0:
                label = "(none — no team had ≥2 R1 picks)"
            print(f"    {g:<16}: {label}")

        if pivots_seen:
            seen_pivot_keys: set[tuple[str, str]] = set()
            unique_pivots: list[tuple[int | None, str, str]] = []
            for pg, tc, desc in pivots_seen:
                key = (tc, desc)
                if key not in seen_pivot_keys:
                    seen_pivot_keys.add(key)
                    unique_pivots.append((pg, tc, desc))
            unique_pivots.sort(key=lambda x: (x[0] or 0))
            print("\n  Pivots observed (in trades you saw):")
            for pg, tc, desc in unique_pivots:
                game_label = f"game {pg}" if pg is not None else "game ?"
                print(f"    [{game_label}] {tc}: {desc}")
        else:
            print("\n  No plan pivots observed this session.")

    print(sep)
    print(f"  Replay later: cat {_LOG_FILE} | jq .  (or open in any text editor)")
    print(sep)
