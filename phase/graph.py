"""Phase-adjacency graph — the legal *next phase* for `league_service.advance_phase`.

Distinct concern from `phase/transitions.py`'s `ALLOWED` map: that module answers
"which phases may this *command* run in" (command -> eligible phases, used by
`phase/helpers.require_phase`). This module answers "which phase may a league
legally move *to* from its current phase" (phase -> legal next phases), which
nothing validated before this file existed (see docs/design/phase-transition-
logic-rules.md PT1, and RO9 in rollover-logic-rules.md, which first flagged
this gap and deferred it).

IMPORTANT: this is NOT simply "the next member in phase/states.py's Phase enum
declaration order" -- that naive model was checked against every real
`advance_phase` call site in the codebase (12 total: 11 hardcoded, 1 free-text
commissioner command) and does not hold. Two evidenced branch points exist
where the real system legitimately skips ahead:

  - `OFFSEASON_AWARDS_CLOSED` / `DRAFT_LOTTERY_DONE` -> `PROGRESSION_PENDING`:
    `rollover_service.run_rollover` (via `/offseason rollover`) fires from
    either of these two phases, skipping `DRAFT_IN_PROGRESS` and
    `POST_DRAFT_TRADES_OPEN` entirely when the commissioner rolls over before
    manually walking the league through the draft phases.
  - `NBA_FINALS` -> `OFFSEASON_AWARDS_CLOSED`: `playoff_cog.py`'s
    champion-decided auto-advance jumps straight here, skipping the
    `OFFSEASON_AWARDS_OPEN` voting phase (awards_cog.py's `/awards` commands
    never touch `current_phase` at all -- voting can run while the league is
    still nominally in `NBA_FINALS`, or the commissioner can manually advance
    to `OFFSEASON_AWARDS_OPEN` first via `/league advance`; both are legal).

The enum's declared order also doesn't hold for the season-loop-closing pair:
`PROGRESSION_PENDING` -> `FA_OPEN` (the real `/offseason progression` handler
call site) and `WAIVERS_OPEN` -> `PRESEASON_READY` (closing the loop into next
season, via manual `/league advance`, per `phase/helpers.py`'s own hint text)
are both real production transitions even though `PROGRESSION_PENDING` and
`WAIVERS_OPEN` are declared last/second-to-last in the enum.

Two additional phases have more than one legal next phase for the same
disclosed reason: `SETUP` (a league's very first season can skip
`PRESEASON_READY` entirely -- `/season start` accepts either phase) and
`REGULAR_SEASON_ACTIVE` (a league whose schedule has no computable trade-
deadline game index -- `game_repo.get_deadline_game_index` can return `None`
-- never enters `TRADE_DEADLINE_OPEN` at all, and `_maybe_advance_season_complete`
must be able to close the season directly from `REGULAR_SEASON_ACTIVE` in
that case).
"""
from __future__ import annotations

from phase.states import Phase

NEXT_PHASES: dict[Phase, frozenset[Phase]] = {
    Phase.SETUP: frozenset({Phase.PRESEASON_READY, Phase.REGULAR_SEASON_ACTIVE}),
    Phase.PRESEASON_READY: frozenset({Phase.REGULAR_SEASON_ACTIVE}),
    Phase.REGULAR_SEASON_ACTIVE: frozenset({Phase.TRADE_DEADLINE_OPEN, Phase.REGULAR_SEASON_COMPLETE}),
    Phase.TRADE_DEADLINE_OPEN: frozenset({Phase.REGULAR_SEASON_POSTDEADLINE}),
    Phase.REGULAR_SEASON_POSTDEADLINE: frozenset({Phase.REGULAR_SEASON_COMPLETE}),
    Phase.REGULAR_SEASON_COMPLETE: frozenset({Phase.PLAYIN_ACTIVE}),
    Phase.PLAYIN_ACTIVE: frozenset({Phase.PLAYOFFS_R1}),
    Phase.PLAYOFFS_R1: frozenset({Phase.PLAYOFFS_R2}),
    Phase.PLAYOFFS_R2: frozenset({Phase.CONFERENCE_FINALS}),
    Phase.CONFERENCE_FINALS: frozenset({Phase.NBA_FINALS}),
    Phase.NBA_FINALS: frozenset({Phase.OFFSEASON_AWARDS_OPEN, Phase.OFFSEASON_AWARDS_CLOSED}),
    Phase.OFFSEASON_AWARDS_OPEN: frozenset({Phase.OFFSEASON_AWARDS_CLOSED}),
    Phase.OFFSEASON_AWARDS_CLOSED: frozenset({Phase.DRAFT_LOTTERY_DONE, Phase.PROGRESSION_PENDING}),
    Phase.DRAFT_LOTTERY_DONE: frozenset({Phase.DRAFT_IN_PROGRESS, Phase.PROGRESSION_PENDING}),
    Phase.DRAFT_IN_PROGRESS: frozenset({Phase.POST_DRAFT_TRADES_OPEN}),
    Phase.POST_DRAFT_TRADES_OPEN: frozenset({Phase.PROGRESSION_PENDING}),
    Phase.PROGRESSION_PENDING: frozenset({Phase.FA_OPEN}),
    Phase.FA_OPEN: frozenset({Phase.FA_CLOSED}),
    Phase.FA_CLOSED: frozenset({Phase.WAIVERS_OPEN}),
    Phase.WAIVERS_OPEN: frozenset({Phase.PRESEASON_READY}),
}


def legal_next_phases(current_phase: str) -> list[str]:
    """Return the legal next phase value(s) from `current_phase`, or [] if
    `current_phase` is unrecognized or terminal."""
    try:
        current = Phase(current_phase)
    except ValueError:
        return []
    return sorted(p.value for p in NEXT_PHASES.get(current, frozenset()))


def is_legal_transition(current_phase: str, new_phase: str) -> bool:
    """True if `new_phase` is a legal next phase from `current_phase`."""
    try:
        current = Phase(current_phase)
        target = Phase(new_phase)
    except ValueError:
        return False
    return target in NEXT_PHASES.get(current, frozenset())
