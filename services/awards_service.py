from __future__ import annotations

from typing import Optional

import asyncpg

from core.errors import DBAError
from core.logging import get_logger
from data.db import get_pool
from services.cpu_voter import VoterProfile, get_cpu_profile, score_player_for_award

log = get_logger(__name__)

_SINGLE_WINNER_AWARDS = {"mvp", "dpoy", "roy", "6moy"}
_ALL_NBA_AWARDS = {"all_nba_1", "all_nba_2", "all_nba_3"}
_ALL_STAR_AWARDS = {"all_star_east", "all_star_west"}

# All-NBA award types ordered 1st → 3rd so we can slice by index.
_ALL_NBA_ORDERED = ["all_nba_1", "all_nba_2", "all_nba_3"]


async def open_voting(league_id: int, season: int, award_type: str) -> int:
    """Opens a voting session for this award. Returns voting_id."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO award_votings (league_id, season, award_type)
        VALUES ($1, $2, $3)
        ON CONFLICT (league_id, season, award_type) DO NOTHING
        RETURNING id
        """,
        league_id,
        season,
        award_type,
    )
    if row is None:
        # Row already existed — fetch it.
        row = await pool.fetchrow(
            "SELECT id FROM award_votings WHERE league_id=$1 AND season=$2 AND award_type=$3",
            league_id,
            season,
            award_type,
        )
    return row["id"]


async def cast_vote(
    voting_id: int,
    team_id: int,
    player_id: int,
    rank: int = 1,
    user_id: Optional[int] = None,
) -> None:
    """Records a human GM vote. Raises DBAError if already voted or voting closed."""
    pool = await get_pool()

    status = await pool.fetchval(
        "SELECT status FROM award_votings WHERE id = $1", voting_id
    )
    if status is None:
        raise DBAError("Voting session not found.")
    if status != "open":
        raise DBAError("This voting session is already closed.")

    existing = await pool.fetchval(
        "SELECT id FROM award_votes WHERE voting_id = $1 AND voter_team_id = $2",
        voting_id,
        team_id,
    )
    if existing is not None:
        raise DBAError("Your team has already cast a vote in this session.")

    await pool.execute(
        """
        INSERT INTO award_votes (voting_id, voter_team_id, voter_user_id, rank, player_id)
        VALUES ($1, $2, $3, $4, $5)
        """,
        voting_id,
        team_id,
        user_id,
        rank,
        player_id,
    )


async def get_season_stats(
    pool: asyncpg.Pool, league_id: int, season: int, player_id: int
) -> dict:
    """Aggregates box scores for a player into season averages for voting."""
    row = await pool.fetchrow(
        """
        SELECT
            COUNT(*)                                                    AS games_played,
            AVG(b.points)                                               AS ppg,
            AVG(b.rebounds_off + b.rebounds_def)                        AS rpg,
            AVG(b.assists)                                              AS apg,
            AVG(b.steals)                                               AS spg,
            AVG(b.blocks)                                               AS bpg,
            CASE WHEN SUM(b.fga) > 0
                 THEN SUM(b.fgm)::REAL / SUM(b.fga) ELSE 0 END         AS fg_pct,
            CASE WHEN (SUM(b.fga) + 0.44 * SUM(b.fta)) > 0
                 THEN SUM(b.points)::REAL /
                      (2.0 * (SUM(b.fga) + 0.44 * SUM(b.fta)))
                 ELSE 0 END                                             AS ts_pct,
            SUM(CASE WHEN b.started THEN 1 ELSE 0 END)                 AS games_started
        FROM game_box_scores b
        JOIN games g ON g.id = b.game_id
        WHERE g.league_id = $1
          AND g.season    = $2
          AND b.player_id = $3
          AND g.season_type = 'regular'
        """,
        league_id,
        season,
        player_id,
    )
    if row is None or row["games_played"] == 0:
        return {
            "games_played": 0,
            "games_started": 0,
            "ppg": 0.0,
            "rpg": 0.0,
            "apg": 0.0,
            "spg": 0.0,
            "bpg": 0.0,
            "fg_pct": 0.0,
            "ts_pct": 0.0,
        }
    return dict(row)


async def _get_eligible_players(
    pool: asyncpg.Pool, league_id: int, season: int, award_type: str
) -> list[dict]:
    """
    Returns rows of {player_id, team_id, is_rookie, games_played, games_started, ...stats}.
    Eligibility rules:
      MVP/DPOY/6MOY/All-NBA/All-Star: games_played > 20
      ROY: games_played > 20 AND is_rookie = TRUE
      6MOY: games_played > 20 AND majority NOT starter
      All-NBA: games_played > 20 AND majority starter
    """
    rows = await pool.fetch(
        """
        SELECT
            p.id                                                        AS player_id,
            p.team_id,
            p.is_rookie,
            COUNT(b.id)                                                 AS games_played,
            SUM(CASE WHEN b.started THEN 1 ELSE 0 END)                 AS games_started,
            AVG(b.points)                                               AS ppg,
            AVG(b.rebounds_off + b.rebounds_def)                        AS rpg,
            AVG(b.assists)                                              AS apg,
            AVG(b.steals)                                               AS spg,
            AVG(b.blocks)                                               AS bpg,
            CASE WHEN SUM(b.fga) > 0
                 THEN SUM(b.fgm)::REAL / SUM(b.fga) ELSE 0 END         AS fg_pct,
            CASE WHEN (SUM(b.fga) + 0.44 * SUM(b.fta)) > 0
                 THEN SUM(b.points)::REAL /
                      (2.0 * (SUM(b.fga) + 0.44 * SUM(b.fta)))
                 ELSE 0 END                                             AS ts_pct
        FROM players p
        JOIN game_box_scores b ON b.player_id = p.id
        JOIN games g ON g.id = b.game_id
        WHERE g.league_id = $1
          AND g.season    = $2
          AND g.season_type = 'regular'
          AND p.league_id = $1
        GROUP BY p.id
        HAVING COUNT(b.id) > 20
        """,
        league_id,
        season,
    )

    eligible: list[dict] = []
    for r in rows:
        row_dict = dict(r)
        gp = row_dict["games_played"]
        gs = row_dict["games_started"]
        majority_starter = gs > gp / 2

        if award_type == "roy" and not row_dict["is_rookie"]:
            continue
        if award_type == "6moy" and majority_starter:
            continue
        if award_type in _ALL_NBA_AWARDS and not majority_starter:
            continue

        eligible.append(row_dict)

    return eligible


async def generate_cpu_votes(
    voting_id: int, league_id: int, season: int
) -> int:
    """
    For each CPU team, pick the top player for this award based on their profile.
    Returns count of CPU votes cast.
    """
    pool = await get_pool()

    voting_row = await pool.fetchrow(
        "SELECT award_type FROM award_votings WHERE id = $1", voting_id
    )
    if voting_row is None:
        raise DBAError("Voting session not found.")
    award_type: str = voting_row["award_type"]

    cpu_teams = await pool.fetch(
        "SELECT id FROM teams WHERE league_id = $1 AND manager_user_id IS NULL",
        league_id,
    )
    if not cpu_teams:
        return 0

    eligible = await _get_eligible_players(pool, league_id, season, award_type)
    if not eligible:
        log.warning(f"No eligible players for award {award_type} in league {league_id} season {season}")
        return 0

    # Pre-fetch team records from standings_cache for all team_ids present.
    team_ids = list({p["team_id"] for p in eligible if p["team_id"] is not None})
    record_rows = await pool.fetch(
        "SELECT team_id, wins, losses FROM standings_cache WHERE league_id=$1 AND season=$2 AND team_id=ANY($3)",
        league_id,
        season,
        team_ids,
    )
    records_by_team: dict[int, dict] = {
        r["team_id"]: {"wins": r["wins"], "losses": r["losses"]} for r in record_rows
    }

    already_voted = set(
        r["voter_team_id"]
        for r in await pool.fetch(
            "SELECT voter_team_id FROM award_votes WHERE voting_id = $1", voting_id
        )
    )

    count = 0
    for team_row in cpu_teams:
        team_id: int = team_row["id"]
        if team_id in already_voted:
            continue

        profile: VoterProfile = get_cpu_profile(team_id)

        best_player_id: Optional[int] = None
        best_score = float("-inf")

        for p in eligible:
            stats = {
                "ppg": float(p["ppg"] or 0),
                "rpg": float(p["rpg"] or 0),
                "apg": float(p["apg"] or 0),
                "spg": float(p["spg"] or 0),
                "bpg": float(p["bpg"] or 0),
                "fg_pct": float(p["fg_pct"] or 0),
                "ts_pct": float(p["ts_pct"] or 0),
            }
            team_rec = records_by_team.get(p["team_id"] or 0, {"wins": 0, "losses": 0})
            score = score_player_for_award(p, stats, team_rec, award_type, profile)
            if score > best_score:
                best_score = score
                best_player_id = p["player_id"]

        if best_player_id is None:
            continue

        await pool.execute(
            """
            INSERT INTO award_votes
                (voting_id, voter_team_id, voter_user_id, cpu_profile, rank, player_id)
            VALUES ($1, $2, NULL, $3, 1, $4)
            ON CONFLICT (voting_id, voter_team_id) DO NOTHING
            """,
            voting_id,
            team_id,
            profile.value,
            best_player_id,
        )
        count += 1

    return count


async def close_voting(voting_id: int) -> list[dict]:
    """
    Tallies all votes, inserts award_results, marks voting closed.
    Returns [{place, player_id, vote_count, vote_share}] sorted by place.
    """
    pool = await get_pool()

    voting_row = await pool.fetchrow(
        "SELECT award_type, status FROM award_votings WHERE id = $1", voting_id
    )
    if voting_row is None:
        raise DBAError("Voting session not found.")
    if voting_row["status"] == "closed":
        raise DBAError("This voting session is already closed.")

    award_type: str = voting_row["award_type"]

    vote_rows = await pool.fetch(
        "SELECT player_id, rank FROM award_votes WHERE voting_id = $1", voting_id
    )
    total_votes = len(vote_rows)

    if award_type in _ALL_NBA_AWARDS:
        # For All-NBA: tally all votes across the three award_type sessions together.
        # Each session (all_nba_1/2/3) tallies its own votes independently —
        # top 5 by vote count in each session map to their respective team.
        from collections import Counter
        counts: Counter = Counter(r["player_id"] for r in vote_rows)
        ranked = counts.most_common()
        results: list[dict] = []
        for place, (player_id, vc) in enumerate(ranked[:5], start=1):
            share = vc / total_votes if total_votes else 0.0
            results.append({
                "place": place,
                "player_id": player_id,
                "vote_count": vc,
                "vote_share": share,
            })
    else:
        # Single-winner and All-Star: rank by vote count.
        from collections import Counter
        counts = Counter(r["player_id"] for r in vote_rows)
        ranked = counts.most_common()
        results = []
        for place, (player_id, vc) in enumerate(ranked, start=1):
            share = vc / total_votes if total_votes else 0.0
            results.append({
                "place": place,
                "player_id": player_id,
                "vote_count": vc,
                "vote_share": share,
            })

    async with pool.acquire() as conn:
        async with conn.transaction():
            for r in results:
                await conn.execute(
                    """
                    INSERT INTO award_results (voting_id, player_id, place, vote_count, vote_share)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    voting_id,
                    r["player_id"],
                    r["place"],
                    r["vote_count"],
                    r["vote_share"],
                )
            await conn.execute(
                "UPDATE award_votings SET status='closed', closed_at=NOW() WHERE id=$1",
                voting_id,
            )

    log.info(f"Closed voting {voting_id} ({award_type}), {total_votes} votes tallied")
    return results


async def open_all_star_voting(league_id: int, season: int) -> tuple[int, int]:
    """Opens east and west All-Star votes simultaneously. Returns (east_voting_id, west_voting_id)."""
    east_id = await open_voting(league_id, season, "all_star_east")
    west_id = await open_voting(league_id, season, "all_star_west")
    return east_id, west_id


async def get_closed_results(
    pool: asyncpg.Pool, league_id: int, season: int, award_type: str
) -> list[dict]:
    """Fetches stored results for a closed award. Used by the results command."""
    rows = await pool.fetch(
        """
        SELECT ar.place, ar.player_id, ar.vote_count, ar.vote_share
        FROM award_results ar
        JOIN award_votings av ON av.id = ar.voting_id
        WHERE av.league_id = $1
          AND av.season    = $2
          AND av.award_type = $3
        ORDER BY ar.place
        """,
        league_id,
        season,
        award_type,
    )
    return [dict(r) for r in rows]
