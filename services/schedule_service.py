from __future__ import annotations

import datetime
from random import Random
from typing import Dict, List, Optional, Set, Tuple

from data.db import get_pool
from data.repositories import game_repo, team_repo
from services import roster_service
from core.logging import get_logger

log = get_logger(__name__)

_SEASON_START_MONTH = 10
_SEASON_START_DAY = 1


def _balance_pairs(pairs: List[Tuple[int, int]], rng: Random) -> List[Tuple[int, int]]:
    """
    Reorder pairs so each team's games are distributed evenly across the list.
    Uses a greedy approach: always next schedule the pair where the most-behind
    team (fewest games scheduled so far) gets a game.

    After this pass, team N's 82 appearances are spread roughly every
    len(pairs)/82 ≈ 14.8 positions. With 195 days in the window, each team
    plays roughly every 195/82 ≈ 2.4 days — about 12-13 games per month.

    list.pop(best_idx) is O(n) per call, O(n²) total. For n=1214 that is
    ~1.5 M operations, well under 1 second.
    """
    from collections import defaultdict
    team_count: dict[int, int] = defaultdict(int)
    pool = list(pairs)
    rng.shuffle(pool)  # initial shuffle for variety
    result: list[tuple[int, int]] = []

    while pool:
        # Score each candidate game by the maximum games-so-far of either team.
        # Lower score → both teams are more behind → pick this game next.
        best_idx = min(
            range(len(pool)),
            key=lambda i: max(team_count[pool[i][0]], team_count[pool[i][1]]),
        )
        game = pool.pop(best_idx)
        result.append(game)
        team_count[game[0]] += 1
        team_count[game[1]] += 1

    return result


def _add_makeup_games(
    pair_counts: Dict[Tuple[int, int], int],
    teams: List[team_repo.Team],
    target: int = 82,
) -> None:
    """
    Adjust pair_counts in place so that every team ends up with exactly `target`
    game appearances.  Cross-conference pairs with only 2 games are preferred
    as makeup candidates because bumping them to 3 is least disruptive.

    Safety cap of 200 iterations prevents infinite loops if a degenerate schedule
    configuration makes it impossible to reach the target.
    """
    team_ids = [t.id for t in teams]
    for _ in range(200):
        appearances: Dict[int, int] = {tid: 0 for tid in team_ids}
        for (lo, hi), count in pair_counts.items():
            appearances[lo] += count
            appearances[hi] += count

        under = [tid for tid in team_ids if appearances[tid] < target]
        if not under:
            break

        # Pick the team furthest under target.
        target_tid = min(under, key=lambda tid: appearances[tid])

        # Among that team's pairs, pick the one with the fewest games (ties broken
        # by preferring cross-conference pairs, which start at 2).
        best_pair: Optional[Tuple[int, int]] = None
        min_count = float("inf")
        for (lo, hi), count in pair_counts.items():
            if lo == target_tid or hi == target_tid:
                if count < min_count:
                    min_count = count
                    best_pair = (lo, hi)

        if best_pair is not None:
            pair_counts[best_pair] += 1


def _build_pairs(teams: List[team_repo.Team], rng: Optional[Random] = None) -> List[Tuple[int, int]]:
    """
    Return list of (home_id, away_id) for all 82-game matchups.

    NBA exact formula — every team plays exactly 82 games:
      - 4 games vs each of the 4 other division opponents           = 16
      - 4 games vs 6 of the 10 other same-conf opponents (rotating) = 24
      - 3 games vs the remaining 4 same-conf opponents              = 12
      - 2 games vs each of the 15 inter-conference opponents         = 30
      Total: 82

    For each pair with N games we generate ceil(N/2) games with the
    lower-id team at home and floor(N/2) with the higher-id team at home,
    keeping home/away balanced.

    Same-conf cross-div design guarantee:
      Each conference has 3 divisions of 5.  Each team plays 10 cross-div
      opponents across 2 other divisions (5 opponents each).  We assign
      exactly 3 of those 5 opponents per opposite division as 4-game and 2
      as 3-game, giving 3+3=6 four-game and 2+2=4 three-game cross-div
      opponents: 6×4+4×3=36 cross-div games, guaranteed per team.

      Within each (div_A vs div_B) pair we use a rng-seeded cyclic shift to
      select which 3 of the 5 opposing teams get the 4-game treatment.
      The shift is applied from both sides, so the 4-game set is symmetric:
      if A plays B four times, B plays A four times.
    """
    by_conf: Dict[str, Dict[str, List[team_repo.Team]]] = {}
    for t in teams:
        by_conf.setdefault(t.conference, {}).setdefault(t.division, []).append(t)

    pair_counts: Dict[Tuple[int, int], int] = {}

    def add_pair(a_id: int, b_id: int, count: int) -> None:
        key = (min(a_id, b_id), max(a_id, b_id))
        pair_counts[key] = pair_counts.get(key, 0) + count

    for conf, divisions in by_conf.items():
        div_names = sorted(divisions.keys())

        # Division games: 4 per intra-division pair → 4 opponents × 4 = 16 per team.
        for div_name, div_teams in divisions.items():
            for i, ta in enumerate(div_teams):
                for tb in div_teams[i + 1:]:
                    add_pair(ta.id, tb.id, 4)

        # Same-conf cross-div: for each ordered (div_a, div_b) pairing we assign
        # exactly 3 four-game and 2 three-game opponents per team on each side.
        # We do this by choosing a cyclic shift s ∈ {0,1,2,3,4} (rng or 0) and
        # marking the 3 pairs (ta[i], tb[(i+s) % 5]), (ta[i], tb[(i+s+1) % 5]),
        # (ta[i], tb[(i+s+2) % 5]) as 4-game for each ta[i].
        # Equivalently: pair (ta[i], tb[j]) is 4-game iff (j - i - s) % 5 < 3.
        # This is symmetric: (j-i-s)%5 < 3 iff (i-j-(-s))%5 in {2,3,4}... wait,
        # symmetry requires that when we flip sides (tb[j] looking at ta[i]):
        # (i - j - s) % 5 < 3. That's a different condition, so we cannot use a
        # simple shift for symmetric 4-game sets.
        #
        # Correct symmetric construction: for each pair (ta[i], tb[j]) we assign
        # 4 games iff (i + j + shift) % 5 < 3. Each ta[i] has j=0..4, and
        # (i+j+shift)%5 < 3 is satisfied for exactly 3 values of j (since
        # residues 0,1,2,3,4 each appear once as j varies over 0..4). Symmetric
        # because the condition is the same when roles are swapped.
        for i, div_a in enumerate(div_names):
            for div_b in div_names[i + 1:]:
                teams_a = divisions[div_a]
                teams_b = divisions[div_b]
                # Pick a per-div-pair shift for schedule variety across seasons/leagues.
                shift = rng.randint(0, 4) if rng is not None else 0
                for ai, ta in enumerate(teams_a):
                    for bi, tb in enumerate(teams_b):
                        if (ai + bi + shift) % 5 < 3:
                            add_pair(ta.id, tb.id, 4)
                        else:
                            add_pair(ta.id, tb.id, 3)

    # Cross-conference: 2 games per pair, 15 opponents × 2 = 30 per team. ✓
    conf_list = list(by_conf.keys())
    if len(conf_list) == 2:
        conf_a, conf_b = conf_list[0], conf_list[1]
        teams_a = [t for div in by_conf[conf_a].values() for t in div]
        teams_b = [t for div in by_conf[conf_b].values() for t in div]
        for ta in teams_a:
            for tb in teams_b:
                add_pair(ta.id, tb.id, 2)

    # Expand pair_counts into (home_id, away_id) game list.
    # For N games between A and B: ceil(N/2) with lower-id at home, floor(N/2) away.
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

    # RO5: guard-rail — block season generation if any team's active roster is
    # below the floor (rollover/FA can leave a team unable to field a lineup;
    # this does not auto-backfill, it just stops an unplayable season from locking in).
    under_floor = await roster_service.get_teams_below_roster_floor(pool, league_id)
    if under_floor:
        team_names = ", ".join(t["team_name"] for t in under_floor)
        raise ValueError(
            f"Teams below the minimum roster size ({roster_service.ROSTER_FLOOR_DEFAULT} active players): "
            f"{team_names} — sign more free agents before starting the season"
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

    pairs = _build_pairs(teams, rng=rng)
    pairs = _balance_pairs(pairs, rng)

    scheduled = _assign_dates(rng, pairs, season)
    scheduled.sort(key=lambda x: x[2])

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
