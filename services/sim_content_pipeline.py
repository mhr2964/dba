"""Columnist/report content posted during batch sim: awards races, POTM, coach
beat, power rankings, and the rest of the `_maybe_post_*` family.

Extracted from sim_orchestrator.py one function at a time (see HANDOFF.md for
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
from collections import Counter, defaultdict
from typing import TYPE_CHECKING

from bot.embeds import awards_embeds
from core.logging import get_logger
from data.repositories import article_repo, game_repo, league_repo
from phase.states import Phase
from services import awards_service, columnist_service, potm_service, strategy_service, team_intel
from services import columnist_ride_along as _columnist_ride_along
from services import feedback_log as _feedback_log
from services.announcer_protocol import EmbedData
from services.personas import PERSONAS as _PERSONAS
from services.player_style_service import context_summary as _player_style_context
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


async def _build_player_form_signals(
    pool, league_id: int, season: int, performers: list[dict],
) -> dict[str, dict]:
    """Compute a real 'recent form vs season/OVR expectation' signal for notable
    batch performers, reusing the trade system's compute_form_map (Finding #2).

    Player-level "declining/washed/cooked" claims previously had no grounded
    threshold to check against — unlike team-level streak claims, which are
    gated on real win_streak/loss_streak values. This gives Jordan Rivera and
    Hot Take Hour a real number to check a decline claim against, the same way
    trade_reasoning_fetchers._fetch_player_form grounds buy-low/sell-high
    framing in trade panels.

    Returns dict keyed by player NAME (how personas reference players in
    prose) -> {"form_modifier": float, "games_played": int, "read": str}.
    Only includes players compute_form_map considers to have a sufficient
    sample (>= 10 games this season) — the same gate the trade system uses —
    so a decline claim is never grounded off a handful of noisy games.
    """
    ids = [p["player_id"] for p in performers if p.get("player_id")]
    if not ids:
        return {}
    try:
        from services import trade_context_builder
        ovr_rows = await pool.fetch(
            "SELECT id, overall FROM players WHERE id = ANY($1)", ids,
        )
        ovr_map = {r["id"]: r["overall"] or 80 for r in ovr_rows}
        form_map = await trade_context_builder.compute_form_map(
            pool, player_ids=ids, ovr_map=ovr_map, position_map={},
            league_id=league_id, season=season,
        )
    except Exception as exc:
        log.warning(f"_build_player_form_signals: compute_form_map failed: {exc}")
        return {}

    signals: dict[str, dict] = {}
    for p in performers:
        pid = p.get("player_id")
        name = p.get("name")
        if not pid or not name or pid not in form_map:
            continue
        modifier, stats = form_map[pid]
        games = stats.get("games_played", 0)
        if games < 10:
            continue  # insufficient sample — same gate compute_form_map itself uses
        if modifier < 0.92:
            read = "cold stretch — running well below season/OVR expectation"
        elif modifier >= 1.10:
            read = "hot stretch — running well above season/OVR expectation"
        else:
            read = "normal form — in line with season expectation"
        signals[name] = {
            "form_modifier": modifier,
            "games_played": games,
            "read": read,
        }
    return signals


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

        # Build rank_deltas from the most recent previous power_rankings article's
        # STRUCTURED rank data (team code -> rank), not by regex-parsing the LLM's
        # own rendered markdown (Finding #5) -- any formatting deviation in the
        # rendered body used to silently degrade every team to "NEW".
        # Positive delta = team moved up; negative = down; 0 = unchanged; missing = NEW.
        rank_deltas: dict[str, int] = {}
        prev_articles = await article_repo.recent_by_persona(pool, league_id, "power_list", limit=1, season=season)
        if prev_articles:
            prev_ranks: dict[str, int] = prev_articles[0].get("structured_data") or {}
            if not prev_ranks:
                # Legacy article predating the structured_data column — fall back to
                # regex-parsing its rendered body so a mid-season upgrade doesn't
                # reset every team to NEW on the very next post.
                prev_body = prev_articles[0].get("body", "") or ""
                for m in re.finditer(r">\s*\*\*(\d+)\.\*\*\s+([A-Z]{2,4})", prev_body):
                    prev_ranks[m.group(2)] = int(m.group(1))
                if prev_ranks:
                    log.debug("_maybe_post_power_list: prior article had no structured_data — used legacy regex parse")
                else:
                    log.debug("_maybe_post_power_list: previous article had no parseable ranks — all NEW")
            # Current rank is positional in the streak_rows (sorted by wins DESC already).
            for cur_rank, row in enumerate(streak_rows, start=1):
                code = row["nba_team_code"]
                if code in prev_ranks:
                    rank_deltas[code] = prev_ranks[code] - cur_rank  # positive = moved up
                # else: not in prev_ranks → missing key → LLM uses NEW
        else:
            log.debug("_maybe_post_power_list: no prior ranking this season — all NEW")

        context["rank_deltas"] = rank_deltas
        if not rank_deltas:
            # Tell the LLM explicitly so it doesn't invent arrows.
            context["rank_deltas_note"] = "No prior ranking exists this season; use NEW for every team's arrow position."

        # The CURRENT ranking, structured (team code -> rank), persisted alongside
        # this article's rendered text so the NEXT post's rank_deltas read real
        # data instead of re-parsing this post's prose.
        current_rank_data: dict[str, int] = {
            row["nba_team_code"]: cur_rank for cur_rank, row in enumerate(streak_rows, start=1)
        }

        article = await asyncio.wait_for(
            columnist_service.generate(
                pool, league_id, season,
                persona_id="power_list",
                category="power_rankings",
                context=context,
                structured_data=current_rank_data,
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

# Recency-weighting knobs for rookie_watch (Finding #6) — the most recent
# _RECENCY_WINDOW games count _RECENCY_WEIGHT times as much as the rest of the
# season-to-date sample, so an October hot streak doesn't dominate a stat line
# the column frames as "current form" once the rookie has cooled off.
_RECENCY_WINDOW = 15
_RECENCY_WEIGHT = 2.0


def _recency_weighted_avg(games: list[dict], stat_key: str) -> float:
    """Weighted average of stat_key across games (assumed sorted most-recent-first),
    with the first _RECENCY_WINDOW entries weighted _RECENCY_WEIGHT times as heavily."""
    if not games:
        return 0.0
    total_weight = 0.0
    total_value = 0.0
    for i, g in enumerate(games):
        w = _RECENCY_WEIGHT if i < _RECENCY_WINDOW else 1.0
        total_weight += w
        total_value += (g.get(stat_key) or 0) * w
    return total_value / total_weight if total_weight else 0.0


def _build_rookie_watch_stats(box_rows: list[dict]) -> list[dict]:
    """Group per-game rookie box scores by player and compute recency-weighted
    PPG/RPG/APG (Finding #6) instead of a flat season-to-date average.

    Previously the SQL query pulled the whole season with no recency window,
    so a rookie who was excellent in October and has since cooled off still
    showed a stat line dominated by early performance, which the column then
    framed as current form. box_rows is one row per (rookie, game) this
    season, each with player_id/name/team/points/rebounds/assists/game_index;
    game_index orders games chronologically within a season so "most recent"
    is well-defined even mid-season.
    """
    by_player: dict[int, dict] = {}
    for row in box_rows:
        pid = row["player_id"]
        entry = by_player.setdefault(pid, {"name": row["name"], "team": row["team"], "games": []})
        entry["games"].append(row)

    out: list[dict] = []
    for pid, entry in by_player.items():
        games = sorted(entry["games"], key=lambda g: g.get("game_index") or 0, reverse=True)
        out.append({
            "id": pid,
            "name": entry["name"],
            "team": entry["team"],
            "ppg": round(_recency_weighted_avg(games, "points"), 1),
            "rpg": round(_recency_weighted_avg(games, "rebounds"), 1),
            "apg": round(_recency_weighted_avg(games, "assists"), 1),
            "gp": len(games),
        })
    out.sort(key=lambda r: r["ppg"], reverse=True)
    return out[:10]


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

        # Fetch one row per (rookie, game) this season — game_index orders them
        # chronologically so _build_rookie_watch_stats can recency-weight the
        # averages instead of flattening the whole season into one number.
        rookie_box_rows = await pool.fetch(
            """
            SELECT p.id AS player_id, p.first_name || ' ' || p.last_name AS name,
                   t.nba_team_code AS team,
                   b.points AS points,
                   b.rebounds_off + b.rebounds_def AS rebounds,
                   b.assists AS assists,
                   g.game_index AS game_index
            FROM players p
            JOIN teams t ON t.id = p.team_id
            JOIN game_box_scores b ON b.player_id = p.id
            JOIN games g ON g.id = b.game_id AND g.season = $2
            WHERE p.league_id = $1 AND p.is_rookie = true
            """,
            league_id, season,
        )
        rookies = _build_rookie_watch_stats([dict(r) for r in rookie_box_rows])
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


# Tracks games processed so Marcus Brooks fires every ~200 games (every 20 batches of 10).
# Keyed by league_id so multi-league bots don't bleed counters across leagues.
_marcus_game_counter: dict[int, int] = {}

# Tracks games processed so Darius Cole fires every ~50 games.
_darius_game_counter: dict[int, int] = {}

# Tracks the game index of the last columnist article so the 50-game fallback works.
# Keyed by league_id.
_last_columnist_game_index: dict[int, int] = {}

# Columnist rotation -- cycles through these personas on every batch (subject to reactive gate).
_COLUMNIST_ROTATION = ["jordan_rivera", "keisha_williams", "hot_take_hour", "pat_chen", "darius_cole", "carla_knox"]
# Keyed by league_id so concurrent leagues each have their own rotation position.
_columnist_rotation_index: dict[int, int] = {}

# Hot Take Hour season-long running narratives.  Seeded on first HTH article of the
# season and then injected into every subsequent HTH context so Dave and Tony keep
# their multi-episode storylines alive.  Keyed by league_id so multi-league bots work.
_HTH_NARRATIVES: dict[int, dict] = {}


async def _fetch_hth_seed_data(pool, league_id: int, season: int) -> tuple[list[dict], str | None]:
    """Fetch the current top-20-scorers pool (>=10 games) and current #1-by-wins
    team code -- the raw data both initial HTH narrative seeding and periodic
    re-validation are built from (Finding #3)."""
    seed_rows = await pool.fetch(
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
    std_rows = await pool.fetch(
        """
        SELECT sc.team_id, t.nba_team_code, sc.wins, sc.losses
        FROM standings_cache sc JOIN teams t ON t.id = sc.team_id
        WHERE sc.league_id = $1 AND sc.season = $2
        ORDER BY sc.wins DESC LIMIT 1
        """,
        league_id, season,
    )
    top_team = std_rows[0]["nba_team_code"] if std_rows else None
    return [dict(r) for r in seed_rows], top_team


def _build_hth_sleeper_slot(seed_rows: list[dict]) -> dict | None:
    """sleeper_pick: the current top scorer, framed as Dave's underrated pick."""
    if not seed_rows:
        return None
    top = seed_rows[0]
    return {
        "player_id": top["id"],
        "text": (
            f"Dave has been insisting all season that {top['player_name']} is criminally underrated "
            f"and deserves more recognition."
        ),
    }


def _build_hth_fraud_slot(top_team: str | None) -> dict | None:
    """fraud_call: the current #1-by-wins team, framed as Tony's 'paper tiger' skepticism."""
    if not top_team:
        return None
    return {
        "team_code": top_team,
        "text": (
            f"Tony has been calling {top_team} a 'paper tiger' since day one — "
            f"great record, no real test, waiting to collapse."
        ),
    }


def _build_hth_rivalry_slot(seed_rows: list[dict]) -> dict | None:
    """rivalry: the #2/#3 scorers, framed as a season-long Dave/Tony debate."""
    if len(seed_rows) <= 2:
        return None
    a, b = seed_rows[1], seed_rows[2]
    return {
        "player_a_id": a["id"],
        "player_b_id": b["id"],
        "text": (
            f"Dave and Tony have been tracking the {a['team_code']} vs {b['team_code']} rivalry — "
            f"{a['player_name']} vs {b['player_name']} — all season, arguing who's the better player."
        ),
    }


async def _seed_or_revalidate_hth_narratives(pool, league_id: int, season: int) -> None:
    """Seed HTH's season-long narratives on first use, then re-validate each
    frozen premise against CURRENT data on every subsequent fire (Finding #3).

    Previously sleeper_pick/fraud_call/rivalry were seeded ONCE from early
    (>=10-game) data and re-injected verbatim for the rest of the season with
    no check for whether the premise was still true — a 'paper tiger' team
    that keeps winning without a real challenger, or a 'sleeper' who gets
    traded away, would keep getting the same stale framing forever. This runs
    the same lightweight seed query every time HTH fires and swaps out (or
    retires) any slot whose subject is no longer supported by current data:
    sleeper_pick/rivalry regenerate when a named player falls out of the
    current top-20-scorers qualifying pool (traded, injured, cooled off past
    the sample threshold); fraud_call regenerates when a different team now
    holds the #1-by-wins spot.
    """
    try:
        seed_rows, top_team = await _fetch_hth_seed_data(pool, league_id, season)
    except Exception as exc:
        log.warning(f"HTH narrative seed/revalidate fetch failed: {exc}")
        _HTH_NARRATIVES.setdefault(league_id, {})
        return

    current = _HTH_NARRATIVES.get(league_id)
    if current is None:
        # First use for this league — seed from scratch.
        _HTH_NARRATIVES[league_id] = {
            "sleeper_pick": _build_hth_sleeper_slot(seed_rows),
            "fraud_call": _build_hth_fraud_slot(top_team),
            "rivalry": _build_hth_rivalry_slot(seed_rows),
        }
        log.info(f"HTH narratives seeded for league {league_id}: {_HTH_NARRATIVES[league_id]}")
        return

    seed_ids = {r["id"] for r in seed_rows}

    sleeper = current.get("sleeper_pick")
    if sleeper and sleeper.get("player_id") not in seed_ids:
        new_slot = _build_hth_sleeper_slot(seed_rows)
        current["sleeper_pick"] = new_slot
        log.info(f"HTH sleeper_pick premise flipped for league {league_id} — regenerated: {new_slot}")

    fraud = current.get("fraud_call")
    if fraud and fraud.get("team_code") != top_team:
        new_slot = _build_hth_fraud_slot(top_team)
        current["fraud_call"] = new_slot
        log.info(f"HTH fraud_call premise flipped for league {league_id} — regenerated: {new_slot}")

    rivalry = current.get("rivalry")
    if rivalry and (rivalry.get("player_a_id") not in seed_ids or rivalry.get("player_b_id") not in seed_ids):
        new_slot = _build_hth_rivalry_slot(seed_rows)
        current["rivalry"] = new_slot
        log.info(f"HTH rivalry premise flipped for league {league_id} — regenerated: {new_slot}")

    _HTH_NARRATIVES[league_id] = current


# Size of the lottery odds pool _compute_lottery_odds normalizes across —
# mirrors the real NBA's ~14-team non-playoff lottery field so odds reflect
# actual record gaps across the whole tanking race, not just the 5 rows shown.
_LOTTERY_POOL_SIZE = 14


def _compute_lottery_odds(pool_rows: list[dict]) -> dict[int, float]:
    """Compute real, record-gap-sensitive draft lottery odds from actual
    standings (Finding #4) instead of a hardcoded rank-position array.

    Weights each team in the lottery pool by inverse win percentage (worse
    record = more ping-pong balls), normalized so the pool sums to 100%.
    Unlike a fixed rank-only spread (14.0/13.4/12.7/12.0/10.5 regardless of
    how bunched or spread the bottom standings actually are), two teams with
    a real multi-game gap now get visibly different odds while two teams a
    half-game apart land close together.
    """
    if not pool_rows:
        return {}
    weights: dict[int, float] = {}
    for row in pool_rows:
        wins = row.get("wins", 0) or 0
        losses = row.get("losses", 0) or 0
        games = wins + losses
        win_pct = (wins / games) if games > 0 else 0.0
        # Floor keeps a still-undefeated-but-untested team (games=0, early
        # season) from producing a zero-weight edge case.
        weights[row["team_id"]] = max(0.02, 1.0 - win_pct)
    total = sum(weights.values()) or 1.0
    return {tid: round(w / total * 100.0, 1) for tid, w in weights.items()}


# Columnist force-mode cadence: minimum game-index gap between articles when
# force=True.  70 games ~= 7 game-days of 10 games each.
_COLUMNIST_FORCE_MIN_GAP: int = 70


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
                "player_id": game_top.get("player_id"),
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
            "player_id": _overall_top_game.get("player_id"),
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

    # Player-level form signals (Finding #2) — grounds "declining/washed/hot"
    # claims about individual performers the same way team streaks are grounded.
    try:
        _performers_seen = [_top_of_batch] if _top_of_batch else []
        for gd in games_data:
            if gd.get("top_performer"):
                _performers_seen.append(gd["top_performer"])
        _form_signals = await _build_player_form_signals(pool, league_id, season, _performers_seen)
        if _form_signals:
            batch_context["player_form_signals"] = _form_signals
    except Exception as _form_exc:
        log.warning(f"_maybe_post_columnist: player form signal enrichment failed: {_form_exc}")

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

        # Seed season-long HTH narratives on first use per league; re-validate
        # each frozen premise against current data on every subsequent fire.
        global _HTH_NARRATIVES
        await _seed_or_revalidate_hth_narratives(pool, league_id, season)

        # Inject non-None narratives into context (text only — the structured
        # identifying fields on each slot are for revalidation, not the prompt).
        _active_narratives = {
            k: v["text"] for k, v in _HTH_NARRATIVES.get(league_id, {}).items() if v
        }
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
                embed_data = EmbedData(
                    title=f"🔥 {article['headline']}",
                    description=body[:2000],
                    color=_rgb_to_int((231, 76, 60)),
                    footer="Dave Collier & Tony Reyes · DBA Sports Debate",
                )
            else:
                persona = _PERSONAS.get(persona_id)
                rgb = _PERSONA_COLORS.get(persona_id, (100, 100, 100))
                embed_data = EmbedData(
                    title=article["headline"],
                    description=article["body"][:2000],
                    color=_rgb_to_int(rgb),
                    footer=f"by {persona.display_name} · {persona.byline}" if persona else None,
                )
            _sent = await _BoundChannelAnnouncer(analysis_channel).post_embed_get_ref(
                "analysis", embed_data
            )
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
                # Build bottom-5 context for Darius Cole from the full lottery pool
                # (mirrors the real NBA's ~14-team non-playoff field) so lottery_odds_pct
                # is computed from actual record gaps, not just rank position.
                _all_standings = await pool.fetch(
                    """
                    SELECT sc.team_id, t.nba_team_code, sc.wins, sc.losses
                    FROM standings_cache sc
                    JOIN teams t ON t.id = sc.team_id
                    WHERE sc.league_id = $1 AND sc.season = $2
                    ORDER BY sc.wins ASC, sc.losses DESC
                    LIMIT $3
                    """,
                    league_id, season, _LOTTERY_POOL_SIZE,
                )
                _lottery_odds = _compute_lottery_odds(_all_standings)
                _bottom5 = []
                _bottom5_team_ids: list[int] = []
                for _row in _all_standings[:5]:
                    _bottom5.append({
                        "team": _row["nba_team_code"],
                        "record": f"{_row['wins']}-{_row['losses']}",
                        "lottery_odds_pct": _lottery_odds.get(_row["team_id"], 0.0),
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
                dc_embed_data = EmbedData(
                    title=f"📋 {dc_article['headline']}",
                    description=dc_article["body"][:2000],
                    color=_rgb_to_int((34, 139, 34)),
                    footer=f"by {dc_persona.display_name} · {dc_persona.byline}",
                )
                try:
                    _sent = await _BoundChannelAnnouncer(analysis_channel).post_embed_get_ref(
                        "analysis", dc_embed_data
                    )
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
            embed_data = EmbedData(
                title=mb_article["headline"],
                description=mb_article["body"][:2000],
                color=_rgb_to_int((0, 128, 255)),
                footer=f"by {mb_persona.display_name} · {mb_persona.byline}" if mb_persona else None,
            )
            _sent = await _BoundChannelAnnouncer(analysis_channel).post_embed_get_ref(
                "analysis", embed_data
            )
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


_PLAYOFF_COLUMNIST_ROTATION = ["jordan_rivera", "keisha_williams", "carla_knox"]
_playoff_rotation_index: dict[int, int] = {}


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
    embed_data = EmbedData(
        title=article["headline"],
        description=article["body"][:2000],
        color=_rgb_to_int(rgb),
        footer=f"by {persona.display_name} · {persona.byline}",
    )
    try:
        _sent = await _BoundChannelAnnouncer(analysis_channel).post_embed_get_ref(
            "analysis", embed_data
        )
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
