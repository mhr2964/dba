# Columnist Ride-Along — Design Spec v2 (2026-05-21)

Architect: claude-opus-4.7
Target builder: backend-dev
Supersedes: v1 (same path, overwritten) — v1's launcher / bootstrap / desktop-shortcut
pipeline is gone. Bot stays running normally; ride-along is opt-in mid-session via a
sidecar CLI.

---

## 1. Overview (v2)

Columnist ride-along is a way to pause the sim every time a single **chosen** columnist
publishes an article, capture free-form feedback to a JSONL log, and resume — without
restarting the bot, without standing up a fresh league, and without contaminating real
league data. Feedback is **log-only** (no live prompt mutation, same as v1).

**v2.1 amendment (2026-05-22):** non-chosen columnists are now **suppressed** while a
sidecar is attached. The original v2 design said "other personas fire normally" — that
was reversed after the first feedback runs proved the cost (Anthropic API calls for
every persona on every batch) and noise (Discord channel filling with unrelated articles
mid-iteration) weren't worth the "other-voice context" they provided. Gate lives at
`columnist_service.generate` (single chokepoint for every persona article in the
system); detached state behaves identically to before (every persona fires normally).

The key shift from v1: ride-along is no longer a *launcher mode* that boots its own
bot and bootstraps its own league. The user's actual workflow is to run the production
bot from the desktop shortcut (`Start DBA Bot.bat`) and `/sim run` in real Discord
against a real league they've already created. The bot process has no terminal stdin
attached. v2 therefore moves feedback intake out of the bot process entirely: a
**separate sidecar CLI** (`scripts/columnist_feedback.ps1`) reads stdin in a terminal
the user controls and communicates with the bot via a **file-IPC directory** that the
bot polls a few times a second. Attach when you want; detach when you're done; the
bot keeps running either way.

What carries over from v1:

- The `asyncio.Event` handshake inside `request_pause` (still the right way to freeze
  the sim at the exact moment a chosen-persona embed lands).
- The JSONL writer (schema unchanged — pending/feedback record pair per fire).
- The four call sites in `services/batch_sim_runner.py` that wrap chosen-persona sends
  with a guarded `request_pause` (rotation arm, Darius arm, Marcus arm, Marcus Cole
  trade-report arm). The guards stay; only what they check changes.
- The `_capture_prompt` parameter on `columnist_service.generate`.
- `:tag` / `:sev` / `:skip` / `:quit` commands at the prompt.

What's deleted: launcher scripts, bootstrap helper, desktop shortcut, env-var-based
activation, env-var-driven JSONL path, the auto-start hook in `bot/client.py::on_ready`,
the `on close()` stop call. See section 7.

---

## 2. User flow

1. User has the bot running (started via `Start DBA Bot.bat` — same as today). The bot
   is connected to Discord. A real league exists in the database. The user has
   `/sim run`-ed or is about to.
2. User opens a terminal window and runs the sidecar:
   `pwsh .\scripts\columnist_feedback.ps1 marcus_cole`
   (Or with no arg: an interactive numbered menu of valid persona ids appears first.)
3. Sidecar writes `start.cmd` into `headless_logs/columnist_ride_along_ipc/` with the
   chosen persona id and the sidecar's PID. The bot's poll task picks it up within
   ~300 ms, validates the persona id, switches its in-memory chosen-persona state to
   `marcus_cole`, writes `state.json` (status=`attached`, persona, bot PID, JSONL log
   path, attached-at timestamp), and deletes `start.cmd`.
4. Sidecar polls `state.json` until `status == "attached"` and prints:
   `attached → marcus_cole | log: headless_logs/columnist_ride_along_marcus_cole_20260521_143218.jsonl`
   Then it prints a spinner / "waiting for marcus_cole posts..." line.
5. User runs `/sim run count:50 force:True` in Discord (or it's already running).
   The full rotation posts as normal. The user watches articles roll into the Discord
   analysis channel.
6. When Marcus Cole's article posts, the bot:
   - Writes a `pending` JSONL record (article + full prompt + context).
   - Writes `pause.json` to the IPC dir (article_id, headline, body, embed_preview,
     persona, batch index).
   - Creates an `asyncio.Event`, awaits it. Sim is frozen.
7. Sidecar's poll picks up `pause.json` within ~300 ms, deletes it, prints the pause
   panel to the terminal:
   ```
   ============================================================
     COLUMNIST RIDE-ALONG — pause #3
     Persona : Marcus Cole
     Headline: Brunson's 38 Lifts NYK Over BOS in Double-OT
     Body    : <first 400 chars>
   ============================================================
     feedback> _
   ```
8. User types a free-form line (or `:skip` / `:tag headline` / `:sev 2`). Sidecar writes
   `feedback.json` (with the matching `article_id`) into the IPC dir. Bot's poll picks
   it up within ~300 ms, validates the `article_id` matches the current pending pause,
   writes a `feedback` JSONL record, deletes `feedback.json`, releases the event. Sim
   resumes.
9. Repeat until the user has seen enough. User types `:quit` in the sidecar. Sidecar
   writes `stop.cmd`. Bot's poll picks it up, clears chosen-persona state, deletes
   `stop.cmd`, updates `state.json` to `status=detached`. Sidecar prints session
   summary (counts + every feedback line) and exits. **The bot keeps running.** The
   user can re-attach later with a different persona id.

---

## 3. Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│ TERMINAL (sidecar process — runs only when user wants feedback intake)  │
│   pwsh ./scripts/columnist_feedback.ps1 marcus_cole                     │
│     ├── interactive persona menu (if no positional arg)                 │
│     ├── reads stdin via prompt_toolkit (or stdlib input()) on main thrd │
│     ├── writes:   start.cmd, feedback.json, stop.cmd                    │
│     ├── reads:    state.json, pause.json                                │
│     ├── deletes:  pause.json                                            │
│     └── on :quit  → writes stop.cmd; prints session summary; exits      │
└─────────────────────────────────────────────────────────────────────────┘
                              ▲          ▲
                              │ writes   │ reads
                              ▼          ▼
                  ┌──────────────────────────────────┐
                  │  headless_logs/                  │
                  │   columnist_ride_along_ipc/      │  ← file IPC directory
                  │     state.json                   │
                  │     start.cmd                    │
                  │     pause.json                   │
                  │     feedback.json                │
                  │     stop.cmd                     │
                  └──────────────────────────────────┘
                              ▲          ▲
                              │ writes   │ reads
                              ▼          ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ BOT PROCESS (run.py — long-running, started by Start DBA Bot.bat)       │
│                                                                          │
│   bot/client.py::setup_hook()                                            │
│     └─ loop.create_task(columnist_ride_along.ipc_watch_task())           │
│           ↑ always-on background poll, no-op until start.cmd appears     │
│                                                                          │
│   services/columnist_ride_along.py  (REWRITTEN — no stdin reader)        │
│     - module-global chosen-persona state (CHOSEN_PERSONA_ID, gate dict)  │
│     - ipc_watch_task(): polls IPC dir every ~300ms; handles start.cmd,   │
│       feedback.json, stop.cmd; writes state.json/pause.json              │
│     - is_enabled() now reads CHOSEN_PERSONA_ID, NOT env vars             │
│     - request_pause(record): same Event handshake as v1; additionally    │
│       writes pause.json before await and clears it on release            │
│                                                                          │
│   services/batch_sim_runner.py                                           │
│     - 4 hook sites UNCHANGED (the if-block guards still call             │
│       columnist_ride_along.is_enabled() + target_persona_id();           │
│       only what those functions check has changed)                       │
│                                                                          │
│   services/columnist_service.py                                          │
│     - _capture_prompt optional param UNCHANGED                           │
└─────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                         Discord (sim runs LIVE here against the real league)
```

Two clean halves with a directory between them. No shared stdin, no shared parent,
no shared process tree. The bot and sidecar each only know about the IPC directory.

---

## 4. Per-question decisions

### Q1 — IPC mechanism

Options:

- **A: File-based polling.** Bot polls a known directory every N ms; sidecar writes
  small JSON files; both sides delete files they've consumed.
- B: Unix domain socket. Cross-platform issue — user's primary env is Windows.
- C: Windows named pipe (`\\.\pipe\dba_columnist_ra`). Works, but pywin32 dependency
  or low-level Win32 calls; bash variant for the sidecar gets fiddly.
- D: Localhost HTTP server inside the bot. Requires opening a port (firewall + collision
  risk), and routing through aiohttp — overkill for one-line messages.
- E: SQLite IPC table. Durable, transactional. Massively heavier than the problem.

**Recommendation: A — file-based.**

Why:
- Trivial cross-platform — same `Path.write_text` / `Path.read_text` on Windows + bash.
- Zero new dependencies. Both sides are already Python with `pathlib` available.
- Self-healing on crash: stale files are easy to inspect manually and easy to ignore
  by stamping a PID into `state.json` (bot owns it; if PID doesn't match the live
  bot's, sidecar treats state as stale).
- The user types feedback by hand. The maximum latency that matters is **less than the
  user's reaction time** — 300 ms is invisible. We don't need socket-tier latency.

**Polling interval: 300 ms on both sides.** Justification: the bot's poll
overhead at 300 ms is ~3 directory listings/second on a single empty-or-near-empty
dir, costing microseconds. The sidecar's poll is even cheaper since it usually has
nothing to do but reprint a spinner frame. From article-post to pause-panel-shown
the user sees at most ~600 ms (300 ms for the bot to drop `pause.json` after the
embed lands + 300 ms for the sidecar to pick it up), which is well below the
threshold of perceptible delay for a non-real-time interaction.

### Q2 — IPC directory layout

Directory: `headless_logs/columnist_ride_along_ipc/` (gitignored; sibling to existing
`headless_logs/ride_along_*.jsonl` files).

Five filenames, one schema each. All JSON unless suffixed `.cmd` (which can be empty
or carry a single line of payload). All files are write-then-rename to avoid
half-written reads (write to `<name>.tmp`, then `os.replace` — atomic on both
Windows and POSIX).

| File | Writer | Reader | Deleter | Purpose |
|---|---|---|---|---|
| `state.json` | bot | sidecar | bot (on detach) | Persistent: current chosen-persona, bot PID, log path, status (`detached`/`attached`/`paused`), last-update ts. Sidecar polls this to confirm attach succeeded and to learn the log path. |
| `start.cmd` | sidecar | bot | bot (after applying) | Transient one-shot: requests bot to switch chosen-persona. Payload: JSON `{persona_id, sidecar_pid, requested_at}`. |
| `pause.json` | bot | sidecar | sidecar (after displaying) | Transient: bot is currently awaiting feedback. Payload: `{article_id, persona_id, persona_display_name, headline, body_preview, embed_preview, pause_index, posted_at}`. Note: full body is intentionally truncated for display; the JSONL log holds the full body. |
| `feedback.json` | sidecar | bot | bot (after applying) | Transient one-shot: user's feedback line. Payload: `{article_id, user_feedback, tag, severity, action}`. Bot only accepts when `article_id` matches the currently pending pause. |
| `stop.cmd` | sidecar | bot | bot (after applying) | Transient one-shot: detach this sidecar; clear chosen-persona state on the bot. Payload: JSON `{sidecar_pid, reason}`. |

**Stale-file policy** (concrete rules — these make the protocol survive crashes):

1. On bot startup, the bot's `ipc_watch_task` does a "cold sweep": delete any
   `start.cmd`, `pause.json`, `feedback.json`, `stop.cmd` that exist (they're all
   transient and have no meaning between bot processes). It then writes a fresh
   `state.json` with `status=detached` and the current bot PID.
2. On sidecar startup, the sidecar reads `state.json`. If `bot_pid` doesn't match a
   live process (cross-check via `psutil` if available, else trust the timestamp:
   if `last_update_ts` > 60 s old, treat as stale), it warns the user that the bot
   may not be running and aborts.
3. `pause.json` older than 30 minutes (stale because the bot crashed mid-pause)
   is deleted by the sidecar on its first poll and never displayed.
4. `feedback.json` older than 60 s with no matching pending pause is deleted by
   the bot (it's stale input from a previous session).

### Q3 — Sidecar CLI surface

**Invocation:**
- `pwsh ./scripts/columnist_feedback.ps1 [persona_id] [flags]`
- `bash ./scripts/columnist_feedback.sh   [persona_id] [flags]`
- No positional arg → print numbered menu of valid persona ids + display names,
  read a number or id from stdin, validate.
- `--list` → print persona ids one per line; exit. (For scripting / completion.)
- `--no-ascii` → suppress the ASCII separator panel; print a single-line
  `[pause #N] <persona> | <headline>` instead. Off by default.

**Waiting display:** when attached but no pause active, the sidecar prints a single
line that updates in place:
```
attached → marcus_cole | log: <path> | waiting for marcus_cole posts...
```
A small spinner glyph cycles (`| / - \`). The line uses `\r` carriage-return to
overwrite itself rather than scrolling — keeps the terminal calm.

**Pause display:** the v1 panel format, mostly unchanged. Sidecar receives
`pause.json` with headline, persona display name, body_preview (≤400 chars), and
embed_preview. Prints:
```
============================================================
  COLUMNIST RIDE-ALONG — pause #3
  Persona : Marcus Cole
  Headline: Brunson's 38 Lifts NYK Over BOS in Double-OT
  Body    : <first 400 chars of body>
============================================================
  Type feedback and Enter — or :skip / :tag <cat> / :sev 0-3 / :quit
feedback> _
```

**Commands at the prompt** (same set as v1, no additions):
- `<free text>` → write `feedback.json` with `action=feedback`, `user_feedback=<text>`.
- `:skip` or empty Enter → write `feedback.json` with `action=skip`, `user_feedback=null`.
- `:tag <cat>` → set the next feedback record's `tag` field; sidecar holds this state
  locally and includes it on the *next* substantive submission. Reset after submission.
- `:sev 0..3` → same pattern, sets `severity` for next submission.
- `:quit` → write `stop.cmd`; print session summary; exit. Does NOT write a feedback
  record for the current pause if one is open — instead writes `feedback.json` with
  `action=quit` so the bot has a record, then writes `stop.cmd`.

**:quit lifecycle (resolves the question explicitly):** `:quit` detaches the sidecar
**and** turns off ride-along mode in the bot. Reason: the alternative (sidecar exits
but bot keeps pause-mode armed) means the next `/sim run` will silently freeze the
sim on the next chosen-persona post with nobody watching, which is a footgun. If the
user wants to "leave ride-along on but stop watching" they can simply leave the
sidecar terminal open and ignore it — the bot doesn't care whether the sidecar's
human is looking.

**Crash semantics:**
- Sidecar crashes mid-pause → bot is still awaiting on the event with `pause.json`
  on disk (sidecar didn't delete it after display). On the next bot poll cycle
  (~300 ms), the bot notices: the sidecar's PID is no longer alive (cross-check
  the `sidecar_pid` recorded in `state.json` against `psutil.pid_exists` or, on
  failure to import psutil, against a 10-second heartbeat file the sidecar
  rewrites). Bot then **drains** — writes a `feedback` JSONL record with
  `action=sidecar_died`, clears chosen-persona state, releases the gate, deletes
  `pause.json`. Sim resumes. Future articles do NOT pause (chosen-persona is
  cleared until the user attaches a new sidecar).
- Bot crashes mid-pause → sidecar polls `state.json` and notices its
  `last_update_ts` is now > 60 s old. Sidecar prints "bot appears to have stopped
  responding — exiting" and exits.

### Q4 — Bot-side state model

**Where chosen-persona lives:** as module-level globals in
`services/columnist_ride_along.py` — `_CHOSEN_PERSONA_ID: str | None` and the
existing `_pending_event` / `_pending_article_id`. No persistence across bot
restarts (intentional — see Q6).

**Polling task lifecycle:**
- Started from `bot/client.py::setup_hook()` as `self.loop.create_task(
  columnist_ride_along.ipc_watch_task(), name="columnist-ra-ipc")`. **Not gated
  on `is_enabled()`** — it runs unconditionally for the bot's lifetime, but
  no-ops until a `start.cmd` lands.
- The task does the cold sweep (delete all transient files; write fresh
  `state.json`) on first iteration.
- Symmetric cleanup in `bot/client.py::close()` — best-effort write of
  `state.json` with `status=detached, reason=bot_shutting_down`.

**Race condition: feedback.json racing a new pause.** Scenario: sidecar wrote
`feedback.json` for pause A; bot's poll thread is about to apply it; mid-flight,
`request_pause` for pause B starts and stashes a new `_pending_article_id`. Bot
then opens `feedback.json`, reads `article_id=A`, compares to current
`_pending_article_id=B`, **rejects** (writes a JSONL warning record with
`action=stale_feedback`, deletes the file), and pause B keeps waiting.

This is why every `pause.json` carries its own UUID-style `article_id` (the
existing `_make_article_id()` from v1, format `ra_<utc_ts>_<6hex>`) and every
`feedback.json` must echo that exact `article_id` back. The sidecar reads the
`article_id` from `pause.json`, stores it in a local variable, and includes it
verbatim in the `feedback.json` it writes for that pause. Mismatched IDs are
treated as stale and discarded.

**Concurrent chosen-persona posts:** practically impossible because
`_maybe_post_columnist` is awaited sequentially inside `sim_range`, and the
`await columnist_ride_along.request_pause(...)` call blocks before the function
returns. The next article can't start generating until the current pause releases.
The race window is therefore inside a single batch only if two different chosen
personas could fire from different awaits in the same batch — which isn't true
here because `_CHOSEN_PERSONA_ID` is a single string. Document the invariant; no
code needed.

### Q5 — Persona discovery / validation

**Where does the persona list come from?** The sidecar is a Python script that runs
in the same repo as the bot. It can simply `from services.personas import PERSONAS`
and read keys + display names. The `__init__.py` auto-discovers persona modules
via `pkgutil`, so the import is side-effect-free aside from registering personas.
This is the cheap path and we should take it. The sidecar imports stay
import-only (no DB pool, no Discord client) so startup is fast.

**Validation:**
- Sidecar validates persona id against `PERSONAS.keys()` *before* writing
  `start.cmd`. Fails fast with the list of valid ids if the user typos one.
- Bot also validates inside `ipc_watch_task` when applying `start.cmd`: if the
  persona id isn't in `PERSONAS`, write a `state.json` with
  `status=rejected, reason="unknown persona: <id>"`, delete `start.cmd`, do not
  set `_CHOSEN_PERSONA_ID`. The sidecar polls `state.json`, sees `rejected`,
  prints the reason, exits non-zero. Defence-in-depth in case the sidecar
  validation drifts from the bot's persona registry.

### Q6 — Cleanup / detach / shutdown

Five distinct scenarios; each gets a deliberate behaviour:

| Scenario | What happens |
|---|---|
| Clean detach (`:quit`) | Sidecar writes `stop.cmd`; bot clears state, writes `state.json` with `status=detached`, deletes `stop.cmd`. Sidecar prints summary, exits. Bot keeps running. |
| Sidecar Ctrl+C | Sidecar's signal handler writes `stop.cmd` (same as `:quit`) before exiting; if it can't (hard kill), bot detects dead sidecar_pid on next poll, drains pause, clears state. |
| Bot restart while attached | New bot process's cold sweep deletes all transient files and rewrites `state.json` with `status=detached, bot_pid=<new>`. Sidecar's next poll sees a new `bot_pid`, prints "bot restarted — detaching", exits. (Alternative — auto-re-attach — feels surprising; reject.) |
| Sidecar crash mid-pause | Bot detects (after ~5 s timeout grace) and drains the pause. JSONL records `action=sidecar_died`. |
| Power outage / unclean shutdown | Next bot startup's cold sweep handles it. Any `pause.json` left over is silently dropped (the article it referred to has no live `_pending_event` in the new bot process; releasing nothing is fine). |

**Bot-PID stamping** in `state.json` is the key trick. The sidecar checks
`state.bot_pid == prev_state.bot_pid` between polls; a change implies a bot
restart and the sidecar gives up cleanly. Cross-platform: just `os.getpid()` on
the bot side, no need for psutil here (psutil is only for the sidecar's
dead-process detection of the bot, which is optional — fall back to staleness
of `last_update_ts`).

### Q7 — JSONL log path

**Bot owns it.** The log path is decided by the bot on `start.cmd` apply, not
by the sidecar (sidecar may not know the bot's absolute project root). Bot
constructs `headless_logs/columnist_ride_along_<persona>_<bot_start_ts>.jsonl`
where `<bot_start_ts>` is captured at bot process start (a module-global set
once at import time, mirroring v1's `_SESSION_TS`).

**Why bot-start-ts and not attach-ts:** if the user attaches, detaches, and
re-attaches the same persona during one bot lifetime, both sessions append to the
same file. That's intentional — they're the same conceptual prompt-iteration
session from the user's perspective, and concatenation makes the summary at the
end accurate. Different personas always get different files (the persona id is
in the filename), so cross-pollination across personas can't happen.

**Sidecar discovers the path** by reading `state.json`, which carries
`log_path`. Sidecar prints it on attach so the user can `cat` it later.

### Q8 — Discord-side messaging during pause

**Recommendation: stay silent.** v1 was silent and the user didn't ask to change
that. Posting "🎤 Paused for ride-along feedback" in the analysis channel pollutes
the read-back for the league later and feels noisy. The user already sees the
chosen persona's embed; they know what's pausing. The terminal is the surface
for ride-along status.

If the user later asks for a visible cue, the cleanest addition is a footer
suffix on the chosen persona's embed when ride-along is active — but that's a
v3 detail. Don't add it now.

### Q9 — Scope cut for v2

**Regular-season only.** Reuse v1's scope. The hook sites in
`_maybe_post_columnist` already cover the rotation + Darius + Marcus Brooks
arms; the Marcus Cole trade-report arm is also already wrapped. The playoff
arm (`_maybe_post_playoff_columnist`, if it exists as a separate function)
remains unhooked, same as v1. Reasoning: the user said "league I've already
created" and the regular-season surface covers the columnist work they care about
iterating on. Adding playoff coverage is a one-line copy of the guard pattern
when they ask for it; not worth doing speculatively.

### Q10 — Migration / removal plan

This is build-time work for the builder. See section 7 for the concrete list of
files and code blocks to delete.

---

## 5. IPC file schemas (concrete JSON)

All examples use UTC ISO-8601 timestamps. All transient files are written
atomically via `<name>.tmp` + `os.replace`.

### `state.json` (long-lived, bot writes, sidecar reads)

```json
{
  "status": "attached",
  "persona_id": "marcus_cole",
  "persona_display_name": "Marcus Cole",
  "bot_pid": 18432,
  "bot_started_at": "2026-05-21T13:02:11.040Z",
  "attached_at": "2026-05-21T14:30:00.221Z",
  "last_update_ts": "2026-05-21T14:32:18.421Z",
  "log_path": "C:/Users/Owner/Desktop/AI/Projects/dba/headless_logs/columnist_ride_along_marcus_cole_20260521_130211.jsonl",
  "pauses_seen": 3,
  "sidecar_pid": 22104
}
```

`status` is one of: `detached`, `attached`, `paused`, `rejected`. `paused` is set
while a `pause.json` is on disk — gives the sidecar a way to assert state without
re-reading the pause file. `rejected` carries a `reason` string.

### `start.cmd` (transient, sidecar writes, bot reads + deletes)

```json
{
  "persona_id": "marcus_cole",
  "sidecar_pid": 22104,
  "requested_at": "2026-05-21T14:30:00.110Z"
}
```

Bot, on read: validates persona; if valid, sets `_CHOSEN_PERSONA_ID`, updates
`state.json` to `attached`, deletes `start.cmd`. If invalid, sets state to
`rejected`, deletes `start.cmd`, does not change `_CHOSEN_PERSONA_ID`.

### `pause.json` (transient, bot writes, sidecar reads + deletes)

```json
{
  "article_id": "ra_20260521T143218_a1b2c3",
  "persona_id": "marcus_cole",
  "persona_display_name": "Marcus Cole",
  "headline": "Brunson's 38 Lifts NYK Over BOS in Double-OT Thriller",
  "body_preview": "<first 400 chars of body, newlines preserved>",
  "embed_preview": "<headline>\n\n<first 400 chars>",
  "pause_index": 3,
  "posted_at": "2026-05-21T14:32:18.421Z"
}
```

Bot writes this *immediately before* `await _pending_event.wait()`. Sidecar
reads it, displays the panel, deletes the file (acknowledging receipt). The
fact that the file was deleted is the bot's signal — when its poll observes
`pause.json` is gone but `_pending_event` is still set, that's normal (sidecar
just hasn't sent feedback yet); when its poll observes a fresh `feedback.json`
with a matching `article_id`, it processes the feedback and releases the event.

### `feedback.json` (transient, sidecar writes, bot reads + deletes)

```json
{
  "article_id": "ra_20260521T143218_a1b2c3",
  "user_feedback": "Headline buries the lede. 'Lifts' is twee.",
  "tag": "headline",
  "severity": 2,
  "action": "feedback"
}
```

`action` values: `feedback`, `skip`, `quit`, `sidecar_died` (the last is bot-
generated only; sidecar never writes it). When the bot reads this file:

1. Validate `article_id` against `_pending_article_id`. Mismatch → write a
   stale-feedback JSONL warning record, delete file, **do not release event**.
2. Match → write the `feedback` JSONL record (mirroring v1 schema in section 8),
   delete file, `_pending_event.set()`.

### `stop.cmd` (transient, sidecar writes, bot reads + deletes)

```json
{
  "sidecar_pid": 22104,
  "reason": "user_quit"
}
```

`reason` values: `user_quit`, `sidecar_ctrl_c`, `bot_shutting_down`. Bot, on
read: writes `session_end` JSONL record, clears `_CHOSEN_PERSONA_ID`, updates
`state.json` to `detached`, releases any in-flight `_pending_event` with an
`action=quit` JSONL feedback record, deletes `stop.cmd`.

---

## 6. Files to touch / create

| Path | Action | Notes |
|---|---|---|
| `services/columnist_ride_along.py` | **REWRITE** | Strip the stdin reader thread (`_stdin_reader_thread`) and feedback-intake task (`_feedback_intake_task`). Replace `start_feedback_intake` with `ipc_watch_task` (async, always-on). Add module globals `_CHOSEN_PERSONA_ID`, `_SIDECAR_PID`, `_BOT_PID = os.getpid()`. Rewrite `is_enabled()` to return `_CHOSEN_PERSONA_ID is not None` (env-var check removed). Rewrite `target_persona_id()` to return `_CHOSEN_PERSONA_ID`. Add helpers: `_write_state(status, **fields)`, `_apply_start_cmd(payload)`, `_apply_feedback(payload)`, `_apply_stop_cmd(payload)`, `_cold_sweep_ipc_dir()`. `request_pause` adds a `pause.json` write before the `await` and a delete after the release. JSONL writer + `_make_article_id()` + `summarize_session()` carry forward unchanged. |
| `services/batch_sim_runner.py` | **NO CHANGE** | The four hook sites (lines ~1152, ~2718, ~2832, ~2896 per current file) still call `_columnist_ride_along.is_enabled()` and `_columnist_ride_along.target_persona_id()`. Only the implementations of those two functions change, not the callers. **Verify** during build that no caller depends on the env-var semantics. |
| `services/columnist_service.py` | **NO CHANGE** | `_capture_prompt` param stays. |
| `bot/client.py` | **EDIT** | In `setup_hook` (after cog loading): `self.loop.create_task(columnist_ride_along.ipc_watch_task(), name="columnist-ra-ipc")`. In `on_ready`: **DELETE** the `_cra.start_feedback_intake(asyncio.get_event_loop())` block (currently lines 185–191). In `close()`: keep the structure but change the call — `await _cra.shutdown()` which writes a final `state.json` with `status=detached, reason=bot_shutting_down` and a `session_end` JSONL record if there were any pauses this lifetime. (Replace `_cra.stop()` with `_cra.shutdown()` — different semantics, different name.) |
| `scripts/columnist_feedback.ps1` | **NEW** | PowerShell sidecar. ~150 lines. Persona menu (via `python -c "from services.personas import PERSONAS; print('\n'.join(PERSONAS))"`), validate id, write `start.cmd`, poll `state.json` until `attached` / `rejected` / timeout, then enter main loop: poll `pause.json` every 300 ms, on detect display panel + read stdin via `Read-Host`, write `feedback.json`. On `:quit` write `stop.cmd`, print summary (read the JSONL via `python -c`), exit. |
| `scripts/columnist_feedback.sh` | **NEW** | Bash twin of the above. Same logic. Uses `read -r` for stdin. |
| `headless_logs/columnist_ride_along_ipc/` | **NEW** (created at runtime) | Created by `_cold_sweep_ipc_dir()` on first bot startup if missing. `.gitignore` already covers `headless_logs/`. |

**No new dependencies.** prompt_toolkit is not required (Q3 keeps the sidecar
on stdlib `input()` / `Read-Host`). psutil is optional — used by the sidecar to
spot a dead bot PID; fall back to `last_update_ts` staleness check if not
installed.

---

## 7. Files to DELETE

These are the v1 launcher surface, now obsolete:

| Path | Why |
|---|---|
| `scripts/columnist_ride_along.ps1` | v1 launcher — replaced by `columnist_feedback.ps1`. |
| `scripts/columnist_ride_along.sh` | v1 launcher — replaced by `columnist_feedback.sh`. |
| `scripts/_columnist_ra_bootstrap.py` | League-creation helper. v2 attaches to existing leagues; no bootstrap needed. Also delete `scripts/__pycache__/_columnist_ra_bootstrap.cpython-312.pyc` (auto-cleans on next sweep, but call it out). |
| `Desktop\DBA Columnist Ride-Along.lnk` | Desktop shortcut. The user starts the bot via `Start DBA Bot.bat`; ride-along is opt-in via the sidecar, not a separate shortcut. **Path:** the user's actual Desktop directory; the builder will need to confirm exact filename. |

**Code blocks to remove (concrete file:line references):**

- `bot/client.py` lines 185–191 (inside `on_ready`): the `from services import
  columnist_ride_along as _cra; if _cra.is_enabled(): _cra.start_feedback_intake(...)`
  block. Replace with the `ipc_watch_task` creation in `setup_hook` (see section 6).
- `bot/client.py` lines 194–202 (inside `close`): swap `_cra.stop()` for
  `_cra.shutdown()` and drop the `is_enabled()` guard (shutdown should always
  write the closing `state.json`, regardless of whether anyone's attached).
- `services/columnist_ride_along.py` lines 39–47 (the `_feedback_queue`, etc.
  module globals related to stdin) — replace with new globals (Q4).
- `services/columnist_ride_along.py` lines 51–72 (`is_enabled()`, `target_persona_id()`
  current bodies) — rewrite as described in section 6.
- `services/columnist_ride_along.py` lines 130–146 (`_stdin_reader_thread`) — delete.
- `services/columnist_ride_along.py` lines 153–257 (`_feedback_intake_task`) — delete.
  The command parsing logic (`:tag`, `:sev`, `:skip`, `:quit`) moves to the sidecar.
- `services/columnist_ride_along.py` lines 264–292 (`start_feedback_intake`) — delete;
  replaced by `ipc_watch_task`.

The `request_pause`, `_make_article_id`, `_write_log`, `_get_log_file`, and
`summarize_session` functions carry forward with light edits (add the `pause.json`
write + delete to `request_pause`; `summarize_session` no longer changes).

---

## 8. JSONL log schema

Carry-forward from v1 verbatim. Two record kinds (`pending` + `feedback`) plus an
optional `session_end`. The new IPC layer does not change what gets written, only
where the feedback content comes from (now: sidecar → `feedback.json` → bot, vs
v1's stdin → in-process).

One additional `action` value for the `feedback` record: `sidecar_died`. Written
by the bot when it detects a dead sidecar PID with a pause in flight. Same
shape as a `:quit` record but with `user_feedback=null` and an explanatory
`reason` field. Schema:

```json
{
  "kind": "feedback",
  "ts": "2026-05-21T14:36:01.880Z",
  "article_id": "ra_20260521T143218_a1b2c3",
  "user_feedback": null,
  "tag": null,
  "severity": null,
  "action": "sidecar_died",
  "reason": "sidecar PID 22104 no longer responsive after 5s"
}
```

Other `action` values unchanged: `feedback`, `skip`, `quit`, `shutdown`,
`drain`, `stale_feedback` (new — bot received a feedback.json whose
article_id didn't match the current pending pause; informational only).

---

## 9. Open questions

These I'm calling either way and flagging for the user's review:

1. **psutil dependency on the sidecar.** Optional in this design — fall back to
   timestamp-staleness if missing. Worth nudging the user to add it because the
   "is the bot actually alive" check via PID is more reliable than timestamp,
   and the sidecar is the side most likely to be confused about a hung bot.
   **Default: don't add to `requirements.txt`; the fallback is good enough.**
2. **Sidecar reuse the bot's existing virtual env / Python?** Yes — both `.ps1`
   and `.sh` should invoke `python` (assume the user has the project's venv
   activated, same as for `run.py`). Don't try to be clever about isolation;
   the sidecar imports `services.personas` and that needs the project's
   import path anyway.
3. **What if the user runs two sidecars at once (different terminals,
   different personas)?** First one wins. The second sidecar's `start.cmd`
   writes will be rejected by the bot if `_CHOSEN_PERSONA_ID` is already set
   (bot writes `state.json` with `status=rejected, reason="already attached to
   <persona>"`). Sidecar sees rejection, prints message, exits. **No multi-
   persona simultaneous ride-along** — the JSONL log structure and the pause
   serialization both assume one chosen persona at a time, and the user hasn't
   asked for parallel.
4. **Sidecar's terminal echo of feedback** — should we print "logged: <feedback>
   [tag=headline sev=2]" back to the sidecar terminal after submission? **Yes**,
   one line per submission, after the bot writes the JSONL record. The sidecar
   knows the submission succeeded when it observes that `pause.json` is gone
   AND `state.json.status` flipped back to `attached` (from `paused`). We could
   add a `last_feedback_at` field to `state.json` for sharper feedback echo, but
   that's nice-to-have, not load-bearing.

---

## 10. Risks + mitigations

| Risk | Where it lives | Mitigation |
|---|---|---|
| **Bot crashes mid-pause.** Sidecar sits forever waiting for `state.json` updates. | sidecar | Sidecar staleness check on `state.last_update_ts` — if > 60 s old, print "bot unresponsive", exit. Bot must update `last_update_ts` at least once per poll cycle (every ~300 ms) — cheap. |
| **Sidecar crashes mid-pause.** Bot waits forever on `_pending_event`. | bot | Bot's `ipc_watch_task` tracks `_SIDECAR_PID` from the last `start.cmd`. On every poll while a pause is in flight, check whether sidecar PID is alive (psutil if available; fallback: check whether the sidecar has rewritten a `sidecar_heartbeat.txt` within the last 5 s — sidecar updates this once per poll). On 3 consecutive missed heartbeats, drain the pause as described in Q3. |
| **File system race: sidecar reads `pause.json` mid-write.** | both | All writes go via `<name>.tmp` + `os.replace`. `os.replace` is atomic on Windows (since Python 3.3) and POSIX. Readers see either the old file or the complete new file, never a half-written one. |
| **Polling interval too aggressive.** | both | 300 ms is the floor. The polled dir holds at most 5 files and is on local disk. If perf ever shows up here we should be ashamed. |
| **IPC dir not created.** | bot | `_cold_sweep_ipc_dir()` runs at `ipc_watch_task` startup and creates the dir with `mkdir(parents=True, exist_ok=True)`. If the dir can't be created (disk full, permission denied), the task logs a single warning and exits — chosen-persona functionality stays off but the rest of the bot is unaffected. |
| **Stale `pause.json` from a crashed prior session displayed to the user.** | sidecar | On sidecar startup, sidecar reads `state.json` first. If `state.bot_pid` is different from any pid recorded in a leftover `pause.json` (sidecar doesn't have direct access to that, but the sidecar's own first poll will only act on a `pause.json` that arrives *after* the sidecar saw `status=attached`). In practice the bot's cold sweep deletes any leftover `pause.json` before the sidecar can attach, so this risk is structurally prevented. |
| **Two ride-alongs at once.** | bot | Bot rejects a `start.cmd` whose `_CHOSEN_PERSONA_ID` slot is non-empty; writes `rejected` state with a clear reason. Sidecar exits non-zero. |
| **Mismatched persona registry between sidecar import and bot's actual `PERSONAS`.** | both | Defence-in-depth: sidecar validates, bot validates. If they disagree, bot wins (sidecar gets `rejected` with the list of valid ids). Realistically this only happens if the sidecar runs from a different checkout than the bot, which would already break other things. |
| **JSONL log path collision** when same persona attaches twice in one bot lifetime. | bot | Intentional: same persona, same bot start ts → same file, appended. See Q7 rationale. |
| **`os.replace` on Windows refuses to overwrite an open file handle.** | both | Reader-side code reads the whole file in one shot (`Path.read_text()`) and immediately closes the handle. No long-held read handles on IPC files. |
| **Long body content (>1 MB embed_preview) bloats `pause.json`.** | bot | `body_preview` is truncated to 4 KB before write. The JSONL still has the full body — sidecar's terminal display doesn't need it. |
| **The user expects multi-line feedback** (paste a paragraph). | sidecar | stdlib `input()` reads one line. If the user pastes content containing `\n`, only the first line is captured. Mitigation: print a small hint at attach time — "for multi-line, escape newlines with \\n manually; or paste into a `.txt` and reference it." Don't engineer a `:multiline` open/close pair until the user actually asks. |
| **`asyncio.get_event_loop()` deprecation noise.** v1 used this in `on_ready`; we're removing that call. The new task creation via `self.loop.create_task(...)` in `setup_hook` is the modern path. | bot | No mitigation needed — the deprecation goes away with the v1 code we're deleting. |

---

## 11. Implementation outline (builder steps)

Do these in order. Each step is independently buildable and testable.

1. **Rewrite `services/columnist_ride_along.py`.** Module-global swap: drop the
   stdin/queue/feedback-intake state, add `_CHOSEN_PERSONA_ID`, `_SIDECAR_PID`,
   `_BOT_PID = os.getpid()`, `_BOT_START_TS`. Rewrite `is_enabled()` and
   `target_persona_id()` to read the new globals. Write `_cold_sweep_ipc_dir()`,
   `_write_state(status, **fields)`, `_apply_start_cmd(payload)`,
   `_apply_feedback(payload)`, `_apply_stop_cmd(payload)`, `_atomic_write_json(path, obj)`,
   `_read_json_or_none(path)`. Write `async def ipc_watch_task()` — runs forever, sleeps
   `await asyncio.sleep(0.3)` between iterations, checks each transient file in
   turn, calls the apply helpers. Edit `request_pause` to call `_atomic_write_json`
   for `pause.json` before `await gate.wait()` and to delete it after release. Edit
   stale `stop()` into `shutdown()` (Section 6 semantics).

2. **Edit `bot/client.py`.** Delete the `on_ready` block (lines 185–191).
   In `setup_hook`, after cog loading, add the `ipc_watch_task` task creation.
   In `close()`, swap `_cra.stop()` for `_cra.shutdown()` and drop the
   `is_enabled()` guard.

3. **Verify `services/batch_sim_runner.py` hook sites still work.** They check
   `_columnist_ride_along.is_enabled()` and `target_persona_id()` — the new
   implementations preserve that interface. No code change needed; smoke-test
   after step 1 by manually setting `_CHOSEN_PERSONA_ID` and confirming a chosen
   persona's article triggers `request_pause`.

4. **Write `scripts/columnist_feedback.ps1`.** Persona menu via `python -c`,
   validate, write `start.cmd` via tmp+replace, poll `state.json` until
   `attached`/`rejected`/30 s timeout. Main loop: poll for `pause.json` every
   300 ms; on detect, read body+headline, delete `pause.json`, print panel,
   `Read-Host` a line. Parse `:tag`/`:sev`/`:skip`/`:quit` locally. Write
   `feedback.json` (or `stop.cmd` on `:quit`). On `:quit` exit: print
   summary by calling `python -c "from services.columnist_ride_along import
   summarize_session; summarize_session()"`. Maintain a `sidecar_heartbeat.txt`
   file in the IPC dir — touch its mtime every poll cycle.

5. **Write `scripts/columnist_feedback.sh`.** Mirror of step 4 in bash. `read -r`
   for stdin. `python -c` for menu + summary. The heartbeat file is just
   `touch /path/to/sidecar_heartbeat.txt` once per poll.

6. **Delete v1 launchers + bootstrap.** Files listed in section 7. Also delete
   the `Desktop\DBA Columnist Ride-Along.lnk` shortcut (the user can verify the
   exact path on their machine).

7. **Smoke test (manual, with a real running bot).**
   - Start the bot via `Start DBA Bot.bat`. Confirm `headless_logs/columnist_ride_along_ipc/state.json`
     appears with `status=detached`.
   - In a second terminal, run `./scripts/columnist_feedback.ps1 marcus_brooks`.
     Confirm "attached → marcus_brooks" prints with the JSONL path.
   - In Discord, `/sim run count:50 force:True` (existing league).
   - Watch articles flow. Confirm Jordan/Keisha/Pat etc. post without pausing.
     Confirm Marcus Brooks (when his counter hits) triggers a pause panel in the
     sidecar terminal.
   - Type "this headline is too long" + Enter. Confirm JSONL has one `pending`
     + one `feedback` record with the text. Confirm Discord sim resumes.
   - Try `:tag voice`, `:sev 2`, then `headline buries the lede`. Confirm tag
     + severity attach to the next record.
   - Type `:quit`. Confirm sidecar prints summary + exits. Confirm `state.json`
     flips to `detached`. Confirm bot continues running (try `/league info` in
     Discord — should still work).
   - Re-attach with a different persona (`marcus_cole`). Confirm new JSONL
     file with the new persona id in the name.
   - Kill the sidecar with Ctrl+C while a pause is active (force one by
     triggering a trade with a chosen persona of `marcus_cole`). Confirm bot
     drains the pause within 5 s, JSONL records `action=sidecar_died`,
     `state.json` flips to `detached`.

8. **Update `TESTING.md`** with the new sidecar invocation. Remove any
   references to the old launcher.

---

## 12. Handoff notes for the builder

- The single most fragile piece moves from "stdin reader thread" (v1) to
  "atomic file writes". Make sure every transient-file write goes through
  `_atomic_write_json` (write to `<name>.tmp` + `os.replace`). Reads should
  be a single `Path.read_text()` followed by `json.loads`; on JSONDecodeError
  treat the file as in-flight (skip this poll) rather than corrupted.
- `request_pause` must write `pause.json` **before** awaiting the event,
  same as the JSONL pending record. If the bot dies between writing
  `pause.json` and awaiting, the next bot's cold sweep cleans it up.
- The hook sites in `batch_sim_runner.py` are unchanged on the surface,
  but verify mentally that the in-flight `_capture_prompt` dict still works.
  It does — `is_enabled()` returning true based on `_CHOSEN_PERSONA_ID` is
  what gates the capture dict construction, and the rest is unchanged.
- The sidecar's heartbeat file is a small but important piece. Without it
  the bot has no way to know whether the sidecar is alive without psutil.
  Make sure both the .ps1 and .sh versions touch it every poll cycle.
- When in doubt, mirror v1's JSONL writer behaviour — it's the durable artifact
  and its schema is the contract for whatever prompt-iteration tool reads
  these logs later. Don't change record shapes without a reason.

---

## Handoff block

=== HANDOFF ===
did: redesigned columnist ride-along as attach-only sidecar with file-IPC; v1 launcher / bootstrap / desktop shortcut deleted; bot stays running and gains an always-on poll task; feedback intake leaves the bot process and lives in a separate CLI the user runs in a terminal
found: v1 hook sites in batch_sim_runner.py preserve their interface (is_enabled() + target_persona_id()) — only the implementations change; JSONL schema is unchanged so existing logs stay readable; the bot's on_ready hook and close() stop call must be replaced with setup_hook task creation and shutdown(); race on simultaneous start.cmd is handled by bot-side rejection; sidecar crash detection needs either psutil or a heartbeat file (recommend heartbeat — no new dep)
files-touched: Projects/dba/.design/columnist-ride-along-2026-05-21.md (overwritten v1 with v2)
next-suggested-agent: backend-dev
blockers: none
=== END HANDOFF ===
