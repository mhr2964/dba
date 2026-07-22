# DBA Testing Protocol

## Overview

Full end-to-end testing uses two Discord accounts simultaneously via two Playwright MCP servers. One account is the commissioner, one is a team manager. Together they cover every user-user and user-CPU interaction in the sim.

## Accounts

Credentials live in `C:\Users\Owner\Desktop\AI\System\system-secrets.md`.

| Role | Discord Username | Email Key | MCP Server |
|---|---|---|---|
| Commissioner (Eyeleg) | Eyeleg | `Discord Eyeleg email` | `mcp__playwright__` |
| Team Manager (foxplayer123) | foxplayer123 | `Discord foxplayer123 email` | `mcp__playwright2__` |

Shared password key: `Discord shared password` (note: includes trailing semicolon).

## How to Run

Trigger via `>>team` keyword with user-tester as the active MCP agent. The user-tester agent has both `mcp__playwright__` and `mcp__playwright2__` in its tool list and handles both accounts in a single session.

**Standard invocation:**
```
>>team run full DBA dual-account test — purge, create league, assign teams, sim to playoffs
```

## Setup Flow (run every test)

1. **Purge** — Commissioner runs `/admin purge-server confirm:CONFIRM`  
   Wipes all DBA channels, roles, and the league DB row (cascades to players, games, standings).

2. **Create League** — Commissioner runs `/league create season:2024`  
   Imports 2024 rosters, generates schedule, creates channels and roles.

3. **Assign Teams** — Commissioner assigns a team to foxplayer123 via `/team assign`  
   Commissioner keeps their own team too.

4. **Ready Up** — Both accounts use `/ready` when prompted to start the regular season.

5. **Sim** — Commissioner runs `/sim advance` batches. User account uses `/ready` for user-vs-user games.

## What to Test

### Each test run covers:
- Commissioner-only commands work (phase transitions, reseed, rollback)
- Manager-only commands work for both accounts (roster, lineup, trade block, directives)
- User-vs-CPU games sim correctly
- User-vs-User games require both managers `/ready` before simming
- Trade flow: one manager proposes, other accepts/rejects
- Free agency: both managers can bid
- Standings, box scores, and columnist articles post correctly

### Run until:
- Regular season completes (or 50+ games simmed showing clean results)
- Playoffs bracket generates correctly
- **Stop at playoffs** unless specifically testing offseason/FA/draft

### Offseason test (separate run):
- Awards, FA, draft all trigger correctly
- Progression phase runs
- New season initializes cleanly

## Speed Rules for MCP

- Use `.fill()` / `browser_type` with the full string in one call — **never character by character**
- Use `browser_fill_form` for multi-field forms when possible
- After slash commands: type the command name, wait for Discord's autocomplete listbox, click the suggestion, then fill params — don't press Enter before the suggestion appears
- For dropdowns (e.g. season autocomplete): type the value, wait for the listbox `role=listbox`, click the matching option
- Take snapshots to orient, but don't snapshot after every single click — snapshot when you need to find a ref

## Known Issues / Gotchas

- Discord login via automated browser sometimes triggers "Login or password is invalid" — this is a session/cookie issue. If login fails, check that the password includes the trailing semicolon.
- The bot must be running before Discord commands will respond. Start with `python main.py` from `Projects/dba/`.
- Only one bot process should be running — multiple instances cause 404 Unknown Interaction errors on every command.
- League create requires clicking the season from the autocomplete listbox, not just typing and pressing Enter.
- `/team assign` requires selecting the user from a Discord user picker — type their username in the modal.

## Reporting

User-tester outputs two buckets:
- **BROKEN** — crashed, wrong data, command returned error, sim produced impossible stats
- **FEELS-OFF** — confusing flow, missing feedback, awkward UX

After the test, fixes go to the relevant builder agent. Re-test only the broken flows.
