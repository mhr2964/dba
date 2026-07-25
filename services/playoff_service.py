from __future__ import annotations

import datetime
import random
from typing import Dict, List, Optional, Tuple

import discord

from core.errors import DBAError
from core.logging import get_logger
from data.db import get_pool
from data.repositories import game_repo, league_repo, series_repo, team_repo
from services import records_service, sim_engine, sim_orchestrator
from services.sim_persistence import _persist_injuries
from bot.embeds import sim_embeds

log = get_logger(__name__)


def _extract_top_performer(
    result: dict,
    home_team_code: str,
    away_team_code: str,
) -> dict | None:
    """Return a top-performer dict (same shape sim_orchestrator uses) from a single game result."""
    best_line: dict | None = None
    best_pts = -1
    best_team_code = ""

    for box_lines, team_code in [
        (result.get("home_box", []), home_team_code),
        (result.get("away_box", []), away_team_code),
    ]:
        for line in box_lines:
            if line.get("points", 0) > best_pts:
                best_pts = line.get("points", 0)
                best_line = line
                best_team_code = team_code

    if not best_line:
        return None

    return {
        "name": best_line.get("player_name") or f"Player #{best_line.get('player_id')}",
        "team": best_team_code,
        "pts": best_line.get("points", 0),
        "reb": best_line.get("rebounds_off", 0) + best_line.get("rebounds_def", 0),
        "ast": best_line.get("assists", 0),
        "stl": best_line.get("steals", 0),
        "blk": best_line.get("blocks", 0),
        "tpm": best_line.get("tpm", 0),
        "tpa": best_line.get("tpa", 0),
        "fgm": best_line.get("fgm", 0),
        "fga": best_line.get("fga", 0),
    }

# Round constants — order defines bracket progression
_PLAYIN_ROUNDS = {"play_in_east", "play_in_west"}

# Ordered progression for each conference bracket after play-in completes
_EAST_BRACKET_ORDER = [
    "r1_east",
    "r2_east",
    "conference_finals_east",
    "nba_finals",
]
_WEST_BRACKET_ORDER = [
    "r1_west",
    "r2_west",
    "conference_finals_west",
    "nba_finals",
]


def _standings_to_seeds(
    standings: List[dict],
    conference: str,
) -> List[dict]:
    """Return standings rows for one conference sorted by seed (wins desc, losses asc)."""
    conf_rows = [r for r in standings if r["conference"] == conference]
    conf_rows.sort(key=lambda r: (-r["wins"], r["losses"]))
    return conf_rows


async def seed_playoffs(league_id: int, season: int) -> dict:
    """
    Called after regular season ends. Reads final standings_cache.
    Seeds top-6 per conference into R1 and puts seeds 7-10 into play-in.
    Returns bracket dict with east_bracket, west_bracket, playin_east, playin_west.
    """
    pool = await get_pool()
    standings = await game_repo.get_standings(pool, league_id, season)

    east_seeds = _standings_to_seeds(standings, "East")
    west_seeds = _standings_to_seeds(standings, "West")

    async def _create_playin(conf_seeds: List[dict], playin_round: str) -> List[series_repo.Series]:
        conference = "East" if "east" in playin_round else "West"
        if len(conf_seeds) < 10:
            raise DBAError(
                f"{conference} conference has only {len(conf_seeds)} seeded teams — "
                f"need at least 10 to run the play-in. Check that all 30 teams were imported."
            )
        # seeds 7 vs 8 (index 6 vs 7), seeds 9 vs 10 (index 8 vs 9)
        s1 = await series_repo.create_series(
            pool, league_id, season, playin_round,
            high_seed_id=conf_seeds[6]["team_id"],
            low_seed_id=conf_seeds[7]["team_id"],
            games_needed=2,
        )
        s2 = await series_repo.create_series(
            pool, league_id, season, playin_round,
            high_seed_id=conf_seeds[8]["team_id"],
            low_seed_id=conf_seeds[9]["team_id"],
            games_needed=2,
        )
        return [s1, s2]

    playin_east = await _create_playin(east_seeds, "play_in_east")
    playin_west = await _create_playin(west_seeds, "play_in_west")

    # R1: seeds 1-6 in bracket; seeds 7+8 are TBD (filled after play-in).
    # Create R1 series with real 1-6 matchups; 7-seed and 8-seed slots use
    # placeholder 0 until play-in resolves. Callers call sim_play_in() first.
    # NOTE: 1v8, 4v5, 3v6, 2v7 is the standard bracket. Since 7 and 8 are
    # unknown we skip creating 1v8 and 2v7 now; sim_play_in creates them.
    # We pre-create 3v6 and 4v5 for both conferences.
    async def _create_r1_known(conf_seeds: List[dict], round_name: str) -> List[series_repo.Series]:
        conference = "East" if "east" in round_name else "West"
        if len(conf_seeds) < 6:
            raise DBAError(
                f"{conference} conference has only {len(conf_seeds)} seeded teams — "
                f"need at least 6 for the playoff bracket. Check that all 30 teams were imported."
            )
        s1 = await series_repo.create_series(
            pool, league_id, season, round_name,
            high_seed_id=conf_seeds[3]["team_id"],   # 4 seed
            low_seed_id=conf_seeds[4]["team_id"],    # 5 seed
        )
        s2 = await series_repo.create_series(
            pool, league_id, season, round_name,
            high_seed_id=conf_seeds[2]["team_id"],   # 3 seed
            low_seed_id=conf_seeds[5]["team_id"],    # 6 seed
        )
        return [s1, s2]

    east_r1 = await _create_r1_known(east_seeds, "r1_east")
    west_r1 = await _create_r1_known(west_seeds, "r1_west")

    return {
        "east_bracket": east_r1,
        "west_bracket": west_r1,
        "playin_east": playin_east,
        "playin_west": playin_west,
    }


async def _load_lineup(pool, league_id: int, team_id: int) -> List[dict]:
    rows = await pool.fetch(
        """
        SELECT p.*, l.is_starter, l.slot
        FROM lineups l
        JOIN players p ON p.id = l.player_id
        WHERE l.league_id = $1 AND l.team_id = $2
        ORDER BY l.slot ASC
        """,
        league_id,
        team_id,
    )
    return [dict(r) for r in rows]


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


async def _get_playoff_sim_date(pool, league_id: int, season: int) -> datetime.date:
    """
    Derive a sim-calendar date for the next playoff game.

    Takes the latest scheduled_date already recorded for this league/season
    (regular or playoff) and adds 2 days.  This keeps playoff game dates
    consistent with the simulated calendar rather than real-world wall-clock dates,
    so player game logs remain readable.
    """
    latest = await pool.fetchval(
        "SELECT MAX(scheduled_date) FROM games WHERE league_id = $1 AND season = $2",
        league_id,
        season,
    )
    if latest is not None:
        base: datetime.date = (
            latest if isinstance(latest, datetime.date)
            else datetime.date.fromisoformat(str(latest))
        )
        return base + datetime.timedelta(days=2)
    # No games exist yet — should not happen in normal playoff flow.
    return datetime.date.today()


async def _run_one_game(
    pool,
    league_id: int,
    season: int,
    home_team: team_repo.Team,
    away_team: team_repo.Team,
    series_id: int,
    season_type: str = "playoff",
    guild: Optional[discord.Guild] = None,
) -> Tuple[int, dict]:
    """
    Inserts a game row, runs sim_engine, persists result. Returns (game_id, result).
    season_type distinguishes play-in from playoff for record-keeping.

    PA1: pre-sim inputs (CPU/human gameplans, directive application, strategy
    modifiers, role-stamping, coach minutes plan, back-to-back fatigue) are
    built via the same shared helper the regular season uses
    (sim_orchestrator._build_pre_sim_inputs) before calling sim_engine.sim_game.
    Before this fix, this function called sim_game with only 5 positional
    args, so no player ever had _role_touch_share stamped and every playoff/
    play-in game silently ran sim_engine's legacy fallback path.

    Persistence deliberately does NOT route through
    sim_persistence._persist_game_result -- that call chain touches
    game_repo.update_standings, which must never see playoff/play-in results
    (they must not perturb regular-season standings_cache). PA4 (injuries)
    and PA5 (all-time records) are wired in directly instead, since both are
    standalone with no standings coupling.
    """
    home_players = await _load_lineup(pool, league_id, home_team.id)
    away_players = await _load_lineup(pool, league_id, away_team.id)

    home_ovr = await _compute_team_ovr(pool, league_id, home_team.id)
    away_ovr = await _compute_team_ovr(pool, league_id, away_team.id)

    rng_seed = random.getrandbits(63)

    # Use a sim-calendar date so player game logs stay coherent with regular-season dates.
    sim_date = await _get_playoff_sim_date(pool, league_id, season)

    game_dict = {
        "league_id": league_id,
        "season": season,
        "season_type": season_type,
        "series_id": series_id,
        "game_index": 0,
        "home_team_id": home_team.id,
        "away_team_id": away_team.id,
        "scheduled_date": sim_date,
        "status": "scheduled",
        "is_user_matchup": False,
        "rng_seed": rng_seed,
    }
    game_id = await game_repo.insert_game(pool, game_dict)
    game_dict["id"] = game_id

    pre_sim = await sim_orchestrator._build_pre_sim_inputs(
        pool, league_id, season, game_dict, home_team, away_team, home_players, away_players
    )

    result = sim_engine.sim_game(
        _team_to_sim_dict(home_team, home_ovr),
        _team_to_sim_dict(away_team, away_ovr),
        home_players,
        away_players,
        rng_seed,
        fatigue=pre_sim["fatigue"],
        home_strategy=pre_sim["home_strategy"],
        away_strategy=pre_sim["away_strategy"],
        home_minutes=pre_sim["home_minutes"],
        away_minutes=pre_sim["away_minutes"],
    )

    # Enrich box score lines with player names so columnists can reference them.
    _name_map = {
        p["id"]: f"{p['first_name']} {p['last_name']}"
        for p in home_players + away_players
        if p.get("id") and p.get("first_name")
    }
    for _box_list in (result.get("home_box", []), result.get("away_box", [])):
        for _line in _box_list:
            if "player_name" not in _line:
                _line["player_name"] = _name_map.get(_line.get("player_id"), "")

    await game_repo.mark_simmed(
        pool, game_id,
        result["home_score"],
        result["away_score"],
        result["winner_team_id"],
        rng_seed,
    )

    all_box = result["home_box"] + result["away_box"]
    if all_box:
        await game_repo.insert_box_scores(pool, game_id, all_box)

    def _sum(box: List[dict]) -> dict:
        keys = ["points", "rebounds_off", "rebounds_def", "assists", "steals",
                "blocks", "turnovers", "fouls", "fga", "fgm", "tpa", "tpm", "fta", "ftm"]
        out: dict = {k: sum(ln.get(k, 0) for ln in box) for k in keys}
        out["minutes"] = 240.0
        out["plus_minus"] = result["home_score"] - result["away_score"]
        return out

    if result["home_box"]:
        await game_repo.insert_team_game_stats(pool, game_id, home_team.id, _sum(result["home_box"]))
    if result["away_box"]:
        away_stats = _sum(result["away_box"])
        away_stats["plus_minus"] = result["away_score"] - result["home_score"]
        await game_repo.insert_team_game_stats(pool, game_id, away_team.id, away_stats)

    # PA4: playoff injuries were previously never persisted (result["injuries"]
    # went unread). Resolve the same "injuries" channel (falls back to news)
    # the regular-season path uses, so playoff injuries are announced, not
    # just silently written to the injuries table.
    injury_channel = None
    if guild is not None:
        try:
            from services.sim_channel_announcer import _get_injury_channel
            injury_channel = await _get_injury_channel(guild, pool, league_id)
        except Exception as exc:
            log.warning(f"Failed to resolve injury channel for playoff game {game_id}: {exc}")
    await _persist_injuries(pool, game_dict, game_id, season, result, injury_channel=injury_channel, guild=guild)

    # PA5: playoff performances were previously never checked against all-time
    # records. check_and_update_records needs home_team_id/away_team_id on
    # result to resolve team names -- inject them the same way
    # sim_persistence._persist_game_result does for the regular-season path.
    result["home_team_id"] = home_team.id
    result["away_team_id"] = away_team.id
    try:
        _season_announcements, at_announcements = await records_service.check_and_update_records(
            pool, league_id, season, game_id, result
        )
        for at_announcement in at_announcements:
            log.info(f"Playoff all-time record: {at_announcement}")
    except Exception as exc:
        log.warning(f"records_service.check_and_update_records failed for playoff game {game_id}: {exc}")

    return game_id, result


async def sim_series_game(
    league_id: int,
    series_id: int,
    guild: discord.Guild,
    season: int,
) -> dict:
    """
    Sims the next game in a playoff series. High seed is home for games 1, 2, 5, 7;
    low seed is home for games 3, 4, 6 (standard NBA format).
    Posts recap to #box-scores. Updates series win counts.
    Returns {game_result, series, series_over, winner_team_id}.
    """
    pool = await get_pool()

    series = await series_repo.get_series(pool, series_id)
    if series is None:
        raise ValueError(f"Series {series_id} not found")
    if series.status == "complete":
        raise ValueError(f"Series {series_id} is already complete")

    high_team = await team_repo.get_by_id(pool, series.high_seed_team_id)
    low_team = await team_repo.get_by_id(pool, series.low_seed_team_id)
    if not high_team or not low_team:
        raise ValueError(f"Could not load teams for series {series_id}")

    games_played = series.wins_high + series.wins_low
    game_number = games_played + 1

    # Standard home-court schedule: H H A A H A H (games 1,2,5,7 at high seed)
    high_seed_home_games = {1, 2, 5, 7}
    if game_number in high_seed_home_games:
        home_team, away_team = high_team, low_team
    else:
        home_team, away_team = low_team, high_team

    game_id, result = await _run_one_game(
        pool, league_id, season, home_team, away_team, series_id, "playoff", guild=guild
    )

    updated_series = await series_repo.record_game_result(
        pool, series_id, result["winner_team_id"]
    )

    box_channel_id = await league_repo.get_channel(pool, league_id, "box-scores")
    if box_channel_id:
        channel = guild.get_channel(box_channel_id)
        if channel:
            from bot.ui.box_score_views import PlayoffBoxScoreView
            embed = sim_embeds.game_recap(
                {"game_index": game_number, "scheduled_date": datetime.date.today(), "id": game_id},
                home_team, away_team,
                result["home_score"], result["away_score"],
            )
            view = PlayoffBoxScoreView(
                home_team_name=home_team.full_name,
                home_team_code=home_team.nba_team_code,
                away_team_name=away_team.full_name,
                away_team_code=away_team.nba_team_code,
                home_box=result.get("home_box", []),
                away_box=result.get("away_box", []),
                game_number=game_number,
                recap_embed=embed,
            )
            await channel.send(embed=embed, view=view)

    # Post a playoff recap article — always for clinchers, ~30% for regular games.
    is_clincher = updated_series.status == "complete"
    if is_clincher or random.random() < 0.3:
        try:
            from services import sim_content_pipeline as _scp

            high_team_code = high_team.nba_team_code if hasattr(high_team, "nba_team_code") else str(high_team.id)
            low_team_code = low_team.nba_team_code if hasattr(low_team, "nba_team_code") else str(low_team.id)
            home_team_code = high_team_code if home_team.id == high_team.id else low_team_code
            away_team_code = low_team_code if home_team.id == high_team.id else high_team_code

            winner_code: str | None = None
            if is_clincher and updated_series.winner_team_id:
                winner_code = (
                    high_team_code if updated_series.winner_team_id == high_team.id else low_team_code
                )

            top_performer_dict = _extract_top_performer(result, home_team_code, away_team_code)

            home_score = result.get("home_score", 0)
            away_score = result.get("away_score", 0)
            playoff_context = {
                "round": updated_series.round,
                "series_record": f"{updated_series.wins_high}-{updated_series.wins_low}",
                "is_series_over": is_clincher,
                "high_seed_team": high_team_code,
                "low_seed_team": low_team_code,
                "home_team": home_team_code,
                "away_team": away_team_code,
                "home_score": home_score,
                "away_score": away_score,
                "actual_final_score": f"{away_team_code} {away_score} - {home_team_code} {home_score}",
                "winner": winner_code,
                "result_instruction": (
                    f"IMPORTANT: The actual final score is {away_team_code} {away_score} - "
                    f"{home_team_code} {home_score}. "
                    f"Winner: {winner_code or 'see scores'}. "
                    "Do NOT invent or change the score."
                ),
                "top_performer": top_performer_dict,
                "game_index_range": {"season_pct": 100},
            }
            await _scp._maybe_post_playoff_columnist(pool, league_id, season, playoff_context, guild)
        except Exception as exc:
            log.warning(f"Playoff columnist failed: {exc}")

    return {
        "game_result": result,
        "series": updated_series,
        "series_over": updated_series.status == "complete",
        "winner_team_id": updated_series.winner_team_id,
    }


async def _compute_series_mvp(
    pool,
    league_id: int,
    season: int,
    series_id: int,
    winning_team_id: int,
) -> Optional[dict]:
    """Find the best performer from the winning team across all games in this series."""
    # PA3: scope directly to this series (g.series_id) and playoff/play-in
    # games (g.season_type) -- the previous EXISTS clause only checked that a
    # game's two teams matched the series' two teams, which let regular-season
    # head-to-head games between the same two teams leak into "series" stats.
    rows = await pool.fetch(
        """
        SELECT b.player_id,
               p.first_name || ' ' || p.last_name AS player_name,
               SUM(b.points) + SUM(b.assists)*1.5 + SUM(b.rebounds_off + b.rebounds_def)*1.2 +
               SUM(b.steals)*2 + SUM(b.blocks)*2 AS mvp_score
        FROM game_box_scores b
        JOIN games g ON g.id = b.game_id
        JOIN players p ON p.id = b.player_id
        WHERE g.league_id = $1 AND g.season = $2
          AND b.team_id = $3
          AND g.series_id = $4
          AND g.season_type IN ('playoff', 'play_in')
        GROUP BY b.player_id, p.first_name, p.last_name
        ORDER BY mvp_score DESC
        LIMIT 1
        """,
        league_id, season, winning_team_id, series_id,
    )
    if not rows:
        return None
    return {"player_id": rows[0]["player_id"], "player_name": rows[0]["player_name"]}


async def _get_series_stats_for_player(
    pool,
    league_id: int,
    season: int,
    series_id: int,
    player_id: int,
) -> dict:
    """Compute PPG/RPG/APG and games played for a player across a specific series."""
    # PA3: same series/season_type scoping fix as _compute_series_mvp above.
    row = await pool.fetchrow(
        """
        SELECT
            COUNT(b.id)                                     AS games,
            AVG(b.points)                                   AS ppg,
            AVG(b.rebounds_off + b.rebounds_def)            AS rpg,
            AVG(b.assists)                                  AS apg
        FROM game_box_scores b
        JOIN games g ON g.id = b.game_id
        WHERE g.league_id = $1 AND g.season = $2
          AND b.player_id = $3
          AND g.series_id = $4
          AND g.season_type IN ('playoff', 'play_in')
        """,
        league_id, season, player_id, series_id,
    )
    if not row or not row["games"]:
        return {"games": 0, "ppg": 0.0, "rpg": 0.0, "apg": 0.0}
    return {
        "games": int(row["games"]),
        "ppg": round(float(row["ppg"] or 0), 1),
        "rpg": round(float(row["rpg"] or 0), 1),
        "apg": round(float(row["apg"] or 0), 1),
    }


async def _announce_series_mvp(
    pool,
    league_id: int,
    season: int,
    mvp: dict,
    award_label: str,
    award_type: str,
    guild: Optional[discord.Guild],
    series_id: Optional[int] = None,
) -> None:
    """Post a series MVP announcement to #league-news and persist to player_awards."""
    from data.repositories import player_awards_repo

    try:
        await player_awards_repo.insert_award(
            pool,
            player_id=mvp["player_id"],
            league_id=league_id,
            season=season,
            award_type=award_type,
        )
    except Exception as exc:
        log.warning(f"Failed to insert {award_type} player award: {exc}")

    if not guild:
        return

    news_channel_id = await league_repo.get_channel(pool, league_id, "league-news")
    if not news_channel_id:
        return
    channel = guild.get_channel(news_channel_id)
    if not channel:
        return

    series_stats: dict = {}
    if series_id is not None:
        try:
            series_stats = await _get_series_stats_for_player(
                pool, league_id, season, series_id, mvp["player_id"]
            )
        except Exception as exc:
            log.warning(f"Failed to fetch series stats for {award_type} MVP embed: {exc}")

    if series_stats and series_stats.get("games", 0) > 0:
        stats_line = (
            f"{series_stats['ppg']} PPG / {series_stats['rpg']} RPG / "
            f"{series_stats['apg']} APG in {series_stats['games']} games"
        )
        description = f"{mvp['player_name']} — {stats_line}"
    else:
        description = mvp["player_name"]

    embed = discord.Embed(
        title=f"🏆 {award_label}",
        description=description,
        color=discord.Color.gold(),
    )
    try:
        await channel.send(embed=embed)
    except Exception as exc:
        log.warning(f"Failed to post {award_type} announcement: {exc}")


async def _finals_home_seed(
    pool,
    league_id: int,
    season: int,
    east_winner_id: int,
    west_winner_id: int,
) -> Tuple[int, int]:
    """
    PA2: return (high_seed_id, low_seed_id) for the Finals matchup based on
    each finalist's actual regular-season record, instead of unconditionally
    treating the East winner as the high seed.

    Uses the same (-wins, losses) ordering _standings_to_seeds already applies
    to rank teams within a conference. Falls back to the pre-PA2 default
    (East as high seed) only if standings data is missing for either team --
    should not happen in normal flow, since both teams just finished a full
    regular season.
    """
    standings = await game_repo.get_standings(pool, league_id, season)
    records = {r["team_id"]: r for r in standings}
    east_rec = records.get(east_winner_id)
    west_rec = records.get(west_winner_id)
    if east_rec is None or west_rec is None:
        log.warning(
            f"_finals_home_seed: missing standings for east={east_winner_id} or "
            f"west={west_winner_id} in league={league_id} season={season} -- "
            "falling back to East-as-high-seed default"
        )
        return east_winner_id, west_winner_id

    east_key = (-east_rec["wins"], east_rec["losses"])
    west_key = (-west_rec["wins"], west_rec["losses"])
    if west_key < east_key:
        return west_winner_id, east_winner_id
    return east_winner_id, west_winner_id


async def advance_playoff_round(
    league_id: int,
    season: int,
    guild: Optional[discord.Guild] = None,
) -> str:
    """
    Checks whether all series in the current active round are complete. If so,
    creates next-round matchups and returns the round name. Returns 'champion'
    when the Finals are decided. Returns 'pending' when games are still active.

    Computes and announces Conference Finals MVP and Finals MVP when those series
    conclude, writing them to player_awards and history_seasons.

    Round order per conference:
      play_in → r1 → r2 → conference_finals → nba_finals
    """
    pool = await get_pool()
    all_series = await series_repo.get_bracket(pool, league_id, season)

    # Determine which non-finals rounds are fully done
    round_names = list({s.round for s in all_series})

    def _all_complete(round_name: str) -> bool:
        return all(s.status == "complete" for s in all_series if s.round == round_name)

    def _winners_of(round_name: str) -> Dict[int, int]:
        """Returns {series_index: winner_team_id} for a round, sorted by id."""
        completed = sorted(
            [s for s in all_series if s.round == round_name],
            key=lambda s: s.id,
        )
        return {i: s.winner_team_id for i, s in enumerate(completed)}

    def _series_of(round_name: str) -> List[series_repo.Series]:
        return sorted(
            [s for s in all_series if s.round == round_name],
            key=lambda s: s.id,
        )

    # Finals complete → champion + Finals MVP
    finals = [s for s in all_series if s.round == "nba_finals"]
    if finals and _all_complete("nba_finals"):
        finals_series = finals[0]
        if finals_series.winner_team_id:
            mvp = await _compute_series_mvp(
                pool, league_id, season, finals_series.id, finals_series.winner_team_id
            )
            if mvp:
                await _announce_series_mvp(
                    pool, league_id, season, mvp,
                    award_label="Finals MVP",
                    award_type="finals_mvp",
                    guild=guild,
                    series_id=finals_series.id,
                )
                # Persist to history_seasons if the row exists.
                try:
                    await pool.execute(
                        """
                        UPDATE history_seasons
                        SET finals_mvp_player_id = $3
                        WHERE league_id = $1 AND season = $2
                        """,
                        league_id, season, mvp["player_id"],
                    )
                except Exception as exc:
                    log.warning(f"Failed to update history_seasons finals_mvp: {exc}")
        return "champion"

    # Conference finals complete → create Finals + CF MVPs
    east_cf_done = "conference_finals_east" in round_names and _all_complete("conference_finals_east")
    west_cf_done = "conference_finals_west" in round_names and _all_complete("conference_finals_west")
    if east_cf_done and west_cf_done and not finals:
        east_series_list = _series_of("conference_finals_east")
        west_series_list = _series_of("conference_finals_west")
        east_winner = east_series_list[0].winner_team_id
        west_winner = west_series_list[0].winner_team_id

        # Compute and announce CF MVPs before creating the Finals series.
        if east_winner:
            cf_east_mvp = await _compute_series_mvp(
                pool, league_id, season, east_series_list[0].id, east_winner
            )
            if cf_east_mvp:
                await _announce_series_mvp(
                    pool, league_id, season, cf_east_mvp,
                    award_label="Eastern Conference Finals MVP",
                    award_type="cf_east_mvp",
                    guild=guild,
                    series_id=east_series_list[0].id,
                )
                try:
                    await pool.execute(
                        """
                        INSERT INTO history_seasons (league_id, season, cfmvp_east_player_id)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (league_id, season) DO UPDATE
                        SET cfmvp_east_player_id = EXCLUDED.cfmvp_east_player_id
                        """,
                        league_id, season, cf_east_mvp["player_id"],
                    )
                except Exception as exc:
                    log.warning(f"Failed to upsert cfmvp_east into history_seasons: {exc}")

        if west_winner:
            cf_west_mvp = await _compute_series_mvp(
                pool, league_id, season, west_series_list[0].id, west_winner
            )
            if cf_west_mvp:
                await _announce_series_mvp(
                    pool, league_id, season, cf_west_mvp,
                    award_label="Western Conference Finals MVP",
                    award_type="cf_west_mvp",
                    guild=guild,
                    series_id=west_series_list[0].id,
                )
                try:
                    await pool.execute(
                        """
                        INSERT INTO history_seasons (league_id, season, cfmvp_west_player_id)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (league_id, season) DO UPDATE
                        SET cfmvp_west_player_id = EXCLUDED.cfmvp_west_player_id
                        """,
                        league_id, season, cf_west_mvp["player_id"],
                    )
                except Exception as exc:
                    log.warning(f"Failed to upsert cfmvp_west into history_seasons: {exc}")

        # PA2: home court goes to whichever finalist actually earned it via
        # regular-season record, not unconditionally to the East winner.
        finals_high_seed, finals_low_seed = await _finals_home_seed(
            pool, league_id, season, east_winner, west_winner
        )
        await series_repo.create_series(
            pool, league_id, season, "nba_finals",
            high_seed_id=finals_high_seed,
            low_seed_id=finals_low_seed,
        )
        if guild:
            try:
                from services import sim_content_pipeline as _scp
                _high_t = await team_repo.get_by_id(pool, finals_high_seed)
                _low_t = await team_repo.get_by_id(pool, finals_low_seed)
                if _high_t and _low_t:
                    await _scp._maybe_post_prelude(pool, league_id, season, guild, {
                        "high_seed_team": getattr(_high_t, "nba_team_code", str(finals_high_seed)),
                        "low_seed_team": getattr(_low_t, "nba_team_code", str(finals_low_seed)),
                        "round": "DBA Finals",
                    })
            except Exception as _exc:
                log.warning(f"Prelude post failed (nba_finals): {_exc}")
        return "nba_finals"

    # R2 complete → create conference finals
    async def _maybe_create_conf_finals(r2_round: str, cf_round: str) -> bool:
        if r2_round in round_names and _all_complete(r2_round):
            if cf_round not in round_names:
                winners = _winners_of(r2_round)
                if len(winners) >= 2:
                    await series_repo.create_series(
                        pool, league_id, season, cf_round,
                        high_seed_id=winners[0],
                        low_seed_id=winners[1],
                    )
                    if guild:
                        try:
                            from services import sim_content_pipeline as _scp
                            _ht = await team_repo.get_by_id(pool, winners[0])
                            _lt = await team_repo.get_by_id(pool, winners[1])
                            if _ht and _lt:
                                _conf = "Eastern" if "east" in cf_round else "Western"
                                await _scp._maybe_post_prelude(pool, league_id, season, guild, {
                                    "high_seed_team": getattr(_ht, "nba_team_code", str(winners[0])),
                                    "low_seed_team": getattr(_lt, "nba_team_code", str(winners[1])),
                                    "round": f"{_conf} Conference Finals",
                                })
                        except Exception as _exc:
                            log.warning(f"Prelude post failed ({cf_round}): {_exc}")
                    return True
        return False

    created_ecf = await _maybe_create_conf_finals("r2_east", "conference_finals_east")
    created_wcf = await _maybe_create_conf_finals("r2_west", "conference_finals_west")
    if created_ecf or created_wcf:
        round_label = []
        if created_ecf:
            round_label.append("conference_finals_east")
        if created_wcf:
            round_label.append("conference_finals_west")
        return ", ".join(round_label)

    # R1 complete → create R2 (top winner vs bottom winner — 1/8 winner vs 4/5 winner, etc.)
    async def _maybe_create_r2(r1_round: str, r2_round: str) -> bool:
        if r1_round in round_names and _all_complete(r1_round):
            if r2_round not in round_names:
                r1_series = sorted(
                    [s for s in all_series if s.round == r1_round],
                    key=lambda s: s.id,
                )
                if len(r1_series) >= 4:
                    # Standard bracket: (1v8 winner) vs (4v5 winner), (3v6 winner) vs (2v7 winner)
                    # Series are stored in order: 1v8, 2v7, 3v6, 4v5 (created by sim_play_in)
                    s_1v8 = r1_series[0]
                    s_2v7 = r1_series[1]
                    s_3v6 = r1_series[2]
                    s_4v5 = r1_series[3]
                    await series_repo.create_series(
                        pool, league_id, season, r2_round,
                        high_seed_id=s_1v8.winner_team_id,
                        low_seed_id=s_4v5.winner_team_id,
                    )
                    await series_repo.create_series(
                        pool, league_id, season, r2_round,
                        high_seed_id=s_2v7.winner_team_id,
                        low_seed_id=s_3v6.winner_team_id,
                    )
                    if guild:
                        try:
                            from services import sim_content_pipeline as _scp
                            _conf = "Eastern" if "east" in r2_round else "Western"
                            for _hs_id, _ls_id in [
                                (s_1v8.winner_team_id, s_4v5.winner_team_id),
                                (s_2v7.winner_team_id, s_3v6.winner_team_id),
                            ]:
                                _ht = await team_repo.get_by_id(pool, _hs_id)
                                _lt = await team_repo.get_by_id(pool, _ls_id)
                                if _ht and _lt:
                                    await _scp._maybe_post_prelude(pool, league_id, season, guild, {
                                        "high_seed_team": getattr(_ht, "nba_team_code", str(_hs_id)),
                                        "low_seed_team": getattr(_lt, "nba_team_code", str(_ls_id)),
                                        "round": f"{_conf} Conference Semifinals",
                                    })
                        except Exception as _exc:
                            log.warning(f"Prelude post failed ({r2_round}): {_exc}")
                    return True
        return False

    created_er2 = await _maybe_create_r2("r1_east", "r2_east")
    created_wr2 = await _maybe_create_r2("r1_west", "r2_west")
    if created_er2 or created_wr2:
        round_label = []
        if created_er2:
            round_label.append("r2_east")
        if created_wr2:
            round_label.append("r2_west")
        return ", ".join(round_label)

    return "pending"


async def sim_play_in(
    league_id: int,
    season: int,
    guild: discord.Guild,
) -> dict:
    """
    Sims all play-in games for both conferences.

    Play-in rules:
    - Game 1: 7 seed vs 8 seed. Winner → 7 seed slot (done). Loser gets another chance.
    - Game 2: 9 seed vs 10 seed. Winner stays alive.
    - Game 3: Loser of Game 1 vs Winner of Game 2. Winner → 8 seed slot.

    Each "game" inside the play-in is represented by a separate series row with
    games_needed=2 (so a single win completes it). seed_playoffs() creates only
    the 7v8 and 9v10 series rows; this function creates the loser-bracket game row.

    After all play-in games resolve, creates R1 series rows for:
    - 1 seed vs 8 seed
    - 2 seed vs 7 seed
    (3v6 and 4v5 were already created by seed_playoffs.)

    Returns {east_7_seed, east_8_seed, west_7_seed, west_8_seed} as team_ids.
    """
    pool = await get_pool()
    standings = await game_repo.get_standings(pool, league_id, season)
    east_seeds = _standings_to_seeds(standings, "East")
    west_seeds = _standings_to_seeds(standings, "West")

    box_channel_id = await league_repo.get_channel(pool, league_id, "box-scores")
    box_channel = guild.get_channel(box_channel_id) if box_channel_id else None

    async def _sim_playin_conf(
        conf_seeds: List[dict],
        playin_round: str,
        r1_round: str,
    ) -> Tuple[int, int]:
        """Returns (seven_seed_team_id, eight_seed_team_id)."""
        playin_series = await series_repo.get_series_by_round(pool, league_id, season, playin_round)

        # Identify 7v8 and 9v10 series by high_seed_team_id matching standings
        seed7_id = conf_seeds[6]["team_id"]
        seed9_id = conf_seeds[8]["team_id"]

        s_7v8 = next((s for s in playin_series if s.high_seed_team_id == seed7_id), None)
        s_9v10 = next((s for s in playin_series if s.high_seed_team_id == seed9_id), None)

        if s_7v8 is None or s_9v10 is None:
            raise ValueError(f"Play-in series rows missing for {playin_round}")

        # Game 1: 7 vs 8 (skip if already resolved)
        t7 = await team_repo.get_by_id(pool, s_7v8.high_seed_team_id)
        t8 = await team_repo.get_by_id(pool, s_7v8.low_seed_team_id)
        if s_7v8.status != "complete":
            _, r1 = await _run_one_game(pool, league_id, season, t7, t8, s_7v8.id, "play_in", guild=guild)
            s_7v8 = await series_repo.record_game_result(pool, s_7v8.id, r1["winner_team_id"])
            if box_channel:
                embed = sim_embeds.game_recap(
                    {"game_index": "Play-In G1", "scheduled_date": datetime.date.today(), "id": 0},
                    t7, t8, r1["home_score"], r1["away_score"],
                )
                await box_channel.send(embed=embed)

        seven_seed_team_id = s_7v8.winner_team_id
        loser_7v8_id = t8.id if s_7v8.winner_team_id == t7.id else t7.id

        # Game 2: 9 vs 10 (skip if already resolved)
        t9 = await team_repo.get_by_id(pool, s_9v10.high_seed_team_id)
        t10 = await team_repo.get_by_id(pool, s_9v10.low_seed_team_id)
        if s_9v10.status != "complete":
            _, r2 = await _run_one_game(pool, league_id, season, t9, t10, s_9v10.id, "play_in", guild=guild)
            s_9v10 = await series_repo.record_game_result(pool, s_9v10.id, r2["winner_team_id"])
            if box_channel:
                embed = sim_embeds.game_recap(
                    {"game_index": "Play-In G2", "scheduled_date": datetime.date.today(), "id": 0},
                    t9, t10, r2["home_score"], r2["away_score"],
                )
                await box_channel.send(embed=embed)

        winner_9v10_id = s_9v10.winner_team_id

        # Game 3: loser of 7v8 vs winner of 9v10 (skip if already resolved)
        loser_team = await team_repo.get_by_id(pool, loser_7v8_id)
        winner_team = await team_repo.get_by_id(pool, winner_9v10_id)

        # Game 3: use existing series if present, otherwise create
        existing_g3 = next(
            (s for s in playin_series
             if s.high_seed_team_id == loser_7v8_id and s.low_seed_team_id == winner_9v10_id),
            None,
        )
        if existing_g3 and existing_g3.status == "complete":
            s_g3 = existing_g3
        else:
            s_g3 = existing_g3 or await series_repo.create_series(
                pool, league_id, season, playin_round,
                high_seed_id=loser_7v8_id,
                low_seed_id=winner_9v10_id,
                games_needed=2,
            )
            _, r3 = await _run_one_game(
                pool, league_id, season, loser_team, winner_team, s_g3.id, "play_in", guild=guild
            )
            s_g3 = await series_repo.record_game_result(pool, s_g3.id, r3["winner_team_id"])
            if box_channel:
                embed = sim_embeds.game_recap(
                    {"game_index": "Play-In G3", "scheduled_date": datetime.date.today(), "id": 0},
                    loser_team, winner_team, r3["home_score"], r3["away_score"],
                )
                await box_channel.send(embed=embed)

        eight_seed_team_id = s_g3.winner_team_id

        # Now create the remaining R1 series (1v8 and 2v7); 3v6 and 4v5 were pre-created
        seed1_id = conf_seeds[0]["team_id"]
        seed2_id = conf_seeds[1]["team_id"]

        await series_repo.create_series(
            pool, league_id, season, r1_round,
            high_seed_id=seed1_id,
            low_seed_id=eight_seed_team_id,
        )
        await series_repo.create_series(
            pool, league_id, season, r1_round,
            high_seed_id=seed2_id,
            low_seed_id=seven_seed_team_id,
        )

        return seven_seed_team_id, eight_seed_team_id

    east_7, east_8 = await _sim_playin_conf(east_seeds, "play_in_east", "r1_east")
    west_7, west_8 = await _sim_playin_conf(west_seeds, "play_in_west", "r1_west")

    # Post Prelude previews for every R1 series now that all matchups are set.
    try:
        from services import sim_content_pipeline as _scp
        all_r1 = await series_repo.get_bracket(pool, league_id, season)
        r1_series = [s for s in all_r1 if s.round in ("r1_east", "r1_west")]
        for _s in r1_series:
            _ht = await team_repo.get_by_id(pool, _s.high_seed_team_id)
            _lt = await team_repo.get_by_id(pool, _s.low_seed_team_id)
            if _ht and _lt:
                _round_label = "Eastern Conference R1" if "east" in _s.round else "Western Conference R1"
                _preview_ctx = {
                    "high_seed_team": getattr(_ht, "nba_team_code", str(_s.high_seed_team_id)),
                    "low_seed_team": getattr(_lt, "nba_team_code", str(_s.low_seed_team_id)),
                    "round": _round_label,
                }
                await _scp._maybe_post_prelude(pool, league_id, season, guild, _preview_ctx)
    except Exception as _prelude_exc:
        log.warning(f"sim_play_in: Prelude preview failed: {_prelude_exc}")

    return {
        "east_7_seed": east_7,
        "east_8_seed": east_8,
        "west_7_seed": west_7,
        "west_8_seed": west_8,
    }
