"""Side-effect hooks invoked at batch-flush points during sim_orchestrator's
sim_until_rival/sim_range loops -- phase transitions (trade deadline open/close,
season-complete advance + auto-awards) and the periodic team-state snapshot.

Extracted from batch_sim_runner.py (now sim_orchestrator.py) as a pure move,
same "maybe X at this batch point" shape as sim_content_pipeline.py's
_maybe_post_* functions, just for administrative side effects instead of
columnist content. No behavior change from the move itself.
"""
from __future__ import annotations

from typing import Optional

import discord

from core.logging import get_logger
from bot.embeds import sim_embeds
from data.repositories import game_repo
from phase.states import Phase
from services import awards_service, league_service, team_intel

log = get_logger(__name__)


async def _maybe_snapshot_teams(
    pool,
    league_id: int,
    season: int,
    sim_batch_index: int,
) -> None:
    """Write a team_state_snapshots row for each team in the league.

    Swallows exceptions so a snapshot failure never aborts the sim.
    Called at every batch flush point in sim_until_rival and sim_range.
    """
    try:
        count = await team_intel.snapshot_all_teams(pool, league_id, season, sim_batch_index)
        log.debug(f"Snapshot written: {count} rows (batch={sim_batch_index})")
    except Exception as exc:
        log.warning(f"_maybe_snapshot_teams failed silently: {exc}")


async def _maybe_advance_trade_deadline(
    pool,
    league_id: int,
    current_game_index: int,
    deadline_game_index: Optional[int],
    news_channel: Optional[discord.TextChannel] = None,
) -> None:
    """Auto-advance phase to TRADE_DEADLINE_OPEN when the sim passes the deadline game index.
    Re-reads phase from DB to avoid stale local state firing this multiple times per sim run."""
    if not deadline_game_index or current_game_index < deadline_game_index:
        return
    row = await pool.fetchrow("SELECT current_phase FROM leagues WHERE id = $1", league_id)
    if not row or row["current_phase"] != Phase.REGULAR_SEASON_ACTIVE.value:
        return
    try:
        await league_service.advance_phase(league_id, Phase.TRADE_DEADLINE_OPEN.value)
        log.info(
            f"Trade deadline opened for league {league_id} at game index {current_game_index}"
        )
        if news_channel:
            embed = discord.Embed(
                title="🚨 Trade Deadline Is Open",
                description=(
                    "The trade window is now open. Use `/trade propose` to negotiate deals.\n"
                    "When ready, run `/sim games count:5` (or `/sim season`) to close the window and resume."
                ),
                color=discord.Color.orange(),
            )
            try:
                await news_channel.send(embed=embed)
            except Exception:
                pass
    except Exception as exc:
        log.warning(f"_maybe_advance_trade_deadline failed: {exc}")


async def _auto_run_awards(
    pool,
    league_id: int,
    season: int,
    news_channel: Optional[discord.TextChannel],
) -> None:
    """
    Auto-open, CPU-vote, and close the four individual awards (MVP, DPOY, ROY, 6MOY)
    immediately when the regular season ends.  Posts an announcement embed to
    #league-news with all four winners.

    This runs synchronously (awaited) inside _maybe_advance_season_complete so that
    winners are recorded before the season-complete message goes out.
    """
    _AWARD_TYPES = ["mvp", "dpoy", "roy", "6moy"]
    _AWARD_LABELS = {
        "mvp":  "MVP",
        "dpoy": "DPOY",
        "roy":  "ROY",
        "6moy": "6th Man",
    }

    winners: list[tuple[str, int]] = []  # (award_label, player_id)
    no_winner_labels: list[str] = []     # award labels with no eligible candidates

    for award_type in _AWARD_TYPES:
        try:
            voting_id = await awards_service.open_voting(league_id, season, award_type)
            log.info(f"Auto-awards: opened {award_type} voting (id={voting_id}) for league {league_id}")

            votes_cast = await awards_service.generate_cpu_votes(voting_id, league_id, season)
            log.info(f"Auto-awards: {votes_cast} CPU votes cast for {award_type}")

            results = await awards_service.close_voting(voting_id)
            log.info(f"Auto-awards: closed {award_type} voting; winner player_id={results[0]['player_id'] if results else None}")

            if results:
                winners.append((_AWARD_LABELS[award_type], results[0]["player_id"]))
            else:
                # No eligible players voted on (e.g. no rookies for ROY).
                no_winner_labels.append(_AWARD_LABELS[award_type])
                log.info(f"Auto-awards: no winner for {award_type} (no eligible players)")
        except Exception as exc:
            log.warning(f"Auto-awards: {award_type} pipeline failed: {exc}", exc_info=True)
            no_winner_labels.append(_AWARD_LABELS[award_type])

    if not winners and not no_winner_labels:
        return
    if not news_channel:
        return

    # Resolve player names.
    player_ids = [pid for _, pid in winners]
    try:
        name_rows = await pool.fetch(
            "SELECT id, first_name, last_name FROM players WHERE id = ANY($1)",
            player_ids,
        )
        names: dict[int, str] = {r["id"]: f"{r['first_name']} {r['last_name']}" for r in name_rows}
    except Exception as exc:
        log.warning(f"Auto-awards: name lookup failed: {exc}", exc_info=True)
        names = {}

    lines = [
        f"**{label}:** {names.get(pid, f'Player #{pid}')}"
        for label, pid in winners
    ]
    for label in no_winner_labels:
        lines.append(f"**{label}:** No eligible players")
    embed = discord.Embed(
        title="Season Awards",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    embed.set_footer(text=f"Season {season} — voted by CPU GMs")
    try:
        await news_channel.send(embed=embed)
    except Exception as exc:
        log.warning(f"Auto-awards: announcement post failed: {exc}", exc_info=True)


async def _maybe_advance_season_complete(
    pool,
    league_id: int,
    season: int,
    news_channel: Optional[discord.TextChannel],
    guild: Optional[discord.Guild] = None,
) -> bool:
    """
    If all regular season games are now simmed, advance the league phase to
    REGULAR_SEASON_COMPLETE, auto-run the four individual awards, and post
    an announcement.
    Returns True when the phase was advanced.

    PT4: re-checks current_phase before advancing, mirroring the pattern its
    sibling hooks (_maybe_advance_trade_deadline, _maybe_close_trade_window)
    already use. Previously this fired unconditionally once all games were
    complete -- a single long sim call that crosses both the trade deadline
    and season-end boundaries in the same call (deadline opened mid-call via
    _maybe_advance_trade_deadline, then the season finishes before
    _maybe_close_trade_window ever gets to run at the *start* of a
    subsequent call) landed straight on REGULAR_SEASON_COMPLETE from
    TRADE_DEADLINE_OPEN, silently skipping REGULAR_SEASON_POSTDEADLINE.
    """
    if not await game_repo.all_regular_season_games_complete(pool, league_id, season):
        return False

    row = await pool.fetchrow("SELECT current_phase FROM leagues WHERE id = $1", league_id)
    current_phase = row["current_phase"] if row else None

    if current_phase == Phase.TRADE_DEADLINE_OPEN.value:
        # Same boundary-crossing bug class as _maybe_close_trade_window guards
        # against -- step through REGULAR_SEASON_POSTDEADLINE here too instead
        # of jumping straight to REGULAR_SEASON_COMPLETE.
        await league_service.advance_phase(league_id, Phase.REGULAR_SEASON_POSTDEADLINE.value)
        log.info(
            f"League {league_id} season {season}: trade window auto-closed "
            "mid-call (deadline and season-end crossed in the same sim call) "
            "before advancing to REGULAR_SEASON_COMPLETE"
        )
        current_phase = Phase.REGULAR_SEASON_POSTDEADLINE.value

    if current_phase not in (Phase.REGULAR_SEASON_ACTIVE.value, Phase.REGULAR_SEASON_POSTDEADLINE.value):
        # Already advanced past this point (re-entrant call on an already-complete
        # season) or in some unexpected phase -- don't blindly stomp current_phase.
        return False

    await league_service.advance_phase(league_id, Phase.REGULAR_SEASON_COMPLETE.value)
    log.info(f"League {league_id} season {season}: auto-advanced to REGULAR_SEASON_COMPLETE")

    # Auto-run awards before the season-complete message so winners are ready.
    try:
        await _auto_run_awards(pool, league_id, season, news_channel)
    except Exception as exc:
        log.warning(f"_auto_run_awards failed: {exc}", exc_info=True)

    if news_channel:
        await news_channel.send(embed=sim_embeds.regular_season_complete_embed())
    return True


async def _maybe_close_trade_window(pool, league_id: int, news_channel=None) -> None:
    """If the league is in TRADE_DEADLINE_OPEN, advance to REGULAR_SEASON_POSTDEADLINE
    so sim commands can run. Called at the start of any sim entry-point."""
    row = await pool.fetchrow("SELECT current_phase FROM leagues WHERE id = $1", league_id)
    if row and row["current_phase"] == Phase.TRADE_DEADLINE_OPEN.value:
        await league_service.advance_phase(league_id, Phase.REGULAR_SEASON_POSTDEADLINE.value)
        log.info(f"Trade window closed for league {league_id} — resuming regular season")
        if news_channel:
            try:
                await news_channel.send(embed=discord.Embed(
                    title="⏰ Trade Deadline Closed",
                    description="The trade window has closed. The regular season resumes.",
                    color=discord.Color.blurple(),
                ))
            except Exception:
                pass
