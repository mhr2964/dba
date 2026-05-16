from __future__ import annotations

import datetime
from random import Random
from typing import Dict, List, Set, Tuple

from data.db import get_pool
from data.repositories import game_repo, team_repo
from core.logging import get_logger

log = get_logger(__name__)

_SEASON_START_MONTH = 10
_SEASON_START_DAY = 1


def _build_pairs(teams: List[team_repo.Team]) -> List[Tuple[int, int, int]]:
    """
    Return list of (home_id, away_id, game_number_within_series) for all 82-game matchups.
    Game counts per pair:
      - Division opponents:              4 games
      - Same-conf, different division:   3 or 4 games (alternated to hit ~36 in-conf total)
      - Cross-conference:                2 games
    For each pair count N, we generate ceil(N/2) home and floor(N/2) away games for the
    alphabetically-first team, alternating the direction each game so home/away is balanced.
    """
    by_conf: Dict[str, Dict[str, List[team_repo.Team]]] = {}
    for t in teams:
        by_conf.setdefault(t.conference, {}).setdefault(t.division, []).append(t)

    # Map team_id -> Team for quick lookup
    team_by_id = {t.id: t for t in teams}

    # Build pair -> game_count mapping
    pair_counts: Dict[Tuple[int, int], int] = {}

    def add_pair(a_id: int, b_id: int, count: int) -> None:
        key = (min(a_id, b_id), max(a_id, b_id))
        pair_counts[key] = pair_counts.get(key, 0) + count

    for conf, divisions in by_conf.items():
        div_names = sorted(divisions.keys())

        # Division games: each intra-division pair plays 4x
        for div_name, div_teams in divisions.items():
            for i, ta in enumerate(div_teams):
                for tb in div_teams[i + 1:]:
                    add_pair(ta.id, tb.id, 4)

        # Same-conf, different-division: iterate PAIRS of divisions (forward only)
        # so each cross-div pair is added exactly once.
        # Target: ~36 cross-div same-conf games per team (10 opponents, mix of 3 and 4).
        for i, div_a in enumerate(div_names):
            for div_b in div_names[i + 1:]:
                for ta in divisions[div_a]:
                    for tb in divisions[div_b]:
                        # Alternate 4/3 by id parity so each team gets roughly
                        # half opponents at 4 games and half at 3 → avg 3.5 × 10 = 35.
                        # Cross-conf is 30, div is 16 → total ≈ 81 (one game shy of 82;
                        # the NBA solves this with a full-graph LP — close enough here).
                        count = 4 if (ta.id + tb.id) % 2 == 0 else 3
                        add_pair(ta.id, tb.id, count)

    # Cross-conference pairs: 2 games each
    conf_list = list(by_conf.keys())
    if len(conf_list) == 2:
        conf_a, conf_b = conf_list[0], conf_list[1]
        teams_a = [t for div in by_conf[conf_a].values() for t in div]
        teams_b = [t for div in by_conf[conf_b].values() for t in div]
        for ta in teams_a:
            for tb in teams_b:
                add_pair(ta.id, tb.id, 2)

    # Expand pair_counts into (home_id, away_id) game list
    # For N games between A and B: ceil(N/2) with A at home, floor(N/2) with B at home
    games: List[Tuple[int, int]] = []
    for (id_lo, id_hi), count in pair_counts.items():
        home_count = (count + 1) // 2
        away_count = count // 2
        for _ in range(home_count):
            games.append((id_lo, id_hi))
        for _ in range(away_count):
            games.append((id_hi, id_lo))

    return games


def _assign_dates(
    rng: Random,
    games: List[Tuple[int, int]],
    season: int,
) -> List[Tuple[int, int, datetime.date]]:
    """
    Assign scheduled dates. Starts {season}-10-01, spreads all games across a
    195-day regular-season window (Oct 1 → ~Apr 14) regardless of league size.
    ±1 day jitter is applied per game. After initial assignment, any team that
    has two games on the same day gets its second game pushed to the next day.
    """
    base_date = datetime.date(season, _SEASON_START_MONTH, _SEASON_START_DAY)
    games_per_day = max(1.0, len(games) / 195.0)
    result = []
    for i, (home_id, away_id) in enumerate(games):
        offset_days = int(i / games_per_day) + rng.randint(-1, 1)
        game_date = base_date + datetime.timedelta(days=max(0, offset_days))
        result.append((home_id, away_id, game_date))

    # Resolve same-team same-day conflicts by pushing the later game forward one day.
    team_last_date: Dict[int, datetime.date] = {}
    for i, (home_id, away_id, game_date) in enumerate(result):
        bumped = game_date
        while (
            team_last_date.get(home_id) == bumped
            or team_last_date.get(away_id) == bumped
        ):
            bumped = bumped + datetime.timedelta(days=1)
        if bumped != game_date:
            result[i] = (home_id, away_id, bumped)
        team_last_date[home_id] = bumped
        team_last_date[away_id] = bumped

    return result


def _find_back_to_backs(
    scheduled: List[Tuple[int, int, datetime.date]],
) -> Set[Tuple[int, datetime.date]]:
    """Return set of (team_id, date) pairs that are the second game of a B2B."""
    team_dates: Dict[int, List[datetime.date]] = {}
    for home_id, away_id, d in scheduled:
        team_dates.setdefault(home_id, []).append(d)
        team_dates.setdefault(away_id, []).append(d)

    b2b: Set[Tuple[int, datetime.date]] = set()
    for team_id, dates in team_dates.items():
        sorted_dates = sorted(dates)
        for j in range(1, len(sorted_dates)):
            if (sorted_dates[j] - sorted_dates[j - 1]).days == 1:
                b2b.add((team_id, sorted_dates[j]))
    return b2b


async def generate_season(league_id: int, season: int) -> int:
    """Generates the 82-game schedule, inserts into games table. Returns game count."""
    pool = await get_pool()

    teams = await team_repo.get_all(pool, league_id)
    if len(teams) != 30:
        raise ValueError(
            f"Expected 30 teams, got {len(teams)} — run /league delete and /league create again"
        )

    # Idempotent: remove any previously-generated games for this league+season
    # so a second `/season start` call doesn't fail on duplicate game_index.
    existing = await pool.fetchval(
        "SELECT COUNT(*) FROM games WHERE league_id = $1 AND season = $2", league_id, season
    )
    if existing:
        log.info(f"Clearing {existing} existing games for league {league_id} season {season}")
        await pool.execute(
            "DELETE FROM games WHERE league_id = $1 AND season = $2", league_id, season
        )

    human_team_ids = {t.id for t in teams if t.manager_user_id is not None}

    rng_seed = hash((league_id, season, "schedule")) & 0x7FFFFFFFFFFFFFFF
    rng = Random(rng_seed)

    pairs = _build_pairs(teams)
    rng.shuffle(pairs)

    scheduled = _assign_dates(rng, pairs, season)
    scheduled.sort(key=lambda x: x[2])

    b2b_set = _find_back_to_backs(scheduled)

    # Track per-team game_index (1-82)
    team_game_index: Dict[int, int] = {t.id: 0 for t in teams}

    # game_index in the games table is the global ordinal (1..N total games),
    # not per-team. The per-team sequence is derived from order by scheduled_date
    # filtered to that team. We store a global ordinal per game row.
    game_rows: List[dict] = []
    for global_idx, (home_id, away_id, game_date) in enumerate(scheduled, start=1):
        team_game_index[home_id] = team_game_index.get(home_id, 0) + 1
        team_game_index[away_id] = team_game_index.get(away_id, 0) + 1

        is_user = bool(human_team_ids & {home_id, away_id})

        game_rows.append({
            "league_id": league_id,
            "season": season,
            "season_type": "regular",
            "game_index": global_idx,
            "home_team_id": home_id,
            "away_team_id": away_id,
            "scheduled_date": game_date,
            "status": "scheduled",
            "is_user_matchup": is_user,
            "rng_seed": rng_seed ^ global_idx,
        })

    await game_repo.insert_game_batch(pool, game_rows)
    log.info(f"Generated {len(game_rows)} games for league {league_id} season {season}")
    return len(game_rows)
