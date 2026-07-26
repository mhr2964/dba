from __future__ import annotations

import discord

from phase.guards import all_humans_ready, no_pending_trades
from phase.transitions import is_allowed, allowed_phases_for
from data.db import get_pool
from data.repositories import league_repo
from core.errors import DBAError, PhaseError

PHASE_SUGGESTIONS: dict[str, str] = {
    "SETUP": "Use `/league advance` to move to PRESEASON_READY, then `/season start` to begin.",
    "PRESEASON_READY": "Use `/season start` to create the schedule and start the season.",
    "REGULAR_SEASON_ACTIVE": "The regular season is underway. Use `/sim to-next-rival` or `/sim to-end`.",
    "TRADE_DEADLINE_OPEN": "The trade deadline window is open — make your moves before it closes.",
    "REGULAR_SEASON_POSTDEADLINE": "Trade deadline passed. Continue simming with `/sim to-end`.",
    "REGULAR_SEASON_COMPLETE": "Regular season over. The commissioner should advance to play-in/playoffs.",
    "PLAYIN_ACTIVE": "Play-in tournament is live. Use `/playoffs sim playin` to advance.",
    "PLAYOFFS_R1": "First round of playoffs is active. Use `/playoffs sim round` to advance.",
    "PLAYOFFS_R2": "Second round of playoffs is active. Use `/playoffs sim round` to advance.",
    "CONFERENCE_FINALS": "Conference finals are active. Use `/playoffs sim round` to advance.",
    "NBA_FINALS": "The DBA Finals are happening! Use `/playoffs sim round` to crown a champion.",
    "OFFSEASON_AWARDS_OPEN": "Awards voting is open. Use `/awards vote` to cast your ballot.",
    "OFFSEASON_AWARDS_CLOSED": "Use `/awards open` to begin voting or advance to draft prep.",
    "DRAFT_LOTTERY_DONE": "Lottery complete. Commissioner should advance to DRAFT_IN_PROGRESS.",
    "DRAFT_IN_PROGRESS": "Draft is live. Use `/draft pick` to select players.",
    "POST_DRAFT_TRADES_OPEN": "Post-draft trade window. Use `/trade propose` to make moves.",
    "FA_OPEN": "Free agency is open. Use `/fa offer` to bid on players.",
    "FA_CLOSED": "Free agency closed. Check waivers or wait for the next phase.",
    "WAIVERS_OPEN": "Waiver wire is open. Use `/waiver claim` to pick up players.",
    "PROGRESSION_PENDING": "Season progression pending. Commissioner should run `/progression run`.",
}

# Maps the current phase to the single next command the commissioner should run.
# Appended to PhaseError messages so users are never left wondering what to do next.
_NEXT_COMMAND_HINT: dict[str, str] = {
    "OFFSEASON_AWARDS_CLOSED": "Run `/awards open` to start end-of-season voting.",
    "OFFSEASON_AWARDS_OPEN": "Run `/awards close` to tally votes.",
    "DRAFT_LOTTERY_DONE": "Run `/draft start` to begin the draft.",
    "DRAFT_IN_PROGRESS": "Run `/draft pick` to make a pick.",
    "POST_DRAFT_TRADES_OPEN": "Run `/sim to-deadline` or `/league advance` to open free agency.",
    "FA_OPEN": "Run `/fa advance` to process FA signings.",
    "FA_CLOSED": "Run `/waivers open` to begin waiver claims.",
    "WAIVERS_OPEN": "Run `/waiver claim` or `/league advance` to close waivers.",
    "PROGRESSION_PENDING": "Run `/progression run` to apply player development.",
    "PRESEASON_READY": "Run `/season start` to begin the next season.",
    "REGULAR_SEASON_COMPLETE": "Run `/playoffs seed` to start the playoffs.",
    "PLAYIN_ACTIVE": "Run `/playoffs sim playin` to resolve the play-in.",
    "PLAYOFFS_R1": "Run `/playoffs sim round` to sim the next round.",
    "PLAYOFFS_R2": "Run `/playoffs sim round` to sim the next round.",
    "CONFERENCE_FINALS": "Run `/playoffs sim round` to sim the Conference Finals.",
    "NBA_FINALS": "Run `/playoffs sim round` to sim the DBA Finals.",
}


async def get_league_or_error(guild_id: int) -> league_repo.League:
    """Fetch active league for guild or raise DBAError."""
    pool = await get_pool()
    league = await league_repo.get_by_guild(pool, guild_id)
    if not league:
        raise DBAError("No active league in this server. Use `/league create` to start one.")
    return league


async def require_commissioner(interaction: discord.Interaction, league: league_repo.League) -> None:
    """Raise DBAError if the invoking user is not the commissioner."""
    if interaction.user.id != league.commissioner_user_id:
        raise DBAError("Only the league commissioner can do that.")


async def require_phase(league: league_repo.League, command_name: str) -> None:
    """Raise PhaseError if the current phase doesn't allow this command."""
    if not is_allowed(command_name, league.current_phase):
        allowed = ", ".join(f"`{p}`" for p in allowed_phases_for(command_name))
        suggestion = PHASE_SUGGESTIONS.get(league.current_phase, "")
        next_cmd = _NEXT_COMMAND_HINT.get(league.current_phase, "")
        parts = [f" {suggestion}" if suggestion else ""]
        if next_cmd:
            parts.append(f" **Next:** {next_cmd}")
        hint = "".join(p for p in parts if p)
        raise PhaseError(
            f"**`/{command_name.replace('_', ' ')}`** is not available in phase `{league.current_phase}`.\n"
            f"Available in: {allowed or 'N/A'}.{hint}"
        )


async def require_all_ready(interaction: discord.Interaction, league: league_repo.League) -> None:
    """Raise DBAError listing unready managers if not all are ready."""
    pool = await get_pool()
    all_ready, unready_team_ids = await all_humans_ready(pool, league.id)
    if not all_ready:
        rows = await pool.fetch(
            "SELECT manager_user_id FROM teams WHERE id = ANY($1::int[])",
            unready_team_ids,
        )
        mentions = ", ".join(
            f"<@{r['manager_user_id']}>" for r in rows if r["manager_user_id"]
        )
        raise DBAError(f"Waiting for managers to `/team ready`: {mentions}")


async def require_no_pending_trades(league: league_repo.League) -> None:
    """Raise DBAError if trades are pending commissioner action."""
    pool = await get_pool()
    clean, count = await no_pending_trades(pool, league.id)
    if not clean:
        raise DBAError(
            f"{count} trade(s) are pending your approval. "
            f"Use `/trades pending` to review before simming."
        )
