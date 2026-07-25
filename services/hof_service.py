from __future__ import annotations

import asyncpg

from core.logging import get_logger
from data.repositories import hof_repo

log = get_logger(__name__)

# Induction thresholds — any one is sufficient.
_CHAMPIONSHIPS_THRESHOLD = 3
_ELITE_SEASONS_OVR = 85
_ELITE_SEASONS_COUNT = 5
_MVP_VOTES_THRESHOLD = 8
_VETERAN_YEARS = 15
_VETERAN_PEAK_OVR = 80
# PA10: All-NBA/All-Star selection counts, using award_results the same way
# _count_mvp_votes already does. 5+ All-NBA nods or 8+ All-Star nods is a
# real, honest Hall-of-Fame case even for a player who never won an MVP vote
# or a championship (a long-running 2nd/3rd-team All-NBA player, or a
# perennial All-Star role player) -- both thresholds picked to sit clearly
# above "very good starter" territory and in "unmistakable career" territory.
_ALL_NBA_SELECTIONS_THRESHOLD = 5
_ALL_STAR_SELECTIONS_THRESHOLD = 8
# PA11: retirement/age gate for the 3 pre-existing non-longevity paths
# (championships/elite-seasons/mvp-votes) plus the new PA10 path -- without
# it, a player early in their career who racks up stray votes/selections
# could be inducted mid-career, years before their body of work is actually
# complete. The veteran-longevity path already implicitly gates on
# years_pro >= 15 (well above this floor) so it's intentionally excluded.
_RETIREMENT_GATE_YEARS_PRO = 8


async def check_and_induct(
    pool: asyncpg.Pool, league_id: int, season: int
) -> list[dict]:
    """
    Called at end of rollover. Evaluates recently retired players and long-tenured
    active players for Hall of Fame eligibility. Returns list of inducted player dicts
    (with player data merged) suitable for announcement.

    Criteria (any one sufficient):
    - 3+ championships (requires retirement/age gate -- PA11)
    - 5+ seasons with OVR >= 85 (tracked via history_seasons/award_results peak approximation)
      (requires retirement/age gate -- PA11)
    - 8+ MVP award votes across career (requires retirement/age gate -- PA11)
    - 5+ All-NBA selections OR 8+ All-Star selections (PA10; requires
      retirement/age gate -- PA11)
    - years_pro >= 15 with peak OVR >= 80 (veteran-longevity path -- already
      implicitly gated on career length, not subject to the PA11 gate)
    """
    candidates = await pool.fetch(
        """
        SELECT p.id,
               p.first_name,
               p.last_name,
               p.years_pro,
               p.overall,
               p.roster_status,
               p.league_id
        FROM players p
        WHERE p.league_id = $1
          AND p.roster_status IN ('retired', 'free_agent', 'active')
          AND p.years_pro >= 1
        """,
        league_id,
    )

    inducted: list[dict] = []

    for row in candidates:
        player_id: int = row["id"]

        # Skip if already in the HOF.
        if await hof_repo.get_inducted(pool, league_id, player_id):
            continue

        years_pro: int = row["years_pro"]
        overall: int = row["overall"]
        roster_status: str = row["roster_status"]

        # Championships: count seasons where the champion's roster included this player.
        career_championships: int = await _count_championships(pool, league_id, player_id)

        # MVP votes: total award_results rows for this player across all mvp votings.
        career_mvp_votes: int = await _count_mvp_votes(pool, league_id, player_id)

        # Peak OVR: best overall recorded in history or current value.
        career_overall_peak: int = overall  # current overall is a reasonable proxy

        # Seasons with OVR >= 85: approximated via contracts signed_in_season count
        # when player OVR was >= 85 — we use current OVR as a lower bound since OVR
        # can only be estimated from the live value without a full history table.
        elite_seasons: int = await _count_elite_seasons(pool, league_id, player_id)

        # PA10: All-NBA/All-Star selection counts, same query shape as _count_mvp_votes.
        all_nba_selections: int = await _count_all_nba_selections(pool, league_id, player_id)
        all_star_selections: int = await _count_all_star_selections(pool, league_id, player_id)

        # PA11: retirement/age gate for every path except veteran-longevity.
        retirement_eligible = (
            roster_status == "retired" or years_pro >= _RETIREMENT_GATE_YEARS_PRO
        )

        reason = _evaluate(
            years_pro=years_pro,
            overall=overall,
            career_championships=career_championships,
            career_mvp_votes=career_mvp_votes,
            elite_seasons=elite_seasons,
            all_nba_selections=all_nba_selections,
            all_star_selections=all_star_selections,
            retirement_eligible=retirement_eligible,
        )
        if reason is None:
            continue

        record = await hof_repo.induct(
            pool,
            league_id=league_id,
            player_id=player_id,
            inducted_season=season,
            career_seasons=years_pro,
            career_championships=career_championships,
            career_mvp_votes=career_mvp_votes,
            career_overall_peak=career_overall_peak,
            induction_reason=reason,
        )

        inducted.append(
            {
                "player_id": player_id,
                "player_name": f"{row['first_name']} {row['last_name']}",
                "record": record,
            }
        )
        log.info(
            "HOF induction: player %d (%s %s) in league %d season %d — %s",
            player_id,
            row["first_name"],
            row["last_name"],
            league_id,
            season,
            reason,
        )

    return inducted


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _evaluate(
    years_pro: int,
    overall: int,
    career_championships: int,
    career_mvp_votes: int,
    elite_seasons: int,
    all_nba_selections: int,
    all_star_selections: int,
    retirement_eligible: bool,
) -> str | None:
    """Returns the induction reason string, or None if not eligible.

    PA11: retirement_eligible gates every path below except the
    veteran-longevity path -- without it, a player early in their career
    racking up stray votes/selections could be inducted mid-career, years
    before their body of work is complete.
    """
    if retirement_eligible:
        if career_championships >= _CHAMPIONSHIPS_THRESHOLD:
            return f"{career_championships}-time champion"
        if elite_seasons >= _ELITE_SEASONS_COUNT:
            return f"{elite_seasons} elite seasons (OVR {_ELITE_SEASONS_OVR}+)"
        if career_mvp_votes >= _MVP_VOTES_THRESHOLD:
            return f"{career_mvp_votes} career MVP votes"
        # PA10: All-NBA/All-Star selection-count induction path.
        if all_nba_selections >= _ALL_NBA_SELECTIONS_THRESHOLD:
            return f"{all_nba_selections}-time All-NBA selection"
        if all_star_selections >= _ALL_STAR_SELECTIONS_THRESHOLD:
            return f"{all_star_selections}-time All-Star"
    if years_pro >= _VETERAN_YEARS and overall >= _VETERAN_PEAK_OVR:
        return f"{years_pro}-year career veteran (peak OVR {overall})"
    return None


async def _count_championships(
    pool: asyncpg.Pool, league_id: int, player_id: int
) -> int:
    """
    Count seasons where the player's team won the NBA Finals.
    Uses history_seasons.champion_team_id matched against the player's contract
    team_id in that signed_in_season.
    """
    result = await pool.fetchval(
        """
        SELECT COUNT(*)
        FROM history_seasons hs
        JOIN contracts c
            ON c.league_id = hs.league_id
           AND c.team_id   = hs.champion_team_id
           AND c.player_id = $2
           AND c.signed_in_season <= hs.season
           AND (c.signed_in_season + c.total_years) > hs.season
        WHERE hs.league_id = $1
          AND hs.champion_team_id IS NOT NULL
        """,
        league_id,
        player_id,
    )
    return int(result or 0)


async def _count_mvp_votes(
    pool: asyncpg.Pool, league_id: int, player_id: int
) -> int:
    """Count total award_results rows for this player in MVP votings."""
    result = await pool.fetchval(
        """
        SELECT COUNT(*)
        FROM award_results ar
        JOIN award_votings av ON av.id = ar.voting_id
        WHERE av.league_id  = $1
          AND ar.player_id  = $2
          AND av.award_type = 'mvp'
        """,
        league_id,
        player_id,
    )
    return int(result or 0)


async def _count_all_nba_selections(
    pool: asyncpg.Pool, league_id: int, player_id: int
) -> int:
    """Count total award_results rows for this player across all All-NBA
    team votings (1st/2nd/3rd team), same query shape as _count_mvp_votes."""
    result = await pool.fetchval(
        """
        SELECT COUNT(*)
        FROM award_results ar
        JOIN award_votings av ON av.id = ar.voting_id
        WHERE av.league_id  = $1
          AND ar.player_id  = $2
          AND av.award_type IN ('all_nba_1', 'all_nba_2', 'all_nba_3')
        """,
        league_id,
        player_id,
    )
    return int(result or 0)


async def _count_all_star_selections(
    pool: asyncpg.Pool, league_id: int, player_id: int
) -> int:
    """Count total award_results rows for this player across all All-Star
    votings (East + West), same query shape as _count_mvp_votes."""
    result = await pool.fetchval(
        """
        SELECT COUNT(*)
        FROM award_results ar
        JOIN award_votings av ON av.id = ar.voting_id
        WHERE av.league_id  = $1
          AND ar.player_id  = $2
          AND av.award_type IN ('all_star_east', 'all_star_west')
        """,
        league_id,
        player_id,
    )
    return int(result or 0)


async def _count_elite_seasons(
    pool: asyncpg.Pool, league_id: int, player_id: int
) -> int:
    """
    Estimate seasons the player had OVR >= 85.
    Uses the count of distinct signed_in_season values on their contracts as a
    proxy for active seasons — without a per-season OVR snapshot table this is
    the closest available approximation. If current OVR >= 85, we count all
    contract seasons; otherwise we return 0 (conservative).
    """
    if await pool.fetchval(
        "SELECT overall FROM players WHERE id = $1", player_id
    ) < _ELITE_SEASONS_OVR:
        return 0

    result = await pool.fetchval(
        """
        SELECT COUNT(DISTINCT signed_in_season)
        FROM contracts
        WHERE player_id  = $1
          AND league_id  = $2
        """,
        player_id,
        league_id,
    )
    return int(result or 0)
