"""Columnist ride-along mode for DBA columnist voices.

When DBA_COLUMNIST_RIDE_ALONG=<persona_id> is set, the sim pauses every time
the chosen persona posts an article.  The user types feedback in the terminal;
feedback is written to a JSONL log for later prompt-tuning work.  Feedback is
NEVER injected back into the LLM prompt during the same session.

All other personas post normally.  Only the chosen persona's fire pauses the sim.

Activation:
    export DBA_COLUMNIST_RIDE_ALONG=marcus_cole   # any valid PERSONAS key
    python run.py

Env vars:
    DBA_COLUMNIST_RIDE_ALONG=<persona_id>  — enables the mode, names the persona
    DBA_COLUMNIST_RA_LOG=<absolute_path>   — optional override for the JSONL path

Mutual exclusion: refuses to activate (logs a warning, returns is_enabled()=False)
when DBA_RIDE_ALONG=1 is also set.  Two ride-along modes fight over stdin.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import sys
import uuid
from datetime import timezone
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOG_DIR = _PROJECT_ROOT / "headless_logs"

_SESSION_TS = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
_LOG_FILE: Path | None = None  # initialised lazily on first write

# Module-level asyncio state — initialised by start_feedback_intake().
_feedback_queue: asyncio.Queue[str] | None = None
_pending_event: asyncio.Event | None = None
_pending_article_id: str | None = None
_stop_flag: bool = False
_fires: int = 0   # total chosen-persona fires this session


# ---------------------------------------------------------------------------
# Public gate queries
# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    """True when DBA_COLUMNIST_RIDE_ALONG is set to a non-empty persona id
    AND DBA_RIDE_ALONG is NOT 1 (mutual exclusion guard)."""
    val = os.environ.get("DBA_COLUMNIST_RIDE_ALONG", "").strip()
    if not val:
        return False
    if os.environ.get("DBA_RIDE_ALONG") == "1":
        print(
            "[columnist_ride_along] WARNING: DBA_RIDE_ALONG=1 and "
            "DBA_COLUMNIST_RIDE_ALONG are both set — columnist ride-along "
            "is DISABLED to avoid stdin collision.",
            file=sys.stderr,
        )
        return False
    return True


def target_persona_id() -> str | None:
    """Return the chosen persona id, or None when ride-along is off."""
    if not is_enabled():
        return None
    return os.environ.get("DBA_COLUMNIST_RIDE_ALONG", "").strip() or None


# ---------------------------------------------------------------------------
# JSONL log writer — mirrors ride_along.py conventions exactly
# ---------------------------------------------------------------------------

def _get_log_file() -> Path:
    """Return the JSONL log path for this session, creating the directory if needed."""
    global _LOG_FILE
    if _LOG_FILE is None:
        persona_id = target_persona_id() or "unknown"
        override = os.environ.get("DBA_COLUMNIST_RA_LOG", "").strip()
        if override:
            _LOG_FILE = Path(override)
            _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        else:
            _LOG_DIR.mkdir(parents=True, exist_ok=True)
            _LOG_FILE = _LOG_DIR / f"columnist_ride_along_{persona_id}_{_SESSION_TS}.jsonl"
    return _LOG_FILE


def _write_log(record: dict) -> None:
    """Append one JSON line to the session log file. Never raises."""
    try:
        log_path = _get_log_file()
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        # Warn (but don't abort) if the session log grows unexpectedly large.
        try:
            size = log_path.stat().st_size
            if size > 10 * 1024 * 1024:  # 10 MB
                print(
                    f"[columnist_ride_along] WARNING: log file exceeds 10 MB "
                    f"({size // (1024 * 1024)} MB): {log_path}",
                    file=sys.stderr,
                )
        except OSError:
            pass
    except Exception as exc:  # noqa: BLE001
        print(f"[columnist_ride_along] WARNING: failed to write log: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Article ID generator
# ---------------------------------------------------------------------------

def _make_article_id() -> str:
    ts = datetime.datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return f"ra_{ts}_{suffix}"


# ---------------------------------------------------------------------------
# Stdin reader — runs in a thread via run_in_executor so it never blocks the
# event loop.  Pushes each raw line into _feedback_queue.
# ---------------------------------------------------------------------------

def _stdin_reader_thread(queue: asyncio.Queue[str], loop: asyncio.AbstractEventLoop) -> None:
    """Blocking thread: read stdin line-by-line and push into the asyncio queue.

    Runs for the lifetime of the bot process.  Exits cleanly on EOF or any
    readline error (treat both as ':quit').
    """
    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                # EOF — treat as :quit
                asyncio.run_coroutine_threadsafe(queue.put(":quit"), loop)
                break
            asyncio.run_coroutine_threadsafe(queue.put(line.rstrip("\n")), loop)
        except (EOFError, KeyboardInterrupt, OSError):
            asyncio.run_coroutine_threadsafe(queue.put(":quit"), loop)
            break


# ---------------------------------------------------------------------------
# Feedback intake task — runs as a background asyncio task
# ---------------------------------------------------------------------------

async def _feedback_intake_task() -> None:
    """Consume lines from _feedback_queue.

    When a pause is pending (_pending_event is set), resolve it with the
    user's input and write a feedback record.  Lines arriving while no
    pause is active are ignored (they'd be confusing noise from the user
    typing ahead).

    :tag and :sev modifiers accumulate until the next substantive input line
    and then attach to the feedback record.
    """
    global _pending_event, _pending_article_id, _stop_flag, _fires

    assert _feedback_queue is not None

    # Per-pause accumulators — reset after each feedback record is written.
    _pending_tag: str | None = None
    _pending_sev: int | None = None

    while True:
        line = await _feedback_queue.get()
        stripped = line.strip()

        # ── modifier commands (accumulate for next feedback record) ──────────
        if stripped.lower().startswith(":tag "):
            _pending_tag = stripped[5:].strip() or None
            print(f"   [tag set: {_pending_tag}]", flush=True)
            continue

        if stripped.lower().startswith(":sev "):
            sev_raw = stripped[5:].strip()
            try:
                sev_val = int(sev_raw)
                if 0 <= sev_val <= 3:
                    _pending_sev = sev_val
                    print(f"   [severity set: {_pending_sev}]", flush=True)
                else:
                    print("   [severity must be 0-3]", flush=True)
            except ValueError:
                print("   [invalid severity — use :sev 0..3]", flush=True)
            continue

        # ── quit ─────────────────────────────────────────────────────────────
        if stripped.lower() == ":quit":
            print("\n[columnist_ride_along] :quit received — stopping after current batch.",
                  flush=True)
            _stop_flag = True
            if _pending_event is not None:
                # Write a quit feedback record then release the gate.
                _write_log({
                    "kind": "feedback",
                    "ts": datetime.datetime.now(timezone.utc).isoformat(),
                    "article_id": _pending_article_id,
                    "user_feedback": None,
                    "tag": _pending_tag,
                    "severity": _pending_sev,
                    "action": "quit",
                })
                evt = _pending_event
                _pending_event = None
                _pending_article_id = None
                _pending_tag = None
                _pending_sev = None
                evt.set()
            # Stay alive in drain mode — auto-release every future pause event
            # without prompting the user.  The sim runs to completion; the user
            # exits via Ctrl+C or natural sim end.
            print(
                "[columnist_ride_along] Drain mode: all future pauses will be "
                "auto-released.  Ctrl+C to stop the bot entirely.",
                flush=True,
            )
            continue

        # ── skip (explicit or empty) ──────────────────────────────────────────
        is_skip = stripped.lower() in (":skip", "")
        if is_skip or stripped:
            if _pending_event is None:
                # No pause active — ignore stray input.
                continue

            action = "skip" if is_skip else "feedback"
            user_text: str | None = None if is_skip else stripped

            _write_log({
                "kind": "feedback",
                "ts": datetime.datetime.now(timezone.utc).isoformat(),
                "article_id": _pending_article_id,
                "user_feedback": user_text,
                "tag": _pending_tag,
                "severity": _pending_sev,
                "action": action,
            })

            label = f'"{user_text}"' if user_text else "(skipped)"
            tag_str = f"  tag={_pending_tag}" if _pending_tag else ""
            sev_str = f"  sev={_pending_sev}" if _pending_sev is not None else ""
            print(f"   >> LOGGED {label}{tag_str}{sev_str}", flush=True)

            evt = _pending_event
            _pending_event = None
            _pending_article_id = None
            _pending_tag = None
            _pending_sev = None
            evt.set()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start_feedback_intake(loop: asyncio.AbstractEventLoop) -> None:
    """Start the stdin reader thread and feedback intake task.

    Must be called once after the asyncio event loop is running (e.g. from
    the bot's on_ready or setup_hook).  Idempotent — safe to call multiple
    times (only the first call has any effect).
    """
    global _feedback_queue

    if _feedback_queue is not None:
        return  # already started

    _feedback_queue = asyncio.Queue()

    import threading
    t = threading.Thread(
        target=_stdin_reader_thread,
        args=(_feedback_queue, loop),
        daemon=True,
        name="columnist-ra-stdin",
    )
    t.start()

    loop.create_task(_feedback_intake_task(), name="columnist-ra-intake")
    print(
        f"[columnist_ride_along] Ride-along active — target persona: "
        f"{target_persona_id()}  log: {_get_log_file()}",
        flush=True,
    )


async def stop() -> None:
    """Signal the intake task to exit and flush the session_end record.

    Called from bot/client.py::close() on every clean shutdown path so the
    JSONL always gets a closing record.  Safe to call multiple times.
    """
    global _stop_flag, _pending_event, _pending_article_id
    if _stop_flag:
        # Already in drain/stop state; just write the end record if needed.
        pass
    else:
        _stop_flag = True
        # Release any currently pending pause so request_pause() returns.
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
            evt.set()

    _write_log({
        "kind": "session_end",
        "ts": datetime.datetime.now(timezone.utc).isoformat(),
        "persona_id": target_persona_id(),
        "total_fires": _fires,
        "reason": "stop",
    })


async def request_pause(article_record: dict[str, Any]) -> None:
    """Called from batch_sim_runner after the chosen persona's embed is sent.

    Writes a pending record to JSONL, prints a one-screen summary panel,
    then suspends until the user submits feedback (or :quit/:skip).

    The asyncio.Event handshake ensures the sim freezes at exactly the right
    moment — after the Discord send but before any further batch activity.
    """
    global _pending_event, _pending_article_id, _fires

    if _feedback_queue is None:
        # Intake not started — this shouldn't happen in normal use, but guard it.
        return

    # Drain mode: user typed :quit — auto-release this pause immediately so the
    # sim runs to completion without blocking.
    if _stop_flag:
        _fires += 1
        article_id = _make_article_id()
        _write_log({
            "kind": "feedback",
            "ts": datetime.datetime.now(timezone.utc).isoformat(),
            "article_id": article_id,
            "user_feedback": None,
            "tag": None,
            "severity": None,
            "action": "drain",
        })
        return

    _fires += 1
    article_id = _make_article_id()
    _pending_article_id = article_id

    # Write the pending record BEFORE awaiting — if the process dies mid-pause,
    # the article + prompt are preserved.
    _write_log({
        "kind": "pending",
        "ts": datetime.datetime.now(timezone.utc).isoformat(),
        "article_id": article_id,
        **article_record,
    })

    # Print the one-screen pause panel.
    sep = "=" * 60
    headline = (article_record.get("article") or {}).get("headline", "")
    body = (article_record.get("article") or {}).get("body", "")
    persona_display = article_record.get("persona_display_name", article_record.get("persona_id", ""))
    prompt_preview = ""
    prompt_data = article_record.get("prompt") or {}
    user_msg = prompt_data.get("user", "")
    if user_msg:
        prompt_preview = user_msg[:300].replace("\n", " ")

    print(f"\n{sep}", flush=True)
    print(f"  COLUMNIST RIDE-ALONG — pause #{_fires}", flush=True)
    print(f"  Persona : {persona_display}", flush=True)
    print(f"  Headline: {headline}", flush=True)
    if body:
        print(f"  Body    : {body[:400]}", flush=True)
    if prompt_preview:
        print(f"  Prompt  : {prompt_preview}...", flush=True)
    print(f"{sep}", flush=True)
    print(
        "  Type feedback and press Enter — or :skip  :tag <cat>  :sev 0-3  :quit",
        flush=True,
    )
    print("> ", end="", flush=True)

    # Create the gate event and await it — this suspends the entire sim.
    gate = asyncio.Event()
    _pending_event = gate
    await gate.wait()


def should_stop() -> bool:
    """True when the user typed :quit — sim runner can check this between batches."""
    return _stop_flag


# ---------------------------------------------------------------------------
# Post-session summary — mirrors ride_along.summarize_session()
# ---------------------------------------------------------------------------

def summarize_session() -> None:
    """Read this session's JSONL log and print a structured summary.

    Called from the launcher on exit.
    """
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
    print(f"  Target      : {target_persona_id()}")
    print(f"  Articles    : {len(pending)}")
    print(f"  Feedback    : {len(fb_with_text)} with text, {len(skips)} skipped")

    # Tag breakdown
    tagged = [r for r in feedback if r.get("tag")]
    if tagged:
        from collections import Counter
        tag_counts = Counter(r["tag"] for r in tagged)
        print(f"  Tags        : {dict(tag_counts)}")

    # Severity breakdown
    severed = [r for r in feedback if r.get("severity") is not None]
    if severed:
        from collections import Counter
        sev_counts = Counter(r["severity"] for r in severed)
        print(f"  Severities  : {dict(sev_counts)}")

    # Print all feedback lines
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
