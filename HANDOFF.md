# HANDOFF — DBA

```yaml
last-model: claude-opus-4.7
last-session: 2026-05-21
state: green
```

## Next action

All 17 columnists persona-redesigned per `.design/persona-redesign-2026-05-21.md` and verified 4/4 PASS on final dual-account testing. Focus next on the 5 new UX nits (error-message leaks, missing progress logging, phase auto-advance) and 4 pre-existing backlog items (duplication, data leaks, Ledger wrap, ephemeral state flicker). Known issues preserved below.

## Recent context

- Persona redesign spanned 14 commits (4 backend-dev rounds + 2 critic reviews + 3 user-tester rounds). Final verification landed green on all voice/structure/schema checks.
- Core pattern finalized: `_dedupe_headline` at render-time prevents duplication. Voice_notes must forbid emitting schema keys verbatim and translate to prose (Darius Cole, Coach Beat live examples).
- Renderer headline-prepend pattern locked: Discord embed titles already show headlines; renderers must NOT prepend `**{headline}**` to body. Fixed in `_assemble_tank_watch`, `_assemble_potm`, `_assemble_trade_report`; `_assemble_passthrough` follows convention.
- Power List tier-ranking (1-10 arrows), Pat Chen `[EAST]/[WEST]/[CLOSER]` markers, Marcus Cole `[TEAM_A]/[TEAM_B]` markers, Coach Beat "Coach in focus" framing, Rookie Watch 🥇/🥈 medal opener all verified clean.
- Ledger phase-gate inverted (whitelist trade-era phases); fires exactly once 4 seconds after `TRADE_DEADLINE_OPEN` transition; `_ledger_first_post_done` keyed by `(league_id, season)` to re-arm each season.

## Traps

- **Renderer headline-prepend pattern**: Discord embed title already shows the headline. Renderers must NOT prepend `**{headline}**` to body. `_assemble_tank_watch`, `_assemble_potm`, `_assemble_trade_report`, and `_assemble_passthrough` follow this convention. New renderers should too.
- **Voice_notes that lean on context schema keys**: prompts must explicitly forbid emitting schema keys/codes verbatim and translate them to natural prose. Darius Cole (marker translation, emoji rules with 🚨 HARD RULE anchors), Coach Beat (no "secondary_creator" or "two_way_big" schema leaks) are the live examples.
- Personas with custom shape MUST stay on `passthrough` renderer with body-template baked into `voice_notes`. Don't switch them back to exotic JSON shapes — Claude models reliably leave exotic keys blank and renderers produce stubs.
- Trade reports use ctx-driven asset blocks (Marcus Cole) — the swap structure comes from the trade context, NOT the LLM. Don't move asset rendering back into the LLM body.
- The JSON parser at `services/columnist_service.py:_tolerant_json_parse` was hardened to handle literal newlines inside body strings (Claude emits multi-line code blocks unescaped). If you touch the parser, preserve the `_escape_newlines_in_strings` retry path.
- DPOY scoring is impact-only (no age penalty, no role-tag adjustments). MVP has a conference-rank gate. Don't reintroduce role-based filters — user explicitly rejected "scorers can't win DPOY" and "tank-team players can win MVP."
- `/admin restart` uses `run.py` watchdog + `os._exit(42)`, NOT subprocess.Popen self-spawn. Every Popen variant tried this session failed silently from inside asyncio.
- Triage Report fires only for OVR ≥ 84 (stars). Role-player injuries still get the embed ping but no columnist article.
- Win streaks post to `#records`, not `#league-news`. Records channel is lazy-created via `_ensure_records_channel`.
- **Dual-account testing identity gate**: every `user-tester` dispatch MUST start with a `browser_evaluate` on Discord's bottom-left account panel and assert the username matches the expected account. Profile dirs cache auth tokens that survive in-page wipes — if there's any doubt, wipe `C:\Users\Owner\Desktop\AI\.playwright\profile1` and `profile2` first. Credentials in `system-secrets.md`: eyeleg → playwright1, foxplayer123 → playwright2, shared password ends in a trailing semicolon.
- `Marcus Cole signal enrichment failed: column "scoring_tendency" does not exist` appears in bot.log — this is a pre-existing bug in the signal-enrichment path (the player query references a column not in the schema). It is caught and non-fatal; Marcus Cole still generates the article from the remaining context. Do NOT treat it as a regression.

## Known issues / backlog

**New UX nits (emerged 2026-05-21 dual-account test):**
- (a) `/league create season:2025` errors with "season not supported. Run fetch_bdl_cache.py" — leaks internal dev tooling to Discord user. Should return user-facing error (e.g., "Season not available. Ask the commissioner to run data updates.").
- (b) `/sim run` user-matchup warning is ephemeral and reads as a silent hang — no progress indicator during run. Add streaming game-count updates or a "X of N games complete" embed.
- (c) `/team assign team_code:BOS user_id:<wrong>` swallows the lookup miss and gives a confusing reply — should surface "User not found" or "Team has no matching player slot."
- (d) `/league create` has no progress logging during the 30-team seed loop (~1 min black hole) — users see no feedback. Add a progress embed or checkmark reactions.
- (e) Flow `/league create` → `/team assign` → `/team ready` → `/sim run` is incomplete; bot rejects `/sim run` from SETUP phase. Missing `/league advance phase_name:PRESEASON_READY` + `/season start` steps. Suggestion: auto-advance after all managers ready to collapse the flow.

**Pre-existing FEELS-OFF (out of scope this round; next builder's priority):**
- Rookie Watch 🥇/🥈 medals: round-3 user-tester observed them appearing in JSON but not always in render. Round 4 didn't surface (no Rookie Watch post in that window). May be a Discord-side display quirk or LLM variance. Backend-dev round 3 confirmed no code path strips them. Leave as backlog "look again if it recurs."
- **`_assemble_moment`, `_assemble_verdict`, `_assemble_index` latent duplication bug**: these three in `services/columnist_service.py` also prepend `**{headline}**` like the now-fixed renderers, but no persona currently uses those `format_style` values. If any new persona is added with those styles, the duplication bug returns. Document as "unreachable but latent."
- The Ledger monospace table wraps at Discord embed width (~60 chars) — horizontal scroll lost. Consider tab-separated-values render or a thread split.
- `#general` shows "Message could not be loaded" placeholder during `/sim run` ack→complete swap — ephemeral state transition briefly deletes the message. Low priority UX polish.

## Do not touch

- `services/columnist_service.py` `_tolerant_json_parse` — recently hardened; any change here ripples to all 17 personas.
- `services/personas/*.py` — persona voice redesign complete; prompts are locked. Wait for any architect embeds redesign before tweaking.
- `services/sim_engine.py` block tuning (anchor mult 1.50, blk_tendency /45, C weight 1.30, team total 5-9) — recently calibrated for NBA-realistic Wemby ~3.5 bpg; don't touch unless someone reports it's off.
