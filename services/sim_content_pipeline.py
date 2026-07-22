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
from collections import defaultdict
from typing import TYPE_CHECKING

from bot.embeds import awards_embeds
from core.logging import get_logger
from data.repositories import league_repo
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
