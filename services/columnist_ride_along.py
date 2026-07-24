"""Columnist ride-along v2 — attach-only, file-IPC sidecar model.

The bot runs normally.  Feedback intake lives in a separate sidecar CLI
(scripts/columnist_feedback.ps1 or .sh) that communicates via a small
directory of JSON files the bot polls every 300 ms.

IPC directory: headless_logs/columnist_ride_along_ipc/

Five files, all written atomically via tmp+replace:
  state.json    — long-lived, bot writes; sidecar reads
  start.cmd     — transient; sidecar writes, bot deletes
  pause.json    — transient; bot writes, sidecar deletes
  feedback.json — transient; sidecar writes, bot deletes
  stop.cmd      — transient; sidecar writes, bot deletes
  sidecar_heartbeat.txt — sidecar touches every poll cycle; bot reads mtime

Feedback is log-only — never injected back into a live prompt.
Only the chosen persona's articles pause the sim; all others post normally.

Regular-season only (same scope as v1).
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import uuid
from datetime import timezone
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOG_DIR = _PROJECT_ROOT / "headless_logs"
_IPC_DIR = _LOG_DIR / "columnist_ride_along_ipc"

# Captured once at import time so all JSONL files for one bot lifetime share the
# same timestamp suffix (even when the same persona attaches twice).
_SESSION_TS = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

# Long-lived bot identity — written into every state.json.
_BOT_PID: int = os.getpid()
_BOT_STARTED_AT: str = datetime.datetime.now(timezone.utc).isoformat()

# Module-level state — mutated only by ipc_watch_task() and request_pause().
_CHOSEN_PERSONA_ID: str | None = None
_SIDECAR_PID: int | None = None

# Per-pause state — set by request_pause(), cleared by _apply_feedback() /
# _apply_stop_cmd() / the heartbeat-timeout path.
_pending_event: asyncio.Event | None = None
_pending_article_id: str | None = None

# Session counters
_fires: int = 0
_pauses_seen: int = 0

# JSONL log — initialised lazily on first write for the active persona.
_LOG_FILE: Path | None = None

# Background task handle — stored so close() can cancel it.
_ipc_task: asyncio.Task | None = None

# state.json heartbeat cadence — written periodically to keep last_update_ts
# fresh without hammering the filesystem.  Transition writes are immediate
# (inside _write_state, which every handler calls directly) regardless of
# this interval.  Sidecar "hung bot" threshold is 60 s — must be >= 2× value.
_STATE_WRITE_INTERVAL_SEC: float = 5.0
_last_state_write_ts: float = 0.0  # monotonic seconds; 0 forces write on first cadence tick

# Cold sweep sentinel — True once _cold_sweep_ipc_dir() returns without raising.
# Retry-until-done semantics: a transient permission error won't lock out the
# feature for the whole bot lifetime.
_cold_sweep_done: bool = False


# ---------------------------------------------------------------------------
# Public gate queries  (same interface as v1 — callers in sim_orchestrator.py
# use these two functions; only the implementations change)
# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    """True when a sidecar has attached and chosen a persona."""
    return _CHOSEN_PERSONA_ID is not None


def target_persona_id() -> str | None:
    """Return the chosen persona id, or None when ride-along is off."""
    return _CHOSEN_PERSONA_ID


def should_fire_for(persona_id: str) -> bool:
    """Gate for columnist_service.generate — True when this persona should fire.

    When ride-along is detached, every persona fires (returns True for all).
    When ride-along is attached, ONLY the chosen persona fires; all others are
    suppressed at the generate() entrypoint so no LLM API call is made and no
    Discord post lands. This avoids API cost and channel noise during iteration
    sessions where the user is only critiquing one persona's voice.

    Originally v2 design said "other personas fire normally" — that was reversed
    after the first feedback runs proved the noise/cost wasn't worth the context.
    """
    if _CHOSEN_PERSONA_ID is None:
        return True
    return persona_id == _CHOSEN_PERSONA_ID


# ---------------------------------------------------------------------------
# JSONL log writer — schema unchanged from v1
# ---------------------------------------------------------------------------

def _get_log_file() -> Path:
    """Return (and lazily create) the JSONL path for the current persona."""
    global _LOG_FILE
    if _LOG_FILE is None:
        persona_id = _CHOSEN_PERSONA_ID or "unknown"
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        _LOG_FILE = _LOG_DIR / f"columnist_ride_along_{persona_id}_{_SESSION_TS}.jsonl"
    return _LOG_FILE


def _write_log(record: dict) -> None:
    """Append one JSON line to the session log. Never raises."""
    import sys
    try:
        log_path = _get_log_file()
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        try:
            size = log_path.stat().st_size
            if size > 10 * 1024 * 1024:
                print(
                    f"[columnist_ride_along] WARNING: log file exceeds 10 MB "
                    f"({size // (1024 * 1024)} MB): {log_path}",
                    file=sys.stderr,
                )
        except OSError:
            pass
    except Exception as exc:  # noqa: BLE001
        import sys as _sys
        print(f"[columnist_ride_along] WARNING: failed to write log: {exc}", file=_sys.stderr)


# ---------------------------------------------------------------------------
# Article ID generator  (unchanged from v1)
# ---------------------------------------------------------------------------

def _make_article_id() -> str:
    ts = datetime.datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return f"ra_{ts}_{suffix}"


# ---------------------------------------------------------------------------
# Atomic file I/O helpers
# ---------------------------------------------------------------------------

def _atomic_write_json(path: Path, obj: dict) -> None:
    """Write *obj* to *path* atomically via a sibling .tmp file."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _read_json_or_none(path: Path) -> dict | None:
    """Read and parse a JSON file; return None on any error (treat as in-flight).

    Uses utf-8-sig so PowerShell 5.1's Set-Content -Encoding UTF8 (which prepends
    a BOM in defiance of RFC 8259) parses cleanly. utf-8-sig also handles plain
    UTF-8 without BOM, so the sh sidecar's output works identically.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _delete_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        import sys
        print(f"[columnist_ride_along] WARNING: could not delete {path}: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# State writer
# ---------------------------------------------------------------------------

def _write_state(status: str, **extra_fields: Any) -> None:
    """Write state.json atomically.  Always includes bot identity fields.

    Also resets _last_state_write_ts so the next periodic heartbeat is
    deferred by a full interval — no double-write right after a transition.
    """
    global _pauses_seen, _last_state_write_ts
    import time
    payload: dict[str, Any] = {
        "status": status,
        "persona_id": _CHOSEN_PERSONA_ID,
        "persona_display_name": None,
        "bot_pid": _BOT_PID,
        "bot_started_at": _BOT_STARTED_AT,
        "last_update_ts": datetime.datetime.now(timezone.utc).isoformat(),
        "log_path": str(_get_log_file()) if _CHOSEN_PERSONA_ID else None,
        "pauses_seen": _pauses_seen,
        "sidecar_pid": _SIDECAR_PID,
    }
    # Resolve display name from persona registry when possible.
    if _CHOSEN_PERSONA_ID:
        try:
            from services.personas import PERSONAS as _p
            persona = _p.get(_CHOSEN_PERSONA_ID)
            if persona:
                payload["persona_display_name"] = persona.display_name
        except Exception:  # noqa: BLE001
            pass
    payload.update(extra_fields)
    try:
        _CHOICES_DIR = _IPC_DIR
        _CHOICES_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(_CHOICES_DIR / "state.json", payload)
        _last_state_write_ts = time.monotonic()
    except Exception as exc:  # noqa: BLE001
        import sys
        print(f"[columnist_ride_along] WARNING: failed to write state.json: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# IPC dir cold sweep
# ---------------------------------------------------------------------------

def _cold_sweep_ipc_dir() -> None:
    """Create the IPC dir and delete all transient files from prior bot sessions.

    Raises on IPC dir creation failure so ipc_watch_task() can retry without
    marking _cold_sweep_done=True.  A transient permission error therefore does
    not permanently disable ride-along for the bot lifetime.
    """
    _IPC_DIR.mkdir(parents=True, exist_ok=True)  # raises OSError on failure

    for name in ("start.cmd", "pause.json", "feedback.json", "stop.cmd", "sidecar_heartbeat.txt"):
        _delete_if_exists(_IPC_DIR / name)

    _write_state("detached")


# ---------------------------------------------------------------------------
# IPC command handlers
# ---------------------------------------------------------------------------

def _apply_start_cmd(payload: dict) -> None:
    """Bot side: process a valid start.cmd payload."""
    global _CHOSEN_PERSONA_ID, _SIDECAR_PID, _LOG_FILE

    import sys
    from services.personas import PERSONAS as _p

    persona_id = str(payload.get("persona_id", "")).strip()
    sidecar_pid = payload.get("sidecar_pid")

    # Reject if already attached to a different persona.
    if _CHOSEN_PERSONA_ID is not None:
        _write_state(
            "rejected",
            reason=f"already attached to {_CHOSEN_PERSONA_ID}",
        )
        print(
            f"[columnist_ride_along] start.cmd rejected — already attached to {_CHOSEN_PERSONA_ID}",
            file=sys.stderr,
        )
        return

    # Validate persona id.
    if persona_id not in _p:
        _write_state("rejected", reason=f"unknown persona: {persona_id}")
        print(
            f"[columnist_ride_along] start.cmd rejected — unknown persona: {persona_id}. "
            f"Valid: {sorted(_p.keys())}",
            file=sys.stderr,
        )
        return

    _CHOSEN_PERSONA_ID = persona_id
    _SIDECAR_PID = int(sidecar_pid) if sidecar_pid is not None else None
    _LOG_FILE = None  # reset so _get_log_file() picks up the new persona name

    attached_at = datetime.datetime.now(timezone.utc).isoformat()
    _write_state("attached", attached_at=attached_at)
    print(
        f"[columnist_ride_along] attached — persona={persona_id} "
        f"sidecar_pid={_SIDECAR_PID} log={_get_log_file()}",
        flush=True,
    )


def _is_valid_feedback_payload(payload: dict) -> bool:
    """Minimal structural check — must have article_id and action keys."""
    return isinstance(payload, dict) and "article_id" in payload and "action" in payload


def _apply_feedback(payload: dict) -> None:
    """Bot side: process a feedback.json payload while a pause is in flight."""
    global _pending_event, _pending_article_id, _fires

    import sys

    article_id = payload.get("article_id")

    # Stale-feedback guard: reject if article_id doesn't match current pause.
    if article_id != _pending_article_id:
        _write_log({
            "kind": "feedback",
            "ts": datetime.datetime.now(timezone.utc).isoformat(),
            "article_id": article_id,
            "user_feedback": None,
            "tag": None,
            "severity": None,
            "action": "stale_feedback",
            "reason": f"expected {_pending_article_id}, got {article_id}",
        })
        print(
            f"[columnist_ride_along] stale feedback discarded "
            f"(expected={_pending_article_id} got={article_id})",
            file=sys.stderr,
        )
        return

    _write_log({
        "kind": "feedback",
        "ts": datetime.datetime.now(timezone.utc).isoformat(),
        "article_id": article_id,
        "user_feedback": payload.get("user_feedback"),
        "tag": payload.get("tag"),
        "severity": payload.get("severity"),
        "action": payload.get("action", "feedback"),
    })

    # Release the gate.
    evt = _pending_event
    _pending_event = None
    _pending_article_id = None
    if evt is not None:
        evt.set()

    _write_state("attached")


def _apply_stop_cmd(payload: dict) -> None:
    """Bot side: process a stop.cmd — detach sidecar, clear chosen-persona state."""
    global _CHOSEN_PERSONA_ID, _SIDECAR_PID, _fires, _pauses_seen, _LOG_FILE
    global _pending_event, _pending_article_id

    import sys

    reason = payload.get("reason", "user_quit")

    # Release any in-flight pause.
    if _pending_event is not None:
        _write_log({
            "kind": "feedback",
            "ts": datetime.datetime.now(timezone.utc).isoformat(),
            "article_id": _pending_article_id,
            "user_feedback": None,
            "tag": None,
            "severity": None,
            "action": "quit",
            "reason": reason,
        })
        evt = _pending_event
        _pending_event = None
        _pending_article_id = None
        if evt is not None:
            evt.set()

    _write_log({
        "kind": "session_end",
        "ts": datetime.datetime.now(timezone.utc).isoformat(),
        "persona_id": _CHOSEN_PERSONA_ID,
        "total_fires": _fires,
        "reason": reason,
    })

    print(
        f"[columnist_ride_along] stop.cmd received (reason={reason}) — detaching "
        f"persona={_CHOSEN_PERSONA_ID}",
        flush=True,
    )

    _CHOSEN_PERSONA_ID = None
    _SIDECAR_PID = None
    _fires = 0
    _pauses_seen = 0
    _LOG_FILE = None
    _write_state("detached")


def _drain_dead_sidecar(reason: str) -> None:
    """Release a mid-pause gate when the sidecar is no longer responsive."""
    global _CHOSEN_PERSONA_ID, _SIDECAR_PID, _fires, _pauses_seen, _LOG_FILE
    global _pending_event, _pending_article_id

    import sys

    print(f"[columnist_ride_along] draining dead sidecar: {reason}", file=sys.stderr)

    if _pending_event is not None:
        _write_log({
            "kind": "feedback",
            "ts": datetime.datetime.now(timezone.utc).isoformat(),
            "article_id": _pending_article_id,
            "user_feedback": None,
            "tag": None,
            "severity": None,
            "action": "sidecar_died",
            "reason": reason,
        })
        evt = _pending_event
        _pending_event = None
        _pending_article_id = None
        if evt is not None:
            evt.set()

    _write_log({
        "kind": "session_end",
        "ts": datetime.datetime.now(timezone.utc).isoformat(),
        "persona_id": _CHOSEN_PERSONA_ID,
        "total_fires": _fires,
        "reason": "sidecar_died",
    })

    _delete_if_exists(_IPC_DIR / "pause.json")

    _CHOSEN_PERSONA_ID = None
    _SIDECAR_PID = None
    _fires = 0
    _pauses_seen = 0
    _LOG_FILE = None
    _write_state("detached")


# ---------------------------------------------------------------------------
# Sidecar liveness check (heartbeat file; psutil optional)
# ---------------------------------------------------------------------------

_HEARTBEAT_TIMEOUT_S: float = 300.0   # 5 min — PowerShell's Read-Host blocks Touch-Heartbeat
                                      # for as long as the user takes typing. The whole point of
                                      # the pause is to wait for considered feedback. Only fire
                                      # drain when sidecar is genuinely abandoned (~minutes).
_HEARTBEAT_MISS_COUNT: int = 0        # consecutive polls with stale heartbeat while paused

def _sidecar_alive() -> bool:
    """Return True if the sidecar appears to be running.

    Primary check: heartbeat file mtime within the last 10 s.
    Fallback (if heartbeat never appeared): try psutil; if unavailable, trust
    that the sidecar is alive until heartbeat evidence arrives.
    """
    hb = _IPC_DIR / "sidecar_heartbeat.txt"
    if hb.exists():
        try:
            age = datetime.datetime.now().timestamp() - hb.stat().st_mtime
            return age < _HEARTBEAT_TIMEOUT_S
        except OSError:
            pass
    # No heartbeat file yet — check psutil if available.
    if _SIDECAR_PID is None:
        return False
    try:
        import psutil
        return psutil.pid_exists(_SIDECAR_PID)
    except ImportError:
        # psutil not installed; no heartbeat yet; assume alive (benefit of doubt
        # for the first 5 s after attach before we have evidence either way).
        return True


# ---------------------------------------------------------------------------
# IPC watch task — always-on background coroutine
# ---------------------------------------------------------------------------

async def ipc_watch_task() -> None:
    """Poll the IPC directory every 300 ms and dispatch command handlers.

    Runs for the lifetime of the bot process.  Started by bot/client.py::setup_hook().
    Does the cold sweep on first iteration, then no-ops until a start.cmd appears.
    """
    global _HEARTBEAT_MISS_COUNT, _cold_sweep_done

    while True:
        try:
            if not _cold_sweep_done:
                _cold_sweep_ipc_dir()
                _cold_sweep_done = True
            else:
                # Write state.json on cadence only (not every 300 ms poll).
                # Transition writes happen immediately via _write_state() calls
                # in the command handlers; this branch is the periodic heartbeat.
                import time as _time
                if _time.monotonic() - _last_state_write_ts >= _STATE_WRITE_INTERVAL_SEC:
                    _write_state(
                        "paused" if _pending_event is not None else
                        ("attached" if _CHOSEN_PERSONA_ID else "detached")
                    )

            # --- stop.cmd: highest priority — process before other commands ---
            stop_path = _IPC_DIR / "stop.cmd"
            if stop_path.exists():
                payload = _read_json_or_none(stop_path)
                _delete_if_exists(stop_path)
                if payload is not None:
                    _apply_stop_cmd(payload)
                await asyncio.sleep(0.3)
                continue

            # --- start.cmd ---
            start_path = _IPC_DIR / "start.cmd"
            if start_path.exists() and _CHOSEN_PERSONA_ID is None:
                payload = _read_json_or_none(start_path)
                _delete_if_exists(start_path)
                if payload is not None:
                    _apply_start_cmd(payload)

            # --- feedback.json (only relevant when a pause is in flight) ---
            fb_path = _IPC_DIR / "feedback.json"
            if fb_path.exists() and _pending_event is not None:
                raw = _read_json_or_none(fb_path)
                if raw is None:
                    # File may be mid-write by sidecar; retry next iteration.
                    pass
                elif not _is_valid_feedback_payload(raw):
                    import sys as _sys
                    _delete_if_exists(fb_path)
                    _sys.stderr.write(
                        "[columnist_ride_along] invalid feedback payload — discarded\n"
                    )
                else:
                    # Apply first, delete after — avoids losing a concurrent
                    # sidecar write that arrives between read and delete.
                    _apply_feedback(raw)
                    _delete_if_exists(fb_path)
            elif fb_path.exists():
                # feedback.json arrived with no pending pause — stale; discard.
                try:
                    stat = fb_path.stat()
                    age = datetime.datetime.now().timestamp() - stat.st_mtime
                    if age > 60:
                        _delete_if_exists(fb_path)
                except OSError:
                    pass

            # --- Sidecar liveness check while paused ---
            if _pending_event is not None and _CHOSEN_PERSONA_ID is not None:
                if not _sidecar_alive():
                    _HEARTBEAT_MISS_COUNT += 1
                    if _HEARTBEAT_MISS_COUNT >= 3:
                        _drain_dead_sidecar(
                            f"sidecar PID {_SIDECAR_PID} no longer responsive "
                            f"after {_HEARTBEAT_MISS_COUNT * 0.3:.1f}s"
                        )
                        _HEARTBEAT_MISS_COUNT = 0
                else:
                    _HEARTBEAT_MISS_COUNT = 0

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            import sys
            print(f"[columnist_ride_along] ipc_watch_task error: {exc}", file=sys.stderr)

        await asyncio.sleep(0.3)


# ---------------------------------------------------------------------------
# Public API: request_pause
# ---------------------------------------------------------------------------

async def request_pause(article_record: dict[str, Any]) -> None:
    """Called from sim_orchestrator after the chosen persona's embed is sent.

    Writes a pending JSONL record, writes pause.json (IPC signal to sidecar),
    then suspends until the sidecar submits feedback.json or a stop/timeout
    occurs.  Releases automatically on bot shutdown.
    """
    global _pending_event, _pending_article_id, _fires, _pauses_seen

    if _CHOSEN_PERSONA_ID is None:
        # Ride-along was detached between the is_enabled() check and here.
        return

    _fires += 1
    _pauses_seen += 1
    article_id = _make_article_id()
    _pending_article_id = article_id

    # Write the JSONL pending record before awaiting so it survives a crash.
    _write_log({
        "kind": "pending",
        "ts": datetime.datetime.now(timezone.utc).isoformat(),
        "article_id": article_id,
        **article_record,
    })

    # Write pause.json — sidecar picks this up and displays the panel.
    article = article_record.get("article") or {}
    body = article.get("body", "")
    headline = article.get("headline", "")
    pause_payload: dict[str, Any] = {
        "article_id": article_id,
        "persona_id": article_record.get("persona_id", _CHOSEN_PERSONA_ID),
        "persona_display_name": article_record.get("persona_display_name", ""),
        "headline": headline,
        "body_preview": body[:4096],  # 4 KB ceiling; JSONL has full body
        "embed_preview": article_record.get("embed_preview", f"{headline}\n\n{body[:400]}"),
        "pause_index": _pauses_seen,
        "posted_at": datetime.datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_json(_IPC_DIR / "pause.json", pause_payload)
    _write_state("paused")

    # Gate the sim.
    gate = asyncio.Event()
    _pending_event = gate
    await gate.wait()


# ---------------------------------------------------------------------------
# Bot shutdown
# ---------------------------------------------------------------------------

async def shutdown() -> None:
    """Write a closing state.json and JSONL record; release any in-flight pause.

    Called from bot/client.py::close().  Replaces v1's stop().
    """
    global _pending_event, _pending_article_id

    if _pending_event is not None:
        _write_log({
            "kind": "feedback",
            "ts": datetime.datetime.now(timezone.utc).isoformat(),
            "article_id": _pending_article_id,
            "user_feedback": None,
            "tag": None,
            "severity": None,
            "action": "shutdown",
        })
        evt = _pending_event
        _pending_event = None
        _pending_article_id = None
        if evt is not None:
            evt.set()

    if _fires > 0:
        _write_log({
            "kind": "session_end",
            "ts": datetime.datetime.now(timezone.utc).isoformat(),
            "persona_id": _CHOSEN_PERSONA_ID,
            "total_fires": _fires,
            "reason": "bot_shutting_down",
        })

    _write_state("detached", reason="bot_shutting_down")

    if _ipc_task is not None and not _ipc_task.done():
        _ipc_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(_ipc_task), timeout=1.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


# ---------------------------------------------------------------------------
# Post-session summary — unchanged from v1 (called by the sidecar on :quit)
# ---------------------------------------------------------------------------

def summarize_session(log_path: str | None = None) -> None:
    """Read the session JSONL log and print a structured summary.

    When called by the sidecar (via python -c), pass log_path explicitly.
    When called without args, uses the current module-level log file path.
    """
    if log_path:
        log_file = Path(log_path)
    else:
        log_file = _get_log_file()

    if not log_file.exists():
        print("[columnist_ride_along] No log file found — nothing to summarize.")
        return

    records: list[dict] = []
    try:
        with log_file.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as exc:  # noqa: BLE001
        import sys
        print(f"[columnist_ride_along] could not read log for summary: {exc}", file=sys.stderr)
        return

    if not records:
        return

    sep = "=" * 60
    pending = [r for r in records if r.get("kind") == "pending"]
    feedback = [r for r in records if r.get("kind") == "feedback"]
    fb_with_text = [r for r in feedback if r.get("user_feedback")]
    skips = [r for r in feedback if r.get("action") == "skip"]

    print(f"\n{sep}")
    print("COLUMNIST RIDE-ALONG SESSION SUMMARY")
    print(sep)
    print(f"  Log file    : {log_file}")
    print(f"  Articles    : {len(pending)}")
    print(f"  Feedback    : {len(fb_with_text)} with text, {len(skips)} skipped")

    tagged = [r for r in feedback if r.get("tag")]
    if tagged:
        from collections import Counter
        tag_counts = Counter(r["tag"] for r in tagged)
        print(f"  Tags        : {dict(tag_counts)}")

    severed = [r for r in feedback if r.get("severity") is not None]
    if severed:
        from collections import Counter
        sev_counts = Counter(r["severity"] for r in severed)
        print(f"  Severities  : {dict(sev_counts)}")

    if fb_with_text:
        print(f"\n  Feedback ({len(fb_with_text)}):")
        for r in fb_with_text:
            ts_raw = r.get("ts", "")[:19].replace("T", " ")
            tag_str = f"  [{r['tag']}]" if r.get("tag") else ""
            sev_str = f"  sev={r['severity']}" if r.get("severity") is not None else ""
            print(f"    [{ts_raw}]{tag_str}{sev_str}")
            print(f"      > {r['user_feedback']}")

    print(sep)
    print(f"  Replay later: cat {log_file} | python -m json.tool")
    print(sep)
