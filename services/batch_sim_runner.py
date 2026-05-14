from __future__ import annotations

import datetime
from typing import List, Optional

import discord

from core.logging import get_logger
from data.db import get_pool
from data.repositories import game_repo, league_repo, player_repo, strategy_repo, team_repo
from phase.states import Phase
from services import league_service, records_service, sim_engine, storyline_service, strategy_service
from bot.embeds import sim_embeds

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


async def _ensure_lineup(pool, league_id: int, team_id: int) -> None:
    """Auto-populate lineups for a team that has none, using top players by OVR."""
    count = await pool.fetchval(
        "SELECT COUNT(*) FROM lineups WHERE league_id=$1 AND team_id=$2",
        league_id,
        team_id,
    )
    if count > 0:
        return

    players = await player_repo.get_roster(pool, league_id, team_id)
    if not players:
        return

    for slot, player in enumerate(players[:15], start=1):
        await pool.execute(
            """
            INSERT INTO lineups (league_id, team_id, is_starter, slot, player_id, set_by)
            VALUES ($1, $2, $3, $4, $5, NULL)
            ON CONFLICT (league_id, team_id, slot) DO NOTHING
            """,
            league_id,
            team_id,
            slot <= 5,
            slot,
            player.id,
        )


def _apply_directives(p: dict) -> dict:
    """Apply manager directives as effective-tendency overrides. Modifies in place."""
    shot_diet = p.get("shot_diet") or "auto"
    usage_mode = p.get("usage_mode") or "normal"
    defense_mode = p.get("defense_mode") or "standard"
    role_mode = p.get("role_mode") or "scorer"
    clutch_mode = p.get("clutch_mode") or "normal"

    def clamp(v: int) -> int:
        return max(0, min(100, v))

    if shot_diet == "force_3s":
        p["tendency_3pt"] = clamp(p.get("tendency_3pt", 50) + 25)
        p["tendency_mid"] = clamp(p.get("tendency_mid", 50) - 15)
        p["tendency_drive"] = clamp(p.get("tendency_drive", 50) - 10)
    elif shot_diet == "attack_rim":
        p["tendency_drive"] = clamp(p.get("tendency_drive", 50) + 25)
        p["tendency_3pt"] = clamp(p.get("tendency_3pt", 50) - 25)
        p["tendency_mid"] = clamp(p.get("tendency_mid", 50) - 10)
    elif shot_diet == "post_heavy":
        p["tendency_post"] = clamp(p.get("tendency_post", 20) + 30)
        p["tendency_3pt"] = clamp(p.get("tendency_3pt", 50) - 20)
    elif shot_diet == "midrange":
        p["tendency_mid"] = clamp(p.get("tendency_mid", 50) + 25)
        p["tendency_3pt"] = clamp(p.get("tendency_3pt", 50) - 15)

    if usage_mode == "feature":
        p["usage_weight"] = clamp(int(p.get("usage_weight", 50) * 1.4))
    elif usage_mode == "conserve":
        p["usage_weight"] = clamp(int(p.get("usage_weight", 50) * 0.6))

    if defense_mode == "lockdown":
        p["defensive_effort"] = clamp(p.get("defensive_effort", 50) + 20)
        # slight offensive penalty — reduce usage a touch
        p["usage_weight"] = clamp(p.get("usage_weight", 50) - 5)
    elif defense_mode == "off":
        p["defensive_effort"] = clamp(p.get("defensive_effort", 50) - 20)
        p["usage_weight"] = clamp(p.get("usage_weight", 50) + 5)

    if role_mode == "creator":
        p["tendency_pass"] = clamp(p.get("tendency_pass", 50) + 20)
        p["usage_weight"] = clamp(p.get("usage_weight", 50) + 5)
    elif role_mode == "spot_up":
        p["tendency_3pt"] = clamp(p.get("tendency_3pt", 50) + 15)
        p["tendency_pass"] = clamp(p.get("tendency_pass", 50) - 25)
    elif role_mode == "scorer":
        p["tendency_pass"] = clamp(p.get("tendency_pass", 50) - 10)
        p["usage_weight"] = clamp(p.get("usage_weight", 50) + 5)

    if clutch_mode == "hero":
        p["clutch_rating"] = clamp(p.get("clutch_rating", 50) + 20)
    elif clutch_mode == "hide":
        p["clutch_rating"] = clamp(p.get("clutch_rating", 50) - 30)
        p["usage_weight"] = clamp(int(p.get("usage_weight", 50) * 0.7))

    return p


async def _load_lineup_for_team(pool, league_id: int, team_id: int) -> List[dict]:
    """Load players in lineup order for a team, returning dicts the sim engine expects.

    LEFT JOINs player_directives so tendency overrides are available pre-sim.
    _apply_directives is called on each player to fold directives into tendency fields.
    """
    rows = await pool.fetch(
        """
        SELECT p.*, l.is_starter, l.slot,
               pd.shot_diet, pd.usage_mode, pd.defense_mode, pd.role_mode, pd.clutch_mode
        FROM lineups l
        JOIN players p ON p.id = l.player_id
        LEFT JOIN player_directives pd ON pd.league_id = $1 AND pd.player_id = p.id
        WHERE l.league_id = $1 AND l.team_id = $2
        ORDER BY l.slot ASC
        """,
        league_id,
        team_id,
    )
    return [_apply_directives(dict(r)) for r in rows]


def _team_to_sim_dict(team: team_repo.Team) -> dict:
    return {
        "team_id": team.id,
        "overall": 75,
        "offense_rating": team.team_offense_rating or 75,
        "defense_rating": team.team_defense_rating or 75,
        "pace": team.pace or 100.0,
    }


async def _persist_injuries(
    pool,
    game: dict,
    game_id: int,
    season: int,
    result: dict,
    news_channel: Optional[discord.TextChannel],
) -> None:
    raw_injuries = result.get("injuries", [])
    if not raw_injuries:
        return

    import random as _random
    rng = _random.Random(game_id)

    game_date: datetime.date = game.get("scheduled_date") or datetime.date.today()
    rows: list[dict] = []
    for inj in raw_injuries:
        severity = inj["severity"]
        lo, hi = _INJURY_GAMES_MISSED[severity]
        games_missed = lo if lo == hi else rng.randint(lo, hi)
        start_date = game_date
        return_date = start_date + datetime.timedelta(days=games_missed)
        affects_prog = severity in {"week_4_8", "season_ending"}

        rows.append({
            "league_id": game["league_id"],
            "season": season,
            "player_id": inj["player_id"],
            "team_id": inj["team_id"],
            "severity": severity,
            "games_missed": games_missed,
            "incurred_in_game_id": game_id,
            "start_date": start_date,
            "return_date": return_date,
            "affects_progression": affects_prog,
        })

        if severity in _ANNOUNCE_SEVERITIES and news_channel:
            player_row = await pool.fetchrow(
                "SELECT first_name, last_name, team_id FROM players WHERE id = $1",
                inj["player_id"],
            )
            if player_row:
                player_name = f"{player_row['first_name']} {player_row['last_name']}"
                team_code_row = await pool.fetchrow(
                    "SELECT nba_team_code FROM teams WHERE id = $1", player_row["team_id"]
                )
                team_code = team_code_row["nba_team_code"] if team_code_row else "???"
            else:
                player_name = f"Player #{inj['player_id']}"
                team_code = "???"

            human_severity = _SEVERITY_LABELS.get(severity, severity)
            embed = discord.Embed(
                title="🏥 Injury Report",
                color=discord.Color.red(),
                description=f"**{player_name}** ({team_code}) — {human_severity}",
            )
            gms_label = "Season" if games_missed >= 82 else str(games_missed)
            embed.add_field(name="Games Missed", value=gms_label, inline=True)
            embed.add_field(name="Status", value=human_severity, inline=True)
            embed.set_footer(text=f"Game #{game.get('game_index', game_id)}")
            await news_channel.send(embed=embed)

    await game_repo.insert_injuries(pool, rows)


async def _persist_game_result(
    pool,
    game: dict,
    result: dict,
    home_team: team_repo.Team,
    away_team: team_repo.Team,
    season: int,
    news_channel: Optional[discord.TextChannel],
) -> dict:
    game_id = game["id"]
    await game_repo.mark_simmed(
        pool,
        game_id,
        result["home_score"],
        result["away_score"],
        result["winner_team_id"],
        game.get("rng_seed") or 0,
    )

    all_box = result["home_box"] + result["away_box"]
    if all_box:
        await game_repo.insert_box_scores(pool, game_id, all_box)

    def _sum_stats(box: List[dict]) -> dict:
        keys = ["points", "rebounds_off", "rebounds_def", "assists", "steals",
                "blocks", "turnovers", "fouls", "fga", "fgm", "tpa", "tpm", "fta", "ftm"]
        out: dict = {k: sum(line.get(k, 0) for line in box) for k in keys}
        out["minutes"] = 240.0
        out["plus_minus"] = result["home_score"] - result["away_score"]
        return out

    if result["home_box"]:
        await game_repo.insert_team_game_stats(pool, game_id, home_team.id, _sum_stats(result["home_box"]))
    if result["away_box"]:
        away_stats = _sum_stats(result["away_box"])
        away_stats["plus_minus"] = result["away_score"] - result["home_score"]
        await game_repo.insert_team_game_stats(pool, game_id, away_team.id, away_stats)

    game_result = {
        "game_id": game_id,
        "home_team_id": home_team.id,
        "away_team_id": away_team.id,
        "winner_team_id": result["winner_team_id"],
        "home_conference": home_team.conference,
        "away_conference": away_team.conference,
        "home_division": home_team.division,
        "away_division": away_team.division,
        "home_score": result["home_score"],
        "away_score": result["away_score"],
    }
    standings_update = await game_repo.update_standings(pool, game["league_id"], season, game_result)

    notable_streak = standings_update.get("notable_streak")
    if notable_streak and news_channel:
        streak_team_id, streak_len = notable_streak
        streak_team = await team_repo.get_by_id(pool, streak_team_id)
        streak_name = streak_team.full_name if streak_team else f"Team {streak_team_id}"
        embed = discord.Embed(
            title="🔥 Win Streak",
            color=discord.Color.gold(),
            description=f"**{streak_name}** has won **{streak_len}** straight!",
        )
        await news_channel.send(embed=embed)

    await _persist_injuries(pool, game, game_id, season, result, news_channel)

    record_announcements = await records_service.check_and_update_records(
        pool, game["league_id"], season, game_id, result
    )
    for announcement in record_announcements:
        if news_channel:
            await news_channel.send(embed=sim_embeds.season_record_embed(announcement))

    return standings_update


async def _sim_single_game(
    pool,
    game: dict,
    league_id: int,
    season: int,
    news_channel: Optional[discord.TextChannel],
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

    game_date = game.get("scheduled_date")
    fatigue = {
        "home_b2b": await game_repo.is_back_to_back(pool, league_id, season, game["home_team_id"], game_date),
        "away_b2b": await game_repo.is_back_to_back(pool, league_id, season, game["away_team_id"], game_date),
    }

    home_strategy = await strategy_service.get_sim_modifiers(pool, league_id, home_team.id)
    away_strategy = await strategy_service.get_sim_modifiers(pool, league_id, away_team.id)

    home_player_ids = [p["id"] for p in home_players]
    away_player_ids = [p["id"] for p in away_players]
    home_minutes = await strategy_repo.get_team_minutes_plan(pool, league_id, home_team.id, home_player_ids)
    away_minutes = await strategy_repo.get_team_minutes_plan(pool, league_id, away_team.id, away_player_ids)

    seed = game.get("rng_seed") or (game["id"] * 31337)
    result = sim_engine.sim_game(
        _team_to_sim_dict(home_team),
        _team_to_sim_dict(away_team),
        home_players,
        away_players,
        seed,
        fatigue=fatigue,
        home_strategy=home_strategy,
        away_strategy=away_strategy,
        home_minutes=home_minutes,
        away_minutes=away_minutes,
    )

    # Enrich box lines with player names from already-loaded lineup dicts.
    _name_by_id = {
        p["id"]: f"{p['first_name']} {p['last_name']}"
        for p in home_players + away_players
        if "first_name" in p and "last_name" in p
    }
    for line in result.get("home_box", []) + result.get("away_box", []):
        line["player_name"] = _name_by_id.get(line.get("player_id"), "")

    await _persist_game_result(pool, game, result, home_team, away_team, season, news_channel)
    return {
        "game": game,
        "home_team": home_team,
        "away_team": away_team,
        "result": result,
    }


async def _notify_user_matchup_result(
    bot: discord.Client,
    guild: discord.Guild,
    league_id: int,
    game_result: dict,
    home_manager_id: int,
    away_manager_id: int,
) -> None:
    """Post the box score to #box-scores and DM both managers after their game is simmed."""
    from services import notifier_service

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

    for user_id in (home_manager_id, away_manager_id):
        await notifier_service.send_dm(
            bot,
            league_id,
            user_id,
            embed=dm_embed,
            fallback_message=f"Your matchup result is in: {away_name} {result['away_score']} @ {home_name} {result['home_score']}. Check #box-scores for the full box score.",
        )


async def _get_box_scores_channel(guild: discord.Guild, pool, league_id: int) -> Optional[discord.TextChannel]:
    channel_id = await league_repo.get_channel(pool, league_id, "box-scores")
    if not channel_id:
        return None
    return guild.get_channel(channel_id)


async def _get_news_channel(guild: discord.Guild, pool, league_id: int) -> Optional[discord.TextChannel]:
    channel_id = await league_repo.get_channel(pool, league_id, "league-news")
    if not channel_id:
        return None
    return guild.get_channel(channel_id)


async def _maybe_post_storylines(
    pool,
    league_id: int,
    batch_results: list[dict],
    news_channel: Optional[discord.TextChannel],
) -> None:
    """Build team name lookup and post a sim recap storylines embed if any are generated."""
    if not batch_results or not news_channel:
        return

    team_ids = set()
    for br in batch_results:
        team_ids.add(br["home_team"].id)
        team_ids.add(br["away_team"].id)

    teams_by_id: dict[int, dict] = {}
    for br in batch_results:
        ht = br["home_team"]
        at = br["away_team"]
        teams_by_id[ht.id] = {"name": ht.name if hasattr(ht, "name") else f"Team {ht.id}"}
        teams_by_id[at.id] = {"name": at.name if hasattr(at, "name") else f"Team {at.id}"}

    game_dicts = []
    for br in batch_results:
        r = br["result"]
        game_dicts.append({
            "home_score": r["home_score"],
            "away_score": r["away_score"],
            "home_team_id": br["home_team"].id,
            "away_team_id": br["away_team"].id,
            "winner_team_id": r["winner_team_id"],
            "home_box": r.get("home_box", []),
            "away_box": r.get("away_box", []),
        })

    storylines = await storyline_service.generate_storylines_ai(game_dicts, teams_by_id)
    recap = sim_embeds.sim_recap_embed(storylines)
    if recap:
        await news_channel.send(embed=recap)


async def _maybe_advance_season_complete(
    pool,
    league_id: int,
    season: int,
    news_channel: Optional[discord.TextChannel],
) -> bool:
    """
    If all regular season games are now simmed, advance the league phase to
    REGULAR_SEASON_COMPLETE and post an announcement.
    Returns True when the phase was advanced.
    """
    if not await game_repo.all_regular_season_games_complete(pool, league_id, season):
        return False

    await league_service.advance_phase(league_id, Phase.REGULAR_SEASON_COMPLETE.value)
    log.info(f"League {league_id} season {season}: auto-advanced to REGULAR_SEASON_COMPLETE")

    if news_channel:
        await news_channel.send(embed=sim_embeds.regular_season_complete_embed())
    return True


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

    current_index = await game_repo.get_current_index(pool, league_id, season)
    next_user_game = await game_repo.get_user_matchup_ahead(pool, league_id, season, current_index)

    if next_user_game is None:
        stop_index = 10000
    else:
        stop_index = next_user_game["game_index"] - 1

    if stop_index < current_index + 1:
        return {"games_simmed": 0, "next_matchup": next_user_game}

    games = await game_repo.get_games_in_range(pool, league_id, season, current_index + 1, stop_index)

    box_channel = await _get_box_scores_channel(guild, pool, league_id)
    news_channel = await _get_news_channel(guild, pool, league_id)

    games_simmed = 0
    batch_results = []

    for game in games:
        if game.get("status") == "simmed":
            continue

        sim_result = await _sim_single_game(pool, game, league_id, season, news_channel)
        if sim_result is None:
            continue

        games_simmed += 1
        batch_results.append(sim_result)

        if bot and game.get("is_user_matchup"):
            home_t = sim_result["home_team"]
            away_t = sim_result["away_team"]
            if home_t and home_t.manager_user_id and away_t and away_t.manager_user_id:
                await _notify_user_matchup_result(
                    bot, guild, league_id, sim_result,
                    home_t.manager_user_id, away_t.manager_user_id,
                )

        if len(batch_results) >= _BOX_SCORE_BATCH_SIZE and box_channel:
            embed = sim_embeds.batch_recap(
                batch_results,
                f"Games {batch_results[0]['game']['game_index']}–{batch_results[-1]['game']['game_index']}",
            )
            await box_channel.send(embed=embed)
            await _maybe_post_storylines(pool, league_id, batch_results, news_channel)
            batch_results = []

    if batch_results and box_channel:
        embed = sim_embeds.batch_recap(
            batch_results,
            f"Games {batch_results[0]['game']['game_index']}–{batch_results[-1]['game']['game_index']}",
        )
        await box_channel.send(embed=embed)
        await _maybe_post_storylines(pool, league_id, batch_results, news_channel)

    season_complete = await _maybe_advance_season_complete(pool, league_id, season, news_channel)

    if next_user_game and news_channel and not season_complete and not suppress_matchup_alert:
        home_team = await team_repo.get_by_id(pool, next_user_game["home_team_id"])
        away_team = await team_repo.get_by_id(pool, next_user_game["away_team_id"])
        home_manager = guild.get_member(home_team.manager_user_id) if home_team and home_team.manager_user_id else None
        away_manager = guild.get_member(away_team.manager_user_id) if away_team and away_team.manager_user_id else None

        embed = sim_embeds.matchup_alert(next_user_game, home_team, away_team, home_manager, away_manager)
        await news_channel.send(embed=embed)

    return {"games_simmed": games_simmed, "next_matchup": next_user_game, "season_complete": season_complete}


async def sim_range(
    league_id: int,
    guild: discord.Guild,
    season: int,
    to_game_index: int,
    bot: Optional[discord.Client] = None,
    force: bool = False,
) -> dict:
    pool = await get_pool()

    current_index = await game_repo.get_current_index(pool, league_id, season)

    if not force:
        user_matchups = await check_user_matchups_in_range(
            pool, league_id, season, current_index + 1, to_game_index
        )
        if user_matchups:
            return {"warning": True, "user_matchups": user_matchups, "games_simmed": 0}

    games = await game_repo.get_games_in_range(pool, league_id, season, current_index + 1, to_game_index)

    box_channel = await _get_box_scores_channel(guild, pool, league_id)
    news_channel = await _get_news_channel(guild, pool, league_id)

    games_simmed = 0
    batch_results = []

    for game in games:
        if game.get("status") == "simmed":
            continue

        sim_result = await _sim_single_game(pool, game, league_id, season, news_channel)
        if sim_result is None:
            continue

        games_simmed += 1
        batch_results.append(sim_result)

        if bot and game.get("is_user_matchup"):
            home_t = sim_result["home_team"]
            away_t = sim_result["away_team"]
            if home_t and home_t.manager_user_id and away_t and away_t.manager_user_id:
                await _notify_user_matchup_result(
                    bot, guild, league_id, sim_result,
                    home_t.manager_user_id, away_t.manager_user_id,
                )

        if len(batch_results) >= _BOX_SCORE_BATCH_SIZE and box_channel:
            embed = sim_embeds.batch_recap(
                batch_results,
                f"Games {batch_results[0]['game']['game_index']}–{batch_results[-1]['game']['game_index']}",
            )
            await box_channel.send(embed=embed)
            await _maybe_post_storylines(pool, league_id, batch_results, news_channel)
            batch_results = []

    if batch_results and box_channel:
        embed = sim_embeds.batch_recap(
            batch_results,
            f"Games {batch_results[0]['game']['game_index']}–{batch_results[-1]['game']['game_index']}",
        )
        await box_channel.send(embed=embed)
        await _maybe_post_storylines(pool, league_id, batch_results, news_channel)

    season_complete = await _maybe_advance_season_complete(pool, league_id, season, news_channel)
    return {"warning": False, "games_simmed": games_simmed, "user_matchups": [], "season_complete": season_complete}


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
    return await _sim_single_game(pool, game, league_id, season, news_channel)
