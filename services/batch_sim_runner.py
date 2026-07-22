from __future__ import annotations

import asyncio
import os
from collections import Counter
from typing import List, Optional

import discord

from bot.embeds import sim_embeds
from core.logging import get_logger
from data.db import get_pool
from data.repositories import game_repo, gameplan_repo, league_repo, strategy_repo, team_repo
from phase.states import Phase
from services import awards_service, columnist_service, cpu_coach_service, franchise_plan_service, league_service, notifier_service, sim_engine, strategy_service, team_intel
from services.cpu_trade_round_trigger import _maybe_run_cpu_trades
from services.personas import PERSONAS as _PERSONAS
from services.sim_content_pipeline import (
    _build_batch_game_context,
    _maybe_post_big_picture,
    _maybe_post_coach_beat,
    _maybe_post_ledger,
    _maybe_post_potm,
    _maybe_post_power_list,
    _maybe_post_rookie_watch,
)
from services.player_style_service import context_summary as _player_style_context
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
from services import columnist_ride_along as _columnist_ride_along
from services import feedback_log as _feedback_log

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

# Tracks games processed so Marcus Brooks fires every ~200 games (every 20 batches of 10).
# Keyed by league_id so multi-league bots don't bleed counters across leagues.
_marcus_game_counter: dict[int, int] = {}

# Tracks games processed so Darius Cole fires every ~50 games.
_darius_game_counter: dict[int, int] = {}

# Tracks the game index of the last columnist article so the 50-game fallback works.
# Keyed by league_id.
_last_columnist_game_index: dict[int, int] = {}

# Columnist rotation — cycles through these personas on every batch (subject to reactive gate).
_COLUMNIST_ROTATION = ["jordan_rivera", "keisha_williams", "hot_take_hour", "pat_chen", "darius_cole", "carla_knox"]
# Keyed by league_id so concurrent leagues each have their own rotation position.
_columnist_rotation_index: dict[int, int] = {}

# Hot Take Hour season-long running narratives.  Seeded on first HTH article of the
# season and then injected into every subsequent HTH context so Dave and Tony keep
# their multi-episode storylines alive.  Keyed by league_id so multi-league bots work.
_HTH_NARRATIVES: dict[int, dict] = {}

# Playoff columnist rotation — cycles through recap-capable personas for post-game coverage.
_PLAYOFF_COLUMNIST_ROTATION = ["jordan_rivera", "keisha_williams", "carla_knox"]
# Keyed by league_id.
_playoff_rotation_index: dict[int, int] = {}

# POTM month-gate: keyed by league_id, stores the last "YYYY-MM" for which
# _maybe_post_potm was allowed to call through to potm_service.  Batches within
# the same simulated calendar month are skipped without touching the DB.
_potm_last_checked_month: dict[int, str] = {}

# Columnist force-mode cadence: minimum game-index gap between articles when
# force=True.  70 games ≈ 7 game-days of 10 games each.
_COLUMNIST_FORCE_MIN_GAP: int = 70

# New specialty persona game counters.  Each fires on its own cadence, independent
# of the main columnist rotation.  70 games ≈ weekly; 280 games ≈ monthly.
_race_game_counter: dict[int, int] = {}            # fires every ~280 games (monthly)

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

    game_date = game.get("scheduled_date")
    fatigue = {
        "home_b2b": await game_repo.is_back_to_back(pool, league_id, season, game["home_team_id"], game_date),
        "away_b2b": await game_repo.is_back_to_back(pool, league_id, season, game["away_team_id"], game_date),
    }

    home_gameplan, away_gameplan = await cpu_coach_service.decide_gameplans(
        pool, league_id, season, game, home_players, away_players
    )
    await gameplan_repo.record_gameplan(pool, game["id"], home_team.id, home_gameplan)
    await gameplan_repo.record_gameplan(pool, game["id"], away_team.id, away_gameplan)

    if home_gameplan["source"] == "cpu":
        _apply_cpu_directives(home_players, home_gameplan["player_directives"])
        for _p in home_players:
            _apply_directives(_p)
    if away_gameplan["source"] == "cpu":
        _apply_cpu_directives(away_players, away_gameplan["player_directives"])
        for _p in away_players:
            _apply_directives(_p)

    home_strategy = await strategy_service.get_sim_modifiers(
        pool, league_id, home_team.id, override_strategy=home_gameplan["strategy"]
    )
    away_strategy = await strategy_service.get_sim_modifiers(
        pool, league_id, away_team.id, override_strategy=away_gameplan["strategy"]
    )

    # Phase 2: stamp role-based touch share + shot profile onto player dicts before sim.
    # offensive_scheme comes from the resolved strategy so scheme_synergy is applied correctly.
    home_scheme = home_gameplan["strategy"].get("offensive_scheme", "balanced")
    away_scheme = away_gameplan["strategy"].get("offensive_scheme", "balanced")
    await _stamp_role_data(pool, league_id, home_team.id, season, home_players, home_scheme)
    await _stamp_role_data(pool, league_id, away_team.id, season, away_players, away_scheme)

    home_player_ids = [p["id"] for p in home_players]
    away_player_ids = [p["id"] for p in away_players]
    game_rng_seed = game.get("rng_seed") or (game["id"] * 31337)
    home_minutes = await strategy_repo.get_team_minutes_plan(pool, league_id, home_team.id, home_player_ids, game_seed=game_rng_seed)
    away_minutes = await strategy_repo.get_team_minutes_plan(pool, league_id, away_team.id, away_player_ids, game_seed=game_rng_seed ^ 0xABCD)

    seed = game.get("rng_seed") or (game["id"] * 31337)
    result = sim_engine.sim_game(
        _team_to_sim_dict(home_team, home_ovr),
        _team_to_sim_dict(away_team, away_ovr),
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




def _interest_score_from_batch_result(br: dict) -> float:
    """Compute an interest score from a batch result dict (wraps sim result)."""
    r = br["result"]
    margin = abs(r["home_score"] - r["away_score"])
    all_box = r.get("home_box", []) + r.get("away_box", [])
    top_pts = max((line.get("points", 0) for line in all_box), default=0)
    clutch = max(0.0, 10.0 - margin)
    blowout = max(0.0, margin - 20.0)
    return clutch + blowout + float(top_pts)


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



_PERSONA_COLORS: dict[str, tuple[int, int, int]] = {
    "jordan_rivera":  (138, 43, 226),
    "keisha_williams": (0, 128, 255),
    "hot_take_hour":  (255, 0, 0),
    "pat_chen":       (0, 180, 150),
    "darius_cole":    (34, 139, 34),
    "coach_beat":     (160, 82, 45),
    "carla_knox":     (80, 160, 200),
}


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



async def _maybe_post_the_race(
    pool,
    league_id: int,
    season: int,
    batch_results: list[dict],
    guild: discord.Guild,
) -> None:
    """Post The Race award-race column every ~280 games (approx. monthly)."""
    _race_game_counter[league_id] = _race_game_counter.get(league_id, 0) + len(batch_results)
    if _race_game_counter[league_id] < 280:
        return
    _race_game_counter[league_id] = 0

    analysis_channel_id = await league_repo.get_channel(pool, league_id, "analysis")
    analysis_channel = guild.get_channel(analysis_channel_id) if analysis_channel_id else None
    if not analysis_channel:
        return

    persona = _PERSONAS.get("the_race")
    if not persona:
        log.warning("_maybe_post_the_race: the_race persona not registered — skipping")
        return

    try:
        context = await _build_batch_game_context(batch_results)

        # Fetch top-5 per award race with player names and stat averages.
        race_leaders = await awards_service.get_race_leaders(pool, league_id, season, top_n=5)
        if not race_leaders:
            log.info("_maybe_post_the_race: no award race data — skipping")
            return
        # Skip when all award race lists are empty — no real candidates means
        # the LLM will produce TBD / editor's note placeholders instead of a column.
        has_real_candidates = any(candidates for candidates in race_leaders.values())
        if not has_real_candidates:
            log.info("_maybe_post_the_race: all award races empty — skipping to avoid TBD post")
            return

        # Enrich with player names and per-game averages.
        all_pids = [p["player_id"] for candidates in race_leaders.values() for p in candidates]
        if all_pids:
            name_rows = await pool.fetch(
                """
                SELECT p.id, p.first_name || ' ' || p.last_name AS name,
                       t.nba_team_code AS team,
                       ROUND(AVG(b.points)::numeric, 1) AS ppg,
                       ROUND(AVG(b.rebounds_off + b.rebounds_def)::numeric, 1) AS rpg,
                       ROUND(AVG(b.assists)::numeric, 1) AS apg,
                       COUNT(b.id) AS gp
                FROM players p
                JOIN teams t ON t.id = p.team_id
                LEFT JOIN game_box_scores b ON b.player_id = p.id
                LEFT JOIN games g ON g.id = b.game_id AND g.season = $2
                WHERE p.id = ANY($1)
                GROUP BY p.id, t.nba_team_code
                """,
                all_pids, season,
            )
            player_info = {r["id"]: dict(r) for r in name_rows}
        else:
            player_info = {}

        enriched_races: dict[str, list[dict]] = {}
        for award, candidates in race_leaders.items():
            enriched = []
            for c in candidates:
                pid = c["player_id"]
                info = player_info.get(pid, {})
                enriched.append({
                    "player": info.get("name", f"Player #{pid}"),
                    "team": info.get("team", "???"),
                    "ppg": info.get("ppg"),
                    "rpg": info.get("rpg"),
                    "apg": info.get("apg"),
                    "gp": info.get("gp"),
                    "score": c.get("score"),
                })
            enriched_races[award] = enriched

        context["award_races"] = enriched_races

        article = await asyncio.wait_for(
            columnist_service.generate(
                pool, league_id, season,
                persona_id="the_race",
                category="award_race",
                context=context,
            ),
            timeout=20.0,
        )
        if article:
            embed = discord.Embed(
                title=f"🏅 {article['headline']}",
                description=article["body"][:2000],
                color=discord.Color.from_rgb(200, 160, 40),
            )
            embed.set_footer(text=f"by {persona.display_name} · {persona.byline}")
            _sent = await analysis_channel.send(embed=embed)
            await _feedback_log.register_columnist_post(
                pool, _sent,
                league_id=league_id, season=season,
                persona_id="the_race", category="award_race",
                headline=article["headline"], body=article["body"],
            )
    except Exception as exc:
        log.warning(f"_maybe_post_the_race failed: {exc}", exc_info=True)


async def _maybe_post_triage_report(
    pool,
    league_id: int,
    season: int,
    guild: discord.Guild,
    injury_info: dict,
) -> None:
    """Post The Triage Report when a significant injury is recorded.

    injury_info must contain: player_name, team_code, severity, games_missed.
    Called from _persist_injuries for ANNOUNCE_SEVERITIES injuries.
    """
    analysis_channel_id = await league_repo.get_channel(pool, league_id, "analysis")
    analysis_channel = guild.get_channel(analysis_channel_id) if analysis_channel_id else None
    if not analysis_channel:
        return

    persona = _PERSONAS.get("triage_report")
    if not persona:
        return

    try:
        # Enrich injury_info with the team's other players so the LLM can name
        # a plausible replacement rather than writing "role to be determined."
        triage_context = dict(injury_info)
        try:
            _team_rows = await pool.fetch(
                """
                SELECT p.first_name || ' ' || p.last_name AS name,
                       p.position, p.overall,
                       COALESCE(pr.role, 'rotation') AS role
                FROM players p
                LEFT JOIN player_roles pr ON pr.player_id = p.id AND pr.league_id = $1
                WHERE p.league_id = $1 AND p.team_id = (
                    SELECT team_id FROM players
                    JOIN teams t ON t.id = players.team_id
                    WHERE t.league_id = $1 AND t.nba_team_code = $2
                    LIMIT 1
                )
                ORDER BY p.overall DESC
                LIMIT 12
                """,
                league_id, injury_info.get("team_code", ""),
            )
            triage_context["team_roster"] = [
                {"name": r["name"], "position": r["position"],
                 "ovr": r["overall"], "role": r["role"]}
                for r in _team_rows
            ]
        except Exception as _roster_exc:
            log.debug(f"_maybe_post_triage_report: roster enrichment failed (non-fatal): {_roster_exc}")

        article = await asyncio.wait_for(
            columnist_service.generate(
                pool, league_id, injury_info.get("season", 1),
                persona_id="triage_report",
                category="injury_report",
                context=triage_context,
            ),
            timeout=20.0,
        )
        if article and article.get("body"):
            embed = discord.Embed(
                title=f"🩺 {article['headline']}",
                description=article["body"][:2000],
                color=discord.Color.red(),
            )
            embed.set_footer(text=f"by {persona.display_name} · {persona.byline}")
            _sent = await analysis_channel.send(embed=embed)
            _injured_pid = injury_info.get("player_id")
            await _feedback_log.register_columnist_post(
                pool, _sent,
                league_id=league_id, season=injury_info.get("season", season),
                persona_id="triage_report", category="injury_report",
                headline=article["headline"], body=article["body"],
                subject_player_ids=[_injured_pid] if _injured_pid else None,
            )
    except Exception as exc:
        log.warning(f"_maybe_post_triage_report failed: {exc}", exc_info=True)


async def _maybe_post_prelude(
    pool,
    league_id: int,
    season: int,
    guild: discord.Guild,
    series_context: dict,
) -> None:
    """Post The Prelude series preview when a new playoff matchup is set.

    series_context must contain: high_seed_team, low_seed_team, round.
    Called from playoff_service after series_repo.create_series for R1+.
    """
    analysis_channel_id = await league_repo.get_channel(pool, league_id, "analysis")
    analysis_channel = guild.get_channel(analysis_channel_id) if analysis_channel_id else None
    if not analysis_channel:
        return

    persona = _PERSONAS.get("the_prelude")
    if not persona:
        return

    try:
        article = await asyncio.wait_for(
            columnist_service.generate(
                pool, league_id, season,
                persona_id="the_prelude",
                category="series_preview",
                context=series_context,
            ),
            timeout=20.0,
        )
        if article:
            embed = discord.Embed(
                title=f"🎬 {article['headline']}",
                description=article["body"][:2000],
                color=discord.Color.from_rgb(80, 40, 120),
            )
            embed.set_footer(text=f"by {persona.display_name} · {persona.byline}")
            _sent = await analysis_channel.send(embed=embed)
            _series_team_ids = [
                tid for tid in (
                    series_context.get("high_seed_team_id"),
                    series_context.get("low_seed_team_id"),
                ) if tid
            ]
            await _feedback_log.register_columnist_post(
                pool, _sent,
                league_id=league_id, season=season,
                persona_id="the_prelude", category="series_preview",
                headline=article["headline"], body=article["body"],
                subject_team_ids=_series_team_ids or None,
            )
    except Exception as exc:
        log.warning(f"_maybe_post_prelude failed: {exc}", exc_info=True)


async def _maybe_post_columnist(
    pool,
    league_id: int,
    season: int,
    batch_results: list[dict],
    guild: discord.Guild,
    batch_start_index: int = 0,
    batch_end_index: int = 0,
    total_regular_games: int = 0,
    force: bool = False,
    prefetched_race_leaders: dict | None = None,
) -> None:
    """
    Post a columnist article after each batch, rotating through _COLUMNIST_ROTATION.

    Marcus Brooks also fires every ~200 games (every 20 batches of 10), independently.
    hot_take_hour uses a JSON debate format instead of a plain article embed.

    force=True cadence gate: when running a forced bulk sim (e.g. /sim deadline
    force:True), columnist articles are capped to at most one per
    _COLUMNIST_FORCE_MIN_GAP games.  This avoids ~10s LLM calls on every
    game-day when the sim is covering hundreds of games at once.  The Darius Cole
    independent counter is unaffected — he still fires every ~50 games.
    """
    if not batch_results:
        return

    # Counters must increment even when the analysis channel is absent so that
    # Darius Cole and Marcus Brooks fire correctly once the channel exists.
    _darius_game_counter[league_id] = _darius_game_counter.get(league_id, 0) + len(batch_results)
    _marcus_game_counter[league_id] = _marcus_game_counter.get(league_id, 0) + len(batch_results)
    log.info(f"Darius Cole check: counter={_darius_game_counter[league_id]}")

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
        home_code = ht.nba_team_code if hasattr(ht, "nba_team_code") else "???"
        away_code = at.nba_team_code if hasattr(at, "nba_team_code") else "???"
        winner_code = home_code if winner_id == ht.id else away_code
        loser_code = away_code if winner_id == ht.id else home_code

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

        margin = abs(hs - as_)
        games_data.append({
            "game": f"{away_code} @ {home_code}",
            "actual_final_score": f"{away_code} {as_} - {home_code} {hs}",
            "result_summary": f"{winner_code} beat {loser_code} {max(hs, as_)}-{min(hs, as_)}",
            "result_instruction": (
                f"IMPORTANT: The actual final score is {away_code} {as_} - {home_code} {hs}. "
                "Do NOT invent or change the score."
            ),
            "winner": winner_code,
            "loser": loser_code,
            "margin": margin,
            "was_blowout": margin >= 20,
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

    # The top-3 most interesting games (most pts, biggest upset, closest game).
    # Sort by a combined interest score and take the top 3.
    _by_pts = sorted(games_data, key=lambda g: (g.get("top_performer") or {}).get("pts", 0), reverse=True)
    _by_margin_close = sorted(games_data, key=lambda g: g["margin"])
    _by_blowout = sorted(games_data, key=lambda g: g["margin"], reverse=True)
    _interesting_set: list[dict] = []
    for _g in [_by_pts[0] if _by_pts else None,
               _by_margin_close[0] if _by_margin_close else None,
               _by_blowout[0] if _by_blowout else None]:
        if _g is not None and _g not in _interesting_set:
            _interesting_set.append(_g)

    # Plain-English game results for the prompt.
    game_results_summary = [
        g["result_summary"] for g in games_data if g.get("result_summary")
    ]

    # Enrich top performers with player style context so columnists can write
    # about archetypes, tendencies, and whether performances matched expectations.
    def _enrich_performer(perf: dict | None) -> dict | None:
        if not perf or not perf.get("name"):
            return perf
        style = _player_style_context(perf["name"], season)
        if style:
            perf = dict(perf)
            perf["style"]        = style["style"]
            perf["shot_profile"] = style["shot_profile"]
            perf["playmaking"]   = style["playmaking"]
            perf["defense"]      = style["defense"]
        return perf

    _top_of_batch = _enrich_performer(_top_of_batch)
    for gd in games_data:
        if gd.get("top_performer"):
            gd["top_performer"] = _enrich_performer(gd["top_performer"])

    batch_context = {
        "season_games": games_data[:10],  # all games (≤10 per batch)
        "top_3_interesting_games": _interesting_set,
        "game_results": game_results_summary,
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

        # 1b-2: Standings leaders (East + West top 3) for columnist topic variety.
        batch_context["standings_leaders"] = {
            "east": [
                {"code": r["nba_team_code"], "w": r["wins"], "l": r["losses"]}
                for r in east_rows[:3]
            ],
            "west": [
                {"code": r["nba_team_code"], "w": r["wins"], "l": r["losses"]}
                for r in west_rows[:3]
            ],
        }
    except Exception as _standings_exc:
        log.warning(f"_maybe_post_columnist: standings/hooks enrichment failed: {_standings_exc}")

    # 1c-2: Recent executed trades (last 2-3 approved trades within 50 games).
    try:
        trade_rows = await pool.fetch(
            """
            SELECT tr.id, tr.proposer_team_id, tr.counterparty_team_id,
                   t1.nba_team_code AS proposer_code, t2.nba_team_code AS counterparty_code
            FROM trades tr
            JOIN teams t1 ON t1.id = tr.proposer_team_id
            JOIN teams t2 ON t2.id = tr.counterparty_team_id
            WHERE tr.league_id = $1
              AND tr.status = 'approved'
            ORDER BY tr.id DESC
            LIMIT 3
            """,
            league_id,
        )
        if trade_rows:
            recent_trades = []
            for tr in trade_rows:
                # Fetch player names on each side.
                asset_rows = await pool.fetch(
                    """
                    SELECT ta.from_team_id, ta.asset_type,
                           p.first_name || ' ' || p.last_name AS player_name
                    FROM trade_assets ta
                    LEFT JOIN players p ON p.id = ta.player_id
                    WHERE ta.trade_id = $1
                    """,
                    tr["id"],
                )
                prop_assets = [
                    a["player_name"] for a in asset_rows
                    if a["from_team_id"] == tr["proposer_team_id"] and a["player_name"]
                ]
                counter_assets = [
                    a["player_name"] for a in asset_rows
                    if a["from_team_id"] == tr["counterparty_team_id"] and a["player_name"]
                ]
                recent_trades.append({
                    "teams": f"{tr['proposer_code']} / {tr['counterparty_code']}",
                    f"{tr['counterparty_code']}_receives": prop_assets or ["picks"],
                    f"{tr['proposer_code']}_receives": counter_assets or ["picks"],
                })
            batch_context["recent_trades"] = recent_trades
    except Exception as _trade_exc:
        log.warning(f"_maybe_post_columnist: trade enrichment failed: {_trade_exc}")

    # 1d: Add award race leaders for topic variety.
    # Use pre-fetched leaders from the batch tick (top_n=5 fetched once) to avoid
    # a redundant DB round-trip. Fall back to fetching directly if not provided.
    try:
        if prefetched_race_leaders is not None:
            # Slice each award to top 3 candidates for the columnist context.
            _race_leaders = {
                award: candidates[:3]
                for award, candidates in prefetched_race_leaders.items()
            }
        else:
            _race_leaders = await awards_service.get_race_leaders(pool, league_id, season, top_n=3)
        _race_player_ids = [
            p["player_id"]
            for candidates in _race_leaders.values()
            for p in candidates[:1]  # only top candidate per award
        ]
        if _race_player_ids:
            _race_name_rows = await pool.fetch(
                "SELECT id, first_name, last_name FROM players WHERE id = ANY($1)",
                _race_player_ids,
            )
            _race_names = {r["id"]: f"{r['first_name']} {r['last_name']}" for r in _race_name_rows}
            batch_context["award_race_leaders"] = {
                award: _race_names.get(candidates[0]["player_id"], "Unknown")
                for award, candidates in _race_leaders.items()
                if candidates
            }
    except Exception as _award_exc:
        log.warning(f"_maybe_post_columnist: award race enrichment failed: {_award_exc}")

    # 1f: Add game_index_range.
    if batch_end_index > 0 and total_regular_games > 0:
        batch_context["game_index_range"] = {
            "first": batch_start_index,
            "last": batch_end_index,
            "season_pct": round(batch_end_index / total_regular_games * 100, 1),
        }

    # 1g: Compute subject_team_ids from the two most common teams in this batch.
    _team_id_counter: Counter = Counter()
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

    # --- Reactive regular-season trigger gate ---
    # Before picking a columnist, determine whether anything interesting enough
    # happened to warrant an article.  If none of the conditions below are true,
    # skip the rotation slot entirely (but still count games for Darius Cole and
    # Marcus Brooks, and still advance _last_columnist_game_index on a post).
    _batch_is_interesting = False
    _recent_trade_within_50 = bool(batch_context.get("recent_trades"))

    # Check 5+ win/loss streak for any team in the batch.
    _streak_hooks: list[str] = batch_context.get("narrative_hooks", [])
    _has_long_streak = any(
        ("won 5" in h or "won 6" in h or "won 7" in h or "won 8" in h or "won 9" in h
         or "won 10" in h or "lost 5" in h or "lost 6" in h or "lost 7" in h
         or "lost 8" in h or "lost 9" in h or "lost 10" in h)
        for h in _streak_hooks
    )

    # Check blowout (20+ margin) in this batch.
    _has_blowout = any(g.get("was_blowout", False) for g in games_data)

    # Check 40+ point game.
    _has_big_game = overall_top_pts >= 40

    # Fallback: more than 50 games since the last article.
    _games_since_last = batch_end_index - _last_columnist_game_index.get(league_id, 0)
    _fallback_due = _games_since_last >= 50

    if _has_long_streak or _has_blowout or _has_big_game or _recent_trade_within_50 or _fallback_due:
        _batch_is_interesting = True

    # Force-mode frequency gate: when bulk-simming (force=True) suppress the main
    # columnist unless at least _COLUMNIST_FORCE_MIN_GAP games have elapsed since the
    # last article.  This prevents an LLM call on every game-day when hundreds of games
    # are being pushed through at once.  Darius Cole's independent counter is unaffected.
    if force and _batch_is_interesting:
        _games_since_last_for_force = batch_end_index - _last_columnist_game_index.get(league_id, 0)
        if _games_since_last_for_force < _COLUMNIST_FORCE_MIN_GAP:
            log.debug(
                f"_maybe_post_columnist: force mode — suppressing article "
                f"({_games_since_last_for_force} games since last, need {_COLUMNIST_FORCE_MIN_GAP})"
            )
            _batch_is_interesting = False

    # Rotation — pick this batch's columnist.
    _columnist_rotation_index[league_id] = _columnist_rotation_index.get(league_id, 0)
    persona_id = _COLUMNIST_ROTATION[_columnist_rotation_index[league_id] % len(_COLUMNIST_ROTATION)]
    _columnist_rotation_index[league_id] += 1

    # Pat Chen: enrich context with team strategy data.
    # Build a copy so we don't mutate the shared batch_context used by other callers.
    columnist_context = batch_context

    # Darius Cole fires independently every ~50 games — skip him in the regular rotation
    # so he doesn't consume a rotation slot.
    if persona_id == "darius_cole":
        persona_id = _COLUMNIST_ROTATION[_columnist_rotation_index[league_id] % len(_COLUMNIST_ROTATION)]
        _columnist_rotation_index[league_id] += 1

    if persona_id == "hot_take_hour":
        # Inject format_variant so the four Hot Take Hour variants cycle.
        columnist_context = dict(batch_context)
        columnist_context["format_variant"] = _FORMAT_VARIANTS[(_columnist_rotation_index[league_id] - 1) % len(_FORMAT_VARIANTS)]

        # Seed season-long HTH narratives on first use per league, then inject every time.
        global _HTH_NARRATIVES
        if league_id not in _HTH_NARRATIVES:
            try:
                # Seed: top scorer = "sleeper" Dave has been high on; best team = "fraud"
                # Tony doubts; two closest-stat players on different teams = "rivalry."
                _seed_rows = await pool.fetch(
                    """
                    SELECT p.id, p.first_name || ' ' || p.last_name AS player_name,
                           t.nba_team_code AS team_code, t.conference,
                           AVG(b.points) AS ppg,
                           AVG(b.rebounds_off + b.rebounds_def) AS rpg,
                           AVG(b.assists) AS apg,
                           COUNT(b.id) AS gp
                    FROM players p
                    JOIN game_box_scores b ON b.player_id = p.id
                    JOIN games g ON g.id = b.game_id
                    JOIN teams t ON t.id = p.team_id
                    WHERE g.league_id = $1 AND g.season = $2 AND g.season_type = 'regular'
                    GROUP BY p.id, t.nba_team_code, t.conference
                    HAVING COUNT(b.id) >= 10
                    ORDER BY AVG(b.points) DESC
                    LIMIT 20
                    """,
                    league_id, season,
                )
                _std_rows = await pool.fetch(
                    """
                    SELECT sc.team_id, t.nba_team_code, sc.wins, sc.losses
                    FROM standings_cache sc JOIN teams t ON t.id = sc.team_id
                    WHERE sc.league_id = $1 AND sc.season = $2
                    ORDER BY sc.wins DESC LIMIT 1
                    """,
                    league_id, season,
                )
                _top_team = _std_rows[0]["nba_team_code"] if _std_rows else "the league leader"
                _sleeper = _seed_rows[0]["player_name"] if _seed_rows else "the top scorer"
                # Rivalry: two players from different teams with similar scoring (top 5)
                _rivalry_a = _seed_rows[1]["player_name"] if len(_seed_rows) > 1 else None
                _rivalry_b = _seed_rows[2]["player_name"] if len(_seed_rows) > 2 else None
                _rivalry_teams = (
                    f"{_seed_rows[1]['team_code']} vs {_seed_rows[2]['team_code']}"
                    if len(_seed_rows) > 2 else ""
                )
                _HTH_NARRATIVES[league_id] = {
                    "sleeper_pick": (
                        f"Dave has been insisting all season that {_sleeper} is criminally underrated "
                        f"and deserves more recognition."
                    ),
                    "fraud_call": (
                        f"Tony has been calling {_top_team} a 'paper tiger' since day one — "
                        f"great record, no real test, waiting to collapse."
                    ),
                    "rivalry": (
                        f"Dave and Tony have been tracking the {_rivalry_teams} rivalry — "
                        f"{_rivalry_a} vs {_rivalry_b} — all season, arguing who's the better player."
                    ) if _rivalry_a and _rivalry_b else None,
                }
                log.info(f"HTH narratives seeded for league {league_id}: {_HTH_NARRATIVES[league_id]}")
            except Exception as _hth_exc:
                log.warning(f"HTH narrative seeding failed: {_hth_exc}")
                _HTH_NARRATIVES[league_id] = {}

        # Inject non-None narratives into context.
        _active_narratives = {k: v for k, v in _HTH_NARRATIVES.get(league_id, {}).items() if v}
        if _active_narratives:
            columnist_context["hth_season_narratives"] = _active_narratives
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
            pat_context["gameplans"] = [
                {
                    "matchup": f"{br['home_team'].nba_team_code if hasattr(br['home_team'], 'nba_team_code') else '???'} vs {br['away_team'].nba_team_code if hasattr(br['away_team'], 'nba_team_code') else '???'}",
                    "home": {
                        "team": br["home_team"].nba_team_code if hasattr(br["home_team"], "nba_team_code") else "???",
                        "rationale": br["home_gameplan"]["rationale"],
                        "scheme": br["home_gameplan"]["strategy"]["offensive_scheme"],
                    },
                    "away": {
                        "team": br["away_team"].nba_team_code if hasattr(br["away_team"], "nba_team_code") else "???",
                        "rationale": br["away_gameplan"]["rationale"],
                        "scheme": br["away_gameplan"]["strategy"]["offensive_scheme"],
                    },
                }
                for br in batch_results
                if br.get("home_gameplan") and br.get("away_gameplan")
            ]
            columnist_context = pat_context
        except Exception as _exc:
            log.warning(f"Pat Chen strategy enrichment failed: {_exc}")

    # Only post a regular-season article if something interesting happened.
    if _batch_is_interesting:
        # Ride-along: capture the prompt when the chosen persona is about to fire.
        _ra_capture: dict | None = (
            {} if (
                _columnist_ride_along.is_enabled()
                and persona_id == _columnist_ride_along.target_persona_id()
            ) else None
        )
        try:
            article = await asyncio.wait_for(
                columnist_service.generate(
                    pool, league_id, season,
                    persona_id=persona_id,
                    category="game_recap",
                    context=columnist_context,
                    subject_team_ids=subject_team_ids,
                    _capture_prompt=_ra_capture,
                ),
                timeout=20.0,
            )
        except Exception as _col_exc:
            log.warning(f"_maybe_post_columnist: article timed out or failed ({persona_id}): {_col_exc}")
            article = None
        if article:
            _last_columnist_game_index[league_id] = batch_end_index
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
            _sent = await analysis_channel.send(embed=embed)
            await _feedback_log.register_columnist_post(
                pool, _sent,
                league_id=league_id, season=season,
                persona_id=persona_id, category="game_recap",
                headline=article["headline"], body=article["body"],
                game_index=batch_end_index,
                subject_team_ids=list(subject_team_ids) if subject_team_ids else None,
            )
            # Ride-along: pause AFTER the embed lands in Discord.
            if _ra_capture is not None:
                _persona_obj = _PERSONAS.get(persona_id)
                await _columnist_ride_along.request_pause({
                    "persona_id": persona_id,
                    "persona_display_name": _persona_obj.display_name if _persona_obj else persona_id,
                    "league_id": league_id,
                    "season": season,
                    "game_index_at_post": batch_end_index,
                    "category": "game_recap",
                    "prompt": _ra_capture,
                    "context_dict": columnist_context,
                    "article": {
                        "headline": article.get("headline", ""),
                        "body": article.get("body", ""),
                        "raw_llm_response": _ra_capture.get("raw_llm_response", ""),
                    },
                    "embed_preview": (
                        f"{article.get('headline', '')}\n\n"
                        + article.get("body", "")[:400]
                    ),
                })
    else:
        log.debug(
            f"_maybe_post_columnist: skipping regular-season article (no interesting condition met) "
            f"for batch ending at game {batch_end_index}"
        )

    # Darius Cole — every ~30 games, independently.  Covers bottom-5 teams and lottery odds.
    # Counter was already incremented at the top of this function (before channel guard).
    # Threshold is 30 (not 50) so he fires in early-season testing with fewer games played.
    if _darius_game_counter.get(league_id, 0) >= 30:
        _darius_game_counter[league_id] = 0
        dc_persona = _PERSONAS.get("darius_cole")
        if not dc_persona:
            log.warning("_maybe_post_columnist: darius_cole persona missing from _PERSONAS — skipping")
        else:
            dc_article = None
            try:
                # Build bottom-5 context for Darius Cole.
                _all_standings = await pool.fetch(
                    """
                    SELECT sc.team_id, t.nba_team_code, sc.wins, sc.losses
                    FROM standings_cache sc
                    JOIN teams t ON t.id = sc.team_id
                    WHERE sc.league_id = $1 AND sc.season = $2
                    ORDER BY sc.wins ASC, sc.losses DESC
                    LIMIT 5
                    """,
                    league_id, season,
                )
                # Approximate lottery odds: last-place gets ~14%, decreasing by ~2% per slot.
                _lottery_base = [14.0, 13.4, 12.7, 12.0, 10.5]
                _bottom5 = []
                _bottom5_team_ids: list[int] = []
                for _idx, _row in enumerate(_all_standings):
                    _bottom5.append({
                        "team": _row["nba_team_code"],
                        "record": f"{_row['wins']}-{_row['losses']}",
                        "lottery_odds_pct": _lottery_base[_idx] if _idx < len(_lottery_base) else 9.0,
                    })
                    _bottom5_team_ids.append(_row["team_id"])
                _dc_context = dict(batch_context)
                _dc_context["bottom_5_teams"] = _bottom5
                _dc_context["article_focus"] = (
                    "Draft lottery odds, tanking race, pick asset value. "
                    "Focus on which teams are best positioned in the lottery."
                )
                log.info(f"Darius Cole: firing article (bottom_5={[t['team'] for t in _bottom5]})")
                _dc_ra_capture: dict | None = (
                    {} if (
                        _columnist_ride_along.is_enabled()
                        and "darius_cole" == _columnist_ride_along.target_persona_id()
                    ) else None
                )
                dc_article = await asyncio.wait_for(
                    columnist_service.generate(
                        pool, league_id, season,
                        persona_id="darius_cole",
                        category="tank_watch",
                        context=_dc_context,
                        subject_team_ids=_bottom5_team_ids,
                        _capture_prompt=_dc_ra_capture,
                    ),
                    timeout=20.0,
                )
            except Exception as _dc_exc:
                log.warning(
                    f"_maybe_post_columnist: darius_cole timed out or failed: {_dc_exc}",
                    exc_info=True,
                )
                _dc_ra_capture = None
            if dc_article:
                dc_embed = discord.Embed(
                    title=f"📋 {dc_article['headline']}",
                    description=dc_article["body"][:2000],
                    color=discord.Color.from_rgb(34, 139, 34),
                )
                dc_embed.set_footer(text=f"by {dc_persona.display_name} · {dc_persona.byline}")
                try:
                    _sent = await analysis_channel.send(embed=dc_embed)
                    await _feedback_log.register_columnist_post(
                        pool, _sent,
                        league_id=league_id, season=season,
                        persona_id="darius_cole", category="tank_watch",
                        headline=dc_article["headline"], body=dc_article["body"],
                        game_index=batch_end_index,
                        subject_team_ids=_bottom5_team_ids or None,
                    )
                    log.info("Darius Cole article posted to #analysis")
                    # Ride-along: pause AFTER embed lands in Discord.
                    if _dc_ra_capture is not None:
                        await _columnist_ride_along.request_pause({
                            "persona_id": "darius_cole",
                            "persona_display_name": dc_persona.display_name,
                            "league_id": league_id,
                            "season": season,
                            "game_index_at_post": batch_end_index,
                            "category": "tank_watch",
                            "prompt": _dc_ra_capture,
                            "context_dict": _dc_context,
                            "article": {
                                "headline": dc_article.get("headline", ""),
                                "body": dc_article.get("body", ""),
                                "raw_llm_response": _dc_ra_capture.get("raw_llm_response", ""),
                            },
                            "embed_preview": (
                                f"{dc_article.get('headline', '')}\n\n"
                                + dc_article.get("body", "")[:400]
                            ),
                        })
                except Exception as _dc_send_exc:
                    log.warning(f"_maybe_post_columnist: darius_cole send failed: {_dc_send_exc}")
            else:
                log.warning("Darius Cole: generate() returned None — article not posted")

    # Marcus Brooks — every ~200 games (every 20 batches), independently of the rotation.
    # Counter was already incremented at the top of this function (before channel guard).
    if _marcus_game_counter.get(league_id, 0) >= 200:
        _marcus_game_counter[league_id] = 0
        mb_persona = _PERSONAS.get("marcus_brooks")
        _mb_ra_capture: dict | None = (
            {} if (
                _columnist_ride_along.is_enabled()
                and "marcus_brooks" == _columnist_ride_along.target_persona_id()
            ) else None
        )
        try:
            mb_article = await asyncio.wait_for(
                columnist_service.generate(
                    pool, league_id, season,
                    persona_id="marcus_brooks",
                    category="power_rankings",
                    context=batch_context,
                    subject_team_ids=subject_team_ids,
                    _capture_prompt=_mb_ra_capture,
                ),
                timeout=20.0,
            )
        except Exception as _mb_exc:
            log.warning(f"_maybe_post_columnist: marcus_brooks timed out or failed: {_mb_exc}")
            mb_article = None
        if mb_article:
            embed = discord.Embed(
                title=mb_article["headline"],
                description=mb_article["body"][:2000],
                color=discord.Color.from_rgb(0, 128, 255),
            )
            if mb_persona:
                embed.set_footer(text=f"by {mb_persona.display_name} · {mb_persona.byline}")
            _sent = await analysis_channel.send(embed=embed)
            await _feedback_log.register_columnist_post(
                pool, _sent,
                league_id=league_id, season=season,
                persona_id="marcus_brooks", category="power_rankings",
                headline=mb_article["headline"], body=mb_article["body"],
                game_index=batch_end_index,
                subject_team_ids=list(subject_team_ids) if subject_team_ids else None,
            )
            # Ride-along: pause AFTER embed lands in Discord.
            if _mb_ra_capture is not None:
                _mb_p = mb_persona
                await _columnist_ride_along.request_pause({
                    "persona_id": "marcus_brooks",
                    "persona_display_name": _mb_p.display_name if _mb_p else "Marcus Brooks",
                    "league_id": league_id,
                    "season": season,
                    "game_index_at_post": batch_end_index,
                    "category": "power_rankings",
                    "prompt": _mb_ra_capture,
                    "context_dict": batch_context,
                    "article": {
                        "headline": mb_article.get("headline", ""),
                        "body": mb_article.get("body", ""),
                        "raw_llm_response": _mb_ra_capture.get("raw_llm_response", ""),
                    },
                    "embed_preview": (
                        f"{mb_article.get('headline', '')}\n\n"
                        + mb_article.get("body", "")[:400]
                    ),
                })


async def _maybe_post_playoff_columnist(
    pool,
    league_id: int,
    season: int,
    context: dict,
    guild: discord.Guild,
) -> None:
    """
    Post a playoff recap article to #analysis, rotating through the three
    recap-capable personas (maya_chen, jordan_rivera, keisha_williams).

    Called from playoff_service.sim_series_game — always for clinching/elimination
    games, ~30% of the time for regular playoff games.
    """
    _playoff_rotation_index[league_id] = _playoff_rotation_index.get(league_id, 0)
    persona_id = _PLAYOFF_COLUMNIST_ROTATION[_playoff_rotation_index[league_id] % len(_PLAYOFF_COLUMNIST_ROTATION)]
    _playoff_rotation_index[league_id] += 1

    persona = _PERSONAS.get(persona_id)
    if not persona:
        return

    analysis_channel_id = await league_repo.get_channel(pool, league_id, "analysis")
    analysis_channel = guild.get_channel(analysis_channel_id) if analysis_channel_id else None
    if not analysis_channel:
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
        _sent = await analysis_channel.send(embed=embed)
    except Exception as exc:
        log.warning(f"Playoff columnist post failed: {exc}")
    else:
        _series_team_ids = [
            tid for tid in (
                context.get("high_seed_team_id"),
                context.get("low_seed_team_id"),
            ) if tid
        ]
        await _feedback_log.register_columnist_post(
            pool, _sent,
            league_id=league_id, season=season,
            persona_id=persona_id, category="playoff_recap",
            headline=article["headline"], body=article["body"],
            subject_team_ids=_series_team_ids or None,
        )


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
    """
    if not await game_repo.all_regular_season_games_complete(pool, league_id, season):
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


async def check_user_matchups_in_range(
    pool,
    league_id: int,
    season: int,
    from_idx: int,
    to_idx: int,
) -> List[dict]:
    games = await game_repo.get_games_in_range(pool, league_id, season, from_idx, to_idx)
    return [g for g in games if g.get("is_user_matchup") and g.get("status") == "scheduled"]


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
    strategy_service.clear_archetype_cache()
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
