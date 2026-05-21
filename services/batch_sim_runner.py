from __future__ import annotations

import asyncio
import datetime
import os
import random
import re
import time as _time
from collections import Counter, defaultdict
from typing import List, Optional

import discord

from bot.embeds import awards_embeds, sim_embeds
from core.logging import get_logger
from data.db import get_pool
from data.repositories import game_repo, gameplan_repo, league_repo, player_repo, strategy_repo, team_repo, trade_repo
from phase.states import Phase
from services import awards_service, columnist_service, cpu_coach_service, cpu_trade_service, franchise_plan_service, league_service, notifier_service, potm_service, records_service, sim_engine, strategy_service, team_intel
from services.personas import PERSONAS as _PERSONAS
from services.player_style_service import context_summary as _player_style_context
from services.role_service import ROLE_REGISTRY, get_or_derive_roles

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

# Tracks games processed so Quinn Park (coach_beat) fires every ~50 games (offset from Marcus).
# Keyed by league_id so multi-league bots don't bleed counters across leagues.
_coach_beat_game_counter: dict[int, int] = {}

# Tracks the game index of the last columnist article so the 50-game fallback works.
# Keyed by league_id.
_last_columnist_game_index: dict[int, int] = {}

# Columnist rotation — cycles through these personas on every batch (subject to reactive gate).
_COLUMNIST_ROTATION = ["maya_chen", "jordan_rivera", "keisha_williams", "hot_take_hour", "pat_chen", "darius_cole"]
# Keyed by league_id so concurrent leagues each have their own rotation position.
_columnist_rotation_index: dict[int, int] = {}

# Hot Take Hour season-long running narratives.  Seeded on first HTH article of the
# season and then injected into every subsequent HTH context so Dave and Tony keep
# their multi-episode storylines alive.  Keyed by league_id so multi-league bots work.
_HTH_NARRATIVES: dict[int, dict] = {}

# Playoff columnist rotation — cycles through recap-capable personas for post-game coverage.
_PLAYOFF_COLUMNIST_ROTATION = ["maya_chen", "jordan_rivera", "keisha_williams"]
# Keyed by league_id.
_playoff_rotation_index: dict[int, int] = {}

# POTM month-gate: keyed by league_id, stores the last "YYYY-MM" for which
# _maybe_post_potm was allowed to call through to potm_service.  Batches within
# the same simulated calendar month are skipped without touching the DB.
_potm_last_checked_month: dict[int, str] = {}

# Columnist force-mode cadence: minimum game-index gap between articles when
# force=True.  70 games ≈ 7 game-days of 10 games each.
_COLUMNIST_FORCE_MIN_GAP: int = 70

# ---------------------------------------------------------------------------
# Role cache — Phase 2: touch-share flows from player_roles, not usage_weight.
# Keyed by (league_id, team_id, season).  60-second TTL matches compute_form_map.
# ---------------------------------------------------------------------------

_ROLE_CACHE: dict[tuple[int, int, int], list[dict]] = {}
_ROLE_CACHE_TS: dict[tuple[int, int, int], float] = {}
_ROLE_CACHE_TTL: float = 60.0


async def _get_team_roles_cached(pool, league_id: int, team_id: int, season: int) -> list[dict]:
    """Fetch role assignments for a team, caching for 60 s to reduce DB load during batch sim."""
    key = (league_id, team_id, season)
    if key in _ROLE_CACHE and _time.monotonic() - _ROLE_CACHE_TS[key] < _ROLE_CACHE_TTL:
        return _ROLE_CACHE[key]
    rows = await get_or_derive_roles(pool, league_id, team_id, season)
    _ROLE_CACHE[key] = rows
    _ROLE_CACHE_TS[key] = _time.monotonic()
    return rows


def invalidate_role_cache(league_id: int, team_id: int | None = None, season: int | None = None) -> None:
    """Evict stale entries after roster changes (trades, injuries).  Called by Phase 3 hooks."""
    keys_to_drop = [
        k for k in _ROLE_CACHE
        if k[0] == league_id
        and (team_id is None or k[1] == team_id)
        and (season is None or k[2] == season)
    ]
    for k in keys_to_drop:
        _ROLE_CACHE.pop(k, None)
        _ROLE_CACHE_TS.pop(k, None)


async def _stamp_role_data(
    pool,
    league_id: int,
    team_id: int,
    season: int,
    players: list[dict],
    offensive_scheme: str,
) -> None:
    """Stamp _role_* fields onto each player dict so sim_engine can use them.

    Fields set on each player:
        _role              — role name string (e.g. "post_anchor")
        _role_touch_share  — base touch share from ROLE_REGISTRY (pre-scheme-synergy)
        _role_fga_3pa_pct  — role's 3PA fraction
        _role_fta_per_fga  — role's FTA per FGA ratio
        _role_def_role     — defensive_role string ("anchor"/"perimeter"/"general"/"passive")
        _role_minutes_tier — "starter"/"rotation"/"bench"/"depth"
        _role_tendencies   — list of tendency column names this role amplifies
    """
    assignments = await _get_team_roles_cached(pool, league_id, team_id, season)
    role_by_pid: dict[int, dict] = {a["player_id"]: a for a in assignments}

    # Pass 1: resolve role/registry for every player and apply scheme_synergy bump
    # BEFORE renormalising so the documented +15% relative gain survives.  If we
    # applied synergy after normalisation (old behaviour) the re-normalise step in
    # sim_engine would absorb ~1.4% of the bump, yielding only ~13.6% relative.
    stamped: list[tuple] = []  # (player_dict, role, touch_share, reg)
    for p in players:
        pid = p.get("id") or p.get("player_id")
        assignment = role_by_pid.get(pid)
        if assignment:
            role = assignment["role"]
            touch_share = float(assignment["touch_share"])  # Postgres returns Decimal
        else:
            # Fallback: player not yet in player_roles (shouldn't happen post-Phase-1)
            role = "glue_guy"
            touch_share = 0.08

        reg = ROLE_REGISTRY.get(role, ROLE_REGISTRY["glue_guy"])

        # Apply scheme_synergy modifier (+15%) before renormalising below.
        if offensive_scheme in reg.get("scheme_synergy", []):
            touch_share *= 1.15

        stamped.append((p, role, touch_share, reg))

    # Pass 2: renormalise so the team's touch shares still sum to 1.0.
    # This makes the synergy bump a true +15% relative shift (synergy player gets
    # a larger slice; everyone else proportionally less), matching the docstring.
    total_ts = sum(ts for _, _, ts, _ in stamped) or 1.0
    for p, role, touch_share, reg in stamped:
        p["_role"] = role
        p["_role_touch_share"] = round(touch_share / total_ts, 4)
        p["_role_fga_3pa_pct"] = reg["fga_3pa_pct"]
        p["_role_fta_per_fga"] = reg["fta_per_fga"]
        p["_role_def_role"] = reg["defensive_role"]
        p["_role_minutes_tier"] = reg["minutes_tier"]
        p["_role_tendencies"] = reg.get("tendencies_boosted", [])


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

    if _HEADLESS:
        try:
            _team_row = await pool.fetchrow(
                "SELECT nba_team_code FROM teams WHERE id = $1", team_id
            )
            _tc = _team_row["nba_team_code"] if _team_row else str(team_id)
            starters = [p for i, p in enumerate(players[:15]) if i < 5]
            bench = [p for i, p in enumerate(players[:15]) if i >= 5]
            _s_lines = [
                f"    S{i+1}: {p.full_name} OVR {p.overall} ({p.position})"
                for i, p in enumerate(starters)
            ]
            _b_lines = [
                f"    B{i+1}: {p.full_name} OVR {p.overall} ({p.position})"
                for i, p in enumerate(bench)
            ]
            print(
                f"CPU [{_tc}] — lineup auto-populated (top OVR order)\n"
                + "\n".join(_s_lines)
                + ("\n" + "\n".join(_b_lines) if _b_lines else "")
            )
        except Exception:
            pass  # never let logging break the sim


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


def _apply_cpu_directives(players: list[dict], directives: dict[int, dict]) -> None:
    for p in players:
        pid = p.get("id")
        if pid is None or pid not in directives:
            continue
        d = directives[pid]
        p["shot_diet"] = d.get("shot_diet", "auto")
        p["usage_mode"] = d.get("usage_mode", "normal")
        p["defense_mode"] = d.get("defense_mode", "standard")
        p["role_mode"] = d.get("role_mode", "spot_up")
        p["clutch_mode"] = d.get("clutch_mode", "normal")


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


def _team_to_sim_dict(team: team_repo.Team, top8_avg_ovr: int = 75) -> dict:
    return {
        "team_id": team.id,
        "overall": top8_avg_ovr,
        "offense_rating": team.team_offense_rating or top8_avg_ovr,
        "defense_rating": team.team_defense_rating or top8_avg_ovr,
        "pace": team.pace or 100.0,
    }


async def _compute_team_ovr(pool, league_id: int, team_id: int) -> int:
    """Average OVR of the top-8 lineup slots (starters + primary bench).

    Returns 75 as a safe fallback when the team has no lineup rows or no
    players with a populated overall rating.
    """
    result = await pool.fetchval(
        """
        SELECT ROUND(AVG(p.overall))::INT
        FROM (
            SELECT p.overall
            FROM lineups l
            JOIN players p ON p.id = l.player_id
            WHERE l.league_id = $1 AND l.team_id = $2
            ORDER BY l.slot ASC
            LIMIT 8
        ) p
        """,
        league_id,
        team_id,
    )
    return int(result) if result is not None else 75


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

    rng = random.Random(game_id)

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
    records_channel: Optional[discord.TextChannel] = None,
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

    # Season-scope records are still written to DB for /team records queries, but we
    # suppress their announcements entirely — they triggered on ordinary numbers every
    # game and were the primary spam source. Only all-time records post to #records.
    _record_announcements, at_announcements = await records_service.check_and_update_records(
        pool, game["league_id"], season, game_id, result
    )
    for at_announcement in at_announcements:
        _rec_ch = records_channel or news_channel
        if _rec_ch:
            await _rec_ch.send(embed=sim_embeds.season_record_embed(at_announcement))

    return standings_update


async def _sim_single_game(
    pool,
    game: dict,
    league_id: int,
    season: int,
    news_channel: Optional[discord.TextChannel],
    injury_channel: Optional[discord.TextChannel] = None,
    records_channel: Optional[discord.TextChannel] = None,
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

    await _persist_game_result(pool, game, result, home_team, away_team, season, news_channel, injury_channel, records_channel)
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


async def _ensure_records_channel(guild: discord.Guild, pool, league_id: int) -> Optional[discord.TextChannel]:
    """Return the #records channel, creating it lazily if it doesn't exist yet.

    Existing leagues created before the records channel was added to CHANNEL_ROLES
    won't have a row in league_channels.  On first use we create the Discord channel
    under the same category as #league-news and store its ID — subsequent calls are
    a cheap DB lookup.  Falls back to #league-news if creation fails so records are
    never silently dropped.
    """
    channel_id = await league_repo.get_channel(pool, league_id, "records")
    if channel_id:
        ch = guild.get_channel(channel_id)
        if ch:
            return ch

    # Lazy-create: find the DBA category by looking at where #league-news lives.
    news_id = await league_repo.get_channel(pool, league_id, "league-news")
    category: Optional[discord.CategoryChannel] = None
    if news_id:
        news_ch = guild.get_channel(news_id)
        if news_ch and news_ch.category:
            category = news_ch.category

    try:
        new_ch = await guild.create_text_channel("records", category=category)
        await league_repo.add_channel(pool, league_id, "records", new_ch.id)
        log.info(
            "batch_sim_runner: lazily created #records channel (id=%d) for league %d",
            new_ch.id, league_id,
        )
        return new_ch
    except Exception as exc:
        log.warning(
            "batch_sim_runner: could not create #records channel for league %d: %s — falling back to #league-news",
            league_id, exc,
        )
        # Fall back to league-news so the post isn't silently lost.
        return await _get_news_channel(guild, pool, league_id)


async def _maybe_run_cpu_trades(
    pool,
    league_id: int,
    season: int,
    current_game_index: int,
    total_regular_games: int,
    deadline_game_index: Optional[int],
    guild: discord.Guild,
    refresh_block: bool = True,
) -> None:
    """Wrapper that swallows exceptions so a trade-round failure doesn't abort the sim.

    refresh_block=False skips the CPU trade-block refresh (cpu_block_service.refresh_league)
    for mid-batch calls.  Pass True only on the final flush of each sim function so the
    block is rebuilt once per sim invocation rather than once per game-day.
    """
    try:
        await _run_cpu_trades_inner(
            pool, league_id, season, current_game_index,
            total_regular_games, deadline_game_index, guild,
            refresh_block=refresh_block,
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
    refresh_block: bool = True,
) -> None:
    if not deadline_game_index:
        return

    snapshot_ts = datetime.datetime.now(datetime.timezone.utc)

    trades_proposed = await cpu_trade_service.maybe_initiate_round(
        pool, league_id, season,
        current_game_index, total_regular_games, deadline_game_index,
        guild,
        refresh_block=refresh_block,
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

        # Load pick metadata so embeds show "2026 LAL 1st Round Pick" instead of "Pick #ID".
        _pick_ids = [a.pick_id for a in assets if a.asset_type == "pick" and a.pick_id]
        if _pick_ids:
            _pick_rows = await pool.fetch(
                """SELECT dp.id, dp.season, dp.round, t.nba_team_code AS original_team
                   FROM draft_picks dp
                   JOIN teams t ON t.id = dp.original_team_id
                   WHERE dp.id = ANY($1)""",
                _pick_ids,
            )
            _pick_info: dict[int, dict] = {r["id"]: r for r in _pick_rows}
        else:
            _pick_info = {}

        def _format_pick(pick_id: int) -> str:
            r = _pick_info.get(pick_id)
            if r:
                round_label = "1st Round" if r["round"] == 1 else "2nd Round"
                return f"{r['season']} {r['original_team']} {round_label} Pick"
            return f"Pick #{pick_id}"

        def _asset_lines(from_team_id: int) -> list[str]:
            lines = []
            for a in assets:
                if a.from_team_id != from_team_id:
                    continue
                if a.asset_type == "player" and a.player_id:
                    lines.append(_player_names.get(a.player_id) or f"Player #{a.player_id}")
                elif a.asset_type == "pick" and a.pick_id:
                    lines.append(_format_pick(a.pick_id))
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
            # Build roster-fit context: for each traded player, look up who they'll
            # play alongside on their new team and what that team's build mode is.
            roster_fits: list[str] = []
            try:
                all_teams = await team_repo.get_all(pool, league_id)
                _team_by_id = {t.id: t for t in all_teams}
                for _asset in assets:
                    if _asset.asset_type != "player" or not _asset.player_id:
                        continue
                    p_name = _player_names.get(_asset.player_id, f"Player #{_asset.player_id}")
                    new_team_code = team_codes.get(_asset.to_team_id) or (
                        _team_by_id[_asset.to_team_id].nba_team_code
                        if _asset.to_team_id in _team_by_id else "???"
                    )
                    teammates = await pool.fetch(
                        """SELECT first_name || ' ' || last_name AS name, overall, position
                           FROM players
                           WHERE league_id = $1 AND team_id = $2 AND id != $3
                           ORDER BY overall DESC LIMIT 3""",
                        league_id, _asset.to_team_id, _asset.player_id,
                    )
                    teammate_str = ", ".join(
                        f"{r['name']} ({r['position']}, {r['overall']} OVR)" for r in teammates
                    ) or "no teammates found"
                    new_team_obj = _team_by_id.get(_asset.to_team_id)
                    team_mode = (getattr(new_team_obj, "cpu_mode", None) or "default") if new_team_obj else "default"
                    roster_fits.append(
                        f"{p_name} → {new_team_code} (top teammates: {teammate_str}; team mode: {team_mode})"
                    )
            except Exception as _rf_exc:
                log.warning(f"Marcus Cole roster-fit enrichment failed: {_rf_exc}")

            # Compute context signals for each player arriving at their new team.
            # These are the same signals that drove the CPU's accept/reject math;
            # Marcus Cole's voice_notes instruct him to lean on them in analysis.
            # Signals are computed fresh here (not persisted — Phase 5 adds that).
            context_signals_per_player: dict[int, list[dict]] = {}
            try:
                from services.trade_context import compute_context_modifier
                from services import team_intel as _ti
                _league_row = await pool.fetchrow(
                    "SELECT * FROM leagues WHERE id = $1", league_id
                )
                if _league_row:
                    from data.repositories import league_repo as _lr2
                    _league_obj = _lr2._league_from_record(_league_row)
                    # Fetch plan + posture for each receiving team in one bulk call.
                    _receiving_team_ids = list({
                        a.to_team_id for a in assets
                        if a.asset_type == "player" and a.player_id
                    })
                    if _receiving_team_ids:
                        _ti_data = await _ti.build_team_intel(
                            pool, _league_obj, season,
                            _receiving_team_ids,
                            include=("posture", "plan", "philosophy"),
                        )
                    else:
                        _ti_data = {}

                    for _asset in assets:
                        if _asset.asset_type != "player" or not _asset.player_id:
                            continue
                        _pid = _asset.player_id
                        _recv_tid = _asset.to_team_id
                        _intel = _ti_data.get(_recv_tid, {})
                        _plan = _intel.get("plan") or {}
                        _posture = _intel.get("posture") or {}
                        _phil = _intel.get("philosophy")
                        # Fetch minimal player dict for the detector.
                        _p_row = await pool.fetchrow(
                            """SELECT id, overall, position,
                                      scoring_tendency, playmaking_tendency,
                                      defense_tendency, rebounding_tendency
                               FROM players WHERE id = $1""",
                            _pid,
                        )
                        if not _p_row:
                            continue
                        _player_dict = dict(_p_row)
                        _modifier, _signals = await compute_context_modifier(
                            pool=pool,
                            league_id=league_id,
                            season=season,
                            perspective_team_id=_recv_tid,
                            plan=_plan,
                            posture=_posture,
                            coach_philosophy=_phil,
                            incoming_player=_player_dict,
                            form_mod=1.0,
                        )
                        if _signals:
                            context_signals_per_player[_pid] = [
                                {"code": s.code, "delta": s.delta, "reason": s.reason}
                                for s in _signals
                            ]
            except Exception as _sig_exc:
                log.warning(f"Marcus Cole signal enrichment failed: {_sig_exc}")

            trade_context = {
                "proposer_team": proposer_code,
                "counterparty_team": counterparty_code,
                "proposer_sends": _asset_lines(proposer_id),
                "counterparty_sends": _asset_lines(counterparty_id),
                "trade_status": status,
                "roster_fits": roster_fits,
                "context_signals_per_player": {
                    int(pid): sigs
                    for pid, sigs in context_signals_per_player.items()
                },
            }
            mc_article = await columnist_service.generate(
                pool, league_id, season,
                persona_id="marcus_cole",
                category="trade_report",
                context=trade_context,
                subject_team_ids=[proposer_id, counterparty_id],
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


async def _maybe_post_awards_races(
    pool,
    league_id: int,
    season: int,
    news_channel: Optional[discord.TextChannel],
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


async def _maybe_post_potm(
    pool,
    guild: discord.Guild,
    league_id: int,
    season: int,
    current_game_date: Optional[str],
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

        # Award races fire once per month, right after POTM announcements.
        await _maybe_post_awards_races(
            pool, league_id, season, news_channel,
            current_game_index=current_game_index,
            prefetched_leaders=prefetched_race_leaders,
        )
    except Exception as exc:
        log.warning(f"_maybe_post_potm failed: {exc}", exc_info=True)


_PERSONA_COLORS: dict[str, tuple[int, int, int]] = {
    "maya_chen":      (255, 165, 0),
    "jordan_rivera":  (138, 43, 226),
    "keisha_williams": (0, 128, 255),
    "hot_take_hour":  (255, 0, 0),
    "pat_chen":       (0, 180, 150),
    "darius_cole":    (34, 139, 34),
    "coach_beat":     (160, 82, 45),
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
    _coach_beat_game_counter[league_id] = 0

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

        # Prioritise chaos and vet_overrater; then youth_developer; else any.
        _PRIORITY_PHILOSOPHIES = ("chaos", "vet_overrater", "youth_developer")
        subject_team_id: int | None = None
        for philosophy in _PRIORITY_PHILOSOPHIES:
            for tid, data in intel.items():
                if data.get("philosophy") == philosophy:
                    subject_team_id = tid
                    break
            if subject_team_id is not None:
                break
        if subject_team_id is None and batch_team_ids:
            subject_team_id = batch_team_ids[0]
        if subject_team_id is None:
            return

        subject_intel = intel.get(subject_team_id, {})

        # Pull recent role changes for the subject team.
        recent_role_changes = subject_intel.get("recent_role_changes", [])

        # Fetch team code for context.
        team_row = await pool.fetchrow(
            "SELECT nba_team_code FROM teams WHERE id = $1", subject_team_id
        )
        team_code = team_row["nba_team_code"] if team_row else "???"

        cb_context = {
            "posture":             subject_intel.get("posture"),
            "plan":                subject_intel.get("plan"),
            "philosophy":          subject_intel.get("philosophy", "tendency_respecter"),
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
            timeout=8.0,
        )
        if cb_article:
            embed = discord.Embed(
                title=f"🎤 {cb_article['headline']}",
                description=cb_article["body"][:2000],
                color=discord.Color.from_rgb(160, 82, 45),
            )
            embed.set_footer(text=f"by {cb_persona.display_name} · {cb_persona.byline}")
            await analysis_channel.send(embed=embed)
    except Exception as exc:
        log.warning(f"_maybe_post_coach_beat failed: {exc}", exc_info=True)


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
        try:
            article = await asyncio.wait_for(
                columnist_service.generate(
                    pool, league_id, season,
                    persona_id=persona_id,
                    category="game_recap",
                    context=columnist_context,
                    subject_team_ids=subject_team_ids,
                ),
                timeout=8.0,
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
            await analysis_channel.send(embed=embed)
    else:
        log.debug(
            f"_maybe_post_columnist: skipping regular-season article (no interesting condition met) "
            f"for batch ending at game {batch_end_index}"
        )

    # Darius Cole — every ~50 games, independently.  Covers bottom-5 teams and lottery odds.
    # Counter was already incremented at the top of this function (before channel guard).
    if _darius_game_counter.get(league_id, 0) >= 50:
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
                dc_article = await asyncio.wait_for(
                    columnist_service.generate(
                        pool, league_id, season,
                        persona_id="darius_cole",
                        category="tank_watch",
                        context=_dc_context,
                        subject_team_ids=_bottom5_team_ids,
                    ),
                    timeout=8.0,
                )
            except Exception as _dc_exc:
                log.warning(
                    f"_maybe_post_columnist: darius_cole timed out or failed: {_dc_exc}",
                    exc_info=True,
                )
            if dc_article:
                dc_embed = discord.Embed(
                    title=f"📋 {dc_article['headline']}",
                    description=dc_article["body"][:2000],
                    color=discord.Color.from_rgb(34, 139, 34),
                )
                dc_embed.set_footer(text=f"by {dc_persona.display_name} · {dc_persona.byline}")
                try:
                    await analysis_channel.send(embed=dc_embed)
                    log.info("Darius Cole article posted to #analysis")
                except Exception as _dc_send_exc:
                    log.warning(f"_maybe_post_columnist: darius_cole send failed: {_dc_send_exc}")
            else:
                log.warning("Darius Cole: generate() returned None — article not posted")

    # Marcus Brooks — every ~200 games (every 20 batches), independently of the rotation.
    # Counter was already incremented at the top of this function (before channel guard).
    if _marcus_game_counter.get(league_id, 0) >= 200:
        _marcus_game_counter[league_id] = 0
        mb_persona = _PERSONAS.get("marcus_brooks")
        try:
            mb_article = await asyncio.wait_for(
                columnist_service.generate(
                    pool, league_id, season,
                    persona_id="marcus_brooks",
                    category="power_rankings",
                    context=batch_context,
                    subject_team_ids=subject_team_ids,
                ),
                timeout=8.0,
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
            await analysis_channel.send(embed=embed)


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
        await analysis_channel.send(embed=embed)
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

        sim_result = await _sim_single_game(pool, game, league_id, season, news_channel, injury_channel, records_channel)
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

        sim_result = await _sim_single_game(pool, game, league_id, season, news_channel, injury_channel, records_channel)
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
    return await _sim_single_game(pool, game, league_id, season, news_channel, injury_channel, records_channel)
