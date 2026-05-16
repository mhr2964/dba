from __future__ import annotations

import datetime
import json
from typing import List, Optional

import discord

from core.logging import get_logger
from data.db import get_pool
from data.repositories import game_repo, league_repo, player_repo, strategy_repo, team_repo
from phase.states import Phase
from services import awards_service, columnist_service, cpu_trade_service, league_service, potm_service, records_service, sim_engine, strategy_service
from services.personas import PERSONAS as _PERSONAS
from bot.embeds import awards_embeds, sim_embeds

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

# Tracks games processed so Marcus Brooks fires every ~200 games (every 20 batches of 10).
_marcus_game_counter: int = 0

# Columnist rotation — cycles through these personas on every batch.
_COLUMNIST_ROTATION = ["maya_chen", "jordan_rivera", "keisha_williams", "hot_take_hour", "pat_chen"]
_columnist_rotation_index: int = 0

# Playoff columnist rotation — cycles through recap-capable personas for post-game coverage.
_PLAYOFF_COLUMNIST_ROTATION = ["maya_chen", "jordan_rivera", "keisha_williams"]
_playoff_rotation_index: int = 0


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
    injury_channel: Optional[discord.TextChannel],
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

        if severity in _ANNOUNCE_SEVERITIES and injury_channel:
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
            await injury_channel.send(embed=embed)

            # Ping the team manager if this is a managed team.
            _mgr_row = await pool.fetchrow(
                "SELECT manager_user_id FROM teams WHERE id = $1", inj["team_id"]
            )
            if _mgr_row and _mgr_row["manager_user_id"]:
                await injury_channel.send(
                    f"<@{_mgr_row['manager_user_id']}> — **{player_name}** just went down."
                )

    await game_repo.insert_injuries(pool, rows)


async def _persist_game_result(
    pool,
    game: dict,
    result: dict,
    home_team: team_repo.Team,
    away_team: team_repo.Team,
    season: int,
    news_channel: Optional[discord.TextChannel],
    injury_channel: Optional[discord.TextChannel] = None,
) -> dict:
    game_id = game["id"]
    _quarter_keys = ("q1_home", "q1_away", "q2_home", "q2_away",
                     "q3_home", "q3_away", "q4_home", "q4_away", "ot_home", "ot_away")
    quarters = {k: result.get(k) for k in _quarter_keys}
    await game_repo.mark_simmed(
        pool,
        game_id,
        result["home_score"],
        result["away_score"],
        result["winner_team_id"],
        game.get("rng_seed") or 0,
        quarters=quarters,
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

    await _persist_injuries(pool, game, game_id, season, result, injury_channel or news_channel)

    # Inject team IDs that records_service needs to resolve team names.
    # sim_engine result has winner_team_id but not home_team_id/away_team_id.
    result["home_team_id"] = home_team.id
    result["away_team_id"] = away_team.id

    record_announcements, at_announcements = await records_service.check_and_update_records(
        pool, game["league_id"], season, game_id, result
    )
    for announcement in record_announcements:
        if news_channel:
            await news_channel.send(embed=sim_embeds.season_record_embed(announcement))
    for at_announcement in at_announcements:
        if news_channel:
            await news_channel.send(embed=sim_embeds.season_record_embed(at_announcement))

    return standings_update


async def _sim_single_game(
    pool,
    game: dict,
    league_id: int,
    season: int,
    news_channel: Optional[discord.TextChannel],
    injury_channel: Optional[discord.TextChannel] = None,
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

    home_strategy = await strategy_service.get_sim_modifiers(pool, league_id, home_team.id, players=home_players)
    away_strategy = await strategy_service.get_sim_modifiers(pool, league_id, away_team.id, players=away_players)

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

    await _persist_game_result(pool, game, result, home_team, away_team, season, news_channel, injury_channel)
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


async def _get_standings_channel(guild: discord.Guild, pool, league_id: int) -> Optional[discord.TextChannel]:
    channel_id = await league_repo.get_channel(pool, league_id, "standings")
    if not channel_id:
        return None
    return guild.get_channel(channel_id)


async def _get_news_channel(guild: discord.Guild, pool, league_id: int) -> Optional[discord.TextChannel]:
    channel_id = await league_repo.get_channel(pool, league_id, "league-news")
    if not channel_id:
        return None
    return guild.get_channel(channel_id)


async def _get_injury_channel(guild: discord.Guild, pool, league_id: int) -> Optional[discord.TextChannel]:
    channel_id = await league_repo.get_channel(pool, league_id, "injuries")
    if not channel_id:
        return None
    return guild.get_channel(channel_id) or await _get_news_channel(guild, pool, league_id)


async def _get_transactions_channel(guild: discord.Guild, pool, league_id: int) -> Optional[discord.TextChannel]:
    channel_id = await league_repo.get_channel(pool, league_id, "transactions")
    if not channel_id:
        return None
    return guild.get_channel(channel_id)


async def _maybe_run_cpu_trades(
    pool,
    league_id: int,
    season: int,
    current_game_index: int,
    total_regular_games: int,
    deadline_game_index: Optional[int],
    guild: discord.Guild,
) -> None:
    try:
        await _run_cpu_trades_inner(
            pool, league_id, season, current_game_index,
            total_regular_games, deadline_game_index, guild,
        )
    except Exception as exc:
        log.warning(f"_maybe_run_cpu_trades failed silently: {exc}")


async def _run_cpu_trades_inner(
    pool,
    league_id: int,
    season: int,
    current_game_index: int,
    total_regular_games: int,
    deadline_game_index: Optional[int],
    guild: discord.Guild,
) -> None:
    if not deadline_game_index:
        return

    import datetime as _dt

    snapshot_ts = _dt.datetime.now(_dt.timezone.utc)

    trades_proposed = await cpu_trade_service.maybe_initiate_round(
        pool, league_id, season,
        current_game_index, total_regular_games, deadline_game_index,
        guild,
    )
    if not trades_proposed:
        return

    transactions_channel = await _get_transactions_channel(guild, pool, league_id)
    if not transactions_channel:
        return

    # Fetch trades created in this call (by timestamp).
    new_trades = await pool.fetch(
        """
        SELECT id, proposer_team_id, counterparty_team_id, status
        FROM trades
        WHERE league_id = $1 AND proposed_at >= $2
        ORDER BY id
        """,
        league_id, snapshot_ts,
    )

    from data.repositories import trade_repo

    for trade_row in new_trades:
        trade_id = trade_row["id"]
        status = trade_row["status"]

        # Fetch assets.
        assets = await trade_repo.get_assets(pool, trade_id)

        proposer_id = trade_row["proposer_team_id"]
        counterparty_id = trade_row["counterparty_team_id"]

        team_rows = await pool.fetch(
            "SELECT id, nba_team_code FROM teams WHERE id = ANY($1)",
            [proposer_id, counterparty_id],
        )
        team_codes = {r["id"]: r["nba_team_code"] for r in team_rows}

        # Look up real player names and OVR so embeds, Marcus Cole context, and
        # the blockbuster-importance check all have accurate data.
        _player_ids = [a.player_id for a in assets if a.asset_type == "player" and a.player_id]
        if _player_ids:
            _name_rows = await pool.fetch(
                "SELECT id, first_name, last_name, overall FROM players WHERE id = ANY($1)",
                _player_ids,
            )
            _player_names = {r["id"]: f"{r['first_name']} {r['last_name']}" for r in _name_rows}
            _player_ovrs: dict[int, int] = {r["id"]: r["overall"] for r in _name_rows}
        else:
            _player_names = {}
            _player_ovrs = {}

        def _asset_lines(from_team_id: int) -> list[str]:
            lines = []
            for a in assets:
                if a.from_team_id != from_team_id:
                    continue
                if a.asset_type == "player" and a.player_id:
                    lines.append(_player_names.get(a.player_id) or f"Player #{a.player_id}")
                elif a.asset_type == "pick" and a.pick_id:
                    lines.append(f"Pick #{a.pick_id}")
            return lines or ["(nothing)"]

        proposer_code = team_codes.get(proposer_id, f"Team {proposer_id}")
        counterparty_code = team_codes.get(counterparty_id, f"Team {counterparty_id}")

        title = "✅ Trade Executed" if status == "approved" else "⏳ Trade Pending Review"
        color = discord.Color.green() if status == "approved" else discord.Color.orange()

        embed = discord.Embed(title=title, color=color)
        embed.add_field(
            name=f"{counterparty_code} receives",
            value="\n".join(_asset_lines(proposer_id)),
            inline=True,
        )
        embed.add_field(
            name=f"{proposer_code} receives",
            value="\n".join(_asset_lines(counterparty_id)),
            inline=True,
        )
        if status == "pending_commissioner":
            embed.add_field(
                name="Action required",
                value="Commissioner must review and approve or reject this trade.",
                inline=False,
            )
        embed.set_footer(text=f"CPU-initiated · Trade #{trade_id}")
        trade_msg = await transactions_channel.send(embed=embed)

        # Open a thread on the lead message so all follow-up activity stays grouped.
        try:
            thread_name = f"Trade #{trade_id} — {proposer_code} / {counterparty_code}"
            trade_thread = await trade_msg.create_thread(name=thread_name, auto_archive_duration=1440)
            status_label = "Executed" if status == "approved" else "Pending commissioner review"
            detail_lines = [
                f"**Trade #{trade_id}**",
                f"Status: {status_label}",
                "",
                f"**{counterparty_code} receives:** {', '.join(_asset_lines(proposer_id))}",
                f"**{proposer_code} receives:** {', '.join(_asset_lines(counterparty_id))}",
            ]
            await trade_thread.send("\n".join(detail_lines))
        except Exception as _thread_exc:
            log.warning(f"Failed to create trade thread for trade #{trade_id}: {_thread_exc}")

        # Marcus Cole — insider trade report to #analysis.
        # Only fire for blockbuster trades that actually executed (not pending review).
        mc_article = None
        if status == "approved" and _is_blockbuster_trade(assets, _player_ovrs):
            trade_context = {
                "proposer_team": proposer_code,
                "counterparty_team": counterparty_code,
                "proposer_sends": _asset_lines(proposer_id),
                "counterparty_sends": _asset_lines(counterparty_id),
                "trade_status": status,
            }
            mc_article = await columnist_service.generate(
                pool, league_id, season,
                persona_id="marcus_cole",
                category="trade_report",
                context=trade_context,
            )
        if mc_article:
            analysis_channel_id = await league_repo.get_channel(pool, league_id, "analysis")
            analysis_channel = guild.get_channel(analysis_channel_id) if analysis_channel_id else None
            if analysis_channel:
                mc_persona = _PERSONAS.get("marcus_cole")
                embed = discord.Embed(
                    title=f"📡 {mc_article['headline']}",
                    description=mc_article["body"][:2000],
                    color=discord.Color.from_rgb(255, 69, 0),
                )
                if mc_persona:
                    embed.set_footer(text=f"by {mc_persona.display_name} · {mc_persona.byline}")
                await analysis_channel.send(embed=embed)


def _interest_score_from_batch_result(br: dict) -> float:
    """Compute an interest score from a batch result dict (wraps sim result)."""
    r = br["result"]
    margin = abs(r["home_score"] - r["away_score"])
    all_box = r.get("home_box", []) + r.get("away_box", [])
    top_pts = max((line.get("points", 0) for line in all_box), default=0)
    clutch = max(0.0, 10.0 - margin)
    blowout = max(0.0, margin - 20.0)
    return clutch + blowout + float(top_pts)


def _is_blockbuster_trade(assets: list, player_ovrs: dict[int, int]) -> bool:
    """Return True if the trade involves a star (OVR>=80) or a R1 pick."""
    for a in assets:
        if a.asset_type == "player" and a.player_id:
            if player_ovrs.get(a.player_id, 0) >= 80:
                return True
        if a.asset_type == "pick":
            # Any pick included in a trade is notable enough
            return True
    return False


async def _maybe_post_awards_races(
    pool,
    league_id: int,
    season: int,
    news_channel: Optional[discord.TextChannel],
    current_game_index: int = 0,
) -> None:
    try:
        if not news_channel:
            return
        odds = await awards_service.generate_awards_race_odds(pool, league_id, season)
        if not odds:
            return
        embed = awards_embeds.awards_race_embed(odds, game_index=current_game_index)
        if embed:
            await news_channel.send(embed=embed)
    except Exception as exc:
        log.warning(f"_maybe_post_awards_races failed silently: {exc}")


async def _maybe_post_potm(
    pool,
    guild: discord.Guild,
    league_id: int,
    season: int,
    current_game_date: Optional[str],
) -> None:
    """Post Player of the Month awards if a new month has elapsed since the last award."""
    log.info(
        f"_maybe_post_potm called: league={league_id} season={season} "
        f"current_game_date={current_game_date!r}"
    )
    if not current_game_date:
        log.info("_maybe_post_potm: no current_game_date, skipping")
        return
    try:
        awards = await potm_service.check_and_get_potm_awards(
            pool, league_id, season, current_game_date
        )
        if not awards:
            return
        pat = _PERSONAS.get("pat_chen")
        if not pat:
            return
        news_channel = await _get_news_channel(guild, pool, league_id)
        if not news_channel:
            return
        # Group awards by month so we generate one article per month (East+West together).
        from collections import defaultdict as _defaultdict
        by_month: dict[str, list[dict]] = _defaultdict(list)
        for award in awards:
            by_month[award["month_label"]].append(award)
        for month_awards in by_month.values():
            context = potm_service.get_potm_context(month_awards)
            article = await columnist_service.generate(
                pool, league_id, season,
                persona_id="pat_chen",
                category="player_of_the_month",
                context=context,
            )
            if article:
                rgb = _PERSONA_COLORS.get("pat_chen", (100, 100, 100))
                embed = discord.Embed(
                    title=f"🏆 {article['headline']}",
                    description=article["body"][:2000],
                    color=discord.Color.from_rgb(*rgb),
                )
                embed.set_footer(text=f"by {pat.display_name} · {pat.byline}")
                try:
                    await news_channel.send(embed=embed)
                except Exception as exc:
                    log.warning(f"POTM post failed: {exc}")
    except Exception as exc:
        log.warning(f"_maybe_post_potm failed: {exc}")


_PERSONA_COLORS: dict[str, tuple[int, int, int]] = {
    "maya_chen":      (255, 165, 0),
    "jordan_rivera":  (138, 43, 226),
    "keisha_williams": (0, 128, 255),
    "hot_take_hour":  (255, 0, 0),
    "pat_chen":       (0, 180, 150),
}


async def _maybe_post_columnist(
    pool,
    league_id: int,
    season: int,
    batch_results: list[dict],
    guild: discord.Guild,
    batch_start_index: int = 0,
    batch_end_index: int = 0,
    total_regular_games: int = 0,
) -> None:
    """
    Post a columnist article after each batch, rotating through _COLUMNIST_ROTATION.

    Marcus Brooks also fires every ~200 games (every 20 batches of 10), independently.
    hot_take_hour uses a JSON debate format instead of a plain article embed.
    """
    global _marcus_game_counter, _columnist_rotation_index

    if not batch_results:
        return

    analysis_channel_id = await league_repo.get_channel(pool, league_id, "analysis")
    analysis_channel = guild.get_channel(analysis_channel_id) if analysis_channel_id else None
    if not analysis_channel:
        return

    # Build real per-game context so the AI writes about actual results, not fabrications.
    games_data: list[dict] = []
    overall_top_scorer: str | None = None
    overall_top_pts: int = 0
    overall_top_scorer_team: str | None = None
    _overall_top_game: dict = {}

    for br in batch_results:
        ht = br["home_team"]
        at = br["away_team"]
        r = br["result"]
        hs: int = r["home_score"]
        as_: int = r["away_score"]
        winner_id = r.get("winner_team_id")
        winner_code = (ht.nba_team_code if hasattr(ht, "nba_team_code") and winner_id == ht.id
                       else at.nba_team_code if hasattr(at, "nba_team_code") else "???")

        away_code = at.nba_team_code if hasattr(at, "nba_team_code") else "???"
        home_code = ht.nba_team_code if hasattr(ht, "nba_team_code") else "???"

        # Track which team the top scorer belongs to so the AI gets correct attribution.
        game_top: dict = {}
        game_top_pts_val: int = 0
        game_top_team_code: str = ""
        for box_lines, team_code in [
            (r.get("home_box", []), home_code),
            (r.get("away_box", []), away_code),
        ]:
            for line in box_lines:
                if line.get("points", 0) > game_top_pts_val:
                    game_top_pts_val = line.get("points", 0)
                    game_top = line
                    game_top_team_code = team_code

        game_top_name = game_top.get("player_name", "") if game_top else ""
        game_top_pts = game_top_pts_val

        if game_top_pts_val > overall_top_pts:
            overall_top_pts = game_top_pts_val
            overall_top_scorer = game_top_name
            overall_top_scorer_team = game_top_team_code
            _overall_top_game = game_top

        # Build full stat-line dict for this game's top performer.
        if game_top_name and game_top:
            game_top_performer_dict = {
                "name": game_top_name,
                "team": game_top_team_code,
                "pts": game_top.get("points", 0),
                "reb": game_top.get("rebounds_off", 0) + game_top.get("rebounds_def", 0),
                "ast": game_top.get("assists", 0),
                "stl": game_top.get("steals", 0),
                "blk": game_top.get("blocks", 0),
                "tpm": game_top.get("tpm", 0),
                "tpa": game_top.get("tpa", 0),
                "fgm": game_top.get("fgm", 0),
                "fga": game_top.get("fga", 0),
            }
        else:
            game_top_performer_dict = None

        games_data.append({
            "game": f"{away_code} @ {home_code}",
            "score": f"{away_code} {as_} - {home_code} {hs}",
            "winner": winner_code,
            "margin": abs(hs - as_),
            "top_performer": game_top_performer_dict,
        })

    # Sort by interest: close finishes and big blowouts float to the top.
    def _game_interest(g: dict) -> float:
        m = g["margin"]
        return max(0.0, 8.0 - m) + max(0.0, m - 20.0)
    games_data.sort(key=_game_interest, reverse=True)

    if overall_top_scorer and _overall_top_game:
        _top_of_batch = {
            "name": overall_top_scorer,
            "team": overall_top_scorer_team,
            "pts": _overall_top_game.get("points", 0),
            "reb": _overall_top_game.get("rebounds_off", 0) + _overall_top_game.get("rebounds_def", 0),
            "ast": _overall_top_game.get("assists", 0),
            "stl": _overall_top_game.get("steals", 0),
            "blk": _overall_top_game.get("blocks", 0),
            "tpm": _overall_top_game.get("tpm", 0),
            "tpa": _overall_top_game.get("tpa", 0),
            "fgm": _overall_top_game.get("fgm", 0),
            "fga": _overall_top_game.get("fga", 0),
        }
    else:
        _top_of_batch = None

    batch_context = {
        "season_games": games_data[:10],  # all games (≤10 per batch)
        "top_performer_of_batch": _top_of_batch,
        "games_count": len(batch_results),
    }

    # 1b: Add standings snapshot.
    try:
        standings = await game_repo.get_standings(pool, league_id, season)
        east_rows = sorted(
            [r for r in standings if r.get("conference") == "East"],
            key=lambda r: -(r.get("win_pct") or 0.0),
        )
        west_rows = sorted(
            [r for r in standings if r.get("conference") == "West"],
            key=lambda r: -(r.get("win_pct") or 0.0),
        )
        batch_context["standings_east"] = [
            {"code": r["nba_team_code"], "w": r["wins"], "l": r["losses"], "pct": round(r.get("win_pct") or 0.0, 3)}
            for r in east_rows[:5]
        ]
        batch_context["standings_west"] = [
            {"code": r["nba_team_code"], "w": r["wins"], "l": r["losses"], "pct": round(r.get("win_pct") or 0.0, 3)}
            for r in west_rows[:5]
        ]

        # 1c: Build narrative_hooks.
        narrative_hooks: list[str] = []
        # Collect teams that appeared in this batch.
        batch_team_codes: dict[int, str] = {}
        for br in batch_results:
            ht = br["home_team"]
            at = br["away_team"]
            if ht:
                batch_team_codes[ht.id] = ht.nba_team_code if hasattr(ht, "nba_team_code") else "???"
            if at:
                batch_team_codes[at.id] = at.nba_team_code if hasattr(at, "nba_team_code") else "???"

        # Pull win/loss streaks for those teams.
        if batch_team_codes:
            streak_rows = await pool.fetch(
                """
                SELECT team_id, win_streak, loss_streak
                FROM standings_cache
                WHERE league_id = $1 AND team_id = ANY($2)
                """,
                league_id, list(batch_team_codes.keys()),
            )
            for row in streak_rows:
                code = batch_team_codes.get(row["team_id"], "???")
                ws = row.get("win_streak") or 0
                ls = row.get("loss_streak") or 0
                if ws >= 4 and len(narrative_hooks) < 6:
                    narrative_hooks.append(f"{code} has won {ws} straight")
                elif ls >= 4 and len(narrative_hooks) < 6:
                    narrative_hooks.append(f"{code} has lost {ls} straight")

        # East/West title race hooks.
        if len(east_rows) >= 2 and len(narrative_hooks) < 6:
            e1, e2 = east_rows[0], east_rows[1]
            e1_gb = ((e2["wins"] - e1["wins"]) + (e1["losses"] - e2["losses"])) / 2.0
            if abs(e1_gb) <= 2.0:
                diff_str = f"{abs(e1_gb):.1f}"
                narrative_hooks.append(
                    f"East title race: {e1['nba_team_code']} leads {e2['nba_team_code']} by {diff_str} games"
                )
        if len(west_rows) >= 2 and len(narrative_hooks) < 6:
            w1, w2 = west_rows[0], west_rows[1]
            w1_gb = ((w2["wins"] - w1["wins"]) + (w1["losses"] - w2["losses"])) / 2.0
            if abs(w1_gb) <= 2.0:
                diff_str = f"{abs(w1_gb):.1f}"
                narrative_hooks.append(
                    f"West title race: {w1['nba_team_code']} leads {w2['nba_team_code']} by {diff_str} games"
                )

        # 30+ point games.
        for gd in games_data:
            if len(narrative_hooks) >= 6:
                break
            tp = gd.get("top_performer")
            if isinstance(tp, dict) and tp.get("pts", 0) >= 30:
                wl = "W" if tp.get("team") == gd.get("winner") else "L"
                narrative_hooks.append(
                    f"{tp['name']} dropped {tp['pts']} in a {wl} for {tp['team']}"
                )

        batch_context["narrative_hooks"] = narrative_hooks[:6]
    except Exception as _standings_exc:
        log.warning(f"_maybe_post_columnist: standings/hooks enrichment failed: {_standings_exc}")

    # 1d: Add game_index_range.
    if batch_end_index > 0 and total_regular_games > 0:
        batch_context["game_index_range"] = {
            "first": batch_start_index,
            "last": batch_end_index,
            "season_pct": round(batch_end_index / total_regular_games * 100, 1),
        }

    # 1e: Compute subject_team_ids from the two most common teams in this batch.
    from collections import Counter as _Counter
    _team_id_counter: _Counter = _Counter()
    for br in batch_results:
        ht = br["home_team"]
        at = br["away_team"]
        r = br["result"]
        if ht:
            _team_id_counter[ht.id] += 1
        if at:
            _team_id_counter[at.id] += 1
    subject_team_ids = [tid for tid, _ in _team_id_counter.most_common(2)]

    _FORMAT_VARIANTS = ["classic_debate", "co_sign_trap", "tony_monologue", "trial"]

    # Rotation — pick this batch's columnist.
    persona_id = _COLUMNIST_ROTATION[_columnist_rotation_index % len(_COLUMNIST_ROTATION)]
    _columnist_rotation_index += 1

    # Pat Chen: enrich context with team strategy data.
    # Build a copy so we don't mutate the shared batch_context used by other callers.
    columnist_context = batch_context
    if persona_id == "hot_take_hour":
        # Fix 2: inject format_variant so the four Hot Take Hour variants cycle.
        columnist_context = dict(batch_context)
        columnist_context["format_variant"] = _FORMAT_VARIANTS[(_columnist_rotation_index - 1) % len(_FORMAT_VARIANTS)]
    if persona_id == "pat_chen":
        try:
            team_ids_in_batch = list({
                br["home_team"].id for br in batch_results
            } | {
                br["away_team"].id for br in batch_results
            })
            strat_rows = await pool.fetch(
                """
                SELECT t.id AS team_id, t.nba_team_code,
                       ts.offensive_scheme, ts.defensive_scheme,
                       ts.offensive_pace, ts.defensive_intensity,
                       sc.wins, sc.losses
                FROM teams t
                LEFT JOIN team_strategies ts ON ts.team_id = t.id AND ts.league_id = $1
                LEFT JOIN standings_cache sc ON sc.team_id = t.id AND sc.league_id = $1 AND sc.season = $2
                WHERE t.id = ANY($3)
                """,
                league_id, season, team_ids_in_batch,
            )
            pat_context = dict(batch_context)
            pat_context["team_strategies"] = [
                {
                    "team": r["nba_team_code"],
                    "record": f"{r['wins'] or 0}-{r['losses'] or 0}",
                    "offensive_scheme": r["offensive_scheme"] or "auto",
                    "defensive_scheme": r["defensive_scheme"] or "auto",
                    "pace": r["offensive_pace"] or "normal",
                    "defensive_intensity": r["defensive_intensity"] or "standard",
                    "archetype_label": strategy_service.get_team_archetype_label(league_id, r["team_id"]),
                }
                for r in strat_rows
            ]
            # Fix 3: expose archetype labels as a flat code->label dict.
            pat_context["team_archetypes"] = {
                r["nba_team_code"]: strategy_service.get_team_archetype_label(league_id, r["team_id"])
                for r in strat_rows
                if strategy_service.get_team_archetype_label(league_id, r["team_id"]) is not None
            }
            columnist_context = pat_context
        except Exception as _exc:
            log.warning(f"Pat Chen strategy enrichment failed: {_exc}")

    article = await columnist_service.generate(
        pool, league_id, season,
        persona_id=persona_id,
        category="game_recap",
        context=columnist_context,
        subject_team_ids=subject_team_ids,
    )
    if article:
        if persona_id == "hot_take_hour":
            # Body is plain text formatted as "DAVE: ...\n\nTONY: ...\n\nDAVE: ..."
            # Bold the speaker labels for Discord markdown.
            body = article["body"]
            body = body.replace("DAVE:", "**Dave:**").replace("TONY:", "**Tony:**")
            embed = discord.Embed(
                title=f"🔥 {article['headline']}",
                description=body[:2000],
                color=discord.Color.red(),
            )
            embed.set_footer(text="Dave Collier & Tony Reyes · DBA Sports Debate")
        else:
            persona = _PERSONAS.get(persona_id)
            rgb = _PERSONA_COLORS.get(persona_id, (100, 100, 100))
            embed = discord.Embed(
                title=article["headline"],
                description=article["body"][:2000],
                color=discord.Color.from_rgb(*rgb),
            )
            if persona:
                embed.set_footer(text=f"by {persona.display_name} · {persona.byline}")
        await analysis_channel.send(embed=embed)

    # Marcus Brooks — every ~200 games (every 20 batches), independently of the rotation.
    _marcus_game_counter += len(batch_results)
    if _marcus_game_counter >= 200:
        _marcus_game_counter = 0
        mb_persona = _PERSONAS.get("marcus_brooks")
        mb_article = await columnist_service.generate(
            pool, league_id, season,
            persona_id="marcus_brooks",
            category="power_rankings",
            context=batch_context,
            subject_team_ids=subject_team_ids,
        )
        if mb_article:
            embed = discord.Embed(
                title=mb_article["headline"],
                description=mb_article["body"][:2000],
                color=discord.Color.from_rgb(0, 128, 255),
            )
            if mb_persona:
                embed.set_footer(text=f"by {mb_persona.display_name} · {mb_persona.byline}")
            await analysis_channel.send(embed=embed)


async def _maybe_post_playoff_columnist(
    pool,
    league_id: int,
    season: int,
    context: dict,
    guild: discord.Guild,
) -> None:
    """
    Post a playoff recap article to #league-news, rotating through the three
    recap-capable personas (maya_chen, jordan_rivera, keisha_williams).

    Called from playoff_service.sim_series_game — always for clinching/elimination
    games, ~30% of the time for regular playoff games.
    """
    global _playoff_rotation_index

    persona_id = _PLAYOFF_COLUMNIST_ROTATION[_playoff_rotation_index % len(_PLAYOFF_COLUMNIST_ROTATION)]
    _playoff_rotation_index += 1

    persona = _PERSONAS.get(persona_id)
    if not persona:
        return

    news_channel = await _get_news_channel(guild, pool, league_id)
    if not news_channel:
        return

    article = await columnist_service.generate(
        pool, league_id, season,
        persona_id=persona_id,
        category="playoff_recap",
        context=context,
    )
    if not article:
        return

    rgb = _PERSONA_COLORS.get(persona_id, (100, 100, 100))
    embed = discord.Embed(
        title=article["headline"],
        description=article["body"][:2000],
        color=discord.Color.from_rgb(*rgb),
    )
    embed.set_footer(text=f"by {persona.display_name} · {persona.byline}")
    try:
        await news_channel.send(embed=embed)
    except Exception as exc:
        log.warning(f"Playoff columnist post failed: {exc}")


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
                    "Run `/sim deadline` to close the deadline and resume the season."
                ),
                color=discord.Color.orange(),
            )
            try:
                await news_channel.send(embed=embed)
            except Exception:
                pass
    except Exception as exc:
        log.warning(f"_maybe_advance_trade_deadline failed: {exc}")


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
    strategy_service.clear_archetype_cache()
    pool = await get_pool()
    total_regular_games = await game_repo.get_total_regular_season_games(pool, league_id, season)
    deadline_game_index = await game_repo.get_deadline_game_index(pool, league_id, season)
    _league_phase_row = await pool.fetchrow(
        "SELECT current_phase FROM leagues WHERE id = $1", league_id
    )
    _league_phase = _league_phase_row["current_phase"] if _league_phase_row else ""

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
    standings_channel = await _get_standings_channel(guild, pool, league_id)
    news_channel = await _get_news_channel(guild, pool, league_id)
    injury_channel = await _get_injury_channel(guild, pool, league_id)

    games_simmed = 0
    batch_results = []

    for game in games:
        if game.get("status") == "simmed":
            continue

        sim_result = await _sim_single_game(pool, game, league_id, season, news_channel, injury_channel)
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
            standings = await game_repo.get_standings(pool, league_id, season)
            embed = sim_embeds.batch_recap_with_standings(batch_results, standings)
            try:
                await box_channel.send(embed=embed)
            except (discord.HTTPException, Exception) as exc:
                log.warning(f"channel send failed: {exc}")
            first_game_idx = batch_results[0]["game"].get("game_index", 0) if batch_results else 0
            last_game_idx = batch_results[-1]["game"].get("game_index", 0) if batch_results else 0
            if standings_channel:
                try:
                    await standings_channel.send(embed=sim_embeds.standings_snapshot_embed(standings, last_game_idx))
                except (discord.HTTPException, Exception) as exc:
                    log.warning(f"channel send failed: {exc}")
            await _maybe_post_awards_races(pool, league_id, season, news_channel, current_game_index=last_game_idx)
            await _maybe_post_columnist(
                pool, league_id, season, batch_results, guild,
                batch_start_index=first_game_idx,
                batch_end_index=last_game_idx,
                total_regular_games=total_regular_games,
            )
            _last_game_date = batch_results[-1]["game"].get("scheduled_date")
            _last_game_date_str = str(_last_game_date) if _last_game_date else None
            await _maybe_post_potm(pool, guild, league_id, season, _last_game_date_str)
            last_idx = batch_results[-1]["game"].get("game_index", 0) if batch_results else 0
            await _maybe_run_cpu_trades(pool, league_id, season, last_idx, total_regular_games, deadline_game_index, guild)
            await _maybe_advance_trade_deadline(pool, league_id, last_idx, deadline_game_index, news_channel)
            batch_results = []

    if batch_results and box_channel:
        standings = await game_repo.get_standings(pool, league_id, season)
        embed = sim_embeds.batch_recap_with_standings(batch_results, standings)
        try:
            await box_channel.send(embed=embed)
        except (discord.HTTPException, Exception) as exc:
            log.warning(f"channel send failed: {exc}")
        first_game_idx = batch_results[0]["game"].get("game_index", 0) if batch_results else 0
        last_game_idx = batch_results[-1]["game"].get("game_index", 0) if batch_results else 0
        if standings_channel:
            try:
                await standings_channel.send(embed=sim_embeds.standings_snapshot_embed(standings, last_game_idx))
            except (discord.HTTPException, Exception) as exc:
                log.warning(f"channel send failed: {exc}")
        await _maybe_post_awards_races(pool, league_id, season, news_channel, current_game_index=last_game_idx)
        await _maybe_post_columnist(
            pool, league_id, season, batch_results, guild,
            batch_start_index=first_game_idx,
            batch_end_index=last_game_idx,
            total_regular_games=total_regular_games,
        )
        _last_game_date = batch_results[-1]["game"].get("scheduled_date")
        _last_game_date_str = str(_last_game_date) if _last_game_date else None
        await _maybe_post_potm(pool, guild, league_id, season, _last_game_date_str)
        last_idx = batch_results[-1]["game"].get("game_index", 0) if batch_results else 0
        await _maybe_run_cpu_trades(pool, league_id, season, last_idx, total_regular_games, deadline_game_index, guild)
        await _maybe_advance_trade_deadline(pool, league_id, last_idx, deadline_game_index, news_channel)

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

    return {"games_simmed": games_simmed, "next_matchup": next_user_game, "season_complete": season_complete}


async def sim_range(
    league_id: int,
    guild: discord.Guild,
    season: int,
    to_game_index: int,
    bot: Optional[discord.Client] = None,
    force: bool = False,
) -> dict:
    strategy_service.clear_archetype_cache()
    pool = await get_pool()
    total_regular_games = await game_repo.get_total_regular_season_games(pool, league_id, season)
    deadline_game_index = await game_repo.get_deadline_game_index(pool, league_id, season)
    _league_phase_row = await pool.fetchrow(
        "SELECT current_phase FROM leagues WHERE id = $1", league_id
    )
    _league_phase = _league_phase_row["current_phase"] if _league_phase_row else ""

    current_index = await game_repo.get_current_index(pool, league_id, season)

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

    games_simmed = 0
    batch_results = []

    for game in games:
        if game.get("status") == "simmed":
            continue

        sim_result = await _sim_single_game(pool, game, league_id, season, news_channel, injury_channel)
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
            standings = await game_repo.get_standings(pool, league_id, season)
            embed = sim_embeds.batch_recap_with_standings(batch_results, standings)
            try:
                await box_channel.send(embed=embed)
            except (discord.HTTPException, Exception) as exc:
                log.warning(f"channel send failed: {exc}")
            first_game_idx = batch_results[0]["game"].get("game_index", 0) if batch_results else 0
            last_game_idx = batch_results[-1]["game"].get("game_index", 0) if batch_results else 0
            if standings_channel:
                try:
                    await standings_channel.send(embed=sim_embeds.standings_snapshot_embed(standings, last_game_idx))
                except (discord.HTTPException, Exception) as exc:
                    log.warning(f"channel send failed: {exc}")
            await _maybe_post_awards_races(pool, league_id, season, news_channel, current_game_index=last_game_idx)
            await _maybe_post_columnist(
                pool, league_id, season, batch_results, guild,
                batch_start_index=first_game_idx,
                batch_end_index=last_game_idx,
                total_regular_games=total_regular_games,
            )
            _last_game_date = batch_results[-1]["game"].get("scheduled_date")
            _last_game_date_str = str(_last_game_date) if _last_game_date else None
            await _maybe_post_potm(pool, guild, league_id, season, _last_game_date_str)
            last_idx = batch_results[-1]["game"].get("game_index", 0) if batch_results else 0
            await _maybe_run_cpu_trades(pool, league_id, season, last_idx, total_regular_games, deadline_game_index, guild)
            await _maybe_advance_trade_deadline(pool, league_id, last_idx, deadline_game_index, news_channel)
            batch_results = []

    if batch_results and box_channel:
        standings = await game_repo.get_standings(pool, league_id, season)
        embed = sim_embeds.batch_recap_with_standings(batch_results, standings)
        try:
            await box_channel.send(embed=embed)
        except (discord.HTTPException, Exception) as exc:
            log.warning(f"channel send failed: {exc}")
        first_game_idx = batch_results[0]["game"].get("game_index", 0) if batch_results else 0
        last_game_idx = batch_results[-1]["game"].get("game_index", 0) if batch_results else 0
        if standings_channel:
            try:
                await standings_channel.send(embed=sim_embeds.standings_snapshot_embed(standings, last_game_idx))
            except (discord.HTTPException, Exception) as exc:
                log.warning(f"channel send failed: {exc}")
        await _maybe_post_awards_races(pool, league_id, season, news_channel, current_game_index=last_game_idx)
        await _maybe_post_columnist(
            pool, league_id, season, batch_results, guild,
            batch_start_index=first_game_idx,
            batch_end_index=last_game_idx,
            total_regular_games=total_regular_games,
        )
        _last_game_date = batch_results[-1]["game"].get("scheduled_date")
        _last_game_date_str = str(_last_game_date) if _last_game_date else None
        await _maybe_post_potm(pool, guild, league_id, season, _last_game_date_str)
        last_idx = batch_results[-1]["game"].get("game_index", 0) if batch_results else 0
        await _maybe_run_cpu_trades(pool, league_id, season, last_idx, total_regular_games, deadline_game_index, guild)
        await _maybe_advance_trade_deadline(pool, league_id, last_idx, deadline_game_index, news_channel)

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
    injury_channel = await _get_injury_channel(guild, pool, league_id)
    return await _sim_single_game(pool, game, league_id, season, news_channel, injury_channel)
