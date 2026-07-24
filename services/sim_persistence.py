"""Roster/lineup/game-result persistence used by batch sim.

DB-read/write helpers extracted from sim_orchestrator.py. `_persist_injuries`
and `_persist_game_result` also post announcements (injury alerts, win-streak,
all-time-record embeds) -- they build `EmbedData` and hand it to a
`_BoundChannelAnnouncer` (services/sim_channel_announcer.py) rather than
constructing discord.Embed directly, so this module never imports discord.
Both had zero test coverage before this split; see
tests/test_persist_injuries.py and tests/test_persist_game_result.py for the
characterization tests written before the discord.Embed -> EmbedData
conversion (they pin exact embed title/description/field/footer text).
"""
from __future__ import annotations

import datetime
import os
import random
import time as _time
from typing import List

from core.logging import get_logger
from data.repositories import game_repo, player_repo, team_repo
from services import records_service
from services.announcer_protocol import EmbedData, EmbedField
from services.role_service import ROLE_REGISTRY, get_or_derive_roles
from services.sim_content_pipeline import _maybe_post_triage_report
from services.sim_channel_announcer import _BoundChannelAnnouncer

log = get_logger(__name__)

_HEADLESS = os.environ.get("DBA_HEADLESS_MODE") == "1"

_SEVERITY_LABELS: dict[str, str] = {
    "day_to_day":    "day-to-day",
    "week_2_4":      "2-4 weeks",
    "week_4_8":      "4-8 weeks",
    "season_ending": "season-ending",
}

_INJURY_GAMES_MISSED: dict[str, tuple[int, int]] = {
    "day_to_day":    (1, 3),
    "week_2_4":      (7, 14),
    "week_4_8":      (20, 35),
    "season_ending": (999, 999),
}

_ANNOUNCE_SEVERITIES = frozenset({"week_4_8", "season_ending"})

# Matches discord.Color.red().value / discord.Color.gold().value -- hardcoded
# so this module never needs `import discord`.
_COLOR_RED = 0xE74C3C
_COLOR_GOLD = 0xF1C40F

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
    injury_channel,
    guild=None,
) -> None:
    raw_injuries = result.get("injuries", [])
    if not raw_injuries:
        return

    rng = random.Random(game_id)
    announcer = _BoundChannelAnnouncer(injury_channel)

    game_date: datetime.date = game.get("scheduled_date") or datetime.date.today()
    rows: list[dict] = []
    # Dedupe within this game: a player can only appear once per injury pass.
    # Without this, the same player_id can fire two announcements when the
    # sim engine emits duplicate injury entries for the same player.
    _announced_player_ids: set[int] = set()
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
            # Skip duplicate announcement for same player in this game pass.
            _pid = inj["player_id"]
            if _pid in _announced_player_ids:
                log.debug(
                    "_persist_injuries: skipping duplicate injury announcement for player_id=%d in game_id=%d",
                    _pid, game_id,
                )
                continue
            _announced_player_ids.add(_pid)

            player_row = await pool.fetchrow(
                "SELECT first_name, last_name, team_id, overall FROM players WHERE id = $1",
                inj["player_id"],
            )
            if player_row:
                player_name = f"{player_row['first_name']} {player_row['last_name']}"
                player_overall = player_row["overall"] or 0
                team_code_row = await pool.fetchrow(
                    "SELECT nba_team_code FROM teams WHERE id = $1", player_row["team_id"]
                )
                team_code = team_code_row["nba_team_code"] if team_code_row else "???"
            else:
                player_name = f"Player #{inj['player_id']}"
                player_overall = 0
                team_code = "???"

            human_severity = _SEVERITY_LABELS.get(severity, severity)
            gms_label = "Season" if games_missed >= 82 else str(games_missed)
            embed_data = EmbedData(
                title="\U0001F3E5 Injury Report",
                description=f"**{player_name}** ({team_code}) — {human_severity}",
                color=_COLOR_RED,
                fields=[
                    EmbedField(name="Games Missed", value=gms_label, inline=True),
                    EmbedField(name="Status", value=human_severity, inline=True),
                ],
                footer=f"Game #{game.get('game_index', game_id)}",
            )
            await announcer.post_embed("injuries", embed_data)

            # Ping the team manager if this is a managed team.
            _mgr_row = await pool.fetchrow(
                "SELECT manager_user_id FROM teams WHERE id = $1", inj["team_id"]
            )
            if _mgr_row and _mgr_row["manager_user_id"]:
                await announcer.post_text(
                    "injuries", f"<@{_mgr_row['manager_user_id']}> — **{player_name}** just went down."
                )

            # Fire triage_report columnist article only for star-level injuries
            # (overall >= 84) so role-player injuries don't flood #analysis.
            _TRIAGE_OVR_THRESHOLD = 84
            if guild is not None and player_overall >= _TRIAGE_OVR_THRESHOLD:
                try:
                    await _maybe_post_triage_report(
                        pool,
                        league_id=game["league_id"],
                        season=season,
                        guild=guild,
                        injury_info={
                            "player_name": player_name,
                            "team_code": team_code,
                            "severity": severity,
                            "human_severity": human_severity,
                            "games_missed": games_missed,
                            "season": season,
                        },
                    )
                except Exception as _triage_exc:
                    log.warning(f"_persist_injuries: triage_report failed: {_triage_exc}")
            elif guild is not None:
                log.debug(
                    "_persist_injuries: skipping triage_report for %s (OVR %d < %d threshold)",
                    player_name, player_overall, _TRIAGE_OVR_THRESHOLD,
                )

    await game_repo.insert_injuries(pool, rows)


async def _persist_game_result(
    pool,
    game: dict,
    result: dict,
    home_team: team_repo.Team,
    away_team: team_repo.Team,
    season: int,
    news_channel,
    injury_channel=None,
    records_channel=None,
    guild=None,
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
    if notable_streak and (records_channel or news_channel):
        streak_team_id, streak_len = notable_streak
        streak_team = await team_repo.get_by_id(pool, streak_team_id)
        streak_name = streak_team.full_name if streak_team else f"Team {streak_team_id}"
        embed_data = EmbedData(
            title="\U0001F525 Win Streak",
            description=f"**{streak_name}** has won **{streak_len}** straight!",
            color=_COLOR_GOLD,
        )
        # Win streaks are a records milestone — post to #records, not #league-news.
        _streak_ch = records_channel or news_channel
        await _BoundChannelAnnouncer(_streak_ch).post_embed("records", embed_data)

    await _persist_injuries(pool, game, game_id, season, result, injury_channel or news_channel, guild=guild)

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
            # sim_embeds.season_record_embed returns an already-built discord.Embed
            # (an existing bot/embeds/ builder) -- sent directly rather than through
            # the announcer, consistent with how other bot/embeds/ builders are used.
            from bot.embeds import sim_embeds
            await _rec_ch.send(embed=sim_embeds.season_record_embed(at_announcement))

    return standings_update
