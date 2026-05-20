from __future__ import annotations

import asyncio
import os
import random
from typing import Optional

import discord

from core.logging import get_logger
from data.repositories import league_repo, player_repo, team_repo, trade_block_repo
from services import cpu_block_service
from services.cpu_trade_posture import _compute_team_posture, _default_posture
from services.cpu_trade_proposals import (
    _attempt_one_offer,
    _attempt_three_team_deal,
    _build_cpu_trade_block,
)

_HEADLESS = os.environ.get("DBA_HEADLESS_MODE") == "1"

log = get_logger(__name__)

# Phases in which CPU trade activity is allowed.
_ACTIVE_PHASES = frozenset({
    "REGULAR_SEASON_ACTIVE",
    "TRADE_DEADLINE_OPEN",
})

# Number of games before the deadline over which pressure ramps from 0 → 1.
# A window of 150 games means pressure builds starting from the midseason mark,
# giving steady CPU trade activity through the second half of the season.
_RAMP_WINDOW = 150

# Minimum baseline pressure so trades can fire even early in the season.
# Raised 0.08→0.30: at 0.08 the n_offers formula always resolved to 0 early in the
# season (int(0.08*2)=0, 4% probabilistic +1), producing zero trades across 100 games.
# At 0.30: int(0.30*2)=0 + 30% chance of +1 = ~0.30 offers/tick, which is realistic.
_BASELINE_PRESSURE = 0.30

# Pressure override during the open deadline window.
# Slightly above 0.8 to produce a burst of 5-8 trades at deadline.
_DEADLINE_OPEN_PRESSURE = 1.0

# Grade differential threshold above which a trade is flagged for commissioner
# review instead of being auto-approved.  Expressed as a fraction of max side.
_LOPSIDED_THRESHOLD = 0.30

# Number of trade proposals to attempt per mode per round.  Contending teams
# trade infrequently (they want to protect cores); rebuilding teams fire more.
_MODE_N_OFFERS: dict[str, int] = {
    "contending": 1,
    "play_in_fringe": 2,
    "developing": 2,
    "soft_rebuild": 3,
    "rebuilding": 4,
}


async def _get_recently_signed_player_ids(pool, league_id: int) -> set[int]:
    """
    Return player IDs whose contracts were signed within the last 60 sim games.

    60 games ≈ 144 calendar days at ~2.4 days/game. We anchor to the latest
    simmed game date rather than wall-clock time so the window tracks sim
    progress. Returns an empty set when signed_at data is unavailable (e.g.
    before migration 032 runs) so the caller can safely skip the check.

    NULL signed_at rows are ignored — they predate the column and carry no
    restriction (the migration back-fills them to 2000-01-01).
    """
    try:
        cutoff_row = await pool.fetchrow(
            """
            SELECT MAX(scheduled_date) AS last_game
            FROM games
            WHERE league_id = $1 AND status = 'simmed'
            """,
            league_id,
        )
        if not cutoff_row or not cutoff_row["last_game"]:
            return set()

        rows = await pool.fetch(
            """
            SELECT player_id
            FROM contracts
            WHERE league_id = $1
              AND is_active = TRUE
              AND signed_at IS NOT NULL
              AND signed_at > $2::date - INTERVAL '144 days'
            """,
            league_id,
            cutoff_row["last_game"],
        )
        return {r["player_id"] for r in rows}
    except Exception as exc:
        log.debug(f"_get_recently_signed_player_ids failed (skipping restriction): {exc}")
        return set()


async def _cpu_auto_populate_block(
    pool,
    league: league_repo.League,
    guild: Optional[discord.Guild] = None,
) -> None:
    """
    Refresh CPU teams' trade blocks and post new entries to #trade-block.

    Uses cpu_block_service.refresh_league for the heuristic selection (all
    per-mode rules live there).  After the refresh, posts each newly-listed
    player card to the channel so the channel stays active even without human
    managers running /trade block add.

    Only runs when the league is in an active-trading phase — cpu_block_service
    already guards against deadline-and-later phases, so we let it handle that.
    """
    try:
        result = await cpu_block_service.refresh_league(pool, league.id)
        if not guild or result["players_listed"] == 0:
            return

        block_ch_id = await league_repo.get_channel(pool, league.id, "trade-block")
        if not block_ch_id:
            return
        ch = guild.get_channel(block_ch_id)
        if not ch:
            return

        # Fetch all CPU block entries and post an embed per team that has entries.
        all_teams = await team_repo.get_all(pool, league.id)
        cpu_teams = [t for t in all_teams if t.manager_user_id is None]

        for team in cpu_teams:
            entries = await trade_block_repo.get_team_block(pool, league.id, team.id)
            if not entries:
                continue

            lines: list[str] = []
            for entry in entries:
                p = await player_repo.get_by_id(pool, entry["player_id"])
                if not p:
                    continue
                line = f"**{p.full_name}** — OVR {p.overall} | {p.position}"
                if entry.get("note"):
                    line += f"\n  _{entry['note']}_"
                lines.append(line)

            if not lines:
                continue

            mode_label = {
                "rebuilding": "Rebuilding",
                "soft_rebuild": "Soft Rebuild",
                "contending": "Contending",
                "play_in_fringe": "Play-In Fringe",
                "developing": "Developing",
            }.get(team.cpu_mode or "default", "CPU")

            embed = discord.Embed(
                title=f"{team.full_name} — Trade Block Update",
                description="\n\n".join(lines),
                color=discord.Color.orange(),
            )
            embed.set_footer(text=f"{mode_label} team | CPU-managed")
            try:
                await ch.send(embed=embed)
            except Exception as exc:
                log.warning(f"Failed to post CPU block update for team {team.id}: {exc}")

    except Exception as exc:
        log.warning(f"_cpu_auto_populate_block failed: {exc}", exc_info=True)


async def maybe_initiate_round(
    pool,
    league_id: int,
    season: int,
    current_game_index: int,
    total_regular_games: int,
    deadline_game_index: int,
    guild: Optional[discord.Guild] = None,
    refresh_block: bool = True,
) -> int:
    """
    Possibly initiate 0-N CPU-to-CPU trades based on deadline pressure.
    Returns number of trades proposed.
    Only runs during REGULAR_SEASON_ACTIVE and TRADE_DEADLINE_OPEN phases.

    refresh_block=False skips the cpu_block_service.refresh_league call that
    rebuilds all 28 CPU team trade blocks.  Pass False for mid-batch calls
    during bulk sims so the refresh only runs once per sim invocation (at the
    final flush) instead of once per game-day.
    """
    # Load league to confirm phase and get salary cap / season.
    league_row = await pool.fetchrow(
        "SELECT * FROM leagues WHERE id = $1", league_id
    )
    if not league_row:
        return 0

    league = league_repo._league_from_record(league_row)

    if league.current_phase not in _ACTIVE_PHASES:
        return 0

    # Refresh CPU trade blocks and post to #trade-block — gated by caller so this
    # fires only once per sim batch (final flush) rather than every game-day.
    if refresh_block:
        await _cpu_auto_populate_block(pool, league, guild)
    else:
        log.debug(f"maybe_initiate_round: skipping block refresh for mid-batch call (league {league_id})")

    # Compute deadline pressure.
    # Ramps from _BASELINE_PRESSURE → 1.0 over the last _RAMP_WINDOW games before deadline.
    # Baseline ensures at least some trade activity throughout the season.
    if league.current_phase == "TRADE_DEADLINE_OPEN":
        pressure = _DEADLINE_OPEN_PRESSURE
    else:
        games_until_deadline = deadline_game_index - current_game_index
        ramp = max(0.0, 1.0 - games_until_deadline / _RAMP_WINDOW)
        pressure = max(_BASELINE_PRESSURE, ramp)

    # Number of trade offers to attempt this round.
    # Formula: int(pressure * 2) + probabilistic +1.
    # At baseline (0.30): int(0.60)=0 + 30% chance = ~0.30 offers/tick.
    # At mid-ramp (0.65): int(1.30)=1 + 65% chance = ~1.65 offers/tick.
    # At deadline (1.00): int(2.00)=2 + 100% chance = 3 offers/tick.
    n_offers = int(pressure * 2) + (1 if random.random() < pressure else 0)
    if n_offers == 0:
        return 0

    # Load all teams; filter to CPU-only.
    all_teams = await team_repo.get_all(pool, league_id)
    cpu_teams = [t for t in all_teams if t.manager_user_id is None]

    # Fetch players under the 60-day sign-and-trade restriction upfront so we
    # can exclude them from both the trade block and return packages.
    recently_signed_ids = await _get_recently_signed_player_ids(pool, league_id)

    if len(cpu_teams) < 2:
        return 0

    # Exclude players already in any pending or approved trade this season so a
    # player can't appear in multiple CPU trade proposals across batch rounds.
    already_committed_rows = await pool.fetch(
        """
        SELECT DISTINCT ta.player_id
        FROM trade_assets ta
        JOIN trades t ON t.id = ta.trade_id
        WHERE t.league_id = $1
          AND t.season = $2
          AND ta.asset_type = 'player'
          AND t.status IN ('pending_counterparty', 'pending_commissioner')
          AND ta.player_id IS NOT NULL
        """,
        league_id,
        season,
    )

    # Precompute trade posture for every CPU team so _attempt_one_offer can use
    # live record + age context instead of the static cpu_mode column.
    # Computed before block building so plan-aware filtering can use urgency.
    posture_results = await asyncio.gather(
        *[_compute_team_posture(pool, league, t.id) for t in cpu_teams],
        return_exceptions=True,
    )
    postures: dict[int, dict] = {}
    for team, result in zip(cpu_teams, posture_results):
        if isinstance(result, Exception):
            postures[team.id] = _default_posture(team)
        else:
            postures[team.id] = result

    # Build a synthetic tradeable-player map from CPU team rosters.
    # CPU teams never call /trade block add, so we derive their block on the fly.
    # Postures are passed so plan-aware filtering can use urgency.
    block_by_team = await _build_cpu_trade_block(
        pool, league_id, season, cpu_teams, recently_signed_ids, postures,
    )
    if not block_by_team:
        return 0

    # Mode-driven n_offers: scale by pressure so sell-mode teams can mass-fire
    # without drowning out the global round count.  See _MODE_N_OFFERS constant.
    _mode_max = max(
        (_MODE_N_OFFERS.get(postures[t.id]["mode"], 2) for t in cpu_teams),
        default=2,
    )
    # Blend pressure-based count with mode max: take the larger of the two,
    # scaled by pressure so early-season stays quiet even if modes are active.
    n_offers = max(n_offers, round(_mode_max * pressure))
    # Absolute cap: prevent runaway batches at deadline + lots of rebuild teams.
    n_offers = min(n_offers, 8)

    proposed_count = 0
    used_pairs: set[tuple[int, int]] = set()
    taken_player_ids: set[int] = {row["player_id"] for row in already_committed_rows}

    for _ in range(n_offers):
        try:
            # 10% chance at high pressure: attempt a 3-team deal instead of a
            # standard 2-team offer.  Only fires when pressure >= 0.6 so it's
            # deadline-era behaviour, not a common occurrence.
            if pressure >= 0.6 and random.random() < 0.10:
                count = await _attempt_three_team_deal(
                    pool, league, season, cpu_teams, block_by_team,
                    used_pairs, taken_player_ids, recently_signed_ids, guild,
                    postures=postures,
                )
                proposed_count += count
                continue

            count = await _attempt_one_offer(
                pool, league, season, cpu_teams, block_by_team,
                used_pairs, taken_player_ids, deadline_game_index, recently_signed_ids, guild,
                postures=postures,
            )
            proposed_count += count
        except Exception as exc:
            log.warning(f"CPU trade offer attempt failed: {exc}", exc_info=True)
            continue

    return proposed_count
