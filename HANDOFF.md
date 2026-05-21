# HANDOFF — DBA

```yaml
last-model: claude-haiku-4.5
last-session: 2026-05-21
state: green
```

## Next action

Three landmark columnist fixes have been verified clean across 433 simulated games / 5 batches in dual-account testing. All silent failures (78 JSON serialization, 39 timeouts) resolved; columnists now posting at expected rates. State is green. Next session should focus on the 5 new UX nits (error-message leaks, missing progress logging, `/league advance` auto-promotion) and the 4 pre-existing duplication/data-leak issues documented in "Known issues / backlog" below.

## What landed this session (2026-05-21, claude-sonnet-4-6)

Three landmark fixes committed in separate commits:

1. **`c817246`** — `json.dumps(_context, indent=2, default=str)` in `services/columnist_service.py:369`. Fixes 78 silent article losses per season (58 datetime + 20 Decimal). The intel payload at line 290 already had `default=str`; the context payload at line 369 was missing it. Template dump at line 385 uses string literals and was fine.

2. **`b4d781d`** — Timeout caps bumped across all columnist `wait_for` calls in `services/batch_sim_runner.py`. `coach_beat` 8→15s, all other columnist generators (game_recap, darius_cole, marcus_brooks, power_list, rookie_watch, big_picture, the_ledger, the_race, triage_report, prelude) 8 or 10→20s. Fixes 39 timeouts per season.

3. **`dfaf23e`** — Marcus Cole trade-execution gate in `services/batch_sim_runner.py`. Restructured `if status == "approved" and _is_blockbuster_trade(...)` to an explicit `if status != "approved": log.debug(skip)` + `elif _is_blockbuster_trade(...)` pattern. Added observability log so next test run can verify the gate fires for pending_commissioner trades. Investigation found the original guard was already correct (canonical DB executed status is "approved", not "executed") — the test report's conflation of pending CPU↔Human trades with approved CPU↔CPU Marcus Cole articles was a snapshot timing/scroll issue. The gate restructure makes this verifiable in bot.log going forward.

## Traps

- Personas with custom shape MUST stay on `passthrough` renderer with body-template baked into `voice_notes`. Don't switch them back to exotic JSON shapes — Claude Haiku reliably leaves exotic keys blank and renderers produce stubs.
- Trade reports use ctx-driven asset blocks (Marcus Cole) — the swap structure comes from the trade context, NOT the LLM. Don't move asset rendering back into the LLM body.
- The JSON parser at `services/columnist_service.py:_tolerant_json_parse` was hardened to handle literal newlines inside body strings (Claude emits multi-line code blocks unescaped). If you touch the parser, preserve the `_escape_newlines_in_strings` retry path.
- DPOY scoring is impact-only (no age penalty, no role-tag adjustments). MVP has a conference-rank gate. Don't reintroduce role-based filters — user explicitly rejected "scorers can't win DPOY" and "tank-team players can win MVP."
- `/admin restart` uses `run.py` watchdog + `os._exit(42)`, NOT subprocess.Popen self-spawn. Every Popen variant tried this session failed silently from inside asyncio.
- Triage Report fires only for OVR ≥ 84 (stars). Role-player injuries still get the embed ping but no columnist article.
- Win streaks post to `#records`, not `#league-news`. Records channel is lazy-created via `_ensure_records_channel`.
- **Dual-account testing identity gate**: every `user-tester` dispatch MUST start with a `browser_evaluate` on Discord's bottom-left account panel and assert the username matches the expected account. Profile dirs cache auth tokens that survive in-page wipes — if there's any doubt, wipe `C:\Users\Owner\Desktop\AI\.playwright\profile1` and `profile2` first. Credentials in `system-secrets.md`: eyeleg → playwright1, foxplayer123 → playwright2, shared password ends in a trailing semicolon.
- `Marcus Cole signal enrichment failed: column "scoring_tendency" does not exist` appears in bot.log — this is a pre-existing bug in the signal-enrichment enrichment path (the player query references a column not in the schema). It is caught and non-fatal; Marcus Cole still generates the article from the remaining context. Do NOT treat it as a Fix 3 regression.

## Known issues / backlog

**New UX nits (emerged 2026-05-21 dual-account test):**
- (a) `/league create season:2025` errors with "season not supported. Run fetch_bdl_cache.py" — leaks internal dev tooling to Discord user. Should return user-facing error (e.g., "Season not available. Ask the commissioner to run data updates.").
- (b) `/sim run` user-matchup warning is ephemeral and reads as a silent hang — no progress indicator during run. Add streaming game-count updates or a "X of N games complete" embed.
- (c) `/team assign team_code:BOS user_id:<wrong>` swallows the lookup miss and gives a confusing reply — should surface "User not found" or "Team has no matching player slot."
- (d) `/league create` has no progress logging during the 30-team seed loop (~1 min black hole) — users see no feedback. Add a progress embed or checkmark reactions.
- (e) Flow `/league create` → `/team assign` → `/team ready` → `/sim run` is incomplete; bot rejects `/sim run` from SETUP phase. Missing `/league advance phase_name:PRESEASON_READY` + `/season start` steps. Suggestion: auto-advance after all managers ready to collapse the flow.

**Pre-existing FEELS-OFF (out of scope this round; next builder's priority):**
- Pat Chen POTM + Darius Cole Tank Watch show duplicate headlines (same story surface twice in quick succession). `_dedupe_headline` only covers non-persona renderers; persona prompts need filtering.
- Darius Cole `plan.goal` internal-data leak in prose — "Goal: (int) 67 wins" shows the schema key. Requires rewrite of persona prompt or post-processing mask.
- The Ledger monospace table wraps at Discord embed width (~60 chars) — horizontal scroll lost. Consider tab-separated-values render or a thread split.
- `#general` shows "Message could not be loaded" placeholder during `/sim run` ack→complete swap — ephemeral state transition briefly deletes the message. Low priority UX polish.

## Do not touch

- `services/columnist_service.py` `_tolerant_json_parse` — recently hardened; any change here ripples to all 17 personas.
- `services/personas/*.py` — wait until the architect's embed-shape design is in before tweaking individual voices; the embed redesign may make some prompt content redundant.
- `services/sim_engine.py` block tuning (anchor mult 1.50, blk_tendency /45, C weight 1.30, team total 5-9) — recently calibrated for NBA-realistic Wemby ~3.5 bpg; don't touch unless someone reports it's off.

## Recent context

- 17 personas registered (Maya Chen deleted prior session). All passthrough-based personas use `{headline, body}` JSON shape with body-template in `voice_notes`. See `services/personas/` for examples.
- Diagnostic logging is in place: `services/columnist_service.py` logs every raw LLM response at INFO with `[RAW]` prefix. Use `tail bot.log | grep "RAW response"` to see exactly what each persona returns.
- Discord token was rotated prior session — the chronic `defer 404` issue was a phantom bot using the old token. New token in `.env`.
- Bot restarted at 11:18:12 on 2026-05-21 after applying the three fixes. All 165 slash commands synced cleanly.
- Most recent commits (newest first): `dfaf23e` (marcus cole gate), `b4d781d` (timeout caps), `c817246` (json.dumps default=str), `105efc1` (parser handles raw newlines), `18bd5a9` (HTH NameError fix).
- Dual-account testing protocol at `Projects/dba/TESTING.md`. MCP servers (`playwright`, `playwright2`) configured in `.mcp.json` with `--executable-path` to bundled Chromium.
- Session note for the test run that surfaced these bugs: `Brain/General Session Notes/2026-05-21 - DBA Full Season Sim Test - 117 Silent Columnist Failures + 15 UI Issues.md`.
</content>
</invoke>