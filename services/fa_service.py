from __future__ import annotations

import datetime
import os
import random
from typing import Optional

import asyncpg
import discord

from core.errors import DBAError
from core.logging import get_logger
from data.db import get_pool
from data.repositories import fa_repo, league_repo, player_repo, team_repo
from phase.states import Phase
from services import team_intel
from services.fa_needs import _position_need_multiplier
from services.player_decision import (
    Decision,
    LeagueContext,
    OfferContext,
    PlayerProfile,
    _salary_floor,
    decide,
)

log = get_logger(__name__)

_HEADLESS = os.environ.get("DBA_HEADLESS_MODE") == "1"

DAILY_OFFER_LIMIT = 3
_MIN_SALARY = 1_100_000
_ROSTER_MAX = 15
# Contenders won't bid on players below this OVR threshold.
_CPU_CONTENDER_MIN_OVR = 70

# FA1: posture.mode (from team_intel.build_team_intel) -> (min_ovr, preferred_years).
# Replaces the old static 2-way team.cpu_mode branch, which had a contradictory
# "developing" case (rebuild-level min_ovr=0 paired with contender-level
# preferred_years=3). developing now deliberately splits the difference
# (min_ovr=0, preferred_years=2) instead of inheriting either extreme.
_POSTURE_FA_TERMS: dict[str, tuple[int, int]] = {
    "contending": (_CPU_CONTENDER_MIN_OVR, 3),
    "play_in_fringe": (_CPU_CONTENDER_MIN_OVR, 3),
    "developing": (0, 2),
    "soft_rebuild": (0, 1),
    "rebuilding": (0, 1),
}

# FA3: lightweight GM-philosophy nudges layered on top of the posture defaults.
# Keys mirror services/philosophies/_registry.py's REGISTRY names. Philosophies
# not listed here (egalitarian, tendency_respecter, defense_first) get no bias —
# baseline posture-driven behavior only. Kept intentionally small: this is FA's
# own bias table, not a reuse of the role-scoring philosophy functions.
#   min_ovr_bonus     — raised further on top of the posture-mode floor
#   years_bonus       — added to preferred_years (willing to go longer)
#   max_age           — hard-skip any free agent older than this
#   salary_mult_bonus — added to both ends of FA4's OVR-tiered offer multiplier
_PHILOSOPHY_FA_BIAS: dict[str, dict] = {
    "star_maxer": {"min_ovr_bonus": 5, "years_bonus": 1, "salary_mult_bonus": 0.05},
    "youth_developer": {"max_age": 27},
    "vet_overrater": {"salary_mult_bonus": 0.05},
    "chaos": {"salary_mult_bonus": 0.15},
}


def _philosophy_score_bonus(philosophy: str, age: int) -> float:
    """FA3: small age-lean score nudge so a team's philosophy shows up in
    *which* free agent it prioritizes, not just how much it offers.

    youth_developer favors <=24yo targets; vet_overrater favors >=29yo targets
    ("bias toward older players"). Everything else (including philosophies not
    in _PHILOSOPHY_FA_BIAS) returns 1.0 — no lean.
    """
    if philosophy == "youth_developer" and age <= 24:
        return 1.15
    if philosophy == "vet_overrater" and age >= 29:
        return 1.15
    return 1.0


def _star_premium_range(overall: int) -> tuple[float, float]:
    """FA4: tiered star-premium multiplier by OVR band, replacing the old flat
    uniform(1.0, 1.10) applied identically to every free agent regardless of
    quality. Elite free agents draw a real bidding premium; replacement-level
    players don't.
    """
    if overall >= 90:
        return 1.10, 1.25
    if overall >= 85:
        return 1.05, 1.20
    return 1.0, 1.10


async def submit_cpu_offers(league_id: int, season: int) -> int:
    """
    Called at the start of each FA day before human offers are evaluated.
    Each CPU team assesses needs and submits up to DAILY_OFFER_LIMIT offers to
    available free agents. Skips teams at roster limit or effectively over cap.
    Returns count of CPU offers submitted.
    """
    pool = await get_pool()

    state = await fa_repo.get_or_create_fa_state(pool, league_id, season)
    fa_day = state["current_day"]

    league_row = await pool.fetchrow(
        "SELECT * FROM leagues WHERE id = $1", league_id
    )
    if not league_row:
        return 0
    salary_cap: int = league_row["salary_cap"]

    cpu_teams = await fa_repo.get_cpu_teams(pool, league_id)
    free_agents = await fa_repo.get_unsigned_players(pool, league_id)

    if not cpu_teams:
        return 0

    team_ids = [t["id"] for t in cpu_teams]

    # FA1/FA3: fetch live posture + philosophy for all CPU teams in one batched
    # call (team_intel.py's own "<=6 queries" budget convention), instead of
    # per-team-per-loop queries. FA2: batch each team's active roster position
    # counts the same way so need-weighting doesn't add a query per team either.
    league_obj = league_repo._league_from_record(league_row)
    intel = await team_intel.build_team_intel(
        pool, league_obj, season, team_ids, include=("posture", "philosophy")
    )
    position_counts = await player_repo.get_active_roster_position_counts(
        pool, league_id, team_ids
    )

    # FA4: CPU-vs-CPU bidding awareness needs to know, for every free agent,
    # whether another CPU team already has a live offer out today. Fetch once
    # for the whole fa_day (not per-team-per-candidate) and update in-memory
    # as this call submits its own new offers.
    todays_offer_rows = await pool.fetch(
        """
        SELECT player_id, team_id, salary_per_year, status
        FROM fa_offers
        WHERE league_id = $1 AND season = $2 AND fa_day = $3
        """,
        league_id, season, fa_day,
    )
    offers_by_player: dict[int, list[dict]] = {}
    for r in todays_offer_rows:
        offers_by_player.setdefault(r["player_id"], []).append(dict(r))

    total_submitted = 0

    for team in cpu_teams:
        team_id: int = team["id"]
        cap_used: int = int(team["cap_used"])
        active_count: int = int(team["active_player_count"])

        if active_count >= _ROSTER_MAX:
            continue

        offers_this_day = 0

        team_intel_data = intel.get(team_id, {})
        posture: dict = team_intel_data.get("posture") or {}
        mode: str | None = posture.get("mode")
        philosophy: str = team_intel_data.get("philosophy") or "tendency_respecter"

        terms = _POSTURE_FA_TERMS.get(mode) if mode else None
        if terms is not None:
            min_ovr, preferred_years = terms
        else:
            # Defensive fallback: build_team_intel returned no posture data for
            # this team (e.g. team has no standings_cache row yet). Fall back
            # to the old static cpu_mode 2-way branch rather than crashing.
            log.warning(
                f"submit_cpu_offers: no posture data for team {team_id} "
                f"(league {league_id}) — falling back to static cpu_mode"
            )
            cpu_mode: str = team["cpu_mode"]
            min_ovr = _CPU_CONTENDER_MIN_OVR if cpu_mode == "contending" else 0
            preferred_years = 1 if cpu_mode == "rebuilding" else 3

        bias = _PHILOSOPHY_FA_BIAS.get(philosophy, {})
        min_ovr += bias.get("min_ovr_bonus", 0)
        preferred_years += bias.get("years_bonus", 0)
        max_age = bias.get("max_age")
        salary_mult_bonus = bias.get("salary_mult_bonus", 0.0)

        team_position_counts = position_counts.get(team_id, {})

        # FA2: score each eligible FA by need-adjusted value instead of walking
        # the raw global OVR-sorted list — a team deep at one position and thin
        # at another should chase the thin one first.
        scored_candidates: list[tuple[float, dict]] = []
        for fa in free_agents:
            player_ovr: int = fa["overall"]
            if player_ovr < min_ovr:
                continue
            age = 19 + fa["years_pro"]
            if max_age is not None and age > max_age:
                continue  # youth_developer: hard-skip anyone above the age ceiling

            need_mult = _position_need_multiplier(team_position_counts, fa["position"])
            phil_mult = _philosophy_score_bonus(philosophy, age)
            score = player_ovr * need_mult * phil_mult
            scored_candidates.append((score, fa))

        scored_candidates.sort(key=lambda pair: pair[0], reverse=True)

        for _score, fa in scored_candidates:
            if offers_this_day >= DAILY_OFFER_LIMIT:
                break

            player_ovr = fa["overall"]

            profile = PlayerProfile(
                overall=player_ovr,
                age=19 + fa["years_pro"],
                years_pro=fa["years_pro"],
                loyalty=fa.get("loyalty", 50),
                money_drive=fa.get("money_drive", 50),
                win_drive=fa.get("win_drive", 50),
                star_leverage=fa.get("star_leverage", 50),
                market_pref=fa.get("market_pref", "neutral"),
            )

            floor = _salary_floor(profile, salary_cap)
            lo, hi = _star_premium_range(player_ovr)
            lo += salary_mult_bonus
            hi += salary_mult_bonus
            offer_salary = int(floor * random.uniform(lo, hi))
            offer_salary = max(offer_salary, _MIN_SALARY)

            # FA4: CPU-vs-CPU bidding awareness — if another CPU team already
            # has a live offer out to this same free agent today, don't submit
            # a redundant lowball; bump above the existing best instead (only
            # when cap space still allows it).
            competing = [
                o for o in offers_by_player.get(fa["id"], [])
                if o["status"] in ("submitted", "waiting")
            ]
            if competing:
                best_competing = max(o["salary_per_year"] for o in competing)
                bumped = int(best_competing * random.uniform(1.03, 1.08))
                if bumped > offer_salary and cap_used + bumped <= salary_cap:
                    offer_salary = bumped

            if cap_used + offer_salary > salary_cap:
                continue

            years = random.randint(1, max(preferred_years, 1))

            await fa_repo.submit_offer(
                pool,
                league_id,
                season,
                fa_day,
                team_id,
                fa["id"],
                offer_salary,
                years,
            )
            offers_by_player.setdefault(fa["id"], []).append({
                "player_id": fa["id"],
                "team_id": team_id,
                "salary_per_year": offer_salary,
                "status": "submitted",
            })
            cap_used += offer_salary
            offers_this_day += 1
            total_submitted += 1

            if _HEADLESS:
                try:
                    _team_row = await pool.fetchrow(
                        "SELECT nba_team_code FROM teams WHERE id = $1", team_id
                    )
                    _tc = _team_row["nba_team_code"] if _team_row else str(team_id)
                    _fa_name = f"{fa.get('first_name', '')} {fa.get('last_name', '')}".strip() or f"Player #{fa['id']}"
                    _min_ovr_note = f" (min OVR {min_ovr})" if min_ovr > 0 else ""
                    print(
                        f"CPU [{_tc}] — FA bid: {_fa_name} OVR {player_ovr}\n"
                        f"   mode: {mode or 'unknown'} | philosophy: {philosophy}{_min_ovr_note} | "
                        f"offer: ${offer_salary:,}/yr × {years}yr\n"
                        f"   cap_used after: ${cap_used:,}"
                    )
                except Exception:
                    pass  # never let logging break the sim

    log.info(f"CPU FA offers submitted: {total_submitted} (league {league_id} day {fa_day})")
    return total_submitted


async def open_fa(league_id: int, season: int) -> dict:
    """
    Initialize FA state (day=1, phase='offers_open').
    Sets all players with expired contracts to roster_status='free_agent'.
    Returns FA state dict.
    """
    pool = await get_pool()

    state = await fa_repo.get_or_create_fa_state(pool, league_id, season)

    # Expire active contracts whose years_remaining hit 0.
    # A contract at years_remaining=0 means it has lapsed — set player to free_agent.
    await pool.execute(
        """
        UPDATE players
        SET roster_status = 'free_agent', team_id = NULL
        WHERE league_id = $1
          AND roster_status = 'active'
          AND id IN (
              SELECT player_id FROM contracts
              WHERE league_id = $1 AND is_active = TRUE AND years_remaining <= 0
          )
        """,
        league_id,
    )
    # Deactivate those expired contracts so cap math stays clean.
    await pool.execute(
        """
        UPDATE contracts
        SET is_active = FALSE, terminated_reason = 'expired'
        WHERE league_id = $1 AND is_active = TRUE AND years_remaining <= 0
        """,
        league_id,
    )

    await fa_repo.update_fa_state(pool, league_id, "offers_open", current_day=1)
    state = await fa_repo.get_or_create_fa_state(pool, league_id, season)

    log.info(f"FA opened for league {league_id} season {season}")
    return state


async def submit_offer(
    league_id: int,
    season: int,
    team_id: int,
    player_id: int,
    salary: int,
    years: int,
) -> int:
    """
    Validate: player is free agent, team under cap with this contract, daily limit not exceeded.
    Insert offer with status='submitted'. Returns offer_id.
    """
    pool = await get_pool()

    player = await player_repo.get_by_id(pool, player_id)
    if not player or player.roster_status != "free_agent":
        raise DBAError("That player is not a free agent.")

    state = await fa_repo.get_or_create_fa_state(pool, league_id, season)
    if state["phase"] != "offers_open":
        raise DBAError(f"Offers are not open right now (phase: {state['phase']}).")

    today_count = await fa_repo.count_offers_today(
        pool, league_id, season, state["current_day"], team_id
    )
    if today_count >= DAILY_OFFER_LIMIT:
        raise DBAError(
            f"You have already submitted {DAILY_OFFER_LIMIT} offers today. Wait for the next FA day."
        )

    league = await pool.fetchrow("SELECT salary_cap FROM leagues WHERE id = $1", league_id)
    if not league:
        raise DBAError("League not found.")
    cap: int = league["salary_cap"]

    cap_used = await player_repo.get_team_cap_usage(pool, league_id, team_id)
    if cap_used + salary > cap:
        remaining = cap - cap_used
        raise DBAError(
            f"Over the salary cap. You have ${remaining:,} in space; offer is ${salary:,}/yr."
        )

    offer_id = await fa_repo.submit_offer(
        pool, league_id, season, state["current_day"], team_id, player_id, salary, years
    )
    log.info(
        f"Offer submitted: team {team_id} → player {player_id} "
        f"${salary:,}/yr × {years}yr (offer {offer_id})"
    )
    return offer_id


async def advance_to_responses(
    league_id: int, season: int, guild: discord.Guild
) -> list[dict]:
    """
    Runs the player decision model for every free agent who received offers today.
    Signs, declines, waits, or counters as appropriate.
    Posts announcement to #transactions.
    Returns list of {player_name, decision, team_name} dicts.
    """
    pool = await get_pool()

    state = await fa_repo.get_or_create_fa_state(pool, league_id, season)
    if state["phase"] != "offers_open":
        raise DBAError("FA is not in the offers-open phase.")

    fa_day = state["current_day"]

    # CPU teams submit their offers first so they compete alongside human bids.
    await submit_cpu_offers(league_id, season)

    league_row = await pool.fetchrow(
        "SELECT salary_cap FROM leagues WHERE id = $1", league_id
    )
    if not league_row:
        raise DBAError("League not found.")
    salary_cap: int = league_row["salary_cap"]

    # Collect all players who received offers today.
    player_ids_with_offers: list[int] = [
        r["player_id"]
        for r in await pool.fetch(
            """
            SELECT DISTINCT player_id FROM fa_offers
            WHERE league_id = $1 AND season = $2 AND fa_day = $3 AND status = 'submitted'
            """,
            league_id,
            season,
            fa_day,
        )
    ]

    results: list[dict] = []

    for player_id in player_ids_with_offers:
        player = await player_repo.get_by_id(pool, player_id)
        if not player:
            log.warning(f"Player {player_id} vanished during FA response processing")
            continue

        offers_today = await fa_repo.get_offers_for_player(
            pool, league_id, season, fa_day, player_id
        )
        if not offers_today:
            continue

        best_offer_salary = max(o["salary_per_year"] for o in offers_today)

        # Determine former team from any active contract that was most recently deactivated.
        former_team_row = await pool.fetchrow(
            """
            SELECT team_id FROM contracts
            WHERE player_id = $1 AND league_id = $2
            ORDER BY id DESC LIMIT 1
            """,
            player_id,
            league_id,
        )
        former_team_id: Optional[int] = (
            former_team_row["team_id"] if former_team_row else None
        )

        profile = PlayerProfile(
            overall=player.overall,
            age=_estimate_age(player),
            years_pro=player.years_pro,
            loyalty=player.loyalty,
            money_drive=player.money_drive,
            win_drive=player.win_drive,
            star_leverage=player.star_leverage,
            market_pref=player.market_pref,
        )

        league_ctx = LeagueContext(
            salary_cap=salary_cap,
            current_fa_day=fa_day,
            total_fa_days=state["total_days"],
            best_offer_this_day=best_offer_salary,
        )

        # Sort offers by salary descending so the player sees the best offer first.
        offers_today.sort(key=lambda o: o["salary_per_year"], reverse=True)

        # Build all_offer_ctxs once per player (not per offer) to avoid N^2 DB calls.
        all_offer_ctxs: list[OfferContext] = []
        for o in offers_today:
            o_wins = await _get_team_wins(pool, league_id, o["team_id"], season)
            # FA5: same cap-usage lookup submit_offer already does, so decide()
            # can bound a counter-offer by the offering team's real remaining
            # room instead of a flat 35%-of-cap ceiling.
            o_cap_used = await player_repo.get_team_cap_usage(pool, league_id, o["team_id"])
            o_cap_space = max(salary_cap - o_cap_used, 0)
            all_offer_ctxs.append(
                OfferContext(
                    salary_per_year=o["salary_per_year"],
                    years=o["years"],
                    team_wins=o_wins,
                    team_is_current=(o["team_id"] == former_team_id),
                    cap_space=o_cap_space,
                )
            )

        signed = False
        for offer_row, offer_ctx in zip(offers_today, all_offer_ctxs):
            if signed:
                # Already signed — expire remaining offers.
                await fa_repo.update_offer_status(pool, offer_row["id"], "expired")
                continue

            team = await team_repo.get_by_id(pool, offer_row["team_id"])

            decision, counter_sal, counter_yrs = decide(profile, offer_ctx, league_ctx, all_offer_ctxs)

            team_name = team.full_name if team else f"Team {offer_row['team_id']}"

            if decision == Decision.SIGN:
                await fa_repo.update_offer_status(pool, offer_row["id"], "accepted")
                await _sign_player(pool, league_id, season, offer_row["team_id"], player_id, offer_row["salary_per_year"], offer_row["years"])
                signed = True
                results.append({"player_name": player.full_name, "decision": "signed", "team_name": team_name})
                log.info(f"FA: {player.full_name} signed with {team_name} ${offer_row['salary_per_year']:,}/yr")

            elif decision == Decision.COUNTER:
                assert counter_sal is not None and counter_yrs is not None
                counter_id = await fa_repo.create_counter(pool, offer_row["id"], counter_sal, counter_yrs)
                await fa_repo.update_offer_status(pool, offer_row["id"], "countered")
                results.append({"player_name": player.full_name, "decision": "countered", "team_name": team_name})
                log.info(f"FA: {player.full_name} countered {team_name} with ${counter_sal:,}/yr (counter {counter_id})")

                # Notify team via #transactions.
                transactions_channel_id = await league_repo.get_channel(pool, league_id, "transactions")
                if transactions_channel_id:
                    ch = guild.get_channel(transactions_channel_id)
                    if ch:
                        from bot.embeds.fa_embeds import counter_offer_embed
                        embed = counter_offer_embed(
                            player={"name": player.full_name, "overall": player.overall},
                            counter={"salary_per_year": counter_sal, "years": counter_yrs},
                            team={"name": team_name},
                        )
                        await ch.send(embed=embed)

            elif decision == Decision.WAIT:
                await fa_repo.update_offer_status(pool, offer_row["id"], "waiting")
                results.append({"player_name": player.full_name, "decision": "waiting", "team_name": team_name})

            else:  # DECLINE
                await fa_repo.update_offer_status(pool, offer_row["id"], "declined")
                results.append({"player_name": player.full_name, "decision": "declined", "team_name": team_name})

    await fa_repo.update_fa_state(pool, league_id, "responses_pending")

    transactions_channel_id = await league_repo.get_channel(pool, league_id, "transactions")
    if transactions_channel_id and results:
        ch = guild.get_channel(transactions_channel_id)
        if ch:
            from bot.embeds.fa_embeds import fa_responses_embed
            embed = fa_responses_embed(results)
            await ch.send(embed=embed)

    return results


async def respond_to_counter(
    league_id: int, counter_id: int, team_id: int, accept: bool
) -> dict:
    """
    Team accepts or declines a counter-offer.
    If accept: signs player and creates contract.
    Returns summary dict.
    """
    pool = await get_pool()

    counter_row = await pool.fetchrow(
        "SELECT * FROM fa_counters WHERE id = $1",
        counter_id,
    )
    if not counter_row:
        raise DBAError("Counter offer not found.")
    if counter_row["team_response"] != "pending":
        raise DBAError("This counter has already been responded to.")

    offer_row = await pool.fetchrow(
        "SELECT * FROM fa_offers WHERE id = $1",
        counter_row["original_offer_id"],
    )
    if not offer_row:
        raise DBAError("Original offer not found.")
    if offer_row["team_id"] != team_id:
        raise DBAError("This counter belongs to a different team.")

    player = await player_repo.get_by_id(pool, offer_row["player_id"])
    if not player:
        raise DBAError("Player not found.")

    if accept:
        return await _accept_counter(pool, league_id, counter_row, offer_row, team_id, player)
    else:
        await fa_repo.update_counter_response(pool, counter_id, "declined")
        log.info(f"Counter declined: team {team_id} declined player {player.id}")
        return {"accepted": False, "player_name": player.full_name}


async def _accept_counter(
    pool: asyncpg.Pool,
    league_id: int,
    counter_row: asyncpg.Record | dict,
    offer_row: asyncpg.Record | dict,
    team_id: int,
    player: player_repo.Player,
) -> dict:
    """Shared accept-counter body: cap re-check (FA6) + sign + log.

    Used by both the human /fa counter-accept path (respond_to_counter) and
    the CPU auto-resolution path (_resolve_cpu_counters) — factored out so
    the cap-check fix doesn't have to be duplicated across both callers.

    FA6 real-bug fix: respond_to_counter used to accept a countered offer
    without re-validating cap space at all, unlike submit_offer which always
    checks. Raises the identical DBAError shape submit_offer uses on overflow.
    """
    if player.roster_status != "free_agent":
        raise DBAError("This player is no longer a free agent.")

    league_row = await pool.fetchrow("SELECT salary_cap FROM leagues WHERE id = $1", league_id)
    if not league_row:
        raise DBAError("League not found.")
    cap: int = league_row["salary_cap"]

    cap_used = await player_repo.get_team_cap_usage(pool, league_id, team_id)
    salary = counter_row["salary_per_year"]
    if cap_used + salary > cap:
        remaining = cap - cap_used
        raise DBAError(
            f"Over the salary cap. You have ${remaining:,} in space; offer is ${salary:,}/yr."
        )

    await fa_repo.update_counter_response(pool, counter_row["id"], "accepted")
    await _sign_player(
        pool,
        league_id,
        offer_row["season"],
        team_id,
        player.id,
        counter_row["salary_per_year"],
        counter_row["years"],
    )
    log.info(
        f"Counter accepted: team {team_id} signs player {player.id} "
        f"${counter_row['salary_per_year']:,}/yr × {counter_row['years']}yr"
    )
    return {"accepted": True, "player_name": player.full_name}


async def _resolve_cpu_counters(pool: asyncpg.Pool, league_id: int, season: int, fa_day: int) -> int:
    """FA7 real-bug fix: CPU teams have no manager to invoke
    /fa counter-accept|counter-decline, so a countered offer to a CPU team
    sat in status='countered' forever — advance_day only ever expired
    'waiting' offers, never resolving the CPU side of a counter.

    Finds pending counters on today's offers where the offering team has no
    manager (teams.manager_user_id IS NULL), and auto-resolves each one:
    accept if the counter salary isn't more than 15% over the original offer
    AND cap space allows it (via the shared _accept_counter helper — same
    cap check FA6 added to the human path), else decline.

    Returns the number of counters resolved (accepted + declined).
    """
    rows = await pool.fetch(
        """
        SELECT fc.*, fo.id AS offer_id, fo.team_id AS offer_team_id,
               fo.player_id AS offer_player_id, fo.season AS offer_season,
               fo.salary_per_year AS original_salary
        FROM fa_counters fc
        JOIN fa_offers fo ON fo.id = fc.original_offer_id
        JOIN teams t ON t.id = fo.team_id
        WHERE fc.team_response = 'pending'
          AND fo.league_id = $1
          AND fo.season = $2
          AND fo.fa_day = $3
          AND t.manager_user_id IS NULL
        """,
        league_id,
        season,
        fa_day,
    )

    resolved = 0
    for row in rows:
        team_id: int = row["offer_team_id"]
        counter_salary: int = row["salary_per_year"]
        original_salary: int = row["original_salary"]
        offer_row = {
            "id": row["offer_id"],
            "team_id": team_id,
            "player_id": row["offer_player_id"],
            "season": row["offer_season"],
        }

        accept = counter_salary <= original_salary * 1.15

        if accept:
            player = await player_repo.get_by_id(pool, row["offer_player_id"])
            if not player:
                log.warning(f"_resolve_cpu_counters: player {row['offer_player_id']} vanished — declining counter {row['id']}")
                await fa_repo.update_counter_response(pool, row["id"], "declined")
                resolved += 1
                continue
            try:
                await _accept_counter(pool, league_id, row, offer_row, team_id, player)
            except DBAError as exc:
                log.info(f"_resolve_cpu_counters: counter {row['id']} could not be accepted ({exc}) — declining instead")
                await fa_repo.update_counter_response(pool, row["id"], "declined")
        else:
            await fa_repo.update_counter_response(pool, row["id"], "declined")
            log.info(
                f"_resolve_cpu_counters: counter {row['id']} declined "
                f"(${counter_salary:,} > 115% of original ${original_salary:,})"
            )
        resolved += 1

    if resolved:
        log.info(f"CPU counters resolved: {resolved} (league {league_id} day {fa_day})")
    return resolved


async def advance_day(league_id: int, season: int) -> dict:
    """
    Move to next FA day. Expire 'waiting' offers from the previous day.
    Resolves any pending CPU-team counters (FA7).
    If current_day >= total_days: close FA instead.
    Returns new FA state dict.
    """
    pool = await get_pool()

    state = await fa_repo.get_or_create_fa_state(pool, league_id, season)
    current_day = state["current_day"]
    total_days = state["total_days"]

    # Expire offers that remained 'waiting' — their window has passed.
    await pool.execute(
        """
        UPDATE fa_offers SET status = 'expired'
        WHERE league_id = $1 AND season = $2 AND fa_day = $3 AND status = 'waiting'
        """,
        league_id,
        season,
        current_day,
    )

    # FA7: resolve any 'countered' offers to CPU teams (no manager exists to
    # respond via /fa counter-accept|counter-decline) before this day closes.
    await _resolve_cpu_counters(pool, league_id, season, current_day)

    if current_day >= total_days:
        return await close_fa(league_id, season)

    next_day = current_day + 1
    await fa_repo.update_fa_state(pool, league_id, "offers_open", current_day=next_day)
    state = await fa_repo.get_or_create_fa_state(pool, league_id, season)
    log.info(f"FA advanced to day {next_day} for league {league_id}")
    return state


async def close_fa(league_id: int, season: int) -> dict:
    """
    FA period is over.
    - Waive unsigned players OVR >= 65.
    - Retire unsigned players OVR < 65 who are age-eligible (RO4: age-gated,
      consistent with progression_service's P5 active-roster retirement rule
      — no more force-retiring on tenure alone).
    - Update FA state phase='complete'.
    - Advance league phase to FA_CLOSED.
    Returns summary dict with counts.
    """
    pool = await get_pool()

    # RO4: local import, matching this module's existing style for avoiding
    # circular deps (see the advance_phase import below). Reuses progression's
    # already-shipped age gate/age-computation helper instead of the old bare
    # years_pro > 8 tenure check, which had no age/birth_date awareness at all.
    from services.progression_service import _RETIREMENT_MIN_AGE, _compute_age

    unsigned = await fa_repo.get_unsigned_players(pool, league_id)

    waived_count = 0
    retired_count = 0

    for player_data in unsigned:
        pid = player_data["id"]
        ovr = player_data["overall"]
        age = _compute_age(player_data.get("birth_date"), player_data["years_pro"])

        if ovr >= 65:
            await pool.execute(
                "UPDATE players SET roster_status = 'waived' WHERE id = $1",
                pid,
            )
            waived_count += 1
        elif age >= _RETIREMENT_MIN_AGE:
            await pool.execute(
                "UPDATE players SET roster_status = 'retired' WHERE id = $1",
                pid,
            )
            retired_count += 1
        else:
            # Still young enough — remains a free agent; will be available next cycle.
            pass

    await fa_repo.update_fa_state(pool, league_id, "complete")

    from services.league_service import advance_phase  # local: avoids circular dep
    await advance_phase(league_id, Phase.FA_CLOSED.value)

    log.info(
        f"FA closed for league {league_id}: "
        f"{len(unsigned)} unsigned, {waived_count} waived, {retired_count} retired"
    )
    return {
        "phase": "complete",
        "signed_count": 0,  # informational — callers can compute from advance_to_responses results
        "waived_count": waived_count,
        "retired_count": retired_count,
    }


async def claim_waiver(league_id: int, team_id: int, player_id: int) -> None:
    """
    Sign a waived player at minimum contract ($1.1M, 1 year).
    Validate: player is waived, team has roster space (< 15 active), team under cap.
    Waiver priority: if another team already claimed this player and has a worse
    record (higher priority), raises DBAError to inform the manager.
    """
    pool = await get_pool()

    player = await player_repo.get_by_id(pool, player_id)
    if not player or player.roster_status != "waived":
        raise DBAError("That player is not on waivers.")

    active_count = await pool.fetchval(
        """
        SELECT COUNT(*) FROM players
        WHERE league_id = $1 AND team_id = $2 AND roster_status = 'active'
        """,
        league_id,
        team_id,
    )
    if active_count >= _ROSTER_MAX:
        raise DBAError(f"Roster is full ({_ROSTER_MAX} active players).")

    league_row = await pool.fetchrow("SELECT salary_cap FROM leagues WHERE id = $1", league_id)
    if not league_row:
        raise DBAError("League not found.")
    cap: int = league_row["salary_cap"]
    cap_used = await player_repo.get_team_cap_usage(pool, league_id, team_id)
    if cap_used + _MIN_SALARY > cap:
        raise DBAError("Not enough cap space to claim this player at the minimum salary.")

    # Check waiver priority: teams with worse records (fewer wins) claim first.
    existing_claims = await fa_repo.get_waiver_claims_for_player(pool, league_id, player_id)
    if existing_claims:
        # Get this team's record.
        our_record = await pool.fetchrow(
            "SELECT COALESCE(wins, 0) AS wins FROM standings_cache WHERE league_id = $1 AND team_id = $2 ORDER BY season DESC LIMIT 1",
            league_id,
            team_id,
        )
        our_wins = our_record["wins"] if our_record else 0

        # existing_claims is sorted worst record first (highest priority first).
        top_claim = existing_claims[0]
        if top_claim["team_id"] != team_id and top_claim["wins"] < our_wins:
            raise DBAError(
                "Another team has priority claim on this player. "
                "Their claim will be processed first."
            )

    await fa_repo.add_waiver_claim(pool, league_id, player_id, team_id)

    season = await pool.fetchval("SELECT current_season FROM leagues WHERE id = $1", league_id)

    await _sign_player(pool, league_id, season, team_id, player_id, _MIN_SALARY, 1)
    log.info(f"Waiver claim: team {team_id} signed player {player_id} at minimum")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _sign_player(
    pool: asyncpg.Pool,
    league_id: int,
    season: int,
    team_id: int,
    player_id: int,
    salary: int,
    years: int,
) -> None:
    """Assign player to team, deactivate old contracts, create new FA contract.

    RA1 real-bug fix: mirrors draft_service.py's _add_drafted_player_to_lineup
    exactly. Without this, an FA signing or waiver claim (both routes here,
    since claim_waiver signs via this same helper) left the player active and
    under contract but with NO `lineups` row -- a roster *ghost*, invisible to
    both sim_persistence._load_lineup_for_team and role_service.derive_roles
    (both INNER/JOIN lineups) and thus to sim_engine (zero touch_share, zero
    minutes) until some unrelated later event (a trade, an admin rebuild)
    happened to touch that team and incidentally re-derive roles. The draft
    path already got this fix; FA/waivers never did.
    """
    # Deactivate any lingering active contract (e.g. partial remains from prior season).
    await pool.execute(
        """
        UPDATE contracts SET is_active = FALSE, terminated_reason = 'superseded'
        WHERE player_id = $1 AND is_active = TRUE
        """,
        player_id,
    )
    await pool.execute(
        "UPDATE players SET team_id = $1, roster_status = 'active' WHERE id = $2",
        team_id,
        player_id,
    )
    await pool.execute(
        """
        INSERT INTO contracts
            (league_id, player_id, team_id, salary, years_remaining, total_years,
             contract_type, signed_in_season, is_active, signed_at)
        VALUES ($1, $2, $3, $4, $5, $5, 'fa', $6, TRUE, NOW())
        """,
        league_id,
        player_id,
        team_id,
        salary,
        years,
        season,
    )

    # RA1: insert at MAX(slot)+1 for the receiving team, then invalidate the
    # role cache and re-derive roles immediately so player_roles is consistent
    # with the new lineup right away -- same two calls draft_service.py's
    # _add_drafted_player_to_lineup already makes after a pick.
    next_slot = await pool.fetchval(
        "SELECT COALESCE(MAX(slot), 0) + 1 FROM lineups WHERE league_id = $1 AND team_id = $2",
        league_id,
        team_id,
    )
    await pool.execute(
        """INSERT INTO lineups
               (league_id, team_id, is_starter, slot, player_id, set_by)
           VALUES ($1, $2, FALSE, $3, $4, NULL)
           ON CONFLICT (league_id, team_id, slot) DO NOTHING""",
        league_id,
        team_id,
        next_slot,
        player_id,
    )

    from services import role_service
    from services.sim_persistence import invalidate_role_cache

    invalidate_role_cache(league_id, team_id, season)
    async with pool.acquire() as conn:
        await role_service.derive_and_persist_all_for_team(
            conn, league_id, team_id, season, silent_emit=True,
        )


async def _get_team_wins(
    pool: asyncpg.Pool, league_id: int, team_id: int, season: int
) -> int:
    wins = await pool.fetchval(
        """
        SELECT COALESCE(SUM(
            CASE
                WHEN home_team_id = $2 AND home_score > away_score THEN 1
                WHEN away_team_id = $2 AND away_score > home_score THEN 1
                ELSE 0
            END
        ), 0)
        FROM games
        WHERE league_id = $1 AND season = $3 AND status = 'simmed'
          AND (home_team_id = $2 OR away_team_id = $2)
        """,
        league_id,
        team_id,
        season,
    )
    return int(wins)


def _estimate_age(player: player_repo.Player) -> int:
    """Derive age from birth_date if available; fall back to years_pro heuristic."""
    if player.birth_date is not None:
        today = datetime.date.today()
        born = player.birth_date
        return today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return 19 + player.years_pro

