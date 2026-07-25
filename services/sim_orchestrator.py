from __future__ import annotations

import asyncio
import os
from typing import List, Optional

import discord

from bot.embeds import sim_embeds
from core.logging import get_logger
from data.db import get_pool
from data.repositories import game_repo, gameplan_repo, strategy_repo, team_repo
from services import awards_service, cpu_coach_service, franchise_plan_service, notifier_service, sim_engine, strategy_service
from services.cpu_trade_round_trigger import _maybe_run_cpu_trades
from services.sim_batch_hooks import (
    _maybe_advance_season_complete,
    _maybe_advance_trade_deadline,
    _maybe_close_trade_window,
    _maybe_snapshot_teams,
)
from services.sim_content_pipeline import (
    _maybe_post_big_picture,
    _maybe_post_coach_beat,
    _maybe_post_columnist,
    _maybe_post_ledger,
    _maybe_post_potm,
    _maybe_post_power_list,
    _maybe_post_rookie_watch,
    _maybe_post_the_race,
)
from services.sim_channel_announcer import (
    _ensure_records_channel,
    _get_box_scores_channel,
    _get_injury_channel,
    _get_news_channel,
    _get_standings_channel,
)
from services.sim_persistence import (
    _apply_cpu_directives,
    _apply_directives,
    _compute_team_ovr,
    _ensure_lineup,
    _load_lineup_for_team,
    _persist_game_result,
    _stamp_role_data,
    _team_to_sim_dict,
)

_HEADLESS = os.environ.get("DBA_HEADLESS_MODE") == "1"

_SEVERITY_LABELS: dict[str, str] = {
    "day_to_day":    "day-to-day",
    "week_2_4":      "2-4 weeks",
    "week_4_8":      "4-8 weeks",
    "season_ending": "season-ending",
}

log = get_logger(__name__)

_BOX_SCORE_BATCH_SIZE = 10

_INJURY_GAMES_MISSED: dict[str, tuple[int, int]] = {
    "day_to_day":    (1, 3),
    "week_2_4":      (7, 14),
    "week_4_8":      (20, 35),
    "season_ending": (999, 999),
}

_ANNOUNCE_SEVERITIES = frozenset({"week_4_8", "season_ending"})


async def _build_pre_sim_inputs(
    pool,
    league_id: int,
    season: int,
    game_row: dict,
    home_team: team_repo.Team,
    away_team: team_repo.Team,
    home_players: List[dict],
    away_players: List[dict],
) -> dict:
    """
    Pre-sim input-building shared by the regular-season path (_sim_single_game
    below) and the playoff path (services.playoff_service._run_one_game):
    CPU/human gameplan decision, directive application, strategy modifiers,
    role-stamping, coach minutes plan, and back-to-back fatigue.

    PA1 (playoffs/awards/HOF realism audit): before this extraction, playoff
    games never received any of this -- they called sim_engine.sim_game with
    only 5 positional args, so sim_engine's `_has_role_data` check always came
    back False and every playoff/play-in game silently ran the "should not be
    reached in normal operation" legacy fallback (no CPU gameplans, no player
    directives, no scheme modifiers, no coach-set minutes plan, no fatigue).

    Mutates home_players/away_players in place (directive application +
    _role_* stamping), matching pre-extraction behavior -- callers pass in
    the same list objects they'll hand to sim_engine.sim_game afterward.

    game_row must have: id, home_team_id, away_team_id, scheduled_date,
    rng_seed (regular season passes the games-table row; playoff_service
    passes the dict it builds for game_repo.insert_game, with "id" added
    after insert).

    Returns the sim_game() kwargs this step produces (fatigue/home_strategy/
    away_strategy/home_minutes/away_minutes) plus the two gameplans so
    callers can persist/announce them.
    """
    game_date = game_row.get("scheduled_date")
    fatigue = {
        "home_b2b": await game_repo.is_back_to_back(pool, league_id, season, home_team.id, game_date),
        "away_b2b": await game_repo.is_back_to_back(pool, league_id, season, away_team.id, game_date),
    }

    home_gameplan, away_gameplan = await cpu_coach_service.decide_gameplans(
        pool, league_id, season, game_row, home_players, away_players
    )
    await gameplan_repo.record_gameplan(pool, game_row["id"], home_team.id, home_gameplan)
    await gameplan_repo.record_gameplan(pool, game_row["id"], away_team.id, away_gameplan)

    if home_gameplan["source"] == "cpu":
        _apply_cpu_directives(home_players, home_gameplan["player_directives"])
        for _p in home_players:
            _apply_directives(_p)
    if away_gameplan["source"] == "cpu":
        _apply_cpu_directives(away_players, away_gameplan["player_directives"])
        for _p in away_players:
            _apply_directives(_p)

    # players= is passed alongside override_strategy so get_sim_modifiers can
    # condition press/switch_all's magnitude on each team's own roster
    # (finding #2, realism audit) -- previously this parameter was always None
    # on the live path since override_strategy short-circuits the auto-archetype
    # branch that used to be its only consumer.
    home_strategy = await strategy_service.get_sim_modifiers(
        pool, league_id, home_team.id, players=home_players, override_strategy=home_gameplan["strategy"]
    )
    away_strategy = await strategy_service.get_sim_modifiers(
        pool, league_id, away_team.id, players=away_players, override_strategy=away_gameplan["strategy"]
    )

    # Phase 2: stamp role-based touch share + shot profile onto player dicts before sim.
    # offensive_scheme comes from the resolved strategy so scheme_synergy is applied correctly.
    home_scheme = home_gameplan["strategy"].get("offensive_scheme", "balanced")
    away_scheme = away_gameplan["strategy"].get("offensive_scheme", "balanced")
    await _stamp_role_data(pool, league_id, home_team.id, season, home_players, home_scheme)
    await _stamp_role_data(pool, league_id, away_team.id, season, away_players, away_scheme)

    home_player_ids = [p["id"] for p in home_players]
    away_player_ids = [p["id"] for p in away_players]
    game_rng_seed = game_row.get("rng_seed") or (game_row["id"] * 31337)
    home_minutes = await strategy_repo.get_team_minutes_plan(pool, league_id, home_team.id, home_player_ids, game_seed=game_rng_seed)
    away_minutes = await strategy_repo.get_team_minutes_plan(pool, league_id, away_team.id, away_player_ids, game_seed=game_rng_seed ^ 0xABCD)

    return {
        "fatigue": fatigue,
        "home_strategy": home_strategy,
        "away_strategy": away_strategy,
        "home_minutes": home_minutes,
        "away_minutes": away_minutes,
        "home_gameplan": home_gameplan,
        "away_gameplan": away_gameplan,
    }


async def _sim_single_game(
    pool,
    game: dict,
    league_id: int,
    season: int,
    news_channel: Optional[discord.TextChannel],
    injury_channel: Optional[discord.TextChannel] = None,
    records_channel: Optional[discord.TextChannel] = None,
    guild: Optional[discord.Guild] = None,
) -> Optional[dict]:
    home_team = await team_repo.get_by_id(pool, game["home_team_id"])
    away_team = await team_repo.get_by_id(pool, game["away_team_id"])
    if not home_team or not away_team:
        log.error(f"Could not load teams for game {game['id']}")
        return None

    await _ensure_lineup(pool, league_id, home_team.id)
    await _ensure_lineup(pool, league_id, away_team.id)

    home_players = await _load_lineup_for_team(pool, league_id, home_team.id)
    away_players = await _load_lineup_for_team(pool, league_id, away_team.id)

    home_ovr = await _compute_team_ovr(pool, league_id, home_team.id)
    away_ovr = await _compute_team_ovr(pool, league_id, away_team.id)

    pre_sim = await _build_pre_sim_inputs(
        pool, league_id, season, game, home_team, away_team, home_players, away_players
    )
    home_gameplan = pre_sim["home_gameplan"]
    away_gameplan = pre_sim["away_gameplan"]

    seed = game.get("rng_seed") or (game["id"] * 31337)
    result = sim_engine.sim_game(
        _team_to_sim_dict(home_team, home_ovr),
        _team_to_sim_dict(away_team, away_ovr),
        home_players,
        away_players,
        seed,
        fatigue=pre_sim["fatigue"],
        home_strategy=pre_sim["home_strategy"],
        away_strategy=pre_sim["away_strategy"],
        home_minutes=pre_sim["home_minutes"],
        away_minutes=pre_sim["away_minutes"],
    )

    # Enrich box lines with player names from already-loaded lineup dicts.
    _name_by_id = {
        p["id"]: f"{p['first_name']} {p['last_name']}"
        for p in home_players + away_players
        if "first_name" in p and "last_name" in p
    }
    for line in result.get("home_box", []) + result.get("away_box", []):
        line["player_name"] = _name_by_id.get(line.get("player_id"), "")

    await _persist_game_result(pool, game, result, home_team, away_team, season, news_channel, injury_channel, records_channel, guild=guild)
    return {
        "game": game,
        "home_team": home_team,
        "away_team": away_team,
        "result": result,
        "home_gameplan": home_gameplan,
        "away_gameplan": away_gameplan,
    }


async def _notify_user_matchup_result(
    bot: discord.Client,
    guild: discord.Guild,
    league_id: int,
    game_result: dict,
    *manager_ids: int,
) -> None:
    """Post the box score to #box-scores and DM any human manager whose game was just simmed."""


    home_team = game_result["home_team"]
    away_team = game_result["away_team"]
    result = game_result["result"]
    game = game_result["game"]

    home_name = home_team.full_name if home_team else "Home Team"
    away_name = away_team.full_name if away_team else "Away Team"

    embed = sim_embeds.game_recap(
        game, home_team, away_team, result["home_score"], result["away_score"]
    )

    pool = await get_pool()
    box_channel = await _get_box_scores_channel(guild, pool, league_id)
    if box_channel:
        await box_channel.send(embed=embed)

    dm_embed = discord.Embed(
        title=f"Game Result: {away_name} @ {home_name}",
        description=(
            f"**{result['away_score']} – {result['home_score']}**\n"
            f"Winner: {'Home' if result['winner_team_id'] == (home_team.id if home_team else None) else 'Away'}"
        ),
        color=discord.Color.green(),
    )
    dm_embed.set_footer(text=f"Game #{game.get('game_index', '?')} | {game.get('scheduled_date', '')} — Check #box-scores for full recap.")

    for user_id in manager_ids:
        await notifier_service.send_dm(
            bot,
            league_id,
            user_id,
            embed=dm_embed,
            fallback_message=f"Your matchup result is in: {away_name} {result['away_score']} @ {home_name} {result['home_score']}. Check #box-scores for the full box score.",
        )




async def _fetch_race_leaders_once(pool, league_id: int, season: int) -> dict:
    """Fetch award race leaders (top 5 per award) once per batch tick.

    Called at the batch flush site and forwarded to both _maybe_post_columnist and
    _maybe_post_potm so _get_eligible_players runs 4x per tick instead of 8x.
    Returns an empty dict on any failure so callers fall back gracefully.
    """
    try:
        return await awards_service.get_race_leaders(pool, league_id, season, top_n=5)
    except Exception as exc:
        log.warning(f"_fetch_race_leaders_once: failed for league={league_id}: {exc}")
        return {}



async def check_user_matchups_in_range(
    pool,
    league_id: int,
    season: int,
    from_idx: int,
    to_idx: int,
) -> List[dict]:
    games = await game_repo.get_games_in_range(pool, league_id, season, from_idx, to_idx)
    return [g for g in games if g.get("is_user_matchup") and g.get("status") == "scheduled"]


async def sim_until_rival(
    league_id: int,
    guild: discord.Guild,
    season: int,
    bot: Optional[discord.Client] = None,
    suppress_matchup_alert: bool = False,
) -> dict:
    pool = await get_pool()
    total_regular_games = await game_repo.get_total_regular_season_games(pool, league_id, season)
    deadline_game_index = await game_repo.get_deadline_game_index(pool, league_id, season)
    _league_phase_row = await pool.fetchrow(
        "SELECT current_phase FROM leagues WHERE id = $1", league_id
    )
    _league_phase = _league_phase_row["current_phase"] if _league_phase_row else ""
    await _maybe_close_trade_window(pool, league_id)
    current_index = await game_repo.get_current_index(pool, league_id, season)
    # Refresh franchise plans once per sim batch so plans stay current as records evolve.
    # Pass current_index so checkpoint detection can track which game window triggered
    # each derive and enforce plan stickiness between checkpoints.
    try:
        await franchise_plan_service.derive_and_persist_all(
            pool, league_id, season, current_game_index=current_index
        )
    except Exception as _fp_exc:
        log.warning("franchise_plan refresh failed (sim_until_rival): %s", _fp_exc)

    next_user_game = await game_repo.get_user_matchup_ahead(pool, league_id, season, current_index)

    if next_user_game is None:
        stop_index = 10000
    else:
        teams = await team_repo.get_all(pool, league_id)
        human_teams = [t for t in teams if t.manager_user_id is not None]
        ready_ids = set(await game_repo.get_ready_teams(pool, league_id))
        all_ready = human_teams and all(t.id in ready_ids for t in human_teams)
        if all_ready:
            stop_index = next_user_game["game_index"]
        else:
            stop_index = next_user_game["game_index"] - 1

    if stop_index < current_index + 1:
        return {"games_simmed": 0, "next_matchup": next_user_game}

    games = await game_repo.get_games_in_range(pool, league_id, season, current_index + 1, stop_index)

    box_channel = await _get_box_scores_channel(guild, pool, league_id)
    standings_channel = await _get_standings_channel(guild, pool, league_id)
    news_channel = await _get_news_channel(guild, pool, league_id)
    injury_channel = await _get_injury_channel(guild, pool, league_id)
    records_channel = await _ensure_records_channel(guild, pool, league_id)

    games_simmed = 0
    user_matchups_simmed = 0
    batch_results = []

    for game in games:
        if game.get("status") == "simmed":
            continue

        sim_result = await _sim_single_game(pool, game, league_id, season, news_channel, injury_channel, records_channel, guild=guild)
        if sim_result is None:
            continue

        games_simmed += 1
        if game.get("is_user_matchup"):
            user_matchups_simmed += 1
        batch_results.append(sim_result)

        if bot and game.get("is_user_matchup"):
            home_t = sim_result["home_team"]
            away_t = sim_result["away_team"]
            manager_ids = []
            if home_t and home_t.manager_user_id:
                manager_ids.append(home_t.manager_user_id)
            if away_t and away_t.manager_user_id:
                manager_ids.append(away_t.manager_user_id)
            if manager_ids:
                await _notify_user_matchup_result(bot, guild, league_id, sim_result, *manager_ids)

        # Yield to the event loop after every game so Discord slash-command interactions
        # can be acknowledged within the 3-second window while sim is running.
        await asyncio.sleep(0)

        if len(batch_results) >= _BOX_SCORE_BATCH_SIZE:
            standings = await game_repo.get_standings(pool, league_id, season)
            first_game_idx = batch_results[0]["game"].get("game_index", 0) if batch_results else 0
            last_game_idx = batch_results[-1]["game"].get("game_index", 0) if batch_results else 0
            # Channel sends gate on channel availability — but trade execution
            # below must NOT be gated, or leagues without #box-scores never run
            # CPU trades during batch sims (task #13).
            if box_channel:
                embed = sim_embeds.batch_recap_with_standings(batch_results, standings)
                try:
                    await box_channel.send(embed=embed)
                except (discord.HTTPException, Exception) as exc:
                    log.warning(f"channel send failed: {exc}")
            if standings_channel:
                try:
                    await standings_channel.send(embed=sim_embeds.standings_snapshot_embed(standings, last_game_idx))
                except (discord.HTTPException, Exception) as exc:
                    log.warning(f"channel send failed: {exc}")
            _race_leaders = await _fetch_race_leaders_once(pool, league_id, season)
            await _maybe_post_columnist(
                pool, league_id, season, batch_results, guild,
                batch_start_index=first_game_idx,
                batch_end_index=last_game_idx,
                total_regular_games=total_regular_games,
                prefetched_race_leaders=_race_leaders,
            )
            _last_game_date = batch_results[-1]["game"].get("scheduled_date")
            _last_game_date_str = str(_last_game_date) if _last_game_date else None
            await _maybe_post_potm(pool, guild, league_id, season, _last_game_date_str, current_game_index=last_game_idx, prefetched_race_leaders=_race_leaders)
            # Mid-batch: skip block refresh — fires once at the final flush below.
            await _maybe_run_cpu_trades(pool, league_id, season, last_game_idx, total_regular_games, deadline_game_index, guild, refresh_block=False)
            await _maybe_advance_trade_deadline(pool, league_id, last_game_idx, deadline_game_index, news_channel)
            await _maybe_snapshot_teams(pool, league_id, season, last_game_idx)
            await _maybe_post_coach_beat(pool, league_id, season, batch_results, guild)
            await _maybe_post_power_list(pool, league_id, season, batch_results, guild)
            await _maybe_post_rookie_watch(pool, league_id, season, batch_results, guild)
            await _maybe_post_big_picture(pool, league_id, season, batch_results, guild)
            await _maybe_post_ledger(pool, league_id, season, batch_results, guild)
            await _maybe_post_the_race(pool, league_id, season, batch_results, guild)
            batch_results = []

    if batch_results:
        standings = await game_repo.get_standings(pool, league_id, season)
        first_game_idx = batch_results[0]["game"].get("game_index", 0) if batch_results else 0
        last_game_idx = batch_results[-1]["game"].get("game_index", 0) if batch_results else 0
        if box_channel:
            embed = sim_embeds.batch_recap_with_standings(batch_results, standings)
            try:
                await box_channel.send(embed=embed)
            except (discord.HTTPException, Exception) as exc:
                log.warning(f"channel send failed: {exc}")
        if standings_channel:
            try:
                await standings_channel.send(embed=sim_embeds.standings_snapshot_embed(standings, last_game_idx))
            except (discord.HTTPException, Exception) as exc:
                log.warning(f"channel send failed: {exc}")
        _race_leaders = await _fetch_race_leaders_once(pool, league_id, season)
        await _maybe_post_columnist(
            pool, league_id, season, batch_results, guild,
            batch_start_index=first_game_idx,
            batch_end_index=last_game_idx,
            total_regular_games=total_regular_games,
            prefetched_race_leaders=_race_leaders,
        )
        _last_game_date = batch_results[-1]["game"].get("scheduled_date")
        _last_game_date_str = str(_last_game_date) if _last_game_date else None
        await _maybe_post_potm(pool, guild, league_id, season, _last_game_date_str, current_game_index=last_game_idx, prefetched_race_leaders=_race_leaders)
        # Final flush: run block refresh now (only time it fires this sim call).
        await _maybe_run_cpu_trades(pool, league_id, season, last_game_idx, total_regular_games, deadline_game_index, guild, refresh_block=True)
        await _maybe_advance_trade_deadline(pool, league_id, last_game_idx, deadline_game_index, news_channel)
        await _maybe_snapshot_teams(pool, league_id, season, last_game_idx)
        await _maybe_post_coach_beat(pool, league_id, season, batch_results, guild)
        await _maybe_post_power_list(pool, league_id, season, batch_results, guild)
        await _maybe_post_rookie_watch(pool, league_id, season, batch_results, guild)
        await _maybe_post_big_picture(pool, league_id, season, batch_results, guild)
        await _maybe_post_ledger(pool, league_id, season, batch_results, guild)
        await _maybe_post_the_race(pool, league_id, season, batch_results, guild)

    season_complete = await _maybe_advance_season_complete(pool, league_id, season, news_channel)

    if next_user_game and news_channel and not season_complete and not suppress_matchup_alert:
        home_team = await team_repo.get_by_id(pool, next_user_game["home_team_id"])
        away_team = await team_repo.get_by_id(pool, next_user_game["away_team_id"])
        home_manager = guild.get_member(home_team.manager_user_id) if home_team and home_team.manager_user_id else None
        away_manager = guild.get_member(away_team.manager_user_id) if away_team and away_team.manager_user_id else None

        embed = sim_embeds.matchup_alert(next_user_game, home_team, away_team, home_manager, away_manager)
        try:
            await news_channel.send(embed=embed)
        except (discord.HTTPException, Exception) as exc:
            log.warning(f"channel send failed: {exc}")

    return {"games_simmed": games_simmed, "user_matchups_simmed": user_matchups_simmed, "next_matchup": next_user_game, "season_complete": season_complete}


async def sim_range(
    league_id: int,
    guild: discord.Guild,
    season: int,
    to_game_index: int,
    bot: Optional[discord.Client] = None,
    force: bool = False,
) -> dict:
    pool = await get_pool()
    total_regular_games = await game_repo.get_total_regular_season_games(pool, league_id, season)
    deadline_game_index = await game_repo.get_deadline_game_index(pool, league_id, season)
    _league_phase_row = await pool.fetchrow(
        "SELECT current_phase FROM leagues WHERE id = $1", league_id
    )
    _league_phase = _league_phase_row["current_phase"] if _league_phase_row else ""
    news_channel = await _get_news_channel(guild, pool, league_id)
    await _maybe_close_trade_window(pool, league_id, news_channel)
    current_index = await game_repo.get_current_index(pool, league_id, season)
    # Refresh franchise plans once per sim batch so plans stay current as records evolve.
    # Pass current_index so checkpoint detection can track which game window triggered
    # each derive and enforce plan stickiness between checkpoints.
    try:
        await franchise_plan_service.derive_and_persist_all(
            pool, league_id, season, current_game_index=current_index
        )
    except Exception as _fp_exc:
        log.warning("franchise_plan refresh failed (sim_range): %s", _fp_exc)

    if not force:
        user_matchups = await check_user_matchups_in_range(
            pool, league_id, season, current_index + 1, to_game_index
        )
        if user_matchups:
            return {"warning": True, "user_matchups": user_matchups, "games_simmed": 0}

    games = await game_repo.get_games_in_range(pool, league_id, season, current_index + 1, to_game_index)

    box_channel = await _get_box_scores_channel(guild, pool, league_id)
    standings_channel = await _get_standings_channel(guild, pool, league_id)
    news_channel = await _get_news_channel(guild, pool, league_id)
    injury_channel = await _get_injury_channel(guild, pool, league_id)
    records_channel = await _ensure_records_channel(guild, pool, league_id)

    games_simmed = 0
    user_matchups_simmed = 0
    batch_results = []

    for game in games:
        if game.get("status") == "simmed":
            continue

        sim_result = await _sim_single_game(pool, game, league_id, season, news_channel, injury_channel, records_channel, guild=guild)
        if sim_result is None:
            continue

        games_simmed += 1
        if game.get("is_user_matchup"):
            user_matchups_simmed += 1
        batch_results.append(sim_result)

        if bot and game.get("is_user_matchup"):
            home_t = sim_result["home_team"]
            away_t = sim_result["away_team"]
            manager_ids = []
            if home_t and home_t.manager_user_id:
                manager_ids.append(home_t.manager_user_id)
            if away_t and away_t.manager_user_id:
                manager_ids.append(away_t.manager_user_id)
            if manager_ids:
                await _notify_user_matchup_result(bot, guild, league_id, sim_result, *manager_ids)

        # Yield to the event loop after every game so Discord slash-command interactions
        # can be acknowledged within the 3-second window while sim is running.
        await asyncio.sleep(0)

        if len(batch_results) >= _BOX_SCORE_BATCH_SIZE:
            standings = await game_repo.get_standings(pool, league_id, season)
            first_game_idx = batch_results[0]["game"].get("game_index", 0) if batch_results else 0
            last_game_idx = batch_results[-1]["game"].get("game_index", 0) if batch_results else 0
            # Channel sends gate on channel availability; trade execution below
            # must NOT be gated, or leagues without #box-scores never run CPU
            # trades during batch sims (task #13).
            if box_channel:
                embed = sim_embeds.batch_recap_with_standings(batch_results, standings)
                try:
                    await box_channel.send(embed=embed)
                except (discord.HTTPException, Exception) as exc:
                    log.warning(f"channel send failed: {exc}")
            if standings_channel:
                try:
                    await standings_channel.send(embed=sim_embeds.standings_snapshot_embed(standings, last_game_idx))
                except (discord.HTTPException, Exception) as exc:
                    log.warning(f"channel send failed: {exc}")
            _race_leaders = await _fetch_race_leaders_once(pool, league_id, season)
            await _maybe_post_columnist(
                pool, league_id, season, batch_results, guild,
                batch_start_index=first_game_idx,
                batch_end_index=last_game_idx,
                total_regular_games=total_regular_games,
                force=force,
                prefetched_race_leaders=_race_leaders,
            )
            _last_game_date = batch_results[-1]["game"].get("scheduled_date")
            _last_game_date_str = str(_last_game_date) if _last_game_date else None
            await _maybe_post_potm(pool, guild, league_id, season, _last_game_date_str, current_game_index=last_game_idx, prefetched_race_leaders=_race_leaders)
            # Mid-batch: skip block refresh — fires once at the final flush below.
            await _maybe_run_cpu_trades(pool, league_id, season, last_game_idx, total_regular_games, deadline_game_index, guild, refresh_block=False)
            await _maybe_advance_trade_deadline(pool, league_id, last_game_idx, deadline_game_index, news_channel)
            await _maybe_snapshot_teams(pool, league_id, season, last_game_idx)
            await _maybe_post_coach_beat(pool, league_id, season, batch_results, guild)
            await _maybe_post_power_list(pool, league_id, season, batch_results, guild)
            await _maybe_post_rookie_watch(pool, league_id, season, batch_results, guild)
            await _maybe_post_big_picture(pool, league_id, season, batch_results, guild)
            await _maybe_post_ledger(pool, league_id, season, batch_results, guild)
            await _maybe_post_the_race(pool, league_id, season, batch_results, guild)
            batch_results = []

    if batch_results:
        standings = await game_repo.get_standings(pool, league_id, season)
        first_game_idx = batch_results[0]["game"].get("game_index", 0) if batch_results else 0
        last_game_idx = batch_results[-1]["game"].get("game_index", 0) if batch_results else 0
        if box_channel:
            embed = sim_embeds.batch_recap_with_standings(batch_results, standings)
            try:
                await box_channel.send(embed=embed)
            except (discord.HTTPException, Exception) as exc:
                log.warning(f"channel send failed: {exc}")
        if standings_channel:
            try:
                await standings_channel.send(embed=sim_embeds.standings_snapshot_embed(standings, last_game_idx))
            except (discord.HTTPException, Exception) as exc:
                log.warning(f"channel send failed: {exc}")
        _race_leaders = await _fetch_race_leaders_once(pool, league_id, season)
        await _maybe_post_columnist(
            pool, league_id, season, batch_results, guild,
            batch_start_index=first_game_idx,
            batch_end_index=last_game_idx,
            total_regular_games=total_regular_games,
            force=force,
            prefetched_race_leaders=_race_leaders,
        )
        _last_game_date = batch_results[-1]["game"].get("scheduled_date")
        _last_game_date_str = str(_last_game_date) if _last_game_date else None
        await _maybe_post_potm(pool, guild, league_id, season, _last_game_date_str, current_game_index=last_game_idx, prefetched_race_leaders=_race_leaders)
        # Final flush: run block refresh now (only time it fires this sim call).
        await _maybe_run_cpu_trades(pool, league_id, season, last_game_idx, total_regular_games, deadline_game_index, guild, refresh_block=True)
        await _maybe_advance_trade_deadline(pool, league_id, last_game_idx, deadline_game_index, news_channel)
        await _maybe_snapshot_teams(pool, league_id, season, last_game_idx)
        await _maybe_post_coach_beat(pool, league_id, season, batch_results, guild)
        await _maybe_post_power_list(pool, league_id, season, batch_results, guild)
        await _maybe_post_rookie_watch(pool, league_id, season, batch_results, guild)
        await _maybe_post_big_picture(pool, league_id, season, batch_results, guild)
        await _maybe_post_ledger(pool, league_id, season, batch_results, guild)
        await _maybe_post_the_race(pool, league_id, season, batch_results, guild)

    season_complete = await _maybe_advance_season_complete(pool, league_id, season, news_channel)
    return {"warning": False, "games_simmed": games_simmed, "user_matchups_simmed": user_matchups_simmed, "user_matchups": [], "season_complete": season_complete}


async def sim_single_matchup(
    league_id: int,
    guild: discord.Guild,
    season: int,
) -> Optional[dict]:
    """Sim the next pending user matchup. Returns the full game result dict, or None if no matchup is ready."""
    pool = await get_pool()
    current_index = await game_repo.get_current_index(pool, league_id, season)
    games = await game_repo.get_games_in_range(pool, league_id, season, current_index + 1, current_index + 1)
    if not games:
        return None
    game = games[0]
    if not game.get("is_user_matchup") or game.get("status") == "simmed":
        return None
    news_channel = await _get_news_channel(guild, pool, league_id)
    injury_channel = await _get_injury_channel(guild, pool, league_id)
    records_channel = await _ensure_records_channel(guild, pool, league_id)
    return await _sim_single_game(pool, game, league_id, season, news_channel, injury_channel, records_channel, guild=guild)
