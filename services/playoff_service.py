from __future__ import annotations

import datetime
import random
from typing import Dict, List, Optional, Tuple

import discord

from core.logging import get_logger
from data.db import get_pool
from data.repositories import game_repo, league_repo, series_repo, team_repo
from services import sim_engine
from bot.embeds import sim_embeds

log = get_logger(__name__)

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


def _team_to_sim_dict(team: team_repo.Team) -> dict:
    return {
        "team_id": team.id,
        "overall": 75,
        "offense_rating": team.team_offense_rating or 75,
        "defense_rating": team.team_defense_rating or 75,
        "pace": team.pace or 100.0,
    }


async def _run_one_game(
    pool,
    league_id: int,
    season: int,
    home_team: team_repo.Team,
    away_team: team_repo.Team,
    series_id: int,
    season_type: str = "playoff",
) -> Tuple[int, dict]:
    """
    Inserts a game row, runs sim_engine, persists result. Returns (game_id, result).
    season_type distinguishes play-in from playoff for record-keeping.
    """
    home_players = await _load_lineup(pool, league_id, home_team.id)
    away_players = await _load_lineup(pool, league_id, away_team.id)

    rng_seed = random.getrandbits(63)

    game_id = await game_repo.insert_game(pool, {
        "league_id": league_id,
        "season": season,
        "season_type": season_type,
        "series_id": series_id,
        "game_index": 0,
        "home_team_id": home_team.id,
        "away_team_id": away_team.id,
        "scheduled_date": datetime.date.today(),
        "status": "scheduled",
        "is_user_matchup": False,
        "rng_seed": rng_seed,
    })

    result = sim_engine.sim_game(
        _team_to_sim_dict(home_team),
        _team_to_sim_dict(away_team),
        home_players,
        away_players,
        rng_seed,
    )

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
        pool, league_id, season, home_team, away_team, series_id, "playoff"
    )

    updated_series = await series_repo.record_game_result(
        pool, series_id, result["winner_team_id"]
    )

    box_channel_id = await league_repo.get_channel(pool, league_id, "box-scores")
    if box_channel_id:
        channel = guild.get_channel(box_channel_id)
        if channel:
            embed = sim_embeds.game_recap(
                {"game_index": game_number, "scheduled_date": datetime.date.today(), "id": game_id},
                home_team, away_team,
                result["home_score"], result["away_score"],
            )
            await channel.send(embed=embed)

    return {
        "game_result": result,
        "series": updated_series,
        "series_over": updated_series.status == "complete",
        "winner_team_id": updated_series.winner_team_id,
    }


async def advance_playoff_round(league_id: int, season: int) -> str:
    """
    Checks whether all series in the current active round are complete. If so,
    creates next-round matchups and returns the round name. Returns 'champion'
    when the Finals are decided. Returns 'pending' when games are still active.

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

    # Finals complete → champion
    finals = [s for s in all_series if s.round == "nba_finals"]
    if finals and _all_complete("nba_finals"):
        return "champion"

    # Conference finals complete → create Finals
    east_cf_done = "conference_finals_east" in round_names and _all_complete("conference_finals_east")
    west_cf_done = "conference_finals_west" in round_names and _all_complete("conference_finals_west")
    if east_cf_done and west_cf_done and not finals:
        east_winner = _winners_of("conference_finals_east")[0]
        west_winner = _winners_of("conference_finals_west")[0]
        # Higher win total gets home court; treat East winner as high seed for simplicity
        await series_repo.create_series(
            pool, league_id, season, "nba_finals",
            high_seed_id=east_winner,
            low_seed_id=west_winner,
        )
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
            _, r1 = await _run_one_game(pool, league_id, season, t7, t8, s_7v8.id, "play_in")
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
            _, r2 = await _run_one_game(pool, league_id, season, t9, t10, s_9v10.id, "play_in")
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
                pool, league_id, season, loser_team, winner_team, s_g3.id, "play_in"
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

    return {
        "east_7_seed": east_7,
        "east_8_seed": east_8,
        "west_7_seed": west_7,
        "west_8_seed": west_8,
    }
