from __future__ import annotations

import random
from typing import Optional

import discord

from core.logging import get_logger
from data.repositories import league_repo, player_repo, team_repo, trade_block_repo, trade_repo
from services import trade_evaluator, trade_service

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

# Minimum baseline pressure so trades can fire even early in the season
# (prevents a completely dead trade block for the first half of the year).
_BASELINE_PRESSURE = 0.15

# Pressure override during the open deadline window.
_DEADLINE_OPEN_PRESSURE = 1.2

# Grade differential threshold above which a trade is flagged for commissioner
# review instead of being auto-approved.  Expressed as a fraction of max side.
_LOPSIDED_THRESHOLD = 0.30


async def maybe_initiate_round(
    pool,
    league_id: int,
    season: int,
    current_game_index: int,
    total_regular_games: int,
    deadline_game_index: int,
    guild: Optional[discord.Guild] = None,
) -> int:
    """
    Possibly initiate 0-N CPU-to-CPU trades based on deadline pressure.
    Returns number of trades proposed.
    Only runs during REGULAR_SEASON_ACTIVE and TRADE_DEADLINE_OPEN phases.
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
    n_offers = int(pressure * 2) + (1 if random.random() < pressure else 0)
    if n_offers == 0:
        return 0

    # Load all teams; filter to CPU-only.
    all_teams = await team_repo.get_all(pool, league_id)
    cpu_teams = [t for t in all_teams if t.manager_user_id is None]

    # Build a synthetic tradeable-player map from CPU team rosters.
    # CPU teams never call /trade block add, so we derive their block on the fly.
    block_by_team = await _build_cpu_trade_block(pool, league_id, cpu_teams)
    if not block_by_team:
        return 0

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
          AND t.status NOT IN ('approved', 'rejected', 'declined', 'expired')
          AND ta.player_id IS NOT NULL
        """,
        league_id,
        season,
    )

    proposed_count = 0
    used_pairs: set[tuple[int, int]] = set()
    taken_player_ids: set[int] = {row["player_id"] for row in already_committed_rows}

    for _ in range(n_offers):
        try:
            count = await _attempt_one_offer(
                pool, league, season, cpu_teams, block_by_team, used_pairs, taken_player_ids, guild
            )
            proposed_count += count
        except Exception as exc:
            log.warning(f"CPU trade offer attempt failed: {exc}", exc_info=True)
            continue

    return proposed_count


async def _build_cpu_trade_block(
    pool,
    league_id: int,
    cpu_teams: list[team_repo.Team],
) -> dict[int, list[int]]:
    """
    For each CPU team, identify players that make sense to offer in a trade.
    Returns a map of team_id -> list of player_ids considered tradeable.

    CPU teams never manually populate the trade block, so this derives it from
    each team's roster and cpu_mode instead of querying trade_block entries.

    Logic per cpu_mode:
    - rebuilding: veterans age >= 32, or age >= 29 with OVR >= 65
    - contending: mid-tier players OVR 72–84 (trade bait, not franchise cornerstones)
    - developing: players age >= 30, or age >= 27 with OVR >= 78
    - default: players OVR 70–82
    """
    result: dict[int, list[int]] = {}

    for team in cpu_teams:
        players = await player_repo.get_roster(pool, league_id, team.id)
        tradeable: list[int] = []
        mode = team.cpu_mode or "default"

        for p in players:
            age = _player_age(p)
            ovr = p.overall

            if mode == "rebuilding":
                if age >= 32 or (age >= 29 and ovr >= 65):
                    tradeable.append(p.id)
            elif mode == "contending":
                # Trade non-star bench pieces for better role players.
                if 72 <= ovr <= 84:
                    tradeable.append(p.id)
            elif mode == "developing":
                if age >= 30 or (ovr >= 78 and age >= 27):
                    tradeable.append(p.id)
            else:
                if 70 <= ovr <= 82:
                    tradeable.append(p.id)

        if tradeable:
            result[team.id] = tradeable

    return result


async def _attempt_one_offer(
    pool,
    league: league_repo.League,
    season: int,
    cpu_teams: list[team_repo.Team],
    block_by_team: dict[int, list[int]],
    used_pairs: set[tuple[int, int]],
    taken_player_ids: set[int],
    guild: Optional[discord.Guild] = None,
) -> int:
    """
    Pick a team A, find the best target from team B, build a return package,
    and call trade_service.propose. Returns 1 if a proposal was made, 0 otherwise.
    """
    # Shuffle so we don't always favour the same team.
    candidates_a = random.sample(cpu_teams, len(cpu_teams))
    team_a: Optional[team_repo.Team] = None
    target_team: Optional[team_repo.Team] = None
    target_player: Optional[player_repo.Player] = None

    for a in candidates_a:
        # Team A needs something worth offering — it must have block entries or picks.
        a_picks = await trade_repo.get_team_picks(pool, league.id, a.id)
        a_block_ids = block_by_team.get(a.id, [])

        # Find a target (team B, player X) that team A actually wants.
        b_candidates = [t for t in cpu_teams if t.id != a.id]
        random.shuffle(b_candidates)

        for b in b_candidates:
            pair = (min(a.id, b.id), max(a.id, b.id))
            if pair in used_pairs:
                continue

            # Skip this team pair if they already have an active trade proposal
            # this season (any non-resolved status), preventing duplicate proposals
            # across batch rounds.
            active_pair_rows = await pool.fetch(
                """SELECT 1 FROM trades
                   WHERE league_id = $1 AND season = $2
                     AND ((proposer_team_id = $3 AND counterparty_team_id = $4)
                          OR (proposer_team_id = $4 AND counterparty_team_id = $3))
                     AND status NOT IN ('approved', 'rejected', 'declined', 'expired')
                   LIMIT 1""",
                league.id, season, a.id, b.id,
            )
            if active_pair_rows:
                continue

            b_block_ids = block_by_team.get(b.id, [])
            if not b_block_ids:
                continue

            # Load and score B's trade block players.
            for pid in b_block_ids:
                # Skip players already committed to another offer this round.
                if pid in taken_player_ids:
                    continue

                p = await player_repo.get_by_id(pool, pid)
                if not p:
                    continue

                if not _team_a_wants_player(a, p):
                    continue

                # Found a match.
                team_a = a
                target_team = b
                target_player = p
                break

            if target_player:
                break

        if team_a:
            break

    if not (team_a and target_team and target_player):
        return 0

    pair = (min(team_a.id, target_team.id), max(team_a.id, target_team.id))
    used_pairs.add(pair)

    # Optionally request a second player from B alongside the primary target.
    # Only attempted when the primary target is a star (OVR >= 75) and the 30%
    # dice roll hits.  The secondary must be rated below the primary and not
    # already committed elsewhere this round.  Falls back to single-player if
    # no valid secondary exists — never errors.
    secondary_target: Optional[player_repo.Player] = None
    if target_player.overall >= 75 and random.random() < 0.3:
        b_block_ids = block_by_team.get(target_team.id, [])
        for pid in b_block_ids:
            if pid == target_player.id:
                continue
            if pid in taken_player_ids:
                continue
            candidate = await player_repo.get_by_id(pool, pid)
            if candidate and candidate.overall < target_player.overall:
                secondary_target = candidate
                break

    # Build A's return package sized to match the combined value of all requested
    # players (primary + optional secondary) within the usual 25% tolerance.
    target_contract = await player_repo.get_active_contract(pool, target_player.id)
    target_value = trade_evaluator.player_trade_value(
        {"overall": target_player.overall, "age": _player_age(target_player)},
        {
            "salary": target_contract.salary if target_contract else 0,
            "years_remaining": target_contract.years_remaining if target_contract else 1,
        },
        league.salary_cap,
    )

    if secondary_target:
        secondary_contract = await player_repo.get_active_contract(pool, secondary_target.id)
        secondary_value = trade_evaluator.player_trade_value(
            {"overall": secondary_target.overall, "age": _player_age(secondary_target)},
            {
                "salary": secondary_contract.salary if secondary_contract else 0,
                "years_remaining": secondary_contract.years_remaining if secondary_contract else 1,
            },
            league.salary_cap,
        )
        target_value += secondary_value

    offer_player_ids, offer_pick_ids, package_value = await _build_return_package(
        pool,
        league,
        team_a,
        block_by_team.get(team_a.id, []),
        target_value,
        taken_player_ids,
    )

    if not offer_player_ids and not offer_pick_ids:
        log.debug(
            f"CPU trade skipped: team {team_a.id} has no assets to offer "
            f"for player {target_player.id} (value {target_value:.1f})"
        )
        return 0

    # Sanity check: only propose if the return package value is reasonably close
    # to the target value.  A package worth less than 50% or more than 200% of
    # the target is too lopsided to submit — this prevents absurd offers like
    # SGA for Zubac from ever reaching trade_service.propose.
    log.info(
        f"Trade check: target={target_value:.1f} package={package_value:.1f} "
        f"ratio={package_value / max(target_value, 1):.2f} "
        f"(team {team_a.id} → player {target_player.id} OVR {target_player.overall})"
    )
    if target_value > 0 and (
        package_value < target_value * 0.50 or package_value > target_value * 2.0
    ):
        log.info(
            f"CPU trade aborted (lopsided): team {team_a.id} package value "
            f"{package_value:.1f} vs target value {target_value:.1f}"
        )
        return 0

    counterparty_player_ids = [target_player.id]
    if secondary_target:
        counterparty_player_ids.append(secondary_target.id)

    log.info(
        f"CPU trade: team {team_a.id} ({team_a.cpu_mode}) "
        f"proposes to team {target_team.id} for player {target_player.id} "
        f"(OVR {target_player.overall})"
        + (
            f" + player {secondary_target.id} (OVR {secondary_target.overall})"
            if secondary_target else ""
        )
        + f" — combined value {target_value:.1f}, package value {package_value:.1f}"
    )

    trade = await trade_service.propose(
        league=league,
        proposer_team=team_a,
        counterparty_team=target_team,
        proposer_player_ids=offer_player_ids,
        proposer_pick_ids=offer_pick_ids,
        counterparty_player_ids=counterparty_player_ids,
        counterparty_pick_ids=[],
    )

    # Mark all players in this trade as taken so subsequent offers in this round
    # don't try to re-use them.
    taken_player_ids.add(target_player.id)
    if secondary_target:
        taken_player_ids.add(secondary_target.id)
    taken_player_ids.update(offer_player_ids)

    # trade_service.propose runs cpu_should_accept on target_team's side.
    # If accepted it lands as 'pending_commissioner'; auto-approve immediately for
    # CPU-CPU trades so they never pile up in the commissioner queue.
    if trade.status == "pending_commissioner":
        try:
            await _maybe_auto_approve(pool, league, trade, guild)
        except Exception as exc:
            log.error(
                f"CPU-CPU trade {trade.id} auto-approve failed — "
                f"trade remains pending_commissioner: {exc}",
                exc_info=True,
            )

    return 1


async def _maybe_auto_approve(
    pool,
    league: league_repo.League,
    trade,
    guild: Optional[discord.Guild] = None,
) -> None:
    """
    CPU-to-CPU trades: always auto-approve (no human is involved, no review needed).
    If either team has a human manager the trade stays pending_commissioner for human review.
    After approval, each involved team posts a "looking to deal" embed to #trade-block.
    """
    # Confirm both sides are CPU teams.
    teams = await pool.fetch(
        "SELECT id, manager_user_id FROM teams WHERE id = ANY($1)",
        [trade.proposer_team_id, trade.counterparty_team_id],
    )
    has_human = any(r["manager_user_id"] is not None for r in teams)
    if has_human:
        log.info(
            f"Trade {trade.id} involves a human-managed team — leaving as pending_commissioner"
        )
        return

    assets = await trade_repo.get_assets(pool, trade.id)

    # Rebuild value scores for both sides.
    proposer_value = 0.0
    counterparty_value = 0.0

    for asset in assets:
        if asset.asset_type == "player" and asset.player_id:
            p_row = await pool.fetchrow("SELECT * FROM players WHERE id = $1", asset.player_id)
            c_row = await pool.fetchrow(
                "SELECT salary, years_remaining FROM contracts "
                "WHERE player_id = $1 AND is_active = TRUE LIMIT 1",
                asset.player_id,
            )
            if p_row:
                age = _player_age_from_row(p_row)
                v = trade_evaluator.player_trade_value(
                    {"overall": p_row["overall"], "age": age},
                    {
                        "salary": c_row["salary"] if c_row else 0,
                        "years_remaining": c_row["years_remaining"] if c_row else 1,
                    },
                    league.salary_cap,
                )
                if asset.from_team_id == trade.proposer_team_id:
                    proposer_value += v
                else:
                    counterparty_value += v
        elif asset.asset_type == "pick" and asset.pick_id:
            pk_row = await pool.fetchrow(
                """SELECT dp.season, dp.round,
                          CASE WHEN (sc.wins + sc.losses) > 0
                               THEN sc.wins::float / (sc.wins + sc.losses)
                               ELSE NULL END AS win_pct
                   FROM draft_picks dp
                   LEFT JOIN standings_cache sc
                          ON sc.team_id = dp.original_team_id
                         AND sc.league_id = dp.league_id
                         AND sc.season = $2
                   WHERE dp.id = $1""",
                asset.pick_id,
                league.current_season,
            )
            if pk_row:
                v = trade_evaluator.pick_trade_value(
                    pk_row["season"], pk_row["round"], league.current_season,
                    team_win_pct=pk_row["win_pct"],
                )
                if asset.from_team_id == trade.proposer_team_id:
                    proposer_value += v
                else:
                    counterparty_value += v

    # Second lopsided-trade guard: even if the proposal got through, don't auto-approve
    # a trade where one side's value exceeds the other by more than 50%.
    # This catches edge cases where target_value was 0 at proposal time (contract lookup
    # failure) but the actual values are computable here.
    max_side = max(proposer_value, counterparty_value)
    min_side = min(proposer_value, counterparty_value)
    if max_side > 0 and min_side < max_side * 0.50:
        log.info(
            f"CPU-to-CPU trade {trade.id} blocked at auto-approve: lopsided "
            f"(proposer_value={proposer_value:.1f}, counterparty_value={counterparty_value:.1f}) "
            f"— leaving as pending_commissioner"
        )
        return

    # Always auto-approve CPU-to-CPU (human guard above ensures no human is involved).
    async with pool.acquire() as conn:
        async with conn.transaction():
            for asset in assets:
                if asset.asset_type == "player" and asset.player_id:
                    await conn.execute(
                        "UPDATE players SET team_id = $1 WHERE id = $2",
                        asset.to_team_id,
                        asset.player_id,
                    )
                    await conn.execute(
                        "UPDATE contracts SET team_id = $1 "
                        "WHERE player_id = $2 AND is_active = TRUE",
                        asset.to_team_id,
                        asset.player_id,
                    )
                    # Remove from old team's lineup so the sim engine stops
                    # using them for the wrong team.
                    await conn.execute(
                        "DELETE FROM lineups WHERE league_id = $1 AND player_id = $2",
                        trade.league_id,
                        asset.player_id,
                    )
                    # Insert into new team's lineup at the next available slot.
                    next_slot = await conn.fetchval(
                        "SELECT COALESCE(MAX(slot), 0) + 1 FROM lineups "
                        "WHERE league_id = $1 AND team_id = $2",
                        trade.league_id,
                        asset.to_team_id,
                    )
                    await conn.execute(
                        """INSERT INTO lineups
                               (league_id, team_id, is_starter, slot, player_id, set_by)
                           VALUES ($1, $2, FALSE, $3, $4, NULL)
                           ON CONFLICT (league_id, team_id, slot) DO NOTHING""",
                        trade.league_id,
                        asset.to_team_id,
                        next_slot,
                        asset.player_id,
                    )
                elif asset.asset_type == "pick" and asset.pick_id:
                    await conn.execute(
                        "UPDATE draft_picks SET current_team_id = $1 WHERE id = $2",
                        asset.to_team_id,
                        asset.pick_id,
                    )

            await conn.execute(
                """
                UPDATE trades
                SET status = 'approved',
                    resolved_at = NOW()
                WHERE id = $1
                """,
                trade.id,
            )

    traded_player_ids = [
        a.player_id for a in assets if a.asset_type == "player" and a.player_id
    ]
    if traded_player_ids:
        from data.repositories import trade_block_repo
        await trade_block_repo.remove_players_from_block(
            pool, trade.league_id, traded_player_ids
        )

    log.info(f"CPU-to-CPU trade {trade.id} auto-approved")

    # Post "looking to deal" embeds to #trade-block for each team involved.
    if guild:
        await _post_trade_block_ads(pool, league, trade, guild)


async def _post_trade_block_ads(
    pool,
    league: league_repo.League,
    trade,
    guild: discord.Guild,
) -> None:
    """
    After a CPU-to-CPU trade is approved, post one embed per involved team to
    #trade-block advertising what they're looking to acquire next.
    """
    news_ch_id = await league_repo.get_channel(pool, league.id, "trade-block")
    if not news_ch_id:
        return
    ch = guild.get_channel(news_ch_id)
    if not ch:
        return

    team_rows = await pool.fetch(
        "SELECT nba_team_code, cpu_mode FROM teams WHERE id = ANY($1)",
        [trade.proposer_team_id, trade.counterparty_team_id],
    )

    _mode_descriptions = {
        "rebuilding": "Rebuilding mode — looking for young assets (age ≤ 25) and future picks",
        "contending": "Contending mode — looking for immediate impact role players (OVR 75+)",
        "developing": "Developing mode — looking for veteran depth and expiring contracts",
    }

    for row in team_rows:
        team_code = row["nba_team_code"]
        cpu_mode = row["cpu_mode"] or "default"
        description = _mode_descriptions.get(
            cpu_mode,
            "Open for business — looking to improve at the margins",
        )
        embed = discord.Embed(
            title=f"📋 {team_code} — Looking to Deal",
            description=description,
            color=discord.Color.blue(),
        )
        embed.set_footer(text="CPU-managed team")
        try:
            await ch.send(embed=embed)
        except Exception as exc:
            log.warning(f"Failed to post trade-block ad for {team_code}: {exc}")


async def _build_return_package(
    pool,
    league: league_repo.League,
    team_a: team_repo.Team,
    block_player_ids: list[int],
    target_value: float,
    taken_player_ids: set[int],
) -> tuple[list[int], list[int], float]:
    """
    Build a return package from team A's trade block players and picks that
    roughly matches target_value (within 25%).

    Returns (player_ids, pick_ids, total_value).
    Prefer picks over players when possible to keep rosters intact.
    taken_player_ids prevents double-offering players committed to another offer
    in the same round.
    """
    offer_player_ids: list[int] = []
    offer_pick_ids: list[int] = []
    accumulated = 0.0
    tolerance = target_value * 0.25

    # Load and score A's own trade block players.
    scored_players: list[tuple[float, int]] = []
    for pid in block_player_ids:
        # Skip players already committed to another offer this round.
        if pid in taken_player_ids:
            continue
        p = await player_repo.get_by_id(pool, pid)
        if not p or p.team_id != team_a.id:
            continue  # player was already traded away or belongs to another team
        contract = await player_repo.get_active_contract(pool, p.id)
        v = trade_evaluator.player_trade_value(
            {"overall": p.overall, "age": _player_age(p)},
            {
                "salary": contract.salary if contract else 0,
                "years_remaining": contract.years_remaining if contract else 1,
            },
            league.salary_cap,
        )
        scored_players.append((v, pid))

    # Sort descending — use the best player(s) first.
    scored_players.sort(reverse=True)

    # Load A's available picks: round 2 first, then round 1 (protect higher picks).
    all_picks = await trade_repo.get_team_picks(pool, league.id, team_a.id)
    r2_picks = [p for p in all_picks if p["round"] == 2]
    r1_picks = [p for p in all_picks if p["round"] == 1]
    # Nearest season picks first (most concrete value).
    r2_picks.sort(key=lambda p: p["season"])
    r1_picks.sort(key=lambda p: p["season"])

    # Hard cap of 3 picks per trade (1st + 2nd combined).
    # cpu_mode governs willingness to include picks:
    #   rebuilding  — picks are their future; offer at most 1, prefer 2nds over 1sts.
    #   contending  — willing to deal picks for immediate help; up to 3.
    #   developing / default — balanced; up to 2 picks.
    mode = team_a.cpu_mode or "default"
    if mode == "rebuilding":
        max_picks = 1
        # Offer 2nd-rounders first; only expose 1sts as a last resort.
        sorted_picks = r2_picks + r1_picks
    elif mode == "contending":
        max_picks = 3
        sorted_picks = r2_picks + r1_picks
    else:
        # developing / default
        max_picks = 2
        sorted_picks = r2_picks + r1_picks

    # Pre-fetch win% for all original team IDs so pick_trade_value can discount by record.
    orig_team_ids = list({p["original_team_id"] for p in sorted_picks if p.get("original_team_id")})
    _orig_win_pct: dict[int, float | None] = {}
    if orig_team_ids:
        _wp_rows = await pool.fetch(
            """SELECT team_id,
                      CASE WHEN (wins + losses) > 0
                           THEN wins::float / (wins + losses)
                           ELSE NULL END AS win_pct
               FROM standings_cache
               WHERE league_id = $1 AND season = $2 AND team_id = ANY($3)""",
            league.id, league.current_season, orig_team_ids,
        )
        _orig_win_pct = {r["team_id"]: r["win_pct"] for r in _wp_rows}

    # Greedy fill: players first, then picks (up to the mode cap).
    for v, pid in scored_players:
        if accumulated >= target_value - tolerance:
            break
        offer_player_ids.append(pid)
        accumulated += v

    for pick in sorted_picks:
        if len(offer_pick_ids) >= max_picks:
            break
        if accumulated >= target_value - tolerance:
            break
        orig_tid = pick.get("original_team_id")
        win_pct = _orig_win_pct.get(orig_tid) if orig_tid else None
        pv = trade_evaluator.pick_trade_value(
            pick["season"], pick["round"], league.current_season,
            team_win_pct=win_pct,
        )
        if pick["id"] not in offer_pick_ids:  # prevent duplicates
            offer_pick_ids.append(pick["id"])
            accumulated += pv

    return offer_player_ids, offer_pick_ids, accumulated


def _team_a_wants_player(team_a: team_repo.Team, player: player_repo.Player) -> bool:
    """Return True if team A's mode makes it interested in this player."""
    age = _player_age(player)
    mode = team_a.cpu_mode

    if mode in ("contending", "developing"):
        return player.overall >= 76

    if mode == "rebuilding":
        # Want youth or actively shedding old contracts (age >= 31 means B wants
        # to dump the salary, which rebuilding teams can absorb for picks).
        return age <= 25 or age >= 31

    # Default: accept any player with decent rating.
    return player.overall >= 70


def _player_age(player: player_repo.Player) -> int:
    import datetime
    if player.birth_date:
        today = datetime.date.today()
        age = today.year - player.birth_date.year
        if (today.month, today.day) < (player.birth_date.month, player.birth_date.day):
            age -= 1
        return age
    return 28


def _player_age_from_row(row) -> int:
    import datetime
    birth = row["birth_date"]
    if birth:
        today = datetime.date.today()
        age = today.year - birth.year
        if (today.month, today.day) < (birth.month, birth.day):
            age -= 1
        return age
    return 28
