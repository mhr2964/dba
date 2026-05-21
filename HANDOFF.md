# HANDOFF — dba

Forward-looking handoff for the columnist ride-along v2 work-stream.

```yaml
last-model: claude-sonnet-4-6
last-session: 2026-05-21
state: green
```

## Next action

Hand off to user for first-run smoke test (see spec §11 for the checklist).
Critic round 2 fixes are committed — see "Round 2 fixes" section below.

## Known issues / v2 backlog

Critic round 2 HIGHs and NITs that were intentionally deferred:

- **#2 Pause UUID is article_id** — acceptable; 6-hex random suffix prevents
  spoofing in practice since the sidecar must know the exact article_id to fake
  a matching feedback payload.
- **#5 `_HEARTBEAT_MISS_COUNT` is module-global, not reset on attach** — safe
  today (only one sidecar at a time), fragile if re-attach semantics change.
- **#6 `_sidecar_alive` race during attach window** — narrow window between
  start.cmd accepted and first heartbeat file written; hard to trigger in
  practice.
- **NITs #8–13** — not fixed; tracked here for next reviewer pass.
  (e.g., spinner not cleared before pause banner in .sh, `:tag` re-emit of
  pause.json still races on mid-write pause.json, etc.)

## Round 2 fixes (critic review 2026-05-21)

Four commits (new, not amended):

1. **Fix self-comparison bug in PS1 sidecar restart detection** — captures
   `$ORIGINAL_BOT_PID` after attach; loop compares against it. SH sidecar
   also gets PID-change check (was missing entirely) plus explicit detached-
   status check.
2. **Decouple state.json write from 300 ms poll loop** — periodic heartbeat
   every 5 s (`_STATE_WRITE_INTERVAL_SEC`); transition writes remain immediate.
   Drops ~288 K idle writes/day to ~17 K. Cold-sweep sentinel moved to retry
   semantics: a transient mkdir failure no longer permanently disables the
   feature.
3. **Fix feedback read-then-delete race** — order is now read → validate →
   apply → delete. Mid-write None returns skip (retry); invalid payload deletes
   without applying; valid payload applies first then deletes.
4. **Make cold_sweep retry on failure** — `_cold_sweep_ipc_dir` now raises
   on mkdir failure; `_cold_sweep_done` only set True after success; outer
   except loop retries until it works.

## Traps

- `_pending_event` and `_pending_article_id` are module-level globals mutated
  by both `request_pause()` (coroutine) and `ipc_watch_task()` (background
  coroutine). Both run on the same asyncio loop — no threading race — but any
  future refactor that introduces threads must add a lock.
- `_apply_stop_cmd` resets `_fires`, `_pauses_seen`, and `_LOG_FILE` to 0/None.
  If the same persona re-attaches later in the same bot lifetime, `_get_log_file()`
  re-builds the path with `_SESSION_TS` (bot-start timestamp) — same file, correct.
- `shutdown()` is unconditional (no `is_enabled()` guard). The old `stop()` was
  guarded. Any caller that still calls `stop()` will get AttributeError — but
  grep confirms no live callers remain.
- PS1 sidecar: `:tag` and `:sev` restore `pause.json` so the user can answer the
  current pause after setting the modifier. The bot only acts on `feedback.json`,
  not on `pause.json` absence, so the momentary delete-then-restore is safe.
- Heartbeat timeout is 10 s; sidecar touches every 300 ms; 3 consecutive misses
  (~0.9 s) trigger drain. Fast recovery from a hard-killed sidecar is intentional.
- `should_stop()` was removed from the public API. v1 callers that checked this
  between batches no longer have a drain-mode signal — but none exist in
  batch_sim_runner.py (grep confirmed).

## Do not touch

- `services/batch_sim_runner.py` hook sites — no changes needed; the
  `is_enabled()` / `target_persona_id()` interface is preserved verbatim.
- `services/columnist_service.py` — `_capture_prompt` param unchanged.

## Recent context

- Architect (claude-opus-4.7) wrote the v2 spec at
  `Projects/dba/.design/columnist-ride-along-2026-05-21.md`.
  Key pivot: attach-only bot; sidecar owns stdin; file IPC replaces env vars.
- Four commits landed 2026-05-21:
  - `7493528` — rewrite `columnist_ride_along.py` (no stdin thread; ipc_watch_task; shutdown)
  - `6ecd2f4` — wire v2 into `bot/client.py` (setup_hook task, close→shutdown)
  - `814c8b3` — sidecar CLI (ps1 + sh): persona menu, attach, pause panel, :quit
  - `a5fea55` — delete v1: `columnist_ride_along.ps1/.sh`, `_columnist_ra_bootstrap.py`,
    desktop shortcut `DBA Columnist Ride-Along.lnk`
