"""Columnist/report content posted during batch sim: awards races, POTM, coach
beat, power rankings, and the rest of the `_maybe_post_*` family.

Extracted from batch_sim_runner.py one function at a time (see HANDOFF.md for
progress) -- each conversion is characterization-tests-first: pin the current
inline-discord.Embed output with a recording fake, confirm against the
original, convert to EmbedData + an announcer, confirm again unchanged, then
move here. Functions that only call an existing bot/embeds/ builder (which
already returns an opaque discord.Embed) and forward it to channel.send are
moved as-is without conversion -- same precedent as
sim_persistence.py's use of bot/embeds/sim_embeds.season_record_embed.
"""
from __future__ import annotations

import asyncio
import random
import re
from collections import defaultdict
from typing import TYPE_CHECKING

from bot.embeds import awards_embeds
from core.logging import get_logger
from data.repositories import article_repo, game_repo, league_repo
from phase.states import Phase
from services import awards_service, columnist_service, potm_service, team_intel
from services import feedback_log as _feedback_log
from services.announcer_protocol import EmbedData
from services.personas import PERSONAS as _PERSONAS
from services.sim_channel_announcer import _BoundChannelAnnouncer, _get_news_channel

if TYPE_CHECKING:
    import discord

log = get_logger(__name__)

# Matches discord.Color.from_rgb(r, g, b) -- so this module never needs
# `import discord`. Persona brand colors, shared across the columnist
# content functions.
_PERSONA_COLORS: dict[str, tuple[int, int, int]] = {
    "jordan_rivera":  (138, 43, 226),
    "keisha_williams": (0, 128, 255),
    "hot_take_hour":  (255, 0, 0),
    "pat_chen":       (0, 180, 150),
    "darius_cole":    (34, 139, 34),
    "coach_beat":     (160, 82, 45),
    "carla_knox":     (80, 160, 200),
}


def _rgb_to_int(rgb: tuple[int, int, int]) -> int:
    r, g, b = rgb
    return (r << 16) | (g << 8) | b


async def _build_batch_game_context(batch_results: list[dict]) -> dict:
    """Build a minimal context dict from batch game results for specialty personas.

    Only includes recent_games summary — standings and team intel are not fetched
    here to keep the call lightweight.  The AI is instructed to use only what it
    receives, so partial context is fine for these cadence-driven columns.
    """
    recent_games = []
    for br in batch_results[-10:]:  # last 10 games is enough context
        ht = br.get("home_team")
        at = br.get("away_team")
        r = br.get("result", {})
        if not (ht and at):
            continue
        home_code = getattr(ht, "nba_team_code", "???")
        away_code = getattr(at, "nba_team_code", "???")
        recent_games.append({
            "home": home_code,
            "away": away_code,
            "home_score": r.get("home_score", 0),
            "away_score": r.get("away_score", 0),
            "winner": home_code if r.get("winner_team_id") == ht.id else away_code,
            "top_scorer": r.get("top_scorer", {}),
        })
    return {"recent_games": recent_games}


# fires every ~70 games (weekly)
_power_list_game_counter: dict[int, int] = {}


async def _maybe_post_power_list(
    pool,
    league_id: int,
    season: int,
    batch_results: list[dict],
    guild: discord.Guild,
) -> None:
    """Post The Power List weekly top-10 ranking every ~70 games (approx. one game-week)."""
    _power_list_game_counter[league_id] = _power_list_game_counter.get(league_id, 0) + len(batch_results)
    if _power_list_game_counter[league_id] < 70:
        return
    _power_list_game_counter[league_id] = 0

    analysis_channel_id = await league_repo.get_channel(pool, league_id, "analysis")
    analysis_channel = guild.get_channel(analysis_channel_id) if analysis_channel_id else None
    if not analysis_channel:
        return

    persona = _PERSONAS.get("power_list")
    if not persona:
        log.warning("_maybe_post_power_list: power_list persona not registered — skipping")
        return

    try:
        context = await _build_batch_game_context(batch_results)
        standings = await game_repo.get_standings(pool, league_id, season)
        if not standings:
            log.info("_maybe_post_power_list: no standings data — skipping")
            return
        context["standings"] = standings

        # Add win/loss streaks for richer power-ranking narrative.
        streak_rows = await pool.fetch(
            """
            SELECT t.nba_team_code, sc.wins, sc.losses,
                   sc.win_streak, sc.loss_streak
            FROM standings_cache sc
            JOIN teams t ON t.id = sc.team_id
            WHERE sc.league_id = $1 AND sc.season = $2
            ORDER BY sc.wins DESC, sc.losses ASC
            """,
            league_id, season,
        )
        context["power_rankings_data"] = [
            {
                "team": r["nba_team_code"],
                "record": f"{r['wins']}-{r['losses']}",
                "win_streak": r["win_streak"] or 0,
                "loss_streak": r["loss_streak"] or 0,
            }
            for r in streak_rows
        ]

        # Build rank_deltas from the most recent previous power_rankings article.
        # Positive delta = team moved up; negative = down; 0 = unchanged; missing = NEW.
        rank_deltas: dict[str, int] = {}
        prev_articles = await article_repo.recent_by_persona(pool, league_id, "power_list", limit=1, season=season)
        if prev_articles:
            prev_body = prev_articles[0].get("body", "") or ""
            # Extract team code → rank from "> **N.** TEAM_CODE" lines.
            prev_ranks: dict[str, int] = {}
            for m in re.finditer(r">\s*\*\*(\d+)\.\*\*\s+([A-Z]{2,4})", prev_body):
                prev_ranks[m.group(2)] = int(m.group(1))
            # Current rank is positional in the streak_rows (sorted by wins DESC already).
            for cur_rank, row in enumerate(streak_rows, start=1):
                code = row["nba_team_code"]
                if code in prev_ranks:
                    rank_deltas[code] = prev_ranks[code] - cur_rank  # positive = moved up
                # else: not in prev_ranks → missing key → LLM uses NEW
            if not prev_ranks:
                log.debug("_maybe_post_power_list: previous article had no parseable ranks — all NEW")
        else:
            log.debug("_maybe_post_power_list: no prior ranking this season — all NEW")

        context["rank_deltas"] = rank_deltas
        if not rank_deltas:
            # Tell the LLM explicitly so it doesn't invent arrows.
            context["rank_deltas_note"] = "No prior ranking exists this season; use NEW for every team's arrow position."

        article = await asyncio.wait_for(
            columnist_service.generate(
                pool, league_id, season,
                persona_id="power_list",
                category="power_rankings",
                context=context,
            ),
            timeout=20.0,
        )
        if article:
            embed_data = EmbedData(
                title=f"🏆 {article['headline']}",
                description=article["body"][:2000],
                color=_rgb_to_int((212, 175, 55)),
                footer=f"by {persona.display_name} · {persona.byline}",
            )
            _sent = await _BoundChannelAnnouncer(analysis_channel).post_embed_get_ref(
                "analysis", embed_data
            )
            await _feedback_log.register_columnist_post(
                pool, _sent,
                league_id=league_id, season=season,
                persona_id="power_list", category="power_rankings",
                headline=article["headline"], body=article["body"],
            )
    except Exception as exc:
        log.warning(f"_maybe_post_power_list failed: {exc}", exc_info=True)


# fires every ~70 games (weekly)
_rookie_watch_game_counter: dict[int, int] = {}


async def _maybe_post_rookie_watch(
    pool,
    league_id: int,
    season: int,
    batch_results: list[dict],
    guild: discord.Guild,
) -> None:
    """Post Rookie Watch development tracker every ~70 games (approx. one game-week)."""
    _rookie_watch_game_counter[league_id] = _rookie_watch_game_counter.get(league_id, 0) + len(batch_results)
    if _rookie_watch_game_counter[league_id] < 70:
        return
    _rookie_watch_game_counter[league_id] = 0

    analysis_channel_id = await league_repo.get_channel(pool, league_id, "analysis")
    analysis_channel = guild.get_channel(analysis_channel_id) if analysis_channel_id else None
    if not analysis_channel:
        return

    persona = _PERSONAS.get("rookie_watch")
    if not persona:
        log.warning("_maybe_post_rookie_watch: rookie_watch persona not registered — skipping")
        return

    try:
        context = await _build_batch_game_context(batch_results)

        # Fetch rookie players and their recent stat averages.
        rookie_rows = await pool.fetch(
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
            WHERE p.league_id = $1 AND p.is_rookie = true
            GROUP BY p.id, t.nba_team_code
            ORDER BY AVG(b.points) DESC NULLS LAST
            LIMIT 10
            """,
            league_id, season,
        )
        rookies = [dict(r) for r in rookie_rows]
        if not rookies:
            log.info("_maybe_post_rookie_watch: no rookies found — skipping")
            return
        context["rookies"] = rookies

        article = await asyncio.wait_for(
            columnist_service.generate(
                pool, league_id, season,
                persona_id="rookie_watch",
                category="rookie_watch",
                context=context,
            ),
            timeout=20.0,
        )
        if article:
            embed_data = EmbedData(
                title=f"🌟 {article['headline']}",
                description=article["body"][:2000],
                color=_rgb_to_int((100, 200, 120)),
                footer=f"by {persona.display_name} · {persona.byline}",
            )
            _sent = await _BoundChannelAnnouncer(analysis_channel).post_embed_get_ref(
                "analysis", embed_data
            )
            await _feedback_log.register_columnist_post(
                pool, _sent,
                league_id=league_id, season=season,
                persona_id="rookie_watch", category="rookie_watch",
                headline=article["headline"], body=article["body"],
                subject_player_ids=[r["id"] for r in rookies if r.get("id")],
            )
    except Exception as exc:
        log.warning(f"_maybe_post_rookie_watch failed: {exc}", exc_info=True)


# fires every ~70 games (weekly)
_big_picture_game_counter: dict[int, int] = {}


async def _maybe_post_big_picture(
    pool,
    league_id: int,
    season: int,
    batch_results: list[dict],
    guild: discord.Guild,
) -> None:
    """Post The Big Picture long-form column every ~70 games (weekly cadence, Sunday-equivalent)."""
    _big_picture_game_counter[league_id] = _big_picture_game_counter.get(league_id, 0) + len(batch_results)
    if _big_picture_game_counter[league_id] < 70:
        return
    _big_picture_game_counter[league_id] = 0

    analysis_channel_id = await league_repo.get_channel(pool, league_id, "analysis")
    analysis_channel = guild.get_channel(analysis_channel_id) if analysis_channel_id else None
    if not analysis_channel:
        return

    persona = _PERSONAS.get("big_picture")
    if not persona:
        log.warning("_maybe_post_big_picture: big_picture persona not registered — skipping")
        return

    try:
        context = await _build_batch_game_context(batch_results)
        standings = await game_repo.get_standings(pool, league_id, season)
        context["standings"] = standings

        # Top performers season-to-date for thematic anchor.
        top_performers = await pool.fetch(
            """
            SELECT p.first_name || ' ' || p.last_name AS name,
                   t.nba_team_code AS team,
                   ROUND(AVG(b.points)::numeric, 1) AS ppg,
                   ROUND(AVG(b.assists)::numeric, 1) AS apg,
                   ROUND(AVG(b.rebounds_off + b.rebounds_def)::numeric, 1) AS rpg,
                   COUNT(b.id) AS gp
            FROM players p
            JOIN teams t ON t.id = p.team_id
            JOIN game_box_scores b ON b.player_id = p.id
            JOIN games g ON g.id = b.game_id
            WHERE g.league_id = $1 AND g.season = $2 AND g.season_type = 'regular'
            GROUP BY p.id, t.nba_team_code
            HAVING COUNT(b.id) >= 5
            ORDER BY AVG(b.points) DESC
            LIMIT 8
            """,
            league_id, season,
        )
        context["top_performers"] = [dict(r) for r in top_performers]

        article = await asyncio.wait_for(
            columnist_service.generate(
                pool, league_id, season,
                persona_id="big_picture",
                category="sunday_column",
                context=context,
            ),
            timeout=20.0,
        )
        if article:
            embed_data = EmbedData(
                title=f"🔭 {article['headline']}",
                description=article["body"][:2000],
                color=_rgb_to_int((70, 90, 160)),
                footer=f"by {persona.display_name} · {persona.byline}",
            )
            _sent = await _BoundChannelAnnouncer(analysis_channel).post_embed_get_ref(
                "analysis", embed_data
            )
            await _feedback_log.register_columnist_post(
                pool, _sent,
                league_id=league_id, season=season,
                persona_id="big_picture", category="sunday_column",
                headline=article["headline"], body=article["body"],
            )
    except Exception as exc:
        log.warning(f"_maybe_post_big_picture failed: {exc}", exc_info=True)


# fires every ~280 games (monthly), forced on first post-deadline batch of a season
_ledger_game_counter: dict[int, int] = {}
_ledger_first_post_done: dict[tuple[int, int], bool] = {}


async def _maybe_post_ledger(
    pool,
    league_id: int,
    season: int,
    batch_results: list[dict],
    guild: discord.Guild,
) -> None:
    """Post The Ledger front-office grades, gated to post-deadline phases only.

    First post fires when the league transitions to TRADE_DEADLINE_OPEN or later,
    regardless of counter. Subsequent posts fire every ~280 games (approx. monthly).
    Before the trade deadline: no Ledger columns.
    """
    # Phase gate: whitelist of phases where Ledger is allowed to fire.
    # Spec: "phase-aware gating; bails entirely pre-deadline; first post forced on
    # TRADE_DEADLINE_OPEN phase transition." Playoffs, draft, and offseason phases
    # are intentionally excluded — Ledger is a trade-era column only.
    _LEDGER_ALLOWED_PHASES = {
        Phase.TRADE_DEADLINE_OPEN.value,
        Phase.REGULAR_SEASON_POSTDEADLINE.value,
        Phase.REGULAR_SEASON_COMPLETE.value,
    }
    phase_row = await pool.fetchrow("SELECT current_phase FROM leagues WHERE id = $1", league_id)
    current_phase = phase_row["current_phase"] if phase_row else ""
    if current_phase not in _LEDGER_ALLOWED_PHASES:
        log.debug(f"_maybe_post_ledger: league={league_id} phase={current_phase!r} — not a Ledger phase, skipping")
        return

    _ledger_game_counter[league_id] = _ledger_game_counter.get(league_id, 0) + len(batch_results)
    _season_key = (league_id, season)
    _ledger_first_post_done.setdefault(_season_key, False)

    # Force-fire on the first post-deadline batch of this season (regardless of counter).
    force_fire = not _ledger_first_post_done[_season_key]
    if not force_fire and _ledger_game_counter[league_id] < 280:
        return
    if not force_fire:
        _ledger_game_counter[league_id] = 0

    analysis_channel_id = await league_repo.get_channel(pool, league_id, "analysis")
    analysis_channel = guild.get_channel(analysis_channel_id) if analysis_channel_id else None
    if not analysis_channel:
        return

    persona = _PERSONAS.get("the_ledger")
    if not persona:
        log.warning("_maybe_post_ledger: the_ledger persona not registered — skipping")
        return

    try:
        context = await _build_batch_game_context(batch_results)

        # Fetch recent approved trades with asset summaries.
        trade_rows = await pool.fetch(
            """
            SELECT tr.id, t1.nba_team_code AS proposer, t2.nba_team_code AS counterparty,
                   tr.proposed_at
            FROM trades tr
            JOIN teams t1 ON t1.id = tr.proposer_team_id
            JOIN teams t2 ON t2.id = tr.counterparty_team_id
            WHERE tr.league_id = $1 AND tr.status = 'approved'
            ORDER BY tr.id DESC LIMIT 5
            """,
            league_id,
        )
        recent_trades: list[dict] = []
        for tr in trade_rows:
            asset_rows = await pool.fetch(
                """
                SELECT ta.from_team_id, ta.asset_type,
                       p.first_name || ' ' || p.last_name AS player_name, p.overall
                FROM trade_assets ta
                LEFT JOIN players p ON p.id = ta.player_id
                WHERE ta.trade_id = $1
                """,
                tr["id"],
            )
            recent_trades.append({
                "teams": f"{tr['proposer']} / {tr['counterparty']}",
                "assets": [
                    {"from": a["from_team_id"], "type": a["asset_type"],
                     "name": a["player_name"], "ovr": a["overall"]}
                    for a in asset_rows
                ],
            })

        # Fetch team cpu_mode / win-loss records for mode changes.
        team_mode_rows = await pool.fetch(
            """
            SELECT t.nba_team_code, t.cpu_mode,
                   sc.wins, sc.losses
            FROM teams t
            LEFT JOIN standings_cache sc ON sc.team_id = t.id
                AND sc.league_id = $1 AND sc.season = $2
            WHERE t.league_id = $1
            ORDER BY (sc.wins + sc.losses) DESC NULLS LAST
            """,
            league_id, season,
        )
        team_modes = [
            {"team": r["nba_team_code"], "mode": r["cpu_mode"] or "default",
             "record": f"{r['wins'] or 0}-{r['losses'] or 0}"}
            for r in team_mode_rows
        ]

        if not recent_trades and not team_modes:
            log.info("_maybe_post_ledger: no trade/mode data — skipping")
            return

        context["recent_trades"] = recent_trades
        context["team_modes"] = team_modes

        article = await asyncio.wait_for(
            columnist_service.generate(
                pool, league_id, season,
                persona_id="the_ledger",
                category="front_office_grade",
                context=context,
            ),
            timeout=20.0,
        )
        if article:
            embed_data = EmbedData(
                title=f"📒 {article['headline']}",
                description=article["body"][:2000],
                color=_rgb_to_int((120, 120, 120)),
                footer=f"by {persona.display_name} · {persona.byline}",
            )
            _sent = await _BoundChannelAnnouncer(analysis_channel).post_embed_get_ref(
                "analysis", embed_data
            )
            await _feedback_log.register_columnist_post(
                pool, _sent,
                league_id=league_id, season=season,
                persona_id="the_ledger", category="front_office_grade",
                headline=article["headline"], body=article["body"],
            )
            # Mark first post done and reset counter after successful send.
            _ledger_first_post_done[_season_key] = True
            _ledger_game_counter[league_id] = 0
    except Exception as exc:
        log.warning(f"_maybe_post_ledger failed: {exc}", exc_info=True)


# fires every ~280 games (monthly)
_race_game_counter: dict[int, int] = {}


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
            embed_data = EmbedData(
                title=f"🏅 {article['headline']}",
                description=article["body"][:2000],
                color=_rgb_to_int((200, 160, 40)),
                footer=f"by {persona.display_name} · {persona.byline}",
            )
            _sent = await _BoundChannelAnnouncer(analysis_channel).post_embed_get_ref(
                "analysis", embed_data
            )
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
            embed_data = EmbedData(
                title=f"🩺 {article['headline']}",
                description=article["body"][:2000],
                color=_rgb_to_int((231, 76, 60)),
                footer=f"by {persona.display_name} · {persona.byline}",
            )
            _sent = await _BoundChannelAnnouncer(analysis_channel).post_embed_get_ref(
                "analysis", embed_data
            )
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
            embed_data = EmbedData(
                title=f"🎬 {article['headline']}",
                description=article["body"][:2000],
                color=_rgb_to_int((80, 40, 120)),
                footer=f"by {persona.display_name} · {persona.byline}",
            )
            _sent = await _BoundChannelAnnouncer(analysis_channel).post_embed_get_ref(
                "analysis", embed_data
            )
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


# Tracks the last simulated "YYYY-MM" POTM was checked for, per league_id, so
# batches within the same simulated month short-circuit without a DB round-trip.
_potm_last_checked_month: dict[int, str] = {}


async def _maybe_post_potm(
    pool,
    guild: discord.Guild,
    league_id: int,
    season: int,
    current_game_date: str | None,
    current_game_index: int = 0,
    prefetched_race_leaders: dict | None = None,
) -> None:
    """Post Player of the Month awards if a new month has elapsed since the last award.

    When a new month is detected, also posts a visual month separator to #analysis
    and fires the award race odds (once per simulated month, tied to this cycle).

    Month-gate: batches within the same simulated calendar month are short-circuited
    in the runner without touching the DB, avoiding repeated potm_service round-trips
    that always return None mid-month.
    """
    if not current_game_date:
        log.debug("_maybe_post_potm: no current_game_date, skipping")
        return

    current_month = current_game_date[:7]  # "YYYY-MM"
    if _potm_last_checked_month.get(league_id) == current_month:
        log.debug(
            f"_maybe_post_potm: same month {current_month} as last check for league {league_id}, skipping"
        )
        return
    _potm_last_checked_month[league_id] = current_month

    log.info(
        f"_maybe_post_potm called: league={league_id} season={season} "
        f"current_game_date={current_game_date!r}"
    )
    if not current_game_date:
        return
    try:
        log.info(f"_maybe_post_potm: calling check_and_get_potm_awards for league={league_id}")
        awards = await potm_service.check_and_get_potm_awards(
            pool, league_id, season, current_game_date
        )
        log.info(f"_maybe_post_potm: check_and_get_potm_awards returned {awards!r}")
        if not awards:
            log.info("_maybe_post_potm: no awards to post (None=already awarded, []=no eligible players)")
            return
        pat = _PERSONAS.get("pat_chen")
        if not pat:
            log.warning("_maybe_post_potm: pat_chen persona not found in _PERSONAS")
            return
        news_channel = await _get_news_channel(guild, pool, league_id)
        if not news_channel:
            log.warning(f"_maybe_post_potm: no league-news channel configured for league {league_id}")
            return

        # Post month separator to #analysis before columnist articles.
        analysis_channel_id = await league_repo.get_channel(pool, league_id, "analysis")
        analysis_channel = guild.get_channel(analysis_channel_id) if analysis_channel_id else None
        if analysis_channel:
            # Derive month label from first award (all awards share same month block).
            _sep_label = awards[0]["month_label"] if awards else current_game_date[:7]
            _sep_text = "━" * 22 + f"\n\U0001f4c5  {_sep_label}\n" + "━" * 22
            try:
                await analysis_channel.send(_sep_text)
            except Exception as exc:
                log.warning(f"Month separator post failed: {exc}")

        # Group awards by month so we generate one article per month (East+West together).
        by_month: dict[str, list[dict]] = defaultdict(list)
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
                embed_data = EmbedData(
                    title=f"\U0001F3C6 {article['headline']}",
                    description=article["body"][:2000],
                    color=_rgb_to_int(_PERSONA_COLORS.get("pat_chen", (100, 100, 100))),
                    footer=f"by {pat.display_name} · {pat.byline}",
                )
                try:
                    _sent = await _BoundChannelAnnouncer(news_channel).post_embed_get_ref(
                        "league-news", embed_data
                    )
                except Exception as exc:
                    log.warning(f"POTM post failed: {exc}")
                else:
                    await _feedback_log.register_columnist_post(
                        pool, _sent,
                        league_id=league_id, season=season,
                        persona_id="pat_chen", category="player_of_the_month",
                        headline=article["headline"], body=article["body"],
                        game_index=current_game_index,
                        subject_player_ids=[a["player_id"] for a in month_awards if a.get("player_id")],
                    )

        # Award races fire once per month, right after POTM announcements.
        await _maybe_post_awards_races(
            pool, league_id, season, news_channel,
            current_game_index=current_game_index,
            prefetched_leaders=prefetched_race_leaders,
        )
    except Exception as exc:
        log.warning(f"_maybe_post_potm failed: {exc}", exc_info=True)


async def _maybe_post_awards_races(
    pool,
    league_id: int,
    season: int,
    news_channel: discord.TextChannel | None,
    current_game_index: int = 0,
    prefetched_leaders: dict | None = None,
) -> None:
    """Post award race odds. Called from _maybe_post_potm so it fires once per simulated month."""
    try:
        if not news_channel:
            return
        odds = await awards_service.generate_awards_race_odds(
            pool, league_id, season, prefetched_leaders=prefetched_leaders
        )
        if not odds:
            return
        embed = awards_embeds.awards_race_embed(odds, game_index=current_game_index)
        if embed:
            await news_channel.send(embed=embed)
    except Exception as exc:
        log.warning(f"_maybe_post_awards_races failed: {exc}", exc_info=True)


# Tracks games processed so Quinn Park (coach_beat) fires every ~50 games.
# Keyed by league_id so multi-league bots don't bleed counters across leagues.
_coach_beat_game_counter: dict[int, int] = {}


async def _maybe_post_coach_beat(
    pool,
    league_id: int,
    season: int,
    batch_results: list[dict],
    guild: discord.Guild,
) -> None:
    """Post a Quinn Park (coach_beat) article every ~50 games.

    Focuses on the team with the most extreme philosophy-vs-outcome mismatch in
    the current batch.  Swallows exceptions so article failures never abort the sim.
    """
    _coach_beat_game_counter[league_id] = _coach_beat_game_counter.get(league_id, 0) + len(batch_results)
    if _coach_beat_game_counter[league_id] < 50:
        return
    # Counter is NOT reset here — it resets only after a successful post below.
    # This lets the column fire on the next batch that has actual content to say.

    analysis_channel_id = await league_repo.get_channel(pool, league_id, "analysis")
    analysis_channel = guild.get_channel(analysis_channel_id) if analysis_channel_id else None
    if not analysis_channel:
        return

    cb_persona = _PERSONAS.get("coach_beat")
    if not cb_persona:
        log.warning("_maybe_post_coach_beat: coach_beat persona not registered — skipping")
        return

    try:
        # Identify the most "interesting" coaching team from the recent batch by
        # finding the team with the most extreme philosophy (chaos or vet_overrater)
        # among teams in this batch.  Fall back to any team if none qualify.
        batch_team_ids: list[int] = []
        for br in batch_results:
            ht = br.get("home_team")
            at = br.get("away_team")
            if ht:
                batch_team_ids.append(ht.id)
            if at:
                batch_team_ids.append(at.id)
        batch_team_ids = list(dict.fromkeys(batch_team_ids))  # deduplicate

        raw = await pool.fetchrow(
            "SELECT * FROM leagues WHERE id = $1", league_id
        )
        if not raw:
            return
        from data.repositories import league_repo as _lr
        league = _lr._league_from_record(raw)

        intel = await team_intel.build_team_intel(
            pool, league, season, batch_team_ids,
            include=("posture", "plan", "philosophy", "recent_role_changes"),
        )

        # Prioritise chaos, vet_overrater, and youth_developer teams.
        # Use random.choice among ALL matching teams rather than iterating in
        # tuple order — previously "chaos" always won because it came first,
        # causing every Coach Beat post to be about a chaos team.
        _PRIORITY_PHILOSOPHIES = {"chaos", "vet_overrater", "youth_developer"}
        priority_candidates = [
            tid for tid, data in intel.items()
            if data.get("philosophy") in _PRIORITY_PHILOSOPHIES
        ]
        subject_team_id: int | None = None
        if priority_candidates:
            subject_team_id = random.choice(priority_candidates)
        elif batch_team_ids:
            subject_team_id = random.choice(batch_team_ids)
        if subject_team_id is None:
            return

        subject_intel = intel.get(subject_team_id, {})

        # Content gate: skip the post (but don't reset the counter) when the selected
        # team has nothing interesting to say.
        # Require non-empty recent_role_changes — philosophy alone is too permissive
        # and was causing firings on vet_overrater teams with no actual story.
        # The counter stays at ≥50 so the next batch with real content fires
        # the column immediately rather than waiting another 50 games.
        recent_role_changes = subject_intel.get("recent_role_changes", [])
        subject_philosophy = subject_intel.get("philosophy", "tendency_respecter")
        if not recent_role_changes:
            log.debug(
                f"_maybe_post_coach_beat: league={league_id} team={subject_team_id} "
                f"philosophy={subject_philosophy!r} but no recent_role_changes — "
                "skipping, counter retained at ≥50 for next batch"
            )
            return

        # Fetch team code for context.
        team_row = await pool.fetchrow(
            "SELECT nba_team_code FROM teams WHERE id = $1", subject_team_id
        )
        team_code = team_row["nba_team_code"] if team_row else "???"

        cb_context = {
            "posture":             subject_intel.get("posture"),
            "plan":                subject_intel.get("plan"),
            "philosophy":          subject_philosophy,
            "recent_role_changes": recent_role_changes,
            "subject_team_code":   team_code,
        }

        cb_article = await asyncio.wait_for(
            columnist_service.generate(
                pool, league_id, season,
                persona_id="coach_beat",
                category="coaching_beat",
                context=cb_context,
                subject_team_ids=[subject_team_id],
            ),
            timeout=15.0,
        )
        if cb_article:
            embed_data = EmbedData(
                title=f"\U0001F3A4 {cb_article['headline']}",
                description=cb_article["body"][:2000],
                color=_rgb_to_int((160, 82, 45)),
                footer=f"by {cb_persona.display_name} · {cb_persona.byline}",
            )
            _sent = await _BoundChannelAnnouncer(analysis_channel).post_embed_get_ref(
                "analysis", embed_data
            )
            await _feedback_log.register_columnist_post(
                pool, _sent,
                league_id=league_id, season=season,
                persona_id="coach_beat", category="coaching_beat",
                headline=cb_article["headline"], body=cb_article["body"],
                subject_team_ids=[subject_team_id],
            )
            # Reset counter only after a successful post so empty-content batches
            # can retry immediately on the next batch.
            _coach_beat_game_counter[league_id] = 0
    except Exception as exc:
        log.warning(f"_maybe_post_coach_beat failed: {exc}", exc_info=True)
